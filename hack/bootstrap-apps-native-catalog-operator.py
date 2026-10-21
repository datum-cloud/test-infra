#!/usr/bin/env python3
"""Source-owned Catalog manager prerequisite for one isolated Milo fixture.

This runner never creates a Service, entitlement, App, Workload or route. It
retains the prerequisite graph on uncertainty and keeps credentials only in a
private journal and one scoped host Secret.
"""

from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sys
import tempfile
import time

import yaml

from apps_native_catalog_oci import OCI_DIGEST, transition_contract
from apps_native_catalog_operator_contract import (
    CA_SOURCE, CA_STAGE, HOST_STAGE, IMAGE_INDEX, NAMESPACE, ROOT, USER,
    WEBHOOK_NAMES, contract,
)
from apps_native_fixture_scope import (
    active_catalog_installer, require_same_identity, rollout_static_auth,
    scope_identity, wait_authenticated_root,
)
from apps_native_operator_flux import create_stage, delete_stage, owner
from apps_native_support import (
    APIProxy, Commands, FluxOwned, Forward, Kube, RUN_LABEL, _flux_spec,
    certificate_ready, private_file, private_write, ready, require,
    verify_cert, wait_for,
)


IMAGE_ARM64 = "sha256:eb02b6b364d7ba21234a9049ae549ed934e06a12a937c2e4259577bf9c490485"
IMAGE_ARM64_CONFIG = "sha256:fb97dd3205cd6ac52438b3299a3c5e370de7d1d6a55a2b3bcbe4e8368e7b753a"
SOURCE_REVISION = "5f2b663b6c5e039c4c7b666ef3e959803851b675"
CORE_NAMESPACE = "milo-system"
CA_SECRET = "apps-a1t-services-ca"
SERVING_SECRET = "services-webhook-cert"
CREDENTIAL_SECRET = "milo-kubeconfig"
WEBHOOK_SERVICE = "services-webhook.services-system.svc"
SOURCE_ROLE = "services-controller-manager"
CURL_IMAGE = "docker.io/curlimages/curl@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6"
CURL_ARM64 = "sha256:b949ec80877077def1544f0727d3e3101dd687fe20a5bf74997e557f7e3ac961"
CURL_ARM64_CONFIG = "sha256:bf9e201037a037a4220fc74d1af89b82a8a692349e9af499fb9888de4a0c4470"
TLS_PROBE_NAME = "apps-a1t-services-tls-probe"
PUBLIC_CA_NAME = "apps-a1t-services-public-ca"
ROWS = {
    "namespace": ("namespace", NAMESPACE, None,
                  f"/api/v1/namespaces/{NAMESPACE}"),
    "role": ("clusterrole", SOURCE_ROLE, None,
             f"/apis/rbac.authorization.k8s.io/v1/clusterroles/{SOURCE_ROLE}"),
    "binding": ("clusterrolebinding", SOURCE_ROLE, None,
                f"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{SOURCE_ROLE}"),
    "credential": ("secret", CREDENTIAL_SECRET, NAMESPACE,
                   f"/api/v1/namespaces/{NAMESPACE}/secrets/{CREDENTIAL_SECRET}"),
    "ca_source": ("gitrepository", CA_SOURCE, "flux-system",
                  f"/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/{CA_SOURCE}"),
    "tls_configmap": ("configmap", PUBLIC_CA_NAME, CORE_NAMESPACE,
                      f"/api/v1/namespaces/{CORE_NAMESPACE}/configmaps/{PUBLIC_CA_NAME}"),
    "tls_pod": ("pod", TLS_PROBE_NAME, CORE_NAMESPACE,
                f"/api/v1/namespaces/{CORE_NAMESPACE}/pods/{TLS_PROBE_NAME}"),
}


class PreflightFailure(RuntimeError):
    def __init__(self, stage: str, reason: str):
        self.stage = stage
        self.reason = reason
        super().__init__(f"{stage}/{reason}")


def preflight(stage: str, callback):
    """Expose only fixed stage and exception class, never credential data."""
    try:
        return callback()
    except Exception as error:
        raise PreflightFailure(stage, type(error).__name__) from None


def _dependency_module(name: str):
    path = ROOT / "hack" / name
    specification = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def atomic_json(path: Path, document: dict) -> None:
    require(path.is_absolute() and path.parent.is_dir() and not path.is_symlink(),
            "absolute private journal path required")
    fd, pending = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(document, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        private_file(path)
    finally:
        Path(pending).unlink(missing_ok=True)


def clean_revision(commands: Commands) -> str:
    require(ROOT.is_dir() and not ROOT.is_symlink(), "isolated test-infra source required")
    revision = commands.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None and
            not commands.run("git", "-C", str(ROOT), "status", "--porcelain",
                             "--untracked-files=all"),
            "exact clean signed test-infra source required")
    commands.run("git", "-C", str(ROOT), "verify-commit", "HEAD", sensitive=True)
    return revision


def private_evidence(path: Path) -> None:
    require(path.is_absolute() and path.parent.is_dir() and
            path.parent.stat().st_mode & 0o077 == 0 and
            not path.is_symlink() and
            (not path.exists() or path.stat().st_mode & 0o077 == 0),
            "private evidence file in existing 0700 directory required")


def read_journal(path: Path) -> dict:
    require(not path.is_symlink(), "prerequisite journal symlink refused")
    private_file(path)
    return json.loads(path.read_text())


def source_pins(commands: Commands, args: argparse.Namespace, revision: str,
                run_id: str) -> dict:
    return contract(commands, args.catalog_source, args.catalog_oci_manifest,
                    args.catalog_oci_layer, revision, run_id)


def exact_predecessors(commands: Commands, host: Kube, milo: Kube, identity: dict,
                       args: argparse.Namespace, source: dict) -> dict:
    """Bind the four-row auth chain, OCI graph and exact one-CRD stage."""
    lb = _dependency_module("bootstrap-apps-native-locationbinding-crd.py")
    oci = transition_contract(commands, args.catalog_source,
                              args.catalog_oci_manifest, args.catalog_oci_layer)
    installer, graph, locations, transition = lb.live_dependencies(
        host, milo, identity, args.installer_evidence_file,
        args.catalog_api_evidence_file, args.locations_evidence_file,
        args.catalog_oci_evidence_file, oci)
    schema = read_journal(args.locationbinding_evidence_file)
    run_id = schema.get("run_id")
    require(schema.get("setup_stage") == "ready" and
            schema.get("cleanup") == "not-requested" and
            schema.get("scope_identity") == identity and
            schema.get("source_revision") ==
                "344be39a24bb71247205904928529bd6f015fbe2" and
            isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{24}", run_id) is not None,
            "retained LocationBinding stage is not the reviewed signed run")
    git = host.json("-n", "flux-system", "get", "gitrepository", lb.SOURCE_NAME)
    stage = host.json("-n", CORE_NAMESPACE, "get", "kustomization", lb.STAGE_NAME)
    crd = milo.json("get", "crd", lb.CRD_NAME)
    require(git["metadata"].get("uid") == schema.get("attempted", {}).get("source") and
            git["spec"].get("ref") == {"commit": schema["source_revision"]} and
            git.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + schema["source_revision"] and ready(git) and
            stage["metadata"].get("uid") == schema.get("attempted", {}).get("stage") and
            ready(stage) and stage.get("status", {}).get("lastAppliedRevision") ==
                "sha1:" + schema["source_revision"] and
            len(stage.get("status", {}).get("inventory", {}).get("entries", [])) == 1 and
            crd["metadata"].get("uid") == schema.get("crd_uid") and
            any(item.get("type") == "Established" and item.get("status") == "True"
                for item in crd.get("status", {}).get("conditions", [])),
            "retained LocationBinding source, stage or CRD changed")
    require(source["catalog_oci_digest"] == OCI_DIGEST,
            "Catalog OCI contract changed before operator stage")
    return {"installer": installer, "graph": graph, "locations": locations,
            "transition": transition, "locationbinding": schema,
            "oci": oci}


def baseline_webhooks(milo: Kube, rendered: list[dict], expected: dict | None = None,
                      ca: str = "") -> dict[str, str]:
    source_hooks = {obj["metadata"]["name"]: obj for obj in rendered
                    if obj.get("kind") in ("MutatingWebhookConfiguration",
                                           "ValidatingWebhookConfiguration")}
    require(set(source_hooks) == set(WEBHOOK_NAMES),
            "Catalog source webhook set changed")
    observed = {}
    for name, count in WEBHOOK_NAMES.items():
        kind = ("mutatingwebhookconfiguration" if name == "services-mutating" else
                "validatingwebhookconfiguration")
        obj = milo.json("get", kind, name)
        meta = obj.get("metadata", {})
        expected_hooks = copy.deepcopy(source_hooks[name]["webhooks"])
        for hook in expected_hooks:
            hook.setdefault("matchPolicy", "Equivalent")
            hook.setdefault("namespaceSelector", {})
            hook.setdefault("objectSelector", {})
            hook.setdefault("timeoutSeconds", 10)
            if name == "services-mutating":
                hook.setdefault("reinvocationPolicy", "Never")
            hook["clientConfig"]["service"].setdefault("port", 443)
            for rule in hook["rules"]:
                rule.setdefault("scope", "*")
            if ca:
                hook["clientConfig"]["caBundle"] = ca
        require(len(expected_hooks) == count and
                obj.get("webhooks") == expected_hooks and
                not meta.get("deletionTimestamp") and
                not meta.get("ownerReferences") and
                all(h.get("failurePolicy") == "Fail" for h in expected_hooks),
                "Catalog webhook source rules, routing, failure policy or CA changed")
        observed[name] = meta.get("uid")
    require(all(observed.values()) and
            (expected is None or observed == expected),
            "Catalog webhook UID changed")
    return observed


def absence_preflight(host: Kube, milo: Kube) -> None:
    for key, (kind, name, namespace, _) in ROWS.items():
        cluster = milo if key in ("role", "binding") else host
        require(cluster.optional(kind, name, namespace) is None,
                f"existing Catalog operator {key} blocks trial")
    for name, namespace in ((CA_STAGE, "flux-system"), (HOST_STAGE, "flux-system")):
        require(host.optional("kustomization", name, namespace) is None,
                f"existing Catalog operator Flux stage {name} blocks trial")
    require(host.optional("namespace", NAMESPACE) is None,
            "existing services-system namespace blocks operator trial")
    selected = host.json("get", "deployment,replicaset,pod,statefulset,daemonset,job,cronjob",
                         "-A", "-l", "control-plane=services")
    require(selected.get("items") == [], "existing Catalog manager workload blocks trial")


def auth_rows(encoded: str) -> list[list[str]]:
    try:
        rows = list(csv.reader(io.StringIO(base64.b64decode(
            encoded, validate=True).decode())))
    except (ValueError, TypeError, UnicodeError) as error:
        raise RuntimeError("Milo static auth CSV malformed") from error
    locations_user = _dependency_module("bootstrap-apps-native-locations.py").USER
    expected = {("admin", "admin", "system:masters"),
                ("test-user", "test-user", "system:authenticated"),
                ("apps-a1t-catalog-installer", "apps-a1t-catalog-installer",
                 "system:authenticated"),
                (locations_user, locations_user, "system:authenticated")}
    require(len(rows) == 4 and all(len(row) == 4 and bool(row[0]) for row in rows) and
            {tuple(row[1:]) for row in rows} == expected and
            len({row[0] for row in rows}) == 4,
            "Milo static auth differs from exact four-user predecessor")
    return rows


def extended_auth(encoded: str, token: str) -> str:
    rows = auth_rows(encoded)
    require(re.fullmatch(r"[0-9a-f]{96}", token) is not None,
            "fresh scoped token format changed")
    rows.append([token, USER, USER, "system:authenticated"])
    stream = io.StringIO()
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return base64.b64encode(stream.getvalue().encode()).decode()


def scoped_kubeconfig(ca: str, token: str) -> dict:
    return {"apiVersion": "v1", "kind": "Config",
            "current-context": "catalog-operator",
            "clusters": [{"name": "milo", "cluster": {
                "server": "https://milo-apiserver.milo-system.svc.cluster.local:6443",
                "certificate-authority-data": ca}}],
            "users": [{"name": USER, "user": {"token": token}}],
            "contexts": [{"name": "catalog-operator", "context": {
                "cluster": "milo", "user": USER}}]}


def intent_digest(obj: dict) -> str:
    item = copy.deepcopy(obj)
    meta = item.get("metadata", {})
    require(not meta.get("ownerReferences") and not meta.get("deletionTimestamp") and
            meta.get("finalizers", []) in ([], ["finalizers.fluxcd.io"]),
            "Catalog operator object has unexpected lifecycle")
    for field in ("uid", "resourceVersion", "creationTimestamp", "managedFields",
                  "generation", "finalizers"):
        meta.pop(field, None)
    if item.get("kind") == "Namespace":
        meta.get("labels", {}).pop("kubernetes.io/metadata.name", None)
        require(item.get("spec", {}) in ({}, {"finalizers": ["kubernetes"]}),
                "Catalog operator Namespace lifecycle changed")
        item.pop("spec", None)
        item.pop("status", None)
    if item.get("kind") == "GitRepository":
        provider = item["spec"].pop("provider", "generic")
        require(provider == "generic", "Catalog CA source provider changed")
        item.pop("status", None)
    else:
        item.pop("status", None)
    require(set(item) <= {"apiVersion", "kind", "metadata", "spec", "rules",
                          "roleRef", "subjects", "type", "data"},
            "Catalog operator intent acquired unknown fields")
    return hashlib.sha256(json.dumps(item, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()


def probe_manifests(ca: bytes) -> tuple[dict, dict]:
    public = ca.decode()
    require(public.startswith("-----BEGIN CERTIFICATE-----") and
            "PRIVATE KEY" not in public and len(public) <= 65536,
            "TLS probe may mount only a bounded public CA")
    configmap = {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": PUBLIC_CA_NAME, "namespace": CORE_NAMESPACE},
                 "data": {"ca.crt": public}}
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": TLS_PROBE_NAME, "namespace": CORE_NAMESPACE},
           "spec": {"automountServiceAccountToken": False,
                    "restartPolicy": "Never", "activeDeadlineSeconds": 60,
                    "securityContext": {"runAsNonRoot": True,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": "probe", "image": CURL_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["curl", "--cacert", "/public/ca.crt",
                                                "--silent", "--show-error", "--max-time",
                                                "10", "-o", "/dev/null",
                                                "https://" + WEBHOOK_SERVICE + "/"],
                                    "securityContext": {"allowPrivilegeEscalation": False,
                                                        "capabilities": {"drop": ["ALL"]}},
                                    "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                                  "limits": {"cpu": "100m", "memory": "128Mi"}},
                                    "volumeMounts": [{"name": "public-ca",
                                                      "mountPath": "/public", "readOnly": True}]}],
                    "volumes": [{"name": "public-ca", "configMap": {
                        "name": PUBLIC_CA_NAME, "defaultMode": 420,
                        "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]}}
    return configmap, pod


def probe_pod_matches(current: dict, expected: dict, run_id: str,
                      *, allow_deleting: bool = False,
                      allowed_nodes: set[str] | None = None) -> None:
    meta = current.get("metadata", {})
    spec = copy.deepcopy(current.get("spec", {}))
    target = expected["spec"]
    require(current.get("kind") == "Pod" and meta.get("name") == TLS_PROBE_NAME and
            meta.get("namespace") == CORE_NAMESPACE and
            meta.get("labels") == {RUN_LABEL: run_id} and
            not meta.get("annotations") and not meta.get("finalizers") and
            not meta.get("generateName") and
            not meta.get("ownerReferences") and
            (allow_deleting or not meta.get("deletionTimestamp")),
            "Catalog Milo-network TLS probe Pod identity or lifecycle changed")
    # The dedicated Kind API sets these exact Kubernetes defaults on Pods.
    # Treat every other field as drift, including added env, containers,
    # mounts, service-account tokens, ephemeral containers and scheduling rules.
    defaults = {"dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
                "serviceAccountName": "default", "serviceAccount": "default",
                "terminationGracePeriodSeconds": 30, "enableServiceLinks": True,
                "priority": 0, "preemptionPolicy": "PreemptLowerPriority"}
    for key, value in defaults.items():
        if key in spec:
            require(spec.pop(key) == value,
                    "Catalog TLS probe Pod default changed")
    tolerations = [
        {"effect": "NoExecute", "key": f"node.kubernetes.io/{reason}",
         "operator": "Exists", "tolerationSeconds": 300}
        for reason in ("not-ready", "unreachable")]
    if "tolerations" in spec:
        observed = spec.pop("tolerations")
        require(len(observed) == 2 and
                {json.dumps(item, sort_keys=True) for item in observed} ==
                {json.dumps(item, sort_keys=True) for item in tolerations},
                "Catalog TLS probe Pod tolerations changed")
    node = spec.pop("nodeName", None)
    require(node is None or (allowed_nodes is not None and node in allowed_nodes),
            "Catalog TLS probe Pod moved outside the exact host node set")
    require(len(spec.get("containers", [])) == 1,
            "Catalog TLS probe Pod container count changed")
    container = spec["containers"][0]
    for key, value in (("terminationMessagePath", "/dev/termination-log"),
                       ("terminationMessagePolicy", "File")):
        if key in container:
            require(container.pop(key) == value,
                    "Catalog TLS probe container default changed")
    require(spec == target,
            "Catalog Milo-network TLS probe Pod changed or acquired privilege")


def delete_probe_object(host: Kube, proxy: APIProxy, key: str,
                        expected: dict, evidence: dict, path: Path) -> None:
    if key not in evidence.get("attempted", {}):
        return
    kind, name, namespace, route = ROWS[key]
    current = host.optional(kind, name, namespace)
    if current is None:
        return
    deleting = evidence.get("delete_intents", {}).get(key) == current["metadata"].get("uid")
    if key == "tls_pod":
        nodes = host.json("get", "nodes").get("items", [])
        require(len(nodes) == 1 and
                all(item["metadata"].get("uid") for item in nodes),
                "Catalog TLS probe cleanup requires the exact single host node")
        probe_pod_matches(current, expected, evidence["run_id"],
                          allow_deleting=deleting,
                          allowed_nodes={nodes[0]["metadata"]["name"]})
    else:
        marked = copy.deepcopy(expected)
        marked["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
        observed = copy.deepcopy(current)
        if deleting:
            observed["metadata"].pop("deletionTimestamp", None)
            observed["metadata"].pop("deletionGracePeriodSeconds", None)
        require(intent_digest(observed) == intent_digest(marked),
                "Catalog public CA diagnostic ConfigMap changed")
    uid = evidence["attempted"][key]
    require(uid is None or uid == current["metadata"]["uid"],
            "Catalog network probe object replaced")
    if uid is None:
        evidence["attempted"][key] = current["metadata"]["uid"]
        atomic_json(path, evidence)
    evidence.setdefault("delete_intents", {})[key] = current["metadata"]["uid"]
    atomic_json(path, evidence)
    if current["metadata"].get("deletionTimestamp"):
        def terminating_absent():
            obj = host.optional(kind, name, namespace)
            require(obj is None or obj["metadata"]["uid"] == current["metadata"]["uid"],
                    "Catalog network probe replaced during terminating cleanup")
            return obj is None
        wait_for(terminating_absent,
                 "already terminating Catalog network probe absent", 90)
        return
    proxy.delete(route, current["metadata"]["uid"],
                 current["metadata"]["resourceVersion"])
    def absent():
        obj = host.optional(kind, name, namespace)
        require(obj is None or obj["metadata"]["uid"] == current["metadata"]["uid"],
                "Catalog network probe object replaced during cleanup")
        return obj is None
    wait_for(absent, "Catalog network probe object absent", 90)


def milo_network_tls(host: Kube, ca: bytes, evidence: dict, path: Path) -> None:
    policies = host.json("-n", CORE_NAMESPACE, "get", "networkpolicies")
    apiserver = host.json("-n", CORE_NAMESPACE, "get", "pods", "-l",
                          "app.kubernetes.io/name=milo-apiserver")
    ready_api = [pod for pod in apiserver.get("items", [])
                 if not pod["metadata"].get("deletionTimestamp") and
                 any(item.get("type") == "Ready" and item.get("status") == "True"
                     for item in pod.get("status", {}).get("conditions", []))]
    require(bool(ready_api), "Milo API Pod labels unavailable for network equivalence")
    probe_labels = {RUN_LABEL: evidence["run_id"]}
    for policy in policies.get("items", []):
        selector = policy.get("spec", {}).get("podSelector", {})
        labels = selector.get("matchLabels", {})
        require(isinstance(labels, dict) and labels and
                not selector.get("matchExpressions") and
                all(not all(pod["metadata"].get("labels", {}).get(key) == value
                            for key, value in labels.items())
                    for pod in ready_api) and
                not all(probe_labels.get(key) == value for key, value in labels.items()),
                "Milo NetworkPolicy selects the API or diagnostic Pod")
    require(host.json("-n", NAMESPACE, "get", "networkpolicies").get("items") == [],
            "Catalog target namespace NetworkPolicy changes TLS-probe reachability")
    configmap, pod = probe_manifests(ca)
    proxy = APIProxy(host, path.parent, "catalog-milo-tls-probe")
    try:
        proxy.start()
        create_owned(host, "tls_configmap", configmap, evidence, path)
        create_owned(host, "tls_pod", pod, evidence, path)
        def complete():
            current = host.json("-n", CORE_NAMESPACE, "get", "pod", TLS_PROBE_NAME)
            require(current["metadata"].get("uid") == evidence["attempted"]["tls_pod"],
                    "Catalog TLS probe Pod UID changed")
            phase = current.get("status", {}).get("phase")
            if phase not in ("Succeeded", "Failed"):
                return False
            status = current.get("status", {}).get("containerStatuses", [])
            exit_code = (status[0].get("state", {}).get("terminated", {}).get("exitCode")
                         if len(status) == 1 else None)
            image_id = (status[0].get("imageID", "") if len(status) == 1 else "")
            digest = image_id.removeprefix("docker-pullable://").split("@")[-1]
            require(phase == "Succeeded" and exit_code == 0 and
                    digest in {CURL_IMAGE.split("@")[-1], CURL_ARM64,
                               CURL_ARM64_CONFIG},
                    "Milo namespace DNS/TLS probe failed or ran an unexpected image")
            return {"pod_uid": current["metadata"]["uid"],
                    "image_id": image_id, "exit_code": exit_code}
        evidence["milo_tls_probe"] = wait_for(complete,
                                               "Milo namespace Catalog DNS/TLS", 120)
        atomic_json(path, evidence)
    finally:
        try:
            delete_probe_object(host, proxy, "tls_pod", pod, evidence, path)
            delete_probe_object(host, proxy, "tls_configmap", configmap,
                                evidence, path)
        finally:
            proxy.close()


def create_owned(cluster: Kube, key: str, manifest: dict, evidence: dict,
                 path: Path, *, sensitive: bool = False) -> dict:
    require(key in ROWS and key not in evidence["attempted"],
            "Catalog operator duplicate or unsupported create intent")
    kind, name, namespace, _ = ROWS[key]
    obj = copy.deepcopy(manifest)
    require(obj["metadata"]["name"] == name and
            obj["kind"].lower() == kind.lower() and
            obj["metadata"].get("namespace") in (None, namespace),
            "Catalog operator create differs from reviewed object identity")
    obj["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    evidence["attempted"][key] = None
    evidence.setdefault("intents", {})[key] = intent_digest(obj)
    atomic_json(path, evidence)
    result = cluster.create(obj, namespace, sensitive=sensitive)
    uid = result.get("metadata", {}).get("uid")
    require(isinstance(uid, str) and bool(uid), f"{key} create response lacks UID")
    evidence["attempted"][key] = uid
    atomic_json(path, evidence)
    return result


def delete_owned(cluster: Kube, proxy: APIProxy, key: str, manifest: dict,
                 evidence: dict, path: Path) -> None:
    if key not in evidence.get("attempted", {}):
        return
    kind, name, namespace, route = ROWS[key]
    current = cluster.optional(kind, name, namespace)
    if current is None:
        return
    deleting = evidence.get("delete_intents", {}).get(key) == current["metadata"].get("uid")
    observed = copy.deepcopy(current)
    if deleting:
        observed["metadata"].pop("deletionTimestamp", None)
        observed["metadata"].pop("deletionGracePeriodSeconds", None)
    expected = copy.deepcopy(manifest)
    expected["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    require(intent_digest(observed) == evidence.get("intents", {}).get(key) ==
            intent_digest(expected),
            f"Catalog operator {key} differs from exact source intent")
    uid = evidence["attempted"][key]
    require(uid is None or uid == current["metadata"]["uid"],
            f"Catalog operator {key} replaced")
    if uid is None:
        evidence["attempted"][key] = current["metadata"]["uid"]
        atomic_json(path, evidence)
    evidence.setdefault("delete_intents", {})[key] = current["metadata"]["uid"]
    atomic_json(path, evidence)
    if current["metadata"].get("deletionTimestamp"):
        def terminating_absent():
            obj = cluster.optional(kind, name, namespace)
            require(obj is None or obj["metadata"]["uid"] == current["metadata"]["uid"],
                    f"Catalog operator {key} replaced during terminating cleanup")
            return obj is None
        wait_for(terminating_absent,
                 f"already terminating Catalog operator {key} absent", 120)
        return
    proxy.delete(route, current["metadata"]["uid"],
                 current["metadata"]["resourceVersion"])
    def absent():
        obj = cluster.optional(kind, name, namespace)
        require(obj is None or obj["metadata"]["uid"] == current["metadata"]["uid"],
                f"Catalog operator {key} replaced during cleanup")
        return obj is None
    wait_for(absent, f"Catalog operator {key} absent", 120)


def probe_identity(scoped: Kube) -> None:
    for verb, resource in (("get", "projects.resourcemanager.miloapis.com"),
                           ("list", "services.services.miloapis.com"),
                           ("patch", "services.services.miloapis.com/status"),
                           ("list", "locations.locations.miloapis.com")):
        scoped.can_i(verb, resource, True)
    for verb, resource in (("get", "secrets"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("create", "clusterrolebindings.rbac.authorization.k8s.io"),
                           ("impersonate", "users"),
                           ("create", "workloads.compute.datumapis.com")):
        scoped.can_i(verb, resource, False)


def patch_auth(host: Kube, private: Path, uid: str, before: str, after: str) -> None:
    host.patch_json(private, "secret", "milo-apiserver-auth-tokens", [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/data/tokens.csv", "value": before},
        {"op": "replace", "path": "/data/tokens.csv", "value": after},
    ], namespace=CORE_NAMESPACE, sensitive=True)


def root_user_config(private: Path, root: Kube, identity: dict,
                     token: str, port: int) -> Kube:
    config = scoped_kubeconfig(base64.b64encode(
        base64.b64decode(root.json("config", "view", "--raw")
                         ["clusters"][0]["cluster"]["certificate-authority-data"],
                         validate=True)).decode(), token)
    ca = base64.b64decode(config["clusters"][0]["cluster"]["certificate-authority-data"],
                          validate=True)
    require(hashlib.sha256(ca).hexdigest() == identity["milo"]["ca_sha256"],
            "operator scoped kubeconfig public CA differs from root")
    config["clusters"][0]["cluster"]["server"] = f"https://127.0.0.1:{port}"
    path = private / "operator-scoped.kubeconfig"
    private_write(path, yaml.safe_dump(config))
    return Kube(root.commands, path, "catalog-operator")


def require_cert_chain(commands: Commands, private: Path, ca_secret: dict,
                       serving_secret: dict) -> bytes:
    require(ca_secret.get("type") == "kubernetes.io/tls" and
            serving_secret.get("type") == "kubernetes.io/tls" and
            {"tls.crt", "tls.key"} <= set(ca_secret.get("data", {})) and
            {"tls.crt", "tls.key"} <= set(serving_secret.get("data", {})),
            "Catalog serving or CA TLS Secret shape changed")
    ca = base64.b64decode(ca_secret["data"]["tls.crt"], validate=True)
    leaf = base64.b64decode(serving_secret["data"]["tls.crt"], validate=True)
    require(b"BEGIN CERTIFICATE" in ca and b"PRIVATE KEY" not in ca,
            "Catalog public CA certificate invalid")
    text = commands.run("openssl", "x509", "-noout", "-text", "-in",
                        str(_public_file(private, ca)), sensitive=True)
    require("CA:TRUE" in text, "Catalog issuer certificate lacks CA basic constraint")
    verify_cert(commands, private, ca, leaf, dns=WEBHOOK_SERVICE)
    return ca


def _public_file(private: Path, value: bytes) -> Path:
    path = private / ("public-" + hashlib.sha256(value).hexdigest() + ".pem")
    private_write(path, value)
    return path


def source_ready(host: Kube, evidence: dict, revision: str) -> bool:
    obj = host.optional("gitrepository", CA_SOURCE, "flux-system")
    require(obj is not None and
            obj["metadata"].get("uid") == evidence["attempted"]["ca_source"] and
            obj.get("spec", {}).get("ref") == {"commit": revision},
            "Catalog CA source UID or commit changed")
    return ready(obj) and obj.get("status", {}).get("artifact", {}).get(
        "revision") == "sha1:" + revision


def stage_ready(host: Kube, key: str, namespace: str, name: str,
                revision: str, count: int, evidence: dict) -> bool:
    obj = host.optional("kustomization", name, namespace)
    require(obj is not None and
            obj["metadata"].get("uid") == evidence["attempted"][key],
            f"Catalog {key} stage UID changed")
    return (ready(obj) and obj.get("status", {}).get("lastAppliedRevision") == revision and
            len(obj.get("status", {}).get("inventory", {}).get("entries", [])) == count)


def cert_ready(host: Kube, name: str) -> bool:
    certificate = host.optional("certificate", name, NAMESPACE)
    return certificate is not None and certificate_ready(certificate)


def direct_manifest(key: str, contracts: dict, token: str | None = None,
                    ca_data: str | None = None) -> dict:
    if key == "namespace":
        return {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": NAMESPACE}}
    if key == "role":
        return contracts["role"]
    if key == "binding":
        return contracts["binding"]
    if key == "ca_source":
        return contracts["ca"]["source"]
    if key == "credential":
        require(token is not None and ca_data is not None,
                "scoped Catalog operator Secret needs fresh token and verified public CA")
        content = yaml.safe_dump(scoped_kubeconfig(ca_data, token))
        return {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": CREDENTIAL_SECRET, "namespace": NAMESPACE},
                "type": "Opaque", "data": {"kubeconfig": base64.b64encode(
                    content.encode()).decode()}}
    raise RuntimeError("unsupported Catalog operator direct manifest")


def verified_pod(host: Kube, evidence: dict) -> dict:
    deployment = host.json("-n", NAMESPACE, "get", "deployment",
                           "services-controller-manager")
    meta = deployment["metadata"]
    status = deployment.get("status", {})
    require(meta.get("labels", {}).get(RUN_LABEL) == evidence["run_id"] and
            meta.get("generation") == status.get("observedGeneration") and
            deployment["spec"].get("replicas") == 1 and
            status.get("readyReplicas") == 1 and status.get("availableReplicas") == 1 and
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] ==
                "ghcr.io/milo-os/service-catalog@" + IMAGE_INDEX,
            "Catalog source Deployment generation, replica or image changed")
    replicasets = host.json("-n", NAMESPACE, "get", "replicasets", "-l",
                            "control-plane=services").get("items", [])
    owned_rs = {rs["metadata"]["uid"] for rs in replicasets
                if any(ref.get("kind") == "Deployment" and ref.get("uid") == meta["uid"]
                       and ref.get("controller") is True
                       for ref in rs["metadata"].get("ownerReferences", []))}
    pods = host.json("-n", NAMESPACE, "get", "pods", "-l",
                     "control-plane=services").get("items", [])
    require(len(pods) == 1 and not pods[0]["metadata"].get("deletionTimestamp") and
            any(ref.get("kind") == "ReplicaSet" and ref.get("uid") in owned_rs and
                ref.get("controller") is True
                for ref in pods[0]["metadata"].get("ownerReferences", [])),
            "Catalog Pod is not the unique selected manager replica")
    pod = pods[0]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    require(len(statuses) == 1,
            "Catalog Pod must report exactly one manager container status")
    image_id = statuses[0].get("imageID", "")
    digest = image_id.removeprefix("docker-pullable://").split("@")[-1]
    require(pod.get("status", {}).get("phase") == "Running" and
            any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", [])) and
            statuses[0].get("ready") is True and
            digest in {IMAGE_INDEX, IMAGE_ARM64, IMAGE_ARM64_CONFIG},
            "Catalog Pod readiness or exact arm64 imageID unverified")
    node = host.json("get", "node", pod["spec"]["nodeName"])
    require(node["metadata"].get("labels", {}).get("kubernetes.io/arch") == "arm64",
            "Catalog manager ran on unverified node architecture")
    return {"deployment_uid": meta["uid"], "pod_uid": pod["metadata"]["uid"],
            "node_uid": node["metadata"]["uid"], "image_id": image_id}


def service_tls(commands: Commands, host: Kube, private: Path, ca: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    forward = Forward(host, NAMESPACE, "services-webhook", port, 443)
    try:
        forward.start()
        context = ssl.create_default_context(cadata=ca.decode())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=WEBHOOK_SERVICE) as peer:
                require(bool(peer.getpeercert()),
                        "Catalog webhook Service did not present trusted DNS certificate")
    finally:
        forward.close()


def api_stage_owner(host: Kube, source: dict, predecessors: dict,
                    evidence: dict, path: Path) -> FluxOwned:
    oci = predecessors["oci"]
    graph = predecessors["graph"]
    transition = predecessors["transition"]
    def save(state: dict) -> None:
        evidence["api_flux_state"] = state
        atomic_json(path, evidence)
    ref = FluxOwned(host, CORE_NAMESPACE, "service-catalog-api-crds",
                    oci["rendered"], OCI_DIGEST, save)
    saved = evidence.get("api_flux_state", transition["target_flux_state"])
    ref.recover(oci["target"], graph["run_id"], saved)
    require(ref.uid == transition["stage_uid"],
            "Catalog API stage UID differs from retained OCI transition")
    return ref


def reconcile(host: Kube, kind: str, name: str, namespace: str,
              seconds: int) -> None:
    host.commands.run("flux", "--kubeconfig", str(host.config), "--context",
                      host.context, "reconcile", kind, name, "-n", namespace,
                      f"--timeout={seconds}s", timeout=seconds + 10)


def patch_existing_webhooks(host: Kube, milo: Kube, private: Path, ca: bytes,
                            evidence: dict, path: Path, predecessors: dict) -> None:
    owner_ref = api_stage_owner(host, {}, predecessors, evidence, path)
    owner_ref.bind_webhook_ca(private, ca)
    reconcile(host, "kustomization", "service-catalog-api-crds", CORE_NAMESPACE, 180)
    require(stage_ready(host, "api_stage", CORE_NAMESPACE, "service-catalog-api-crds",
                        OCI_DIGEST, 13, {"attempted": {"api_stage": owner_ref.uid}}),
            "Catalog API stage did not reconcile public CA")
    expected = evidence["webhook_uids"]
    baseline_webhooks(milo, predecessors["oci"]["rendered"], expected,
                      base64.b64encode(ca).decode())
    evidence["ca_bound"] = True
    atomic_json(path, evidence)


def restore_existing_webhooks(host: Kube, milo: Kube, private: Path,
                              evidence: dict, path: Path, predecessors: dict) -> None:
    saved = evidence.get("api_flux_state")
    if saved is None or saved.get("ca_bundle") is None:
        return
    ref = api_stage_owner(host, {}, predecessors, evidence, path)
    ref.restore_webhook_ca(private)
    reconcile(host, "kustomization", "service-catalog-api-crds", CORE_NAMESPACE, 180)
    require(stage_ready(host, "api_stage", CORE_NAMESPACE, "service-catalog-api-crds",
                        OCI_DIGEST, 13, {"attempted": {"api_stage": ref.uid}}),
            "Catalog API stage did not restore empty CA")
    baseline_webhooks(milo, predecessors["oci"]["rendered"],
                      evidence["webhook_uids"])
    evidence["ca_bound"] = False
    atomic_json(path, evidence)


def cleanup_predecessors(commands: Commands, host: Kube, milo: Kube,
                         identity: dict, args: argparse.Namespace, evidence: dict,
                         state: dict) -> dict:
    """Rebind the original chain while this stage may own a CA/auth extension."""
    graph = read_journal(args.catalog_api_evidence_file)
    locations = read_journal(args.locations_evidence_file)
    transition = read_journal(args.catalog_oci_evidence_file)
    locationbinding = read_journal(args.locationbinding_evidence_file)
    require(evidence.get("predecessor_runs") == {
        "catalog_api": graph.get("run_id"), "locations": locations.get("run_id"),
        "catalog_oci": transition.get("run_id"),
        "locationbinding": locationbinding.get("run_id")} and
        graph.get("cleanup") == locations.get("cleanup") ==
            transition.get("cleanup") == locationbinding.get("cleanup") == "not-requested" and
        graph.get("scope_identity") == locations.get("scope_identity") ==
            locationbinding.get("scope_identity") == identity and
        transition.get("scope_identity") == identity,
        "Catalog operator retained predecessor journal identity changed")
    allowed = {state["old_b64"]}
    if state.get("auth_intent"):
        allowed.add(state["new_b64"])
    active_catalog_installer(host, milo, identity, args.installer_evidence_file,
                             auth_allowed=allowed)
    oci = transition_contract(commands, args.catalog_source,
                              args.catalog_oci_manifest, args.catalog_oci_layer)
    source = host.json("-n", "flux-system", "get", "ocirepository",
                       "apps-a1t-catalog-bundle")
    require(source["metadata"].get("uid") == transition.get("oci_uid") and
            source.get("status", {}).get("artifact", {}).get("revision") == OCI_DIGEST and
            ready(source), "Catalog OCI source changed during operator fixture")
    lb_stage = host.json("-n", CORE_NAMESPACE, "get", "kustomization",
                         "apps-a1t-locationbinding-crd")
    lb_crd = milo.json("get", "crd", "locationbindings.networking.datumapis.com")
    require(lb_stage["metadata"].get("uid") == locationbinding.get("attempted", {}).get(
                "stage") and ready(lb_stage) and
            lb_crd["metadata"].get("uid") == locationbinding.get("crd_uid"),
            "LocationBinding dependency changed during operator fixture")
    retained = {"graph": graph, "locations": locations,
                "transition": transition, "locationbinding": locationbinding, "oci": oci}
    ref = api_stage_owner(host, {}, retained, evidence, args.evidence_file)
    observed_stage = host.json("-n", CORE_NAMESPACE, "get", "kustomization",
                               "service-catalog-api-crds")
    ref._validate(observed_stage, graph["run_id"])
    require(observed_stage.get("status", {}).get("lastAppliedRevision") in
                (None, OCI_DIGEST),
            "Catalog API stage observed a different OCI revision")
    return retained


def data_census(milo: Kube) -> None:
    """No operator-produced API data may remain before removing its admission."""
    resources = ("services", "serviceavailabilities", "serviceconfigurations",
                 "serviceconsumers", "serviceentitlements",
                 "serviceactivationrequests", "serviceagents",
                 "serviceagentconfigurations")
    project_set = milo.json("get", "projects.resourcemanager.miloapis.com")
    require({item["metadata"]["name"] for item in project_set.get("items", [])} ==
            {"apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b"},
            "Catalog data census Project set changed")
    for project in (None, "apps-a1t-provider", "apps-a1t-consumer-a",
                    "apps-a1t-consumer-b"):
        prefix = ("" if project is None else
                  "/apis/resourcemanager.miloapis.com/v1alpha1/projects/" +
                  project + "/control-plane")
        for group, version, kinds in (
            ("services.miloapis.com", "v1alpha1", resources),
            ("networking.datumapis.com", "v1alpha", ("locationbindings",)),
        ):
            for resource in kinds:
                result = json.loads(milo.k("get", "--raw",
                    f"{prefix}/apis/{group}/{version}/{resource}"))
                require(isinstance(result.get("items"), list) and
                        result["items"] == [],
                        "Catalog or LocationBinding data remains in root/Project API")


def generated_secret_delete(host: Kube, proxy: APIProxy, name: str,
                            expected_uid: str | None, evidence: dict,
                            path: Path) -> None:
    current = host.optional("secret", name, NAMESPACE)
    if current is None:
        return
    require(expected_uid is not None and
            current["metadata"].get("uid") == expected_uid and
            current.get("type") == "kubernetes.io/tls" and
            set(current.get("data", {})) >= {"tls.crt", "tls.key"} and
            secret_fingerprint(current) == evidence.get("generated_secret_sha256", {}).get(name),
            "Catalog generated TLS Secret cannot be proven owned")
    if current["metadata"].get("deletionTimestamp"):
        require(evidence.get("generated_delete_intents", {}).get(name) == expected_uid,
                "Catalog generated Secret termination was not journaled")
    else:
        evidence.setdefault("generated_delete_intents", {})[name] = expected_uid
        atomic_json(path, evidence)
        proxy.delete(f"/api/v1/namespaces/{NAMESPACE}/secrets/{name}",
                     expected_uid, current["metadata"]["resourceVersion"])
    def absent():
        obj = host.optional("secret", name, NAMESPACE)
        require(obj is None or obj["metadata"]["uid"] == expected_uid,
                "Catalog generated TLS Secret replaced during delete")
        return obj is None
    wait_for(absent, f"Catalog TLS Secret {name} absent", 120)


def secret_fingerprint(obj: dict) -> str:
    return hashlib.sha256(json.dumps({"type": obj.get("type"),
                                      "data": obj.get("data", {})},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def host_objects_absent(host: Kube, rendered: list[dict]) -> None:
    for obj in rendered:
        kind, meta = obj["kind"], obj["metadata"]
        require(host.optional(kind, meta["name"], meta.get("namespace")) is None,
                "Catalog host Flux object remained after source-bound prune")
    selected = host.json("get", "deployment,replicaset,pod,statefulset,daemonset,job,cronjob",
                         "-A", "-l", "control-plane=services")
    require(selected.get("items") == [],
            "Catalog manager or terminating Pod remains after host stage prune")


def auth_restore(host: Kube, private: Path, evidence: dict,
                 state: dict, state_path: Path) -> None:
    if not state.get("auth_intent"):
        return
    auth = host.json("-n", CORE_NAMESPACE, "get", "secret",
                     "milo-apiserver-auth-tokens")
    require(auth["metadata"].get("uid") == state["auth_uid"],
            "Milo static auth Secret UID changed")
    current = auth["data"].get("tokens.csv")
    require(current in (state["old_b64"], state["new_b64"]),
            "Milo static auth differs from exact operator transition")
    if current == state["new_b64"]:
        patch_auth(host, private, state["auth_uid"],
                   state["new_b64"], state["old_b64"])
    if not state.get("restored_auth_rollout"):
        proof = rollout_static_auth(host)
        observed = host.json("-n", CORE_NAMESPACE, "get", "secret",
                             "milo-apiserver-auth-tokens")
        require(observed["metadata"].get("uid") == state["auth_uid"] and
                observed["data"].get("tokens.csv") == state["old_b64"],
                "Milo old static auth not retained through postrestore rollout")
        state["restored_auth_rollout"] = proof
        atomic_json(state_path, state)


def cleanup(host: Kube, milo: Kube, commands: Commands, args: argparse.Namespace,
            contracts: dict, evidence: dict, state: dict, state_path: Path,
            identity: dict) -> None:
    require_same_identity(evidence["scope_identity"], identity)
    predecessors = cleanup_predecessors(commands, host, milo, identity,
                                        args, evidence, state)
    private = args.evidence_file.parent
    host_proxy = APIProxy(host, private, "catalog-operator-host")
    root_proxy = APIProxy(milo, private, "catalog-operator-root")
    errors: list[str] = []
    try:
        host_proxy.start()
        root_proxy.start()
        if ({"tls_pod", "tls_configmap"} &
                set(evidence.get("attempted", {}))):
            try:
                ca_secret = host.json("-n", NAMESPACE, "get", "secret", CA_SECRET)
                require(ca_secret["metadata"].get("uid") ==
                        evidence.get("ca_secret_uid"),
                        "Catalog CA Secret changed before network probe cleanup")
                ca = base64.b64decode(ca_secret["data"]["tls.crt"], validate=True)
                require(hashlib.sha256(ca).hexdigest() ==
                        evidence.get("public_ca_sha256"),
                        "Catalog public CA changed before network probe cleanup")
                cm, pod = probe_manifests(ca)
                delete_probe_object(host, host_proxy, "tls_pod", pod,
                                    evidence, args.evidence_file)
                delete_probe_object(host, host_proxy, "tls_configmap", cm,
                                    evidence, args.evidence_file)
            except Exception:
                errors.append("Milo namespace TLS probe cleanup failed")
        if ("host_stage" in evidence.get("attempted", {}) or
                evidence.get("api_flux_state", {}).get("ca_bundle") is not None):
            try:
                data_census(milo)
            except Exception:
                errors.append("Catalog API data or LocationBinding census failed")
        if not errors and evidence.get("api_flux_state", {}).get("ca_bundle") is not None:
            try:
                restore_existing_webhooks(host, milo, private, evidence,
                                          args.evidence_file, predecessors)
            except Exception:
                errors.append("source-owned public webhook CA restore failed")
        if not errors and "host_stage" in evidence.get("attempted", {}):
            try:
                delete_stage(host, host_proxy, "host_stage",
                             {"host_stage": contracts["operator"]["flux_contract"]},
                             evidence, args.evidence_file, atomic_json)
                require(host.optional("kustomization", HOST_STAGE, "flux-system") is None,
                        "Catalog host Flux stage still active")
                wait_for(lambda: host_objects_absent(
                    host, contracts["operator"]["rendered"]) is None,
                    "Catalog host source-owned objects absent", 120)
            except Exception:
                errors.append("source-bound Catalog host stage prune failed")
        if not errors and "credential" in evidence.get("attempted", {}):
            try:
                delete_owned(host, host_proxy, "credential",
                             direct_manifest("credential", contracts,
                                             state["token"], state["milo_ca_b64"]),
                             evidence, args.evidence_file)
            except Exception:
                errors.append("scoped Catalog operator host credential cleanup failed")
        if not errors:
            try:
                auth_restore(host, private, evidence, state, state_path)
                wait_authenticated_root(milo, 45)
            except Exception:
                errors.append("Catalog operator static auth restoration failed")
        if not errors:
            for key in ("binding", "role"):
                try:
                    delete_owned(milo, root_proxy, key, direct_manifest(key, contracts),
                                 evidence, args.evidence_file)
                except Exception:
                    errors.append(f"Catalog operator {key} cleanup failed")
                    break
        if not errors and "ca_stage" in evidence.get("attempted", {}):
            try:
                delete_stage(host, host_proxy, "ca_stage",
                             {"ca_stage": contracts["ca"]["flux_contract"]},
                             evidence, args.evidence_file, atomic_json)
                require(host.optional("kustomization", CA_STAGE, "flux-system") is None,
                        "Catalog CA Flux stage still active")
                wait_for(lambda: host_objects_absent(
                    host, contracts["ca"]["rendered"]) is None,
                    "Catalog CA source-owned objects absent", 120)
            except Exception:
                errors.append("source-bound Catalog CA stage prune failed")
        if not errors:
            for name, field in ((SERVING_SECRET, "serving_secret_uid"),
                                (CA_SECRET, "ca_secret_uid")):
                try:
                    generated_secret_delete(host, host_proxy, name,
                                            evidence.get(field), evidence,
                                            args.evidence_file)
                except Exception:
                    errors.append(f"Catalog generated {name} TLS Secret cleanup failed")
                    break
        if not errors and "ca_source" in evidence.get("attempted", {}):
            try:
                delete_owned(host, host_proxy, "ca_source",
                             contracts["ca"]["source"], evidence, args.evidence_file)
            except Exception:
                errors.append("Catalog CA Git source cleanup failed")
        if not errors and "namespace" in evidence.get("attempted", {}):
            namespace = host.optional("namespace", NAMESPACE)
            require(namespace is not None and
                    namespace["metadata"].get("uid") ==
                        evidence["attempted"]["namespace"] and
                    namespace["metadata"].get("labels", {}).get(RUN_LABEL) ==
                        evidence["run_id"],
                    "Catalog retained namespace identity changed")
            # Keep the exact task-owned Namespace. Cascading deletion requires a
            # separate exhaustive census and is outside this prerequisite.
            evidence["retained_namespace_uid"] = namespace["metadata"]["uid"]
    except Exception:
        errors.append("Catalog operator private cleanup transport or namespace check failed")
    finally:
        root_proxy.close()
        host_proxy.close()
    evidence["cleanup"] = "complete" if not errors else "incomplete"
    evidence["cleanup_errors"] = errors
    atomic_json(args.evidence_file, evidence)
    require(not errors, "Catalog operator cleanup incomplete: " + "; ".join(errors))


def run(args: argparse.Namespace) -> None:
    for path in (args.host_kubeconfig, args.milo_kubeconfig):
        require(not path.is_symlink(), "private kubeconfig symlink refused")
        private_file(path)
    private_evidence(args.evidence_file)
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    revision = preflight("signed-source", lambda: clean_revision(commands))
    if args.cleanup:
        evidence = read_journal(args.evidence_file)
        run_id = evidence.get("run_id")
    else:
        require(not args.evidence_file.exists(), "Catalog operator evidence already exists")
        run_id = secrets.token_hex(12)
    contracts = preflight("source-render", lambda:
                          source_pins(commands, args, revision, run_id))
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    identity = preflight("api-scope", lambda:
                         scope_identity(host, milo, args.host_context,
                                        args.milo_port))
    state_path = args.evidence_file.parent / ("catalog-operator-" + run_id + "-private.json")
    require(state_path.parent == args.evidence_file.parent and
            not state_path.is_symlink(), "Catalog operator private state path changed")
    if args.cleanup:
        private_file(state_path)
        state = json.loads(state_path.read_text())
        require(evidence.get("version") == 1 and
                evidence.get("source_revision") == revision and
                evidence.get("run_id") == state.get("run_id") and
                evidence.get("catalog_source") == SOURCE_REVISION and
                evidence.get("catalog_oci_digest") == OCI_DIGEST and
                evidence.get("image_index") == IMAGE_INDEX and
                evidence.get("scope_identity") == identity and
                evidence.get("cleanup") in ("not-requested", "incomplete") and
                state.get("private_path") == str(state_path),
                "Catalog operator cleanup source, state or scope changed")
        cleanup(host, milo, commands, args, contracts, evidence,
                state, state_path, identity)
        print("Catalog operator optional stage cleanup complete; owned Namespace retained",
              flush=True)
        return
    predecessors = preflight("retained-predecessors", lambda:
        exact_predecessors(commands, host, milo, identity, args, contracts))
    preflight("resource-collisions", lambda: absence_preflight(host, milo))
    preflight("catalog-webhooks", lambda:
        baseline_webhooks(milo, predecessors["oci"]["rendered"],
                          predecessors["locationbinding"].get("webhook_uids")))
    auth = preflight("static-auth", lambda: host.json(
        "-n", CORE_NAMESPACE, "get", "secret", "milo-apiserver-auth-tokens"))
    locations_state_path = (args.locations_evidence_file.parent /
                            "apps-a1t-locations-private/state.json")
    locations_state = preflight("static-auth-chain", lambda:
                                read_journal(locations_state_path))
    preflight("static-auth-chain", lambda: require(
        auth["data"].get("tokens.csv") == locations_state.get("new_b64"),
        "Catalog operator expected exact four-row Locations auth chain"))
    preflight("static-auth-users", lambda: auth_rows(auth["data"]["tokens.csv"]))
    milo_ca = preflight("root-public-ca", lambda: host.json(
        "-n", CORE_NAMESPACE, "get", "secret", "milo-ca-tls"))
    ca_b64 = preflight("root-public-ca", lambda: milo_ca["data"]["ca.crt"])
    ca_bytes = preflight("root-public-ca", lambda:
                         base64.b64decode(ca_b64, validate=True))
    preflight("root-public-ca", lambda: require(
        hashlib.sha256(ca_bytes).hexdigest() == identity["milo"]["ca_sha256"],
        "Catalog operator Milo CA differs from verified root endpoint"))
    serving = preflight("root-serving-cert", lambda: host.json(
        "-n", CORE_NAMESPACE, "get", "secret", "milo-apiserver-tls"))
    with tempfile.TemporaryDirectory(prefix="catalog-operator-public-ca-") as temporary:
        preflight("root-serving-cert", lambda: verify_cert(
            commands, Path(temporary), ca_bytes,
            base64.b64decode(serving["data"]["tls.crt"], validate=True),
            dns="milo-apiserver.milo-system.svc"))
    preflight("private-state-collision", lambda: require(
        not state_path.exists(), "Catalog operator private state already exists"))
    print("Catalog operator preflight passed: retained source, scope and collisions",
          flush=True)
    if not args.execute:
        return
    token = secrets.token_hex(48)
    state = {"run_id": run_id, "private_path": str(state_path),
             "auth_uid": auth["metadata"]["uid"],
             "old_b64": auth["data"]["tokens.csv"],
             "new_b64": extended_auth(auth["data"]["tokens.csv"], token),
             "auth_intent": False, "token": token, "milo_ca_b64": ca_b64}
    evidence = {"version": 1, "run_id": run_id,
                "source_revision": revision,
                "catalog_source": SOURCE_REVISION,
                "catalog_oci_digest": OCI_DIGEST,
                "image_index": IMAGE_INDEX,
                "scope_identity": identity,
                "predecessor_runs": {
                    "catalog_api": predecessors["graph"]["run_id"],
                    "locations": predecessors["locations"]["run_id"],
                    "catalog_oci": predecessors["transition"]["run_id"],
                    "locationbinding": predecessors["locationbinding"]["run_id"]},
                "webhook_uids": baseline_webhooks(
                    milo, predecessors["oci"]["rendered"],
                    predecessors["locationbinding"].get("webhook_uids")),
                "attempted": {}, "intents": {}, "flux_stages": {},
                "cleanup": "not-requested", "setup_stage": "journal-written"}
    atomic_json(state_path, state)
    atomic_json(args.evidence_file, evidence)

    def stage(name: str) -> None:
        evidence["setup_stage"] = name
        atomic_json(args.evidence_file, evidence)

    try:
        stage("owned-host-namespace")
        create_owned(host, "namespace", direct_manifest("namespace", contracts),
                     evidence, args.evidence_file)
        stage("static-auth-cas")
        state["auth_intent"] = True
        atomic_json(state_path, state)
        patch_auth(host, args.evidence_file.parent, state["auth_uid"],
                   state["old_b64"], state["new_b64"])
        proof = rollout_static_auth(host)
        state["active_auth_rollout"] = proof
        atomic_json(state_path, state)
        wait_authenticated_root(milo, 45)
        stage("source-derived-root-role")
        create_owned(milo, "role", direct_manifest("role", contracts),
                     evidence, args.evidence_file)
        create_owned(milo, "binding", direct_manifest("binding", contracts),
                     evidence, args.evidence_file)
        stage("dedicated-operator-authorization-probes")
        scoped = root_user_config(args.evidence_file.parent, milo, identity,
                                  token, args.milo_port)
        try:
            probe_identity(scoped)
        finally:
            scoped.config.unlink(missing_ok=True)
        stage("pinned-task-ca-source")
        create_owned(host, "ca_source", direct_manifest("ca_source", contracts),
                     evidence, args.evidence_file)
        wait_for(lambda: source_ready(host, evidence, revision),
                 "exact Catalog test-infra CA source", 240)
        stage("source-owned-stable-ca")
        create_stage(host, "ca_stage", contracts["ca"]["stage"],
                     {"ca_stage": contracts["ca"]["flux_contract"]},
                     evidence, args.evidence_file, atomic_json)
        wait_for(lambda: stage_ready(host, "ca_stage", "flux-system", CA_STAGE,
                                     "sha1:" + revision, 3, evidence) and
                 cert_ready(host, "apps-a1t-services-ca"),
                 "Catalog stable CA and exact Flux inventory", 240)
        ca_secret = host.json("-n", NAMESPACE, "get", "secret", CA_SECRET)
        evidence["ca_secret_uid"] = ca_secret["metadata"]["uid"]
        evidence.setdefault("generated_secret_sha256", {})[CA_SECRET] = (
            secret_fingerprint(ca_secret))
        atomic_json(args.evidence_file, evidence)
        stage("scoped-host-credential")
        create_owned(host, "credential",
                     direct_manifest("credential", contracts, token, ca_b64),
                     evidence, args.evidence_file, sensitive=True)
        stage("pinned-catalog-host-operator")
        create_stage(host, "host_stage", contracts["operator"]["stage"],
                     {"host_stage": contracts["operator"]["flux_contract"]},
                     evidence, args.evidence_file, atomic_json)
        wait_for(lambda: stage_ready(host, "host_stage", "flux-system", HOST_STAGE,
                                     OCI_DIGEST, 6, evidence) and
                 cert_ready(host, "services-serving-cert"),
                 "Catalog manager and serving Certificate at exact OCI source", 240)
        serving_secret = host.json("-n", NAMESPACE, "get", "secret", SERVING_SECRET)
        evidence["serving_secret_uid"] = serving_secret["metadata"]["uid"]
        evidence.setdefault("generated_secret_sha256", {})[SERVING_SECRET] = (
            secret_fingerprint(serving_secret))
        atomic_json(args.evidence_file, evidence)
        ca = require_cert_chain(commands, args.evidence_file.parent,
                                ca_secret, serving_secret)
        evidence["public_ca_sha256"] = hashlib.sha256(ca).hexdigest()
        stage("manager-image-and-service-tls")
        evidence["manager"] = verified_pod(host, evidence)
        service_tls(commands, host, args.evidence_file.parent, ca)
        atomic_json(args.evidence_file, evidence)
        stage("milo-namespace-dns-and-tls")
        milo_network_tls(host, ca, evidence, args.evidence_file)
        stage("milo-source-owned-webhook-ca")
        patch_existing_webhooks(host, milo, args.evidence_file.parent,
                                ca, evidence, args.evidence_file, predecessors)
        stage("ready")
        evidence["complete"] = True
        atomic_json(args.evidence_file, evidence)
        print("Catalog operator Ready at exact source, scoped credential and public CA",
              flush=True)
    except Exception as error:
        evidence["setup_failure"] = {"stage": evidence.get("setup_stage", "unknown"),
                                     "class": type(error).__name__}
        atomic_json(args.evidence_file, evidence)
        commands.deadline = time.monotonic() + 300
        try:
            cleanup(host, milo, commands, args, contracts, evidence,
                    state, state_path, identity)
        except Exception:
            print("Catalog operator cleanup incomplete; private journal retained",
                  file=sys.stderr)
        raise RuntimeError("Catalog operator setup failed; inspect sanitized evidence") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--installer-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-api-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-oci-evidence-file", required=True, type=Path)
    parser.add_argument("--locations-evidence-file", required=True, type=Path)
    parser.add_argument("--locationbinding-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-source", required=True, type=Path)
    parser.add_argument("--catalog-oci-manifest", required=True, type=Path)
    parser.add_argument("--catalog-oci-layer", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=1200)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    require(not (args.execute and args.cleanup) and 1 <= args.milo_port <= 65535 and
            300 <= args.deadline_seconds <= 1800,
            "invalid Catalog operator mode, port or deadline")
    old_umask = os.umask(0o077)
    try:
        run(args)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    try:
        main()
    except PreflightFailure as error:
        print(f"Catalog operator preflight failed: {error.stage}/{error.reason}",
              file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        # Arbitrary kube errors can echo request fragments, including tokens.
        # The private journal records a fixed stage and exception class only.
        print("Catalog operator fixture failed: " + type(error).__name__,
              file=sys.stderr)
        raise SystemExit(1) from None
