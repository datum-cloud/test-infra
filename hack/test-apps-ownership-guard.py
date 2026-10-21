#!/usr/bin/env python3
"""Probe Milo's reserved Apps ownership markers through its real Project API.

The runner provisions the isolated Milo instance and fresh Project. This script
uses the ordinary root kubeconfig for actual Project-routed API requests; it
never reads or writes backend storage. Use --offline to run contract/failure
tests without contacting a cluster.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import secrets
import sys
import tempfile
import time
from urllib.parse import quote
import unittest
from unittest import mock
from types import SimpleNamespace

sys.dont_write_bytecode = True

from apps_native_support import APIProxy, Commands, Kube, RUN_LABEL, private_file, require
from apps_native_guard_quota import QuotaFixture


MILO_SHA = "200de48a305732720377cb83308d57c84fd5f688"
MILO_IMAGE = "ghcr.io/milo-os/milo@sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c"
MILO_INDEX = MILO_IMAGE.split("@", 1)[1].removeprefix("sha256:")
MILO_AMD64 = "f85dfc9d5c96e9730719d904a53d94a2d616c6dac38af173fbeb865434900a15"
MILO_ARM64 = "e69a2d2a5223f074555f51247d43b1ebe51395dbd20764083ef0af1b11393e04"
PROJECTS = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
MARKERS = (
    "apps.miloapis.com/project-uid",
    "apps.miloapis.com/app-uid",
    "apps.miloapis.com/namespace-reservation",
)
RUN_KEY = "apps-guard-probe.datum.net/run-id"
BOOTSTRAP_ORG = "apps-a1t-ci-org"
BOOTSTRAP_PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")


def project_path(project: str, resource_path: str) -> str:
    return f"{PROJECTS}/{quote(project, safe='')}/control-plane/{resource_path.lstrip('/')}"


def verify_source_pin(source: dict, core: dict, deployment: dict,
                      pods: list[dict], architecture: str) -> list[str]:
    require(source.get("spec", {}).get("url") == "https://github.com/milo-os/milo.git" and
            source.get("spec", {}).get("ref") == {"commit": MILO_SHA},
            "Milo GitRepository is not pinned to reviewed ownership-guard source")
    spec = core.get("spec", {})
    require(spec.get("sourceRef") == {"kind": "GitRepository", "name": "milo"} and
            spec.get("path") == "./config/overlays/test-infra" and
            spec.get("images") == [{"name": "ghcr.io/milo-os/milo",
                                    "digest": MILO_IMAGE.split("@", 1)[1]}] and
            core.get("status", {}).get("lastAppliedRevision", "").endswith(MILO_SHA),
            "Milo root Kustomization did not apply the exact guard source/image")
    status = deployment.get("status", {})
    replicas = deployment.get("spec", {}).get("replicas", 1)
    require(status.get("observedGeneration", 0) >= deployment.get("metadata", {}).get("generation", 1) and
            status.get("availableReplicas", 0) == replicas and
            status.get("updatedReplicas", 0) == replicas,
            "Milo apiserver Deployment has not rolled out the exact image")
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    server = [container for container in containers if container.get("image") == MILO_IMAGE]
    require(len(server) == 1,
            "Milo apiserver Deployment image differs from reviewed immutable image")
    server_name = server[0]["name"]
    expected_child = MILO_ARM64 if architecture == "arm64" else MILO_AMD64
    require(architecture in ("arm64", "amd64"), "unsupported Milo node architecture")
    require(pods and all(
        any(status.get("name") == server_name and status.get("ready") and
            any(status.get("imageID", "").endswith(digest)
                for digest in (expected_child, MILO_INDEX))
            for status in pod.get("status", {}).get("containerStatuses", []))
        for pod in pods),
        "running Milo apiserver pod image IDs do not match the verified source image")
    return sorted({status["imageID"] for pod in pods
                   for status in pod.get("status", {}).get("containerStatuses", [])
                   if status.get("name") == server_name})


def verify_loopback_config(document: dict, context: str, server_pattern: str) -> None:
    require(document.get("current-context") == context,
            "kubeconfig does not select the requested isolated context")
    contexts = document.get("contexts", [])
    clusters = document.get("clusters", [])
    require(len(contexts) == 1 and contexts[0].get("name") == context and
            len(clusters) == 1,
            "kubeconfig has extra contexts or clusters")
    cluster_ref = contexts[0].get("context", {}).get("cluster")
    selected = next((item.get("cluster", {}) for item in clusters
                     if item.get("name") == cluster_ref), {})
    require(re.fullmatch(server_pattern, selected.get("server", "")) is not None and
            bool(selected.get("certificate-authority-data")) and
            not selected.get("insecure-skip-tls-verify"),
            "kubeconfig must use the isolated loopback endpoint and verified CA")


def verify_bootstrap_evidence(evidence: dict, host_context: str,
                              project_name: str) -> tuple[str, str]:
    run_id = evidence.get("run_id", "")
    uids = evidence.get("uids", {})
    require(re.fullmatch(r"kind-apps-a1t-ci-[a-z0-9-]+", host_context) is not None and
            evidence.get("host_context") == host_context and
            evidence.get("organization") == BOOTSTRAP_ORG and
            evidence.get("projects") == list(BOOTSTRAP_PROJECTS) and
            evidence.get("cleanup") == "not-requested" and
            re.fullmatch(r"[0-9a-f]{24}", run_id) is not None and
            project_name in BOOTSTRAP_PROJECTS and
            isinstance(uids, dict) and all(uids.get(name) for name in
                                           (BOOTSTRAP_ORG, *BOOTSTRAP_PROJECTS)),
            "bootstrap evidence does not identify this dedicated Project fixture")
    return run_id, uids[project_name]


def write_private_json(path: Path, document: dict) -> None:
    require(path.is_absolute() and path.parent.is_dir() and
            not path.parent.is_symlink() and path.parent.resolve() == path.parent and
            path.parent.stat().st_uid == os.getuid() and
            path.parent.stat().st_mode & 0o077 == 0,
            "evidence parent must be an owned mode-0700 directory")
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        private_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def create_private_json(path: Path, document: dict) -> None:
    require(path.is_absolute() and path.parent.is_dir() and
            not path.parent.is_symlink() and path.parent.resolve() == path.parent and
            path.parent.stat().st_uid == os.getuid() and
            path.parent.stat().st_mode & 0o077 == 0 and
            not path.exists() and not path.is_symlink(),
            "failure evidence path must be new under an owned mode-0700 directory")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(document, stream, separators=(",", ":"), sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    private_file(path)


def ensure_project_current(proxy: APIProxy, project: str, identity: dict) -> dict:
    code, value = request(proxy, "GET", f"{PROJECTS}/{quote(project, safe='')}")
    current = assert_success(code, value, 200, "Project UID recheck")
    metadata = current.get("metadata", {})
    require(metadata.get("uid") == identity["uid"] and
            metadata.get("labels", {}).get(RUN_LABEL) == identity["bootstrap_run_id"] and
            current.get("spec", {}).get("ownerRef") == {
                "kind": "Organization", "name": BOOTSTRAP_ORG} and
            not metadata.get("deletionTimestamp"),
            "Project UID, owner or run label changed during the probe")
    return current


def project_request(proxy: APIProxy, project: str, identity: dict, method: str,
                    path: str, body: dict | None = None,
                    content_type: str = "application/json") -> tuple[int, dict | str]:
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        ensure_project_current(proxy, project, identity)
    return request(proxy, method, path, body, content_type)


def project_delete(proxy: APIProxy, project: str, identity: dict,
                   path: str, uid: str, rv: str) -> int:
    ensure_project_current(proxy, project, identity)
    code, value = request(proxy, "DELETE", path, {
        "apiVersion": "v1", "kind": "DeleteOptions",
        "preconditions": {"uid": uid, "resourceVersion": rv},
    })
    require(code in (200, 202), "UID/RV-preconditioned DELETE was not accepted")
    return code


def assert_guard_denial(status: int, response: object, marker: str) -> None:
    require(status == 403 and isinstance(response, dict) and
            response.get("kind") == "Status" and response.get("reason") == "Forbidden" and
            response.get("code") == 403,
            "marker write was not rejected as an API Forbidden response")
    message = response.get("message", "")
    expected = ("Apps ownership namespace reservation is reserved" if
                marker.endswith("namespace-reservation") else
                "Apps ownership is reserved; managed resource writes remain disabled")
    require(isinstance(message, str) and expected in message,
            "403 did not contain the Milo Apps ownership guard denial")


def assert_success(status: int, response: object, expected: int, label: str) -> dict:
    require(status == expected and isinstance(response, dict) and response.get("kind") != "Status",
            f"ordinary {label} returned HTTP {status}, expected {expected}")
    return response


def request(proxy: APIProxy, method: str, path: str,
            body: dict | None = None, content_type: str = "application/json") -> tuple[int, dict | str]:
    from apps_native_support import UnixHTTPConnection

    deadline = proxy.kube.commands.deadline
    timeout = 10.0 if deadline is None else min(10.0, deadline - time.monotonic())
    require(timeout > 0, "Apps ownership guard probe deadline exceeded")
    conn = UnixHTTPConnection(proxy.path)
    conn.timeout = timeout
    try:
        conn.connect()
        conn.sock.settimeout(timeout)
        payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": content_type} if body is not None else {})
        response = conn.getresponse()
        raw = response.read(65537)
        require(len(raw) <= 65536, "Milo API response exceeded bounded diagnostic size")
        value = json.loads(raw) if raw.startswith(b"{") else raw.decode(errors="replace")
        return response.status, value
    finally:
        conn.close()


def get_optional(proxy: APIProxy, path: str) -> dict | None:
    status, value = request(proxy, "GET", path)
    if status == 404:
        return None
    return assert_success(status, value, 200, "GET")


def wait_absent(proxy: APIProxy, path: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        if get_optional(proxy, path) is None:
            return
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise RuntimeError("timed out waiting for UID-preconditioned cleanup")


def assert_project_ready(project: dict, name: str) -> str:
    metadata = project.get("metadata", {})
    generation = metadata.get("generation", 1)
    ready = any(condition.get("type") == "Ready" and condition.get("status") == "True" and
                condition.get("observedGeneration", 0) >= generation
                for condition in project.get("status", {}).get("conditions", []))
    require(metadata.get("name") == name and metadata.get("uid") and
            not metadata.get("deletionTimestamp") and ready,
            "selected Project is missing, not Ready, terminating or has no UID")
    return metadata["uid"]


def source_preflight(args, commands: Commands, bootstrap: dict) -> tuple[Kube, Kube, dict]:
    private_file(args.host_kubeconfig)
    private_file(args.milo_kubeconfig)
    require(args.host_kubeconfig != args.milo_kubeconfig,
            "host and Milo kubeconfigs must be distinct")
    require(re.fullmatch(r"kind-apps-a1t-ci-[a-z0-9-]+", args.host_context) is not None,
            "host context must be the dedicated kind-apps-a1t-ci-* fixture")
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, args.milo_context)
    host_config = json.loads(host.k("config", "view", "--raw", "-o", "json", sensitive=True))
    verify_loopback_config(host_config, args.host_context, r"https://127\.0\.0\.1:[0-9]+")
    milo_config = json.loads(milo.k("config", "view", "--raw", "-o", "json", sensitive=True))
    verify_loopback_config(milo_config, args.milo_context,
                           rf"https://127\.0\.0\.1:{args.milo_port}")
    source = host.json("get", "gitrepository", "milo", "-n", "flux-system")
    core = host.json("get", "kustomization", "milo-core", "-n", "flux-system")
    deployment = host.json("get", "deployment", "milo-apiserver", "-n", "milo-system")
    require(core.get("status", {}).get("conditions") and any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in core["status"]["conditions"]),
        "Milo root Flux Kustomization is not Ready")
    architecture = host.json("get", "nodes")["items"][0]["status"]["nodeInfo"]["architecture"]
    selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    require(selector, "Milo apiserver Deployment has no pod selector")
    label_selector = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = host.json("get", "pods", "-n", "milo-system", "-l", label_selector)["items"]
    image_ids = verify_source_pin(source, core, deployment, pods, architecture)
    username = json.loads(commands.run(
        "kubectl", "--kubeconfig", str(args.milo_kubeconfig), "--context",
        args.milo_context, "auth", "whoami", "-o", "json")).get("status", {}).get("userInfo", {}).get("username")
    require(username == args.expected_user,
            "Milo kubeconfig resolved to an unexpected authenticated user")
    project = milo.json("get", "project", args.project)
    project_uid = assert_project_ready(project, args.project)
    bootstrap_run_id, expected_project_uid = verify_bootstrap_evidence(
        bootstrap, args.host_context, args.project)
    metadata = project["metadata"]
    require(project_uid == expected_project_uid and
            metadata.get("labels", {}).get(RUN_LABEL) == bootstrap_run_id and
            project.get("spec", {}).get("ownerRef") == {
                "kind": "Organization", "name": BOOTSTRAP_ORG},
            "Project UID, owner or run label differs from bootstrap evidence")
    organization = milo.json("get", "organization", BOOTSTRAP_ORG)
    require(organization.get("metadata", {}).get("uid") == bootstrap["uids"][BOOTSTRAP_ORG] and
            organization.get("metadata", {}).get("labels", {}).get(RUN_LABEL) == bootstrap_run_id and
            organization.get("spec", {}).get("type") == "Standard",
            "Organization UID, owner-run label or type differs from bootstrap evidence")
    projects = milo.json("get", "projects").get("items", [])
    observed = {item.get("metadata", {}).get("name"): item.get("metadata", {}).get("uid")
                for item in projects}
    require(observed == {name: bootstrap["uids"][name] for name in BOOTSTRAP_PROJECTS},
            "Milo Project inventory differs from the dedicated bootstrap evidence")
    return host, milo, {"uid": project_uid, "architecture": architecture,
                        "bootstrap_run_id": bootstrap_run_id,
                        "milo_apiserver_image_ids": image_ids}


def marker_keys_in(metadata: dict) -> list[str]:
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    return [key for key in MARKERS if key in labels or key in annotations]


def object_matches_create_intent(obj: dict, name: str, namespace: str,
                                 intent: dict, run_id: str) -> bool:
    metadata = obj.get("metadata", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    return (obj.get("kind") == "ConfigMap" and metadata.get("name") == name and
            obj.get("apiVersion") == "v1" and metadata.get("namespace") == namespace and
            labels == intent.get("labels", {}) and
            annotations == intent.get("annotations", {}) and
            labels.get(RUN_KEY) == run_id and
            not obj.get("binaryData") and not obj.get("immutable") and
            not metadata.get("ownerReferences") and not metadata.get("finalizers") and
            not metadata.get("deletionTimestamp") and not metadata.get("generateName") and
            obj.get("data", {}) == intent.get("data", {}))


def namespace_matches_create_intent(obj: dict, name: str, intent: dict,
                                    run_id: str, project_uid: str) -> bool:
    metadata = obj.get("metadata", {})
    expected_labels = dict(intent.get("metadata", {}).get("labels", {}))
    actual_labels = metadata.get("labels", {})
    # Kubernetes adds this label to Namespace objects on creation.
    if actual_labels.get("kubernetes.io/metadata.name") == name:
        expected_labels["kubernetes.io/metadata.name"] = name
    return (obj.get("apiVersion") == "v1" and obj.get("kind") == "Namespace" and
            metadata.get("name") == name and actual_labels == expected_labels and
            actual_labels.get(RUN_KEY) == run_id and
            metadata.get("annotations", {}) == intent.get("metadata", {}).get("annotations", {}) and
            metadata.get("annotations", {}).get(
                "apps-guard-probe.datum.net/project-uid") == project_uid and
            not metadata.get("ownerReferences") and not metadata.get("finalizers") and
            not metadata.get("deletionTimestamp") and not metadata.get("generateName"))


def marker_patch_ops(seed: dict, marker: str, location: str) -> list[dict]:
    pointer_key = marker.replace("~", "~0").replace("/", "~1")
    return [
        {"op": "test", "path": "/metadata/uid", "value": seed["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion",
         "value": seed["metadata"]["resourceVersion"]},
        {"op": "add", "path": f"/metadata/{location}/{pointer_key}", "value": ""},
    ]


def cleanup_probe(args, proxy: APIProxy, identity: dict, state: dict,
                  evidence_path: Path) -> list[str]:
    errors: list[str] = []
    commands = proxy.kube.commands
    commands.deadline = time.monotonic() + args.cleanup_deadline_seconds
    state["cleanup"] = {"state": "in-progress", "errors": []}
    write_private_json(evidence_path, state)
    namespace = state["namespace"]
    namespace_path = project_path(args.project, f"api/v1/namespaces/{namespace['name']}")
    cm_collection = namespace_path + "/configmaps"
    try:
        current_namespace = get_optional(proxy, namespace_path)
    except Exception:
        current_namespace = None
        errors.append("could not re-read run Namespace")
    if current_namespace is None:
        # A missing Namespace is safe only if no known ConfigMap is visible.
        for name, entry in state["configmaps"].items():
            try:
                current = get_optional(proxy, f"{cm_collection}/{quote(name, safe='')}")
                if current is not None:
                    errors.append("ConfigMap remains while its Namespace is absent")
            except Exception:
                errors.append("could not verify ConfigMap after Namespace disappearance")
        state["cleanup"] = {
            "state": "incomplete" if errors else "configmaps-clean",
            "namespace": "absent",
            "errors": errors,
        }
        write_private_json(evidence_path, state)
        return errors

    namespace_meta = current_namespace.get("metadata", {})
    expected_namespace_uid = namespace.get("uid")
    if expected_namespace_uid and namespace_meta.get("uid") != expected_namespace_uid:
        errors.append("refused replacement Namespace UID")
    elif not expected_namespace_uid and not errors:
        if (namespace.get("post_started") and
                namespace_matches_create_intent(current_namespace, namespace["name"],
                                                namespace.get("intent", {}),
                                                state["probe_run_id"], identity["uid"])):
            namespace["uid"] = namespace_meta.get("uid")
            namespace["state"] = "uid-recovered-by-exact-run-label"
            write_private_json(evidence_path, state)
        else:
            errors.append("refused Namespace without an exact recorded CREATE intent")
    if not namespace_matches_create_intent(current_namespace, namespace["name"],
                                           namespace.get("intent", {}),
                                           state["probe_run_id"], identity["uid"]):
        errors.append("refused Namespace with changed or unexpected fixture metadata")

    if not errors:
        for name, entry in state["configmaps"].items():
            path = f"{cm_collection}/{quote(name, safe='')}"
            if entry.get("state") in {"protected-marker-remains", "possible-protected-object"}:
                errors.append(f"prior marker admission requires manual fixture cleanup: {name}")
                continue
            try:
                current = get_optional(proxy, path)
            except Exception:
                errors.append(f"could not re-read expected ConfigMap {name}")
                continue
            if current is None:
                entry["state"] = "absent"
                write_private_json(evidence_path, state)
                continue
            metadata = current.get("metadata", {})
            uid = entry.get("uid")
            if entry.get("state") == "denied":
                errors.append(f"unexpected ConfigMap appeared after denied CREATE: {name}")
                continue
            if uid is None:
                if not entry.get("post_started"):
                    errors.append(f"refused ConfigMap without a recorded CREATE attempt: {name}")
                    continue
                if entry.get("marker") or marker_keys_in(metadata):
                    entry["observed_uid"] = metadata.get("uid")
                    entry["state"] = "possible-protected-object"
                    errors.append(f"possible protected ConfigMap remains: {name}")
                    write_private_json(evidence_path, state)
                    continue
                if not object_matches_create_intent(current, name, namespace["name"], entry["intent"],
                                                    state["probe_run_id"]):
                    errors.append(f"could not safely recover created ConfigMap identity: {name}")
                    continue
                uid = metadata.get("uid")
                if not uid:
                    errors.append(f"created ConfigMap lacks UID: {name}")
                    continue
                entry["uid"] = uid
                entry["state"] = "uid-recovered-by-exact-create-intent"
                write_private_json(evidence_path, state)
            if metadata.get("uid") != uid:
                errors.append(f"refused replacement ConfigMap UID: {name}")
                continue
            if metadata.get("labels", {}).get(RUN_KEY) != state["probe_run_id"]:
                errors.append(f"refused ConfigMap with changed run label: {name}")
                continue
            present_markers = marker_keys_in(metadata)
            if present_markers:
                entry["state"] = "protected-marker-remains"
                entry["present_markers"] = present_markers
                errors.append(f"protected ownership marker remains on ConfigMap: {name}")
                write_private_json(evidence_path, state)
                continue
            try:
                ensure_project_current(proxy, args.project, identity)
                code = project_delete(proxy, args.project, identity, path, uid,
                                      metadata["resourceVersion"])
                state["cleanup"].setdefault("delete_statuses", []).append(
                    {"resource": name, "status": code})
                write_private_json(evidence_path, state)
                wait_absent(proxy, path, commands.deadline)
                entry["state"] = "deleted"
                write_private_json(evidence_path, state)
            except Exception:
                errors.append(f"UID/RV ConfigMap cleanup failed: {name}")

    # Keep the run Namespace for an explicitly authorized fixture teardown.
    # Never delete a Namespace: it can contain resources outside this probe's
    # inventory, and namespace deletion would cascade into them.
    if (current_namespace is not None and
            namespace_meta.get("uid") == namespace.get("uid") and
            namespace_matches_create_intent(current_namespace, namespace["name"],
                                            namespace.get("intent", {}),
                                            state["probe_run_id"], identity["uid"])):
        namespace["state"] = ("retained-for-fixture-teardown" if not errors else
                               "retained-configmap-cleanup-incomplete")
        namespace["retained_uid"] = namespace_meta.get("uid")
        state["cleanup"]["namespace"] = namespace["state"]
        state["cleanup"]["namespace_uid"] = namespace_meta.get("uid")
    else:
        state["cleanup"]["namespace"] = "retained-or-unconfirmed"
        if namespace.get("uid"):
            state["cleanup"]["namespace_uid"] = namespace["uid"]
    state["cleanup"]["state"] = "configmaps-clean" if not errors else "incomplete"
    state["cleanup"]["errors"] = errors
    write_private_json(evidence_path, state)
    return errors


def run_probe(args) -> dict:
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    private_file(args.bootstrap_evidence)
    bootstrap = json.loads(args.bootstrap_evidence.read_text())
    _, milo, project_identity = source_preflight(args, commands, bootstrap)
    run_id = secrets.token_hex(8)
    namespace = f"apps-guard-{run_id}"
    namespace_path = project_path(args.project, f"api/v1/namespaces/{namespace}")
    cm_collection = project_path(args.project, f"api/v1/namespaces/{namespace}/configmaps")
    proxy = APIProxy(milo, Path(args.private_dir), "ownership-guard")
    evidence_path = args.evidence_file.resolve()
    require(not evidence_path.exists() and not evidence_path.is_symlink(),
            "probe evidence file already exists; refusing overwrite")
    state = {
        "host_context": args.host_context,
        "milo_source": MILO_SHA,
        "milo_image": MILO_IMAGE,
        "milo_apiserver_image_ids": project_identity["milo_apiserver_image_ids"],
        "project": args.project,
        "project_uid": project_identity["uid"],
        "project_owner": BOOTSTRAP_ORG,
        "project_bootstrap_run_id": project_identity["bootstrap_run_id"],
        "probe_run_id": run_id,
        "namespace": {"name": namespace, "uid": None, "state": "not-started"},
        "configmaps": {},
        "operations": [],
        "attempts": [],
        "namespace_cleanup_policy": "never-delete; retain UID for authorized fixture teardown",
        "cleanup": {"state": "not-started", "errors": []},
    }
    create_private_json(evidence_path, state)
    quota = QuotaFixture(lambda *values: request(proxy, *values), args.project,
                         project_identity["uid"], run_id, state,
                         lambda: write_private_json(evidence_path, state),
                         lambda: ensure_project_current(proxy, args.project, project_identity))

    def record_operation(verb: str, resource: str, status: int) -> None:
        state["operations"].append({"verb": verb, "resource": resource, "status": status})
        write_private_json(evidence_path, state)

    primary_failure = None
    try:
        proxy.start()
        code, value = request(proxy, "GET", f"{PROJECTS}/{quote(args.project, safe='')}")
        root_project = assert_success(code, value, 200, "root Project GET")
        require(root_project.get("metadata", {}).get("uid") == project_identity["uid"],
                "root Project API UID differs from bootstrap Project UID")
        require(get_optional(proxy, namespace_path) is None,
                "run-randomized Namespace name already exists")
        quota.prepare()

        namespace_doc = {
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": namespace, "labels": {RUN_KEY: run_id},
                         "annotations": {"apps-guard-probe.datum.net/project-uid":
                                         project_identity["uid"]}},
        }
        state["namespace"].update({"state": "create-intent", "intent": namespace_doc})
        write_private_json(evidence_path, state)
        state["namespace"]["post_started"] = True
        write_private_json(evidence_path, state)
        code, value = project_request(proxy, args.project, project_identity, "POST",
                                      project_path(args.project, "api/v1/namespaces"), namespace_doc)
        if code == 409:
            state["namespace"].update({"state": "create-conflict", "post_started": False})
            write_private_json(evidence_path, state)
        created_namespace = assert_success(code, value, 201, "Namespace CREATE")
        record_operation("CREATE", "Namespace", code)
        state["namespace"].update({"uid": created_namespace["metadata"]["uid"],
                                   "state": "created"})
        write_private_json(evidence_path, state)
        require(namespace_matches_create_intent(created_namespace, namespace,
                                                namespace_doc, run_id,
                                                project_identity["uid"]),
                "created Namespace differs from exact fixture intent")

        def create_cm(name: str, data: dict, annotations: dict | None = None,
                      marker: tuple[str, str] | None = None) -> dict:
            labels = {RUN_KEY: run_id}
            annotation_values = dict(annotations or {})
            if marker:
                location, key = marker
                target = labels if location == "labels" else annotation_values
                target[key] = ""
            body = {"apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": name, "labels": labels,
                                 "annotations": annotation_values},
                    "data": data}
            entry = {"uid": None, "state": "create-intent",
                     "intent": {"labels": labels, "annotations": annotation_values,
                                "data": data},
                     "marker": marker[1] if marker else None}
            state["configmaps"][name] = entry
            write_private_json(evidence_path, state)
            path = f"{cm_collection}/{quote(name, safe='')}"
            require(get_optional(proxy, path) is None,
                    "run-randomized ConfigMap name already exists")
            entry["post_started"] = True
            write_private_json(evidence_path, state)
            code, value = project_request(proxy, args.project, project_identity,
                                          "POST", cm_collection, body)
            if marker:
                if code == 403:
                    try:
                        assert_guard_denial(code, value, marker[1])
                    except Exception:
                        entry["state"] = "denial-unattributed"
                        entry["post_started"] = False
                        write_private_json(evidence_path, state)
                        raise
                    entry["state"] = "denied"
                    entry["post_started"] = False
                    state["attempts"].append({"verb": "CREATE", "location": marker[0],
                                              "marker": marker[1], "status": code,
                                              "empty_value": True})
                    record_operation("CREATE_DENIED", marker[1] + "/" + marker[0], code)
                    require(get_optional(proxy, f"{cm_collection}/{quote(name, safe='')}") is None,
                            "denied marker CREATE left a ConfigMap")
                    return {}
                if code in (200, 201) and isinstance(value, dict):
                    entry["uid"] = value.get("metadata", {}).get("uid")
                    entry["state"] = "protected-marker-remains"
                    write_private_json(evidence_path, state)
                elif 400 <= code < 500:
                    entry["state"] = "create-rejected"
                    entry["post_started"] = False
                    write_private_json(evidence_path, state)
                raise RuntimeError("Milo did not deny marker-bearing ConfigMap CREATE")
            if 400 <= code < 500:
                entry["state"] = "create-rejected"
                entry["post_started"] = False
                write_private_json(evidence_path, state)
            created = assert_success(code, value, 201, "unmarked ConfigMap CREATE")
            require(created.get("metadata", {}).get("name") == name and
                    created.get("metadata", {}).get("uid") and
                    created.get("metadata", {}).get("labels", {}).get(RUN_KEY) == run_id,
                    "ConfigMap CREATE response did not preserve its intended identity")
            entry.update({"uid": created["metadata"]["uid"], "state": "created"})
            record_operation("CREATE", name, code)
            return created

        ordinary = f"ordinary-{run_id}"
        ordinary_path = f"{cm_collection}/{ordinary}"
        ordinary = create_cm(ordinary, {"phase": "created"},
                             {"apps-guard-probe.datum.net/project-uid":
                              project_identity["uid"]})
        code, value = request(proxy, "GET", ordinary_path)
        current = assert_success(code, value, 200, "unmarked ConfigMap GET")
        record_operation("GET", ordinary_path.rsplit("/", 1)[-1], code)
        require(current["metadata"]["uid"] == ordinary["metadata"]["uid"] and
                current["data"] == {"phase": "created"},
                "unmarked ConfigMap GET differs from the created object")
        current["data"] = {"phase": "updated"}
        state["configmaps"][f"ordinary-{run_id}"]["state"] = "update-intent"
        state["configmaps"][f"ordinary-{run_id}"]["intent"]["data"] = {"phase": "updated"}
        write_private_json(evidence_path, state)
        code, value = project_request(proxy, args.project, project_identity, "PUT",
                                      ordinary_path, current)
        current = assert_success(code, value, 200, "unmarked ConfigMap UPDATE")
        record_operation("UPDATE", ordinary_path.rsplit("/", 1)[-1], code)
        require(current["metadata"]["uid"] == ordinary["metadata"]["uid"] and
                current["data"] == {"phase": "updated"},
                "unmarked ConfigMap UPDATE changed identity or failed to persist")
        state["configmaps"][f"ordinary-{run_id}"]["state"] = "updated"
        write_private_json(evidence_path, state)
        code, value = request(proxy, "GET", ordinary_path)
        current = assert_success(code, value, 200, "unmarked ConfigMap GET after UPDATE")
        record_operation("GET_AFTER_UPDATE", ordinary_path.rsplit("/", 1)[-1], code)
        require(current["data"] == {"phase": "updated"}, "unmarked UPDATE did not persist")

        for marker in MARKERS:
            for location in ("labels", "annotations"):
                name = f"create-{len(state['attempts'])}-{run_id}"
                create_cm(name, {}, marker=(location, marker))

        for marker in MARKERS:
            for location in ("labels", "annotations"):
                name = f"update-{len(state['attempts'])}-{run_id}"
                seed = create_cm(name, {"keep": "yes"},
                                 {"apps-guard-probe.datum.net/project-uid":
                                  project_identity["uid"]})
                path = f"{cm_collection}/{name}"
                entry = state["configmaps"][name]
                entry["state"] = "marker-update-intent"
                entry["pending_marker"] = marker
                entry["pending_location"] = location
                write_private_json(evidence_path, state)
                patch = marker_patch_ops(seed, marker, location)
                code, value = project_request(proxy, args.project, project_identity, "PATCH",
                                              path, patch, "application/json-patch+json")
                if code == 403:
                    assert_guard_denial(code, value, marker)
                    entry["state"] = "marker-update-denied"
                    state["attempts"].append({"verb": "PATCH", "location": location,
                                              "marker": marker, "status": code,
                                              "empty_value": True})
                    record_operation("PATCH_DENIED", marker + "/" + location, code)
                    observed = assert_success(*request(proxy, "GET", path), 200,
                                              "ConfigMap GET after denied marker UPDATE")
                    require(observed["metadata"]["uid"] == seed["metadata"]["uid"] and
                            marker not in observed["metadata"].get(location, {}) and
                            observed.get("data") == {"keep": "yes"},
                            "denied marker UPDATE changed the ConfigMap")
                    continue
                if code in (200, 201) and isinstance(value, dict):
                    entry["uid"] = seed["metadata"]["uid"]
                    entry["state"] = "protected-marker-remains"
                    write_private_json(evidence_path, state)
                raise RuntimeError("Milo did not deny marker-bearing ConfigMap PATCH")

        # The test deliberately never creates a marked old object, so marker
        # removal cannot be safely exercised through the public API.
        entry = state["configmaps"][f"ordinary-{run_id}"]
        entry["state"] = "delete-intent"
        write_private_json(evidence_path, state)
        code = project_delete(proxy, args.project, project_identity, ordinary_path,
                              entry["uid"], current["metadata"]["resourceVersion"])
        record_operation("DELETE_UID_RV", f"ConfigMap/ordinary-{run_id}", code)
        wait_absent(proxy, ordinary_path, commands.deadline)
        entry["state"] = "absent"
        write_private_json(evidence_path, state)
        state["phase"] = "probe-complete"
    except Exception as error:
        primary_failure = f"{type(error).__name__}: {str(error)[:180]}"
        state["failure"] = primary_failure
        write_private_json(evidence_path, state)
    cleanup_errors: list[str] = []
    if proxy.process and state["namespace"]["state"] != "not-started":
        try:
            cleanup_errors = cleanup_probe(args, proxy, project_identity, state, evidence_path)
        except Exception as error:
            cleanup_errors = [f"cleanup failed: {type(error).__name__}: {str(error)[:160]}"]
            state["cleanup"] = {"state": "incomplete", "errors": cleanup_errors}
            write_private_json(evidence_path, state)
    if proxy.process:
        commands.deadline = time.monotonic() + 120
        try:
            quota.cleanup()
        except Exception:
            cleanup_errors.append("quota cleanup incomplete; exact grant intent retained")
            if "quota" in state:
                state["quota"]["cleanup"] = "incomplete"
            state["cleanup"]["errors"] = cleanup_errors
            write_private_json(evidence_path, state)
    if proxy.process:
        proxy.close()
    if primary_failure or cleanup_errors:
        raise RuntimeError(f"probe failed; private evidence retained at {evidence_path}")
    state["claim_limit"] = "real Project API guard probe; no writer exception or IAM claim"
    state["marker_removal"] = "not attempted: no marked old object was created"
    write_private_json(evidence_path, state)
    return state


def offline_tests() -> None:
    class ProbeContractTests(unittest.TestCase):
        def test_exact_source_and_digest_required(self):
            source = {"spec": {"url": "https://github.com/milo-os/milo.git",
                               "ref": {"commit": MILO_SHA}}}
            core = {"spec": {"sourceRef": {"kind": "GitRepository", "name": "milo"},
                              "path": "./config/overlays/test-infra",
                              "images": [{"name": "ghcr.io/milo-os/milo",
                                          "digest": MILO_IMAGE.split("@", 1)[1]}]},
                    "status": {"lastAppliedRevision": "main@sha1:" + MILO_SHA}}
            deploy = {"metadata": {"generation": 1},
                      "status": {"observedGeneration": 1, "availableReplicas": 1,
                                 "updatedReplicas": 1},
                      "spec": {"replicas": 1, "template": {"spec": {"containers": [
                          {"name": "milo-apiserver", "image": MILO_IMAGE}]}}}}
            pods = [{"status": {"containerStatuses": [{"ready": True,
                      "name": "milo-apiserver",
                      "imageID": "ghcr.io/milo-os/milo@sha256:" + MILO_ARM64}]}}]
            verify_source_pin(source, core, deploy, pods, "arm64")
            pods[0]["status"]["containerStatuses"][0]["imageID"] = \
                "ghcr.io/milo-os/milo@sha256:" + MILO_INDEX
            verify_source_pin(source, core, deploy, pods, "arm64")
            core["spec"]["images"][0]["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(RuntimeError, "exact guard source/image"):
                verify_source_pin(source, core, deploy, pods, "arm64")
            core["spec"]["images"][0]["digest"] = MILO_IMAGE.split("@", 1)[1]
            source["spec"]["ref"]["commit"] = "0" * 40
            with self.assertRaisesRegex(RuntimeError, "reviewed ownership-guard source"):
                verify_source_pin(source, core, deploy, pods, "arm64")

        def test_runtime_pod_must_match_the_selected_platform_child(self):
            source = {"spec": {"url": "https://github.com/milo-os/milo.git",
                               "ref": {"commit": MILO_SHA}}}
            core = {"spec": {"sourceRef": {"kind": "GitRepository", "name": "milo"},
                              "path": "./config/overlays/test-infra",
                              "images": [{"name": "ghcr.io/milo-os/milo",
                                          "digest": MILO_IMAGE.split("@", 1)[1]}]},
                    "status": {"lastAppliedRevision": "main@sha1:" + MILO_SHA}}
            deploy = {"metadata": {"generation": 1},
                      "status": {"observedGeneration": 1, "availableReplicas": 1,
                                 "updatedReplicas": 1},
                      "spec": {"replicas": 1, "template": {"spec": {"containers": [
                          {"name": "milo-apiserver", "image": MILO_IMAGE}]}}}}
            pods = [{"status": {"containerStatuses": [{"ready": True,
                      "name": "milo-apiserver",
                      "imageID": "ghcr.io/milo-os/milo@sha256:" + MILO_AMD64}]}}]
            with self.assertRaisesRegex(RuntimeError, "running Milo apiserver pod image IDs"):
                verify_source_pin(source, core, deploy, pods, "arm64")

        def test_kubeconfigs_are_loopback_ca_verified_and_single_context(self):
            config = {"current-context": "kind-apps-a1t-ci-run",
                      "contexts": [{"name": "kind-apps-a1t-ci-run",
                                    "context": {"cluster": "host"}}],
                      "clusters": [{"name": "host", "cluster": {
                          "server": "https://127.0.0.1:6443",
                          "certificate-authority-data": "cHVibGlj"}}]}
            verify_loopback_config(config, "kind-apps-a1t-ci-run",
                                   r"https://127\.0\.0\.1:[0-9]+")
            unsafe = json.loads(json.dumps(config))
            unsafe["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
            with self.assertRaisesRegex(RuntimeError, "loopback endpoint"):
                verify_loopback_config(unsafe, "kind-apps-a1t-ci-run",
                                       r"https://127\.0\.0\.1:[0-9]+")
            foreign = json.loads(json.dumps(config))
            foreign["clusters"][0]["cluster"]["server"] = "https://milo.example.com"
            with self.assertRaisesRegex(RuntimeError, "loopback endpoint"):
                verify_loopback_config(foreign, "kind-apps-a1t-ci-run",
                                       r"https://127\.0\.0\.1:[0-9]+")

        def test_bootstrap_scope_requires_exact_project_uid_owner_and_run(self):
            evidence = {"host_context": "kind-apps-a1t-ci-run",
                        "organization": BOOTSTRAP_ORG,
                        "projects": list(BOOTSTRAP_PROJECTS),
                        "run_id": "a" * 24,
                        "uids": {BOOTSTRAP_ORG: "org-uid",
                                 **{name: name + "-uid" for name in BOOTSTRAP_PROJECTS}},
                        "cleanup": "not-requested"}
            run_id, uid = verify_bootstrap_evidence(
                evidence, "kind-apps-a1t-ci-run", BOOTSTRAP_PROJECTS[0])
            self.assertEqual(run_id, "a" * 24)
            self.assertEqual(uid, BOOTSTRAP_PROJECTS[0] + "-uid")
            evidence["uids"][BOOTSTRAP_PROJECTS[0]] = None
            with self.assertRaisesRegex(RuntimeError, "dedicated Project fixture"):
                verify_bootstrap_evidence(evidence, "kind-apps-a1t-ci-run",
                                          BOOTSTRAP_PROJECTS[0])

        def test_denial_requires_guard_specific_status_message(self):
            body = {"kind": "Status", "reason": "Forbidden", "code": 403,
                    "message": "Apps ownership is reserved; managed resource writes remain disabled"}
            assert_guard_denial(403, body, MARKERS[0])
            with self.assertRaisesRegex(RuntimeError, "guard denial"):
                assert_guard_denial(403, {**body, "message": "forbidden by RBAC"}, MARKERS[0])
            with self.assertRaisesRegex(RuntimeError, "Forbidden"):
                assert_guard_denial(403, {**body, "reason": "Unauthorized"}, MARKERS[0])

        def test_namespace_reservation_requires_its_specific_guard_reason(self):
            body = {"kind": "Status", "reason": "Forbidden", "code": 403,
                    "message": "Apps ownership namespace reservation is reserved; acquisition, mutation and removal remain disabled"}
            assert_guard_denial(403, body, MARKERS[2])
            with self.assertRaisesRegex(RuntimeError, "guard denial"):
                assert_guard_denial(403, {**body, "message": "Apps ownership is reserved"}, MARKERS[2])

        def test_marker_json_patch_is_bound_to_seed_uid_and_resource_version(self):
            seed = {"metadata": {"uid": "seed-uid", "resourceVersion": "17"}}
            ops = marker_patch_ops(seed, MARKERS[0], "labels")
            self.assertEqual(ops[:2], [
                {"op": "test", "path": "/metadata/uid", "value": "seed-uid"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "17"},
            ])
            self.assertEqual(ops[2], {"op": "add",
                "path": "/metadata/labels/apps.miloapis.com~1project-uid", "value": ""})
            replacement = {"metadata": {"uid": "replacement-uid", "resourceVersion": "18"}}
            for changed in (replacement,
                            {"metadata": {"uid": "seed-uid", "resourceVersion": "18"}}):
                self.assertFalse(all(changed["metadata"][op["path"].rsplit("/", 1)[1]] ==
                                     op["value"] for op in ops[:2]))

        def test_lost_create_recovery_rejects_unintended_configmap_fields(self):
            intent = {"labels": {RUN_KEY: "probe"},
                      "annotations": {"fixture": "expected"}, "data": {"x": "y"}}
            obj = {"apiVersion": "v1", "kind": "ConfigMap",
                   "metadata": {"name": "known", "namespace": "ns", "uid": "uid",
                                "resourceVersion": "3", "labels": intent["labels"],
                                "annotations": intent["annotations"]},
                   "data": intent["data"]}
            self.assertTrue(object_matches_create_intent(obj, "known", "ns", intent, "probe"))
            for mutation in (
                {"binaryData": {"unexpected": "eQ=="}},
                {"immutable": True},
                {"metadata": {"ownerReferences": [{"uid": "foreign"}]}},
                {"metadata": {"finalizers": ["foreign.example/hold"]}},
                {"metadata": {"deletionTimestamp": "2026-10-21T00:00:00Z"}},
                {"metadata": {"annotations": {"fixture": "expected", MARKERS[0]: ""}}},
            ):
                changed = json.loads(json.dumps(obj))
                if "metadata" in mutation:
                    changed["metadata"].update(mutation["metadata"])
                else:
                    changed.update(mutation)
                self.assertFalse(object_matches_create_intent(
                    changed, "known", "ns", intent, "probe"), mutation)

        def test_namespace_recovery_requires_exact_create_intent(self):
            intent = {"apiVersion": "v1", "kind": "Namespace",
                      "metadata": {"name": "ns", "labels": {RUN_KEY: "probe"},
                                   "annotations": {
                                       "apps-guard-probe.datum.net/project-uid": "project"}}}
            obj = json.loads(json.dumps(intent))
            obj["metadata"].update({"uid": "uid", "resourceVersion": "3"})
            obj["metadata"]["labels"]["kubernetes.io/metadata.name"] = "ns"
            self.assertTrue(namespace_matches_create_intent(obj, "ns", intent, "probe", "project"))
            obj["metadata"]["ownerReferences"] = [{"uid": "unexpected"}]
            self.assertFalse(namespace_matches_create_intent(obj, "ns", intent, "probe", "project"))

        def test_api_call_refuses_expired_overall_deadline(self):
            proxy = mock.Mock()
            proxy.kube.commands.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(RuntimeError, "deadline exceeded"):
                request(proxy, "GET", "/api/v1/namespaces")

        def test_full_cleanup_retains_namespace_on_replacement_read_failure_or_marker(self):
            project = BOOTSTRAP_PROJECTS[0]
            identity = {"uid": "project-uid", "bootstrap_run_id": "b" * 24}
            namespace_name = "apps-guard-test"
            namespace_path = project_path(project, f"api/v1/namespaces/{namespace_name}")
            cm_path = namespace_path + "/configmaps/owned"
            namespace_obj = {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": namespace_name, "uid": "ns-uid",
                "resourceVersion": "21", "labels": {RUN_KEY: "probe"},
                "annotations": {"apps-guard-probe.datum.net/project-uid": "project-uid"}}}

            for scenario in ("replacement", "read-failure", "marker"):
                with self.subTest(scenario=scenario):
                    if scenario == "replacement":
                        cm_obj = {"metadata": {"name": "owned", "uid": "new-uid",
                            "resourceVersion": "22", "labels": {RUN_KEY: "probe"}},
                            "data": {"v": "x"}}
                    elif scenario == "marker":
                        cm_obj = {"metadata": {"name": "owned", "uid": "cm-uid",
                            "resourceVersion": "22", "labels": {RUN_KEY: "probe",
                                MARKERS[0]: ""}}, "data": {"v": "x"}}
                    else:
                        cm_obj = None
                    objects = {namespace_path: namespace_obj, cm_path: cm_obj}
                    proxy = SimpleNamespace(kube=SimpleNamespace(commands=Commands()))
                    args = SimpleNamespace(cleanup_deadline_seconds=10, project=project)
                    state = {"project": project, "probe_run_id": "probe",
                             "namespace": {"name": namespace_name, "uid": "ns-uid",
                                 "intent": {"apiVersion": "v1", "kind": "Namespace",
                                     "metadata": {"name": namespace_name,
                                         "labels": {RUN_KEY: "probe"},
                                         "annotations": {
                                             "apps-guard-probe.datum.net/project-uid":
                                             "project-uid"}}}},
                             "configmaps": {"owned": {"uid": "cm-uid", "state": "created",
                                 "intent": {"labels": {RUN_KEY: "probe"},
                                            "annotations": {}, "data": {"v": "x"}}}}}
                    deletes = mock.Mock()
                    def get_object(_proxy, path):
                        if scenario == "read-failure" and path == cm_path:
                            raise RuntimeError("offline read failure fixture")
                        return objects.get(path)
                    with (mock.patch(__name__ + ".get_optional", side_effect=get_object),
                          mock.patch(__name__ + ".ensure_project_current"),
                          mock.patch(__name__ + ".project_delete", deletes),
                          mock.patch(__name__ + ".wait_absent"),
                          mock.patch(__name__ + ".write_private_json")):
                        errors = cleanup_probe(args, proxy, identity, state, Path("/private/evidence"))
                    self.assertTrue(errors)
                    self.assertFalse(any(call.args[3] == cm_path
                                         for call in deletes.call_args_list))
                    self.assertFalse(any(call.args[3] == namespace_path
                                         for call in deletes.call_args_list))
                    self.assertEqual(state["cleanup"]["namespace_uid"], "ns-uid")
                    self.assertEqual(state["cleanup"]["namespace"],
                                     "retained-configmap-cleanup-incomplete")

        def test_full_cleanup_deletes_only_known_uid_and_retains_namespace(self):
            project = BOOTSTRAP_PROJECTS[0]
            identity = {"uid": "project-uid", "bootstrap_run_id": "b" * 24}
            namespace_name = "apps-guard-test"
            namespace_path = project_path(project, f"api/v1/namespaces/{namespace_name}")
            cm_path = namespace_path + "/configmaps/owned"
            namespace_obj = {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": namespace_name, "uid": "ns-uid",
                "resourceVersion": "21", "labels": {RUN_KEY: "probe"},
                "annotations": {"apps-guard-probe.datum.net/project-uid": "project-uid"}}}
            cm_obj = {"metadata": {"name": "owned", "uid": "cm-uid",
                "resourceVersion": "22", "labels": {RUN_KEY: "probe"}},
                "data": {"v": "x"}}
            objects = {namespace_path: namespace_obj, cm_path: cm_obj}
            proxy = SimpleNamespace(kube=SimpleNamespace(commands=Commands()))
            args = SimpleNamespace(cleanup_deadline_seconds=10, project=project)
            state = {"project": project, "probe_run_id": "probe",
                     "namespace": {"name": namespace_name, "uid": "ns-uid",
                         "intent": {"apiVersion": "v1", "kind": "Namespace",
                             "metadata": {"name": namespace_name,
                                 "labels": {RUN_KEY: "probe"},
                                 "annotations": {
                                     "apps-guard-probe.datum.net/project-uid":
                                     "project-uid"}}}},
                     "configmaps": {"owned": {"uid": "cm-uid", "state": "created",
                         "intent": {"labels": {RUN_KEY: "probe"}, "annotations": {},
                                    "data": {"v": "x"}}}}}
            deleted = []
            def delete(_proxy, _project, _identity, path, uid, rv):
                deleted.append((path, uid, rv))
                objects.pop(path, None)
            def get(_proxy, path):
                return objects.get(path)
            with (mock.patch(__name__ + ".get_optional", side_effect=get),
                  mock.patch(__name__ + ".request") as request_mock,
                  mock.patch(__name__ + ".ensure_project_current"),
                  mock.patch(__name__ + ".project_delete", side_effect=delete),
                  mock.patch(__name__ + ".wait_absent"),
                  mock.patch(__name__ + ".write_private_json")):
                errors = cleanup_probe(args, proxy, identity, state, Path("/private/evidence"))
            self.assertEqual(errors, [])
            self.assertEqual(deleted, [(cm_path, "cm-uid", "22")])
            self.assertEqual(state["namespace"]["state"], "retained-for-fixture-teardown")
            self.assertEqual(state["namespace"]["retained_uid"], "ns-uid")
            self.assertEqual(state["cleanup"]["state"], "configmaps-clean")
            self.assertEqual(state["cleanup"]["namespace_uid"], "ns-uid")
            request_mock.assert_not_called()

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProbeContractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="run failure-contract tests only")
    parser.add_argument("--host-kubeconfig", type=Path)
    parser.add_argument("--host-context")
    parser.add_argument("--milo-kubeconfig", type=Path)
    parser.add_argument("--milo-context")
    parser.add_argument("--milo-port", type=int)
    parser.add_argument("--bootstrap-evidence", type=Path)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--expected-user")
    parser.add_argument("--deadline-seconds", type=int, default=180)
    parser.add_argument("--cleanup-deadline-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.offline:
        offline_tests()
        return
    require(args.host_kubeconfig and args.host_context and args.milo_kubeconfig and
            args.milo_context and args.milo_port and args.bootstrap_evidence and
            args.project and args.expected_user,
            "live probe requires isolated kubeconfigs/contexts, Milo port, bootstrap evidence, Project and username")
    require(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.project) is not None,
            "Project name must be a DNS label")
    require(90 <= args.deadline_seconds <= 600, "deadline must be between 90 and 600 seconds")
    require(15 <= args.cleanup_deadline_seconds <= 180,
            "cleanup deadline must be between 15 and 180 seconds")
    require(1 <= args.milo_port <= 65535, "invalid Milo loopback port")
    require(not args.host_kubeconfig.is_symlink() and
            not args.milo_kubeconfig.is_symlink() and
            not args.bootstrap_evidence.is_symlink(),
            "kubeconfig and bootstrap evidence symlinks are refused")
    args.host_kubeconfig = args.host_kubeconfig.resolve()
    args.milo_kubeconfig = args.milo_kubeconfig.resolve()
    args.bootstrap_evidence = args.bootstrap_evidence.resolve()
    require(args.host_context.startswith("kind-apps-a1t-ci-") and
            args.milo_context == "milo-root" and
            args.milo_kubeconfig.parent == args.bootstrap_evidence.parent,
            "probe must use matching dedicated host and Milo bootstrap fixture paths")
    if args.evidence_file is None:
        args.evidence_file = args.bootstrap_evidence.parent / "apps-a1t-ownership-guard-evidence.json"
    require(not args.evidence_file.is_symlink(), "evidence file symlink is refused")
    args.evidence_file = args.evidence_file.absolute()
    require(args.evidence_file.parent == args.bootstrap_evidence.parent,
            "probe evidence must be saved beside the private bootstrap evidence")
    try:
        with tempfile.TemporaryDirectory(prefix="apps-guard-probe-") as private:
            args.private_dir = Path(private)
            evidence = run_probe(args)
        print(json.dumps(evidence, sort_keys=True, indent=2))
    except Exception as error:
        # Do not print kubectl config, token, or response body on failures.
        print(f"Milo Apps ownership guard probe failed: {type(error).__name__}: {str(error)[:240]}",
              file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
