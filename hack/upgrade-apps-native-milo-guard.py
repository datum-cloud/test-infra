#!/usr/bin/env python3
"""Upgrade only a dedicated pre-operator Apps fixture from baseline Milo to #870.

Read-only preflight by default. --execute creates a private, noncredential
journal before the first mutation; --execute --resume recovers that exact
journal after a failed/lost response. There is no downgrade or cleanup mode.
All writes are public Kubernetes JSONPatch with UID/resourceVersion tests.

Example (paths must be private, absolute, nonsymlink files/directories):
  python3 hack/upgrade-apps-native-milo-guard.py \
    --host-kubeconfig /private/run/host.kubeconfig \
    --host-context kind-apps-a1t-ci-example \
    --milo-kubeconfig /private/run/milo-root.kubeconfig \
    --bootstrap-evidence /private/run/apps-a1t-project-bootstrap-evidence.json \
    --journal /private/run/milo-guard-upgrade.json --upgrade-port 19443 --execute

The runner owns the existing reconnecting Milo Service port-forward on the
separate --upgrade-port. It never depends on the bootstrap forward surviving a
Pod restart, never uses Envoy, and never changes an original kubeconfig. Keep
all other fixture stages stopped for this whole operation. Scope checks cannot
atomically exclude an independent operator; exclusive fixture ownership is a
required caller contract. A failed check retains the journal and current Flux
state; inspect and resume only after correcting the external failure. Status RV
churn is allowed; changes to either controlled spec or object UID are refused.
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time

import yaml

from apps_native_support import Commands, Kube, RUN_LABEL, ready, require

PHASE = "arguments"
JOURNAL_PATH = None

ROOT = Path(__file__).resolve().parent.parent
BASE_SHA = "ba4b06e8dde99dd70e9a2f34e011d635e4408a76"
BASE_DIGEST = "sha256:3a89b1edb93eb3d4bb74808cc031897055e6130bf36aea56d978fcf252464afd"
GUARD_SHA = "200de48a305732720377cb83308d57c84fd5f688"
GUARD_DIGEST = "sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c"
GUARD_CHILDREN = {"amd64": "sha256:f85dfc9d5c96e9730719d904a53d94a2d616c6dac38af173fbeb865434900a15",
                  "arm64": "sha256:e69a2d2a5223f074555f51247d43b1ebe51395dbd20764083ef0af1b11393e04"}
IMAGE = "ghcr.io/milo-os/milo"
ORG = "apps-a1t-ci-org"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
ROWS = {"source": ("gitrepository", "milo"), "core": ("kustomization", "milo-core")}
STEPS = ("suspend-core", "pin-source", "pin-image", "resume-core")
BLOCKED_SUFFIXES = (".apps.miloapis.com", ".locations.miloapis.com", ".services.miloapis.com",
                    ".billing.miloapis.com", ".compute.datumapis.com", ".networking.datumapis.com")
BLOCKED_NAMESPACES = {"apps-system", "locations-system", "services-system", "compute-system",
                      "network-services-system", "ipam-system"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_path(path: Path, exists: bool = True) -> None:
    require(path.is_absolute() and path.parent.resolve() == path.parent and
            path.parent.is_dir() and path.parent.stat().st_uid == os.getuid() and
            path.parent.stat().st_mode & 0o077 == 0 and not path.is_symlink(),
            "owned private nonsymlink directory required")
    if exists:
        require(path.is_file() and path.stat().st_uid == os.getuid() and
                path.stat().st_mode & 0o077 == 0, "owned private regular file required")
    else:
        require(not path.exists(), "refusing existing output file")


def save(path: Path, document: dict, exclusive: bool = False) -> None:
    private_path(path, exists=not exclusive)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        temporary = None
    else:
        fd, name = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
        temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def exact_config(document: dict, context: str, root: bool) -> dict:
    require(document.get("current-context") == context and
            len(document.get("contexts", [])) == len(document.get("clusters", [])) ==
            len(document.get("users", [])) == 1, "single explicit kubeconfig context required")
    entry = document["contexts"][0]
    cluster, user = document["clusters"][0], document["users"][0]
    require(entry.get("name") == context and entry.get("context", {}).get("cluster") == cluster.get("name") and
            entry["context"].get("user") == user.get("name"), "kubeconfig references differ")
    config, credential = cluster["cluster"], user["user"]
    match = re.fullmatch(r"https://127\.0\.0\.1:([0-9]+)", config.get("server", ""))
    require(match is not None and 1 <= int(match[1]) <= 65535 and
            bool(config.get("certificate-authority-data")) and
            set(config).issubset({"server", "certificate-authority-data", "insecure-skip-tls-verify"}) and
            not config.get("insecure-skip-tls-verify"), "verified exact loopback TLS config required")
    allowed = {"token"} if root else {"client-certificate-data", "client-key-data"}
    require(set(credential) == allowed and all(credential.values()),
            "unexpected credentials, execution plugin or impersonation configuration")
    base64.b64decode(config["certificate-authority-data"], validate=True)
    return config


def verify_bootstrap(bootstrap: dict, context: str) -> None:
    require(re.fullmatch(r"kind-apps-a1t-ci-[a-z0-9-]+", context) is not None and
            bootstrap.get("host_context") == context and bootstrap.get("milo_source") == BASE_SHA and
            bootstrap.get("organization") == ORG and bootstrap.get("projects") == list(PROJECTS) and
            bootstrap.get("cleanup") == "not-requested" and
            re.fullmatch(r"[0-9a-f]{24}", bootstrap.get("run_id", "")) is not None and
            set(bootstrap.get("uids", {})) == {ORG, *PROJECTS} and
            all(isinstance(uid, str) and uid for uid in bootstrap["uids"].values()),
            "exact completed baseline Project bootstrap evidence required")


def stage_files(parent: Path, bootstrap: Path, journal: Path) -> None:
    for path in parent.glob("*.json"):
        if path in (bootstrap, journal):
            continue
        require(not any(word in path.name.lower() for word in
                        ("apps", "location", "catalog", "compute", "ipam", "stage", "state", "evidence")),
                "other stage evidence exists; use pre-operator fixture only")


def verify_auth(secret: dict, config: dict) -> None:
    rows = list(csv.reader(io.StringIO(base64.b64decode(secret["data"]["tokens.csv"], validate=True).decode())))
    require(len(rows) == 2 and all(len(row) == 4 and row[0] for row in rows) and
            len({row[0] for row in rows}) == 2 and
            {(row[1], row[2], row[3]) for row in rows} == {
                ("admin", "admin", "system:masters"), ("test-user", "test-user", "system:authenticated")},
            "exact two-user static authentication baseline required")
    require(config["users"][0]["user"]["token"] == next(row[0] for row in rows if row[1] == "admin"),
            "root credential is not the unchanged baseline administrator")


def fixture(host: Kube, milo: Kube, bootstrap: dict, root_config: dict) -> dict:
    # No credential material leaves this function or is recorded in the journal.
    auth = json.loads(host.k("-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens", "-o", "json", sensitive=True))
    verify_auth(auth, root_config)
    ca = json.loads(host.k("-n", "milo-system", "get", "secret", "milo-ca-tls", "-o", "json", sensitive=True))
    public_ca = base64.b64decode(ca["data"]["ca.crt"], validate=True)
    require(public_ca == base64.b64decode(root_config["clusters"][0]["cluster"]["certificate-authority-data"], validate=True),
            "root CA differs from dedicated Milo CA")
    identity = milo.json("auth", "whoami").get("status", {}).get("userInfo", {})
    require(identity.get("username") == identity.get("uid") == "admin" and
            "system:masters" in identity.get("groups", []), "unexpected effective root identity")
    organizations = milo.json("get", "organizations.resourcemanager.miloapis.com")["items"]
    organization = next((obj for obj in organizations if obj["metadata"]["name"] == ORG), None)
    require(organization is not None and organization["metadata"].get("uid") == bootstrap["uids"][ORG] and
            organization["metadata"].get("labels", {}).get(RUN_LABEL) == bootstrap["run_id"] and
            not organization["metadata"].get("deletionTimestamp") and organization.get("spec", {}).get("type") == "Standard",
            "bootstrap Organization changed")
    projects = milo.json("get", "projects.resourcemanager.miloapis.com")["items"]
    require({obj["metadata"]["name"]: obj["metadata"].get("uid") for obj in projects} ==
            {name: bootstrap["uids"][name] for name in PROJECTS}, "Project inventory or UID changed")
    for obj in projects:
        require(obj["metadata"].get("labels", {}).get(RUN_LABEL) == bootstrap["run_id"] and
                not obj["metadata"].get("deletionTimestamp") and obj.get("spec", {}).get("ownerRef") ==
                {"kind": "Organization", "name": ORG}, "Project owner or bootstrap run changed")
        require(any(condition.get("type") == "Ready" and condition.get("status") == "True" and
                    condition.get("observedGeneration", 0) >= obj["metadata"].get("generation", 1)
                    for condition in obj.get("status", {}).get("conditions", [])), "Project is not Ready")
    for kube in (host, milo):
        require(not any(obj["metadata"]["name"].endswith(BLOCKED_SUFFIXES)
                        for obj in kube.json("get", "crd")["items"]), "native stage CRDs already exist")
    require(not BLOCKED_NAMESPACES.intersection(obj["metadata"]["name"] for obj in host.json("get", "namespaces")["items"]),
            "operator or Apps namespace already exists")
    for obj in host.json("get", "deployments", "-A")["items"]:
        require(not any(word in obj["metadata"]["name"].lower() for word in ("apps-controller", "locations", "service-catalog")) and
                not any(c.get("image", "").startswith("ghcr.io/datum-labs/apps") for c in obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])),
                "Apps or native operator deployment already exists")
    for resource in ("gitrepositories.source.toolkit.fluxcd.io", "ocirepositories.source.toolkit.fluxcd.io", "kustomizations.kustomize.toolkit.fluxcd.io"):
        for obj in host.json("get", resource, "-A")["items"]:
            require(not any(word in obj["metadata"]["name"].lower() for word in ("apps", "location", "service-catalog", "catalog-billing", "compute", "network-services")),
                    "another native stage Flux object exists")
    service = host.json("-n", "milo-system", "get", "service", "milo-apiserver")
    verify_service(service)
    deployment = host.json("-n", "milo-system", "get", "deployment", "milo-apiserver")
    cluster = host.json("get", "namespace", "kube-system")
    return {"host_cluster_uid": cluster["metadata"]["uid"], "milo_service_uid": service["metadata"]["uid"],
            "apiserver_deployment_uid": deployment["metadata"]["uid"],
            "milo_ca_sha256": hashlib.sha256(public_ca).hexdigest(), "bootstrap_sha256": digest(bootstrap)}


def verify_service(service: dict) -> None:
    # Milo200de48 config/apiserver/service.yaml:8-15;
    # config/overlays/test-infra/kustomization.yaml:4 supplies namespace.
    require(service["metadata"].get("uid") and not service["metadata"].get("deletionTimestamp") and
            service["spec"].get("type") == "ClusterIP" and
            service["spec"].get("selector") == {"app.kubernetes.io/name": "milo-apiserver"} and
            service["spec"].get("ports") == [{"name": "https", "port": 6443, "protocol": "TCP", "targetPort": "https"}],
            "unexpected Milo forwarding Service")


def baseline_deployment(host: Kube) -> None:
    deployment = host.json("-n", "milo-system", "get", "deployment", "milo-apiserver")
    spec, status = deployment["spec"], deployment.get("status", {})
    replicas = spec.get("replicas", 1)
    containers = [c for c in spec["template"]["spec"]["containers"] if c["name"] == "milo-apiserver"]
    require(replicas >= 1 and not deployment["metadata"].get("deletionTimestamp") and
            status.get("observedGeneration", 0) >= deployment["metadata"]["generation"] and
            all(status.get(k, 0) == replicas for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")) and
            len(containers) == 1 and containers[0].get("image") == IMAGE + "@" + BASE_DIGEST,
            "baseline apiserver rollout/image differs")


def objects(host: Kube) -> dict:
    return {key: host.json("-n", "flux-system", "get", *row) for key, row in ROWS.items()}


def stable_spec(key: str, spec: dict) -> str:
    value = copy.deepcopy(spec)
    for field in (("ref",) if key == "source" else ("images", "suspend")):
        value.pop(field, None)
    return digest(value)


def baseline(current: dict) -> None:
    for key, filename in (("source", "source.yaml"), ("core", "core.yaml")):
        expected = yaml.safe_load((ROOT / "components/milo-core" / filename).read_text())["spec"]
        actual = current[key]["spec"]
        defaults = {"provider": "generic", "timeout": "60s"} if key == "source" else {"force": False, "wait": False, "suspend": False, "deletionPolicy": "MirrorPrune"}
        require(all(actual.get(k) == v for k, v in expected.items()) and
                all(k in expected or k in defaults and v == defaults[k] for k, v in actual.items()),
                "baseline Flux spec differs from reviewed source-owned manifest")
        require(ready(current[key]) and not actual.get("suspend", False), "baseline Flux object is not Ready/active")
    require(current["source"]["spec"]["url"] == "https://github.com/milo-os/milo.git" and
            current["source"]["spec"]["ref"] == {"commit": BASE_SHA} and
            current["source"].get("status", {}).get("artifact", {}).get("revision") == "sha1:" + BASE_SHA and
            current["core"]["spec"]["images"] == [{"name": IMAGE, "digest": BASE_DIGEST}] and
            current["core"].get("status", {}).get("lastAppliedRevision") in ("sha1:" + BASE_SHA, "main@sha1:" + BASE_SHA),
            "baseline applied source/image differs")


def initial(current: dict, binding: dict, context: str) -> dict:
    baseline(current)
    return {"version": 1, "host_context": context, "binding": binding,
            "base_sha": BASE_SHA, "target_sha": GUARD_SHA, "target_digest": GUARD_DIGEST,
            "completed_steps": 0, "pending": None, "history": [], "complete": False,
            "objects": {key: {"uid": obj["metadata"]["uid"], "initial_rv": obj["metadata"]["resourceVersion"],
                               "stable_spec_sha256": stable_spec(key, obj["spec"])} for key, obj in current.items()},
            "original_suspend": {"present": "suspend" in current["core"]["spec"], "value": current["core"]["spec"].get("suspend", False)}}


def matches(current: dict, state: dict, step: int) -> bool:
    for key, obj in current.items():
        record = state["objects"][key]
        if (obj["metadata"].get("uid") != record["uid"] or obj["metadata"].get("deletionTimestamp") or
                stable_spec(key, obj["spec"]) != record["stable_spec_sha256"]):
            return False
    source, core = current["source"]["spec"], current["core"]["spec"]
    suspension = {"present": "suspend" in core, "value": core.get("suspend", False)}
    return (source.get("ref") == {"commit": GUARD_SHA if step >= 2 else BASE_SHA} and
            core.get("images") == [{"name": IMAGE, "digest": GUARD_DIGEST if step >= 3 else BASE_DIGEST}] and
            suspension == ({"present": True, "value": True} if 1 <= step <= 3 else state["original_suspend"]))


def advance(host: Kube, private: Path, path: Path, state: dict, check_scope) -> None:
    """Resume only exact before/after progress; never rebase onto foreign changes."""
    global PHASE
    while state["completed_steps"] < 4:
        PHASE = STEPS[state["completed_steps"]]
        current = objects(host)
        step = state["completed_steps"]
        if state["pending"] is not None:
            require(state["pending"]["step"] == step + 1, "invalid pending upgrade step")
            if matches(current, state, step + 1):
                state["history"].append({**state["pending"], "recovered": True})
                state["completed_steps"] += 1
                state["pending"] = None
                save(path, state)
                continue
        require(matches(current, state, step), "Flux spec or UID changed outside recorded upgrade")
        require(check_scope() == state["binding"], "dedicated fixture binding changed")
        key = "source" if step == 1 else "core"
        obj = current[key]
        operations = [{"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
                      {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]}]
        if step in (0, 3):
            if step == 3 and not state["original_suspend"]["present"]:
                operations.append({"op": "remove", "path": "/spec/suspend"})
            else:
                operations.append({"op": "add", "path": "/spec/suspend", "value": step == 0})
        elif step == 1:
            operations.append({"op": "replace", "path": "/spec/ref", "value": {"commit": GUARD_SHA}})
        else:
            operations.append({"op": "replace", "path": "/spec/images", "value": [{"name": IMAGE, "digest": GUARD_DIGEST}]})
        state["pending"] = {"step": step + 1, "operation": STEPS[step], "uid": obj["metadata"]["uid"],
                            "requested_rv": obj["metadata"]["resourceVersion"]}
        save(path, state)  # fsync before the API request; lost reply is recoverable.
        host.patch_json(private, *ROWS[key], operations, namespace="flux-system", sensitive=True)
        observed = objects(host)
        require(matches(observed, state, step + 1), "post-patch Flux state differs; retain journal")
        state["history"].append({**state["pending"], "observed_rv": observed[key]["metadata"]["resourceVersion"]})
        state["completed_steps"] += 1
        state["pending"] = None
        save(path, state)


def controller_owner(obj: dict, kind: str, uid: str) -> bool:
    owners = [owner for owner in obj.get("metadata", {}).get("ownerReferences", []) if owner.get("controller")]
    return len(owners) == 1 and owners[0].get("apiVersion") == "apps/v1" and owners[0].get("kind") == kind and owners[0].get("uid") == uid


def rollout(host: Kube, state: dict) -> list[str] | None:
    current = objects(host)
    require(matches(current, state, 4), "Flux changed while waiting for rollout")
    if not all(ready(obj) for obj in current.values()):
        return None
    if current["source"].get("status", {}).get("artifact", {}).get("revision") != "sha1:" + GUARD_SHA or current["core"].get("status", {}).get("lastAppliedRevision") not in ("sha1:" + GUARD_SHA, "main@sha1:" + GUARD_SHA):
        return None
    deployment = host.json("-n", "milo-system", "get", "deployment", "milo-apiserver")
    require(deployment["metadata"].get("uid") == state["binding"]["apiserver_deployment_uid"] and
            not deployment["metadata"].get("deletionTimestamp"), "apiserver Deployment replaced")
    spec, status = deployment["spec"], deployment.get("status", {})
    replicas = spec.get("replicas", 1)
    if replicas < 1 or status.get("observedGeneration", 0) < deployment["metadata"]["generation"] or any(status.get(k, 0) != replicas for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")):
        return None
    containers = [c for c in spec["template"]["spec"]["containers"] if c["name"] == "milo-apiserver"]
    if len(containers) != 1 or containers[0].get("image") != IMAGE + "@" + GUARD_DIGEST:
        return None
    labels = spec.get("selector", {}).get("matchLabels", {})
    require(labels.get("app.kubernetes.io/name") == "milo-apiserver", "unexpected apiserver selector")
    pods = host.json("-n", "milo-system", "get", "pods", "-l", ",".join(k + "=" + v for k, v in sorted(labels.items())))["items"]
    if len(pods) != replicas or any(p["metadata"].get("deletionTimestamp") for p in pods):
        return None
    sets = host.json("-n", "milo-system", "get", "replicasets")["items"]
    owned_sets = {rs["metadata"]["uid"] for rs in sets if not rs["metadata"].get("deletionTimestamp") and
                  controller_owner(rs, "Deployment", deployment["metadata"]["uid"])}
    require(all(any(controller_owner(pod, "ReplicaSet", uid) for uid in owned_sets) for pod in pods),
            "apiserver Pod is not owned by the recorded Deployment")
    nodes = {n["metadata"]["name"]: n["status"]["nodeInfo"]["architecture"] for n in host.json("get", "nodes")["items"]}
    ids = []
    for pod in pods:
        arch = nodes.get(pod.get("spec", {}).get("nodeName"))
        require(arch in GUARD_CHILDREN, "unknown apiserver node architecture")
        entries = [c for c in pod.get("status", {}).get("containerStatuses", []) if c.get("name") == "milo-apiserver"]
        if len(entries) != 1 or not entries[0].get("ready"):
            return None
        image_id = entries[0].get("imageID", "")
        if image_id.split("@")[-1] not in (GUARD_DIGEST, GUARD_CHILDREN[arch]) and image_id not in ("docker-pullable://" + IMAGE + "@" + GUARD_CHILDREN[arch],):
            return None
        ids.append(image_id)
    return sorted(ids)


def wait_read(call, deadline: float, process=None):
    while time.monotonic() < deadline:
        require(process is None or process.poll() is None, "reconnecting forward stopped")
        try:
            value = call()
            if value:
                return value
        except Exception:
            pass  # bounded read-only recovery; no repeated mutations here
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError("bounded API/rollout readiness deadline exceeded")


def main() -> None:
    global PHASE, JOURNAL_PATH
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("host-kubeconfig", "milo-kubeconfig", "bootstrap-evidence", "journal"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--upgrade-port", type=int, required=True, help="free dedicated loopback port for runner-owned reconnecting Milo Service forward")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    JOURNAL_PATH = args.journal
    PHASE = "private-path-and-option-validation"
    require(120 <= args.deadline_seconds <= 1800 and 1024 <= args.upgrade_port <= 65535,
            "invalid bounded deadline or upgrade port")
    require(not args.resume or args.execute, "resume requires execute")
    for path in (args.host_kubeconfig, args.milo_kubeconfig, args.bootstrap_evidence):
        private_path(path)
    require(args.journal.parent == args.milo_kubeconfig.parent == args.bootstrap_evidence.parent,
            "journal and bootstrap root credentials must share private directory")
    private_path(args.journal, exists=args.resume)
    stage_files(args.journal.parent, args.bootstrap_evidence, args.journal)
    PHASE = "kubeconfig-validation"
    host_config = yaml.safe_load(args.host_kubeconfig.read_text())
    root_config = yaml.safe_load(args.milo_kubeconfig.read_text())
    exact_config(host_config, args.host_context, False)
    selected = exact_config(root_config, "milo-root", True)
    require(selected["server"] != f"https://127.0.0.1:{args.upgrade_port}", "upgrade port must differ from bootstrap port")
    PHASE = "bootstrap-evidence-validation"
    bootstrap = json.loads(args.bootstrap_evidence.read_text())
    verify_bootstrap(bootstrap, args.host_context)
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    # No attempt to attach to somebody else's listener.
    PHASE = "upgrade-port-and-journal-lock"
    with socket.socket() as available:
        available.bind(("127.0.0.1", args.upgrade_port))
    lock_path = args.journal.with_suffix(args.journal.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    process = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix="milo-guard-upgrade-", dir=args.journal.parent) as directory:
            private = Path(directory)
            ephemeral = copy.deepcopy(root_config)
            ephemeral["clusters"][0]["cluster"]["server"] = f"https://127.0.0.1:{args.upgrade_port}"
            config = private / "milo-root.kubeconfig"
            save(config, ephemeral, exclusive=True)
            PHASE = "reconnecting-forward-readiness"
            process = subprocess.Popen([sys.executable, str(ROOT / "hack/forward-milo-reconnecting.py"),
                "--host-kubeconfig", str(args.host_kubeconfig), "--host-context", args.host_context,
                "--local-port", str(args.upgrade_port), "--deadline-seconds", str(args.deadline_seconds),
                "--log-file", str(private / "forward.log")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            milo = Kube(commands, config, "milo-root")
            wait_read(lambda: milo.k("get", "--raw", "/readyz") == "ok", commands.deadline, process)
            def scope():
                stage_files(args.journal.parent, args.bootstrap_evidence, args.journal)
                return fixture(host, milo, bootstrap, root_config)
            PHASE = "fixture-identity-and-stage-census"
            binding = scope()
            if args.resume:
                PHASE = "resume-journal-validation"
                state = json.loads(args.journal.read_text())
                require(state.get("version") == 1 and state.get("host_context") == args.host_context and
                        state.get("base_sha") == BASE_SHA and state.get("target_sha") == GUARD_SHA and
                        state.get("target_digest") == GUARD_DIGEST and state.get("binding") == binding and
                        isinstance(state.get("completed_steps"), int) and 0 <= state["completed_steps"] <= 4 and
                        state.get("original_suspend") in ({"present": False, "value": False}, {"present": True, "value": False}),
                        "journal differs from exact upgrade/fixture contract")
            else:
                PHASE = "baseline-source-and-rollout-validation"
                baseline_deployment(host)
                state = initial(objects(host), binding, args.host_context)
                if not args.execute:
                    print("Read-only guard upgrade preflight passed; no API mutations")
                    return
                save(args.journal, state, exclusive=True)
            advance(host, private, args.journal, state, scope)
            PHASE = "guard-rollout-verification"
            ids = wait_read(lambda: rollout(host, state), commands.deadline, process)
            PHASE = "final-fixture-identity-and-stage-census"
            require(scope() == binding, "fixture changed after rollout")
            state.update({"complete": True, "apiserver_image_ids": ids})
            save(args.journal, state)
            print("Milo #870 source/image upgrade verified; run guard API probe separately")
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # API or kubectl exceptions can include credential material. Do not log
        # them. The already-persisted journal contains safe operation progress.
        journal_exists = JOURNAL_PATH is not None and JOURNAL_PATH.is_file()
        print(f"Milo upgrade stopped in phase {PHASE}; journal={'present (retain for inspection/resume)' if journal_exists else 'not created'}. No downgrade attempted.", file=sys.stderr)
        raise SystemExit(1)
