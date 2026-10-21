#!/usr/bin/env python3
"""Create only the three Milo Projects needed by the isolated native trial.

All resource writes use Milo's public Kubernetes API.  The host API is read
only, to verify its exact Flux source and obtain the public CA and the
test-infra static bootstrap token.  The token remains in a mode-0600 host
kubeconfig and is never printed or installed in a Pod.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

from apps_native_fixture_scope import scope_identity, active_catalog_installer
from apps_native_support import APIProxy, Commands, Kube, RUN_LABEL, private_file, ready, require


MILO_SHA = "ba4b06e8dde99dd70e9a2f34e011d635e4408a76"
MILO_IMAGE = "sha256:3a89b1edb93eb3d4bb74808cc031897055e6130bf36aea56d978fcf252464afd"
ORG = "apps-a1t-ci-org"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
ORG_RESOURCE = "organizations.resourcemanager.miloapis.com"
PROJECT_RESOURCE = "projects.resourcemanager.miloapis.com"
PCP_RESOURCE = "projectcontrolplanes.infrastructure.miloapis.com"
ORG_PATH = "/apis/resourcemanager.miloapis.com/v1alpha1/organizations"
PROJECT_PATH = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
AUTH_SECRET = "milo-apiserver-auth-tokens"


def project_manifest(name: str, run_id: str) -> dict:
    require(name in PROJECTS, "unreviewed Project name")
    return {"apiVersion": "resourcemanager.miloapis.com/v1alpha1",
            "kind": "Project",
            "metadata": {"name": name, "labels": {RUN_LABEL: run_id}},
            "spec": {"ownerRef": {"kind": "Organization", "name": ORG}}}


def organization_manifest(run_id: str) -> dict:
    return {"apiVersion": "resourcemanager.miloapis.com/v1alpha1",
            "kind": "Organization",
            "metadata": {"name": ORG, "labels": {RUN_LABEL: run_id}},
            "spec": {"type": "Standard"}}


def exact_config(config: dict, context: str, expected_server: str) -> None:
    require(config.get("current-context") == context,
            "private kubeconfig selects an unexpected context")
    contexts = {item["name"]: item["context"] for item in config.get("contexts", [])}
    clusters = {item["name"]: item["cluster"] for item in config.get("clusters", [])}
    require(set(contexts) == {context} and len(config.get("contexts", [])) == 1,
            "private kubeconfig has additional contexts")
    require(len(clusters) == 1 and len(config.get("clusters", [])) == 1,
            "private kubeconfig has additional clusters")
    selected = clusters[contexts[context]["cluster"]]
    require(selected.get("server") == expected_server and
            not selected.get("insecure-skip-tls-verify") and
            bool(selected.get("certificate-authority-data")),
            "private kubeconfig server or verified CA differs from isolated endpoint")


def private_destination(path: Path, *, must_exist: bool) -> None:
    require(path.is_absolute() and path.parent.is_dir() and
            not path.parent.is_symlink() and
            path.parent.stat().st_uid == os.getuid() and
            path.parent.stat().st_mode & 0o077 == 0,
            "root kubeconfig parent must be an absolute mode-0700 directory")
    require(path.parent.resolve() == path.parent,
            "root kubeconfig parent must have no symlink")
    if must_exist:
        private_file(path)
    else:
        require(not path.exists() and not path.is_symlink(),
                "root kubeconfig already exists; refusing overwrite")


def verified_milo_ca(commands: Commands, private: Path, ca: bytes,
                     serving: bytes) -> None:
    require(b"BEGIN CERTIFICATE" in ca and b"BEGIN CERTIFICATE" in serving,
            "Milo public CA or server certificate absent")
    with tempfile.TemporaryDirectory(dir=private, prefix="public-cert-") as directory:
        ca_file, server_file = Path(directory) / "ca.pem", Path(directory) / "server.pem"
        ca_file.write_bytes(ca)
        server_file.write_bytes(serving)
        commands.run("openssl", "verify", "-CAfile", str(ca_file), str(server_file),
                     sensitive=True)
        commands.run("openssl", "x509", "-in", str(server_file), "-noout",
                     "-checkip", "127.0.0.1", sensitive=True)
        commands.run("openssl", "x509", "-in", str(server_file), "-noout",
                     "-checkhost", "milo-apiserver.milo-system.svc", sensitive=True)


def bootstrap_token(host: Kube) -> tuple[str, bytes, bytes]:
    auth = json.loads(host.k("-n", "milo-system", "get", "secret", AUTH_SECRET,
                             "-o", "json", sensitive=True))
    rows = list(csv.reader(io.StringIO(base64.b64decode(
        auth["data"]["tokens.csv"], validate=True).decode())))
    identities = {(row[1], row[2], row[3]) for row in rows if len(row) == 4}
    require(len(rows) == 2 and len(identities) == 2 and identities == {
        ("admin", "admin", "system:masters"),
        ("test-user", "test-user", "system:authenticated")},
        "Milo test-infra static authentication baseline changed")
    admin = next(row[0] for row in rows if row[1] == "admin")
    require(bool(admin) and all(row[0] for row in rows) and rows[0][0] != rows[1][0],
            "Milo bootstrap tokens are empty or duplicate")
    ca_secret = json.loads(host.k("-n", "milo-system", "get", "secret", "milo-ca-tls",
                                  "-o", "json", sensitive=True))
    server_secret = json.loads(host.k("-n", "milo-system", "get", "secret",
                                      "milo-apiserver-tls", "-o", "json", sensitive=True))
    return (admin,
            base64.b64decode(ca_secret["data"]["ca.crt"], validate=True),
            base64.b64decode(server_secret["data"]["tls.crt"], validate=True))


def config_document(port: int, ca: bytes, token: str) -> dict:
    server = f"https://127.0.0.1:{port}"
    return {"apiVersion": "v1", "kind": "Config", "current-context": "milo-root",
            "clusters": [{"name": "milo-root", "cluster": {
                "server": server,
                "certificate-authority-data": base64.b64encode(ca).decode()}}],
            "users": [{"name": "milo-root", "user": {"token": token}}],
            "contexts": [{"name": "milo-root", "context": {
                "cluster": "milo-root", "user": "milo-root"}}]}


def write_exclusive(path: Path, content: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(content, stream, separators=(",", ":"))
    private_file(path)


def ready_condition(obj: dict, condition: str) -> bool:
    meta = obj["metadata"]
    generation = meta.get("generation", 1)
    return bool(meta.get("uid") and not meta.get("deletionTimestamp") and any(
        item.get("type") == condition and item.get("status") == "True" and
        item.get("observedGeneration", 0) >= generation
        for item in obj.get("status", {}).get("conditions", [])))


def project_ready(milo: Kube, host: Kube, name: str, uid: str, run_id: str) -> bool:
    project = milo.optional(PROJECT_RESOURCE, name)
    require(project is not None and project["metadata"]["uid"] == uid and
            project["metadata"].get("labels", {}).get(RUN_LABEL) == run_id and
            project.get("spec", {}).get("ownerRef") == {
                "kind": "Organization", "name": ORG},
            f"owned Project/{name} was replaced or changed")
    if not ready_condition(project, "Ready"):
        return False
    # Milo's ProjectController writes ProjectControlPlanes to its separate
    # infrastructure (hosting Kubernetes) client, not the Milo root API.
    pcp = host.optional(PCP_RESOURCE, name)
    if pcp is None:
        return False
    require(pcp["metadata"].get("labels", {}).get(
                "resourcemanager.miloapis.com/project-uid") == uid and
            pcp["metadata"].get("labels", {}).get(
                "resourcemanager.miloapis.com/project-name") == name,
            f"ProjectControlPlane/{name} does not match Project UID")
    # This test overlay serves Project APIs through Milo itself. It creates
    # host-side PCP records, but does not deploy a separate infrastructure
    # reconciler to change their default ControlPlaneReady=False condition.
    # Apps' Project discovery mode gates on the real Project Ready condition.
    return True


def project_control_plane_observation(host: Kube, name: str, project_uid: str) -> dict:
    pcp = host.optional(PCP_RESOURCE, name)
    require(pcp is not None and pcp["metadata"].get("labels", {}).get(
                "resourcemanager.miloapis.com/project-uid") == project_uid and
            pcp["metadata"].get("labels", {}).get(
                "resourcemanager.miloapis.com/project-name") == name,
            f"ProjectControlPlane/{name} does not match Project UID")
    conditions = [{key: condition[key] for key in
                   ("type", "status", "reason", "observedGeneration")
                   if key in condition}
                  for condition in pcp.get("status", {}).get("conditions", [])]
    return {"uid": pcp["metadata"]["uid"], "conditions": conditions}


def wait_true(predicate, label: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {label}")


def wait_milo_ready(milo: Kube, deadline: float) -> None:
    last = "not connected"
    while time.monotonic() < deadline:
        try:
            require(milo.k("get", "--raw", "/readyz") == "ok",
                    "Milo readyz is not OK")
            return
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            last = str(error)[:120]
        time.sleep(2)
    raise RuntimeError(f"CA-verified Milo API did not become ready: {last}")


def verified_owned(milo: Kube, kind: str, name: str, uid: str | None,
                   run_id: str) -> dict | None:
    obj = milo.optional(kind, name)
    if obj is None:
        return None
    meta = obj["metadata"]
    require(meta.get("labels", {}).get(RUN_LABEL) == run_id and
            (uid is None or meta.get("uid") == uid),
            f"refusing ambiguous {kind}/{name} cleanup")
    require(meta.get("uid") and meta.get("resourceVersion"),
            f"{kind}/{name} lacks UID or resourceVersion")
    if kind == PROJECT_RESOURCE:
        require(obj.get("spec", {}).get("ownerRef") == {
                    "kind": "Organization", "name": ORG},
                f"owned Project/{name} changed owner reference")
    elif kind == ORG_RESOURCE:
        require(obj.get("spec", {}).get("type") == "Standard",
                "owned Organization changed type")
    return obj


def delete_owned(milo: Kube, proxy: APIProxy, kind: str, name: str,
                 path: str, uid: str | None, run_id: str, deadline: float) -> None:
    current = verified_owned(milo, kind, name, uid, run_id)
    if current is None:
        return
    meta = current["metadata"]
    proxy.delete(path, meta["uid"], meta["resourceVersion"])
    def gone() -> bool:
        now = milo.optional(kind, name)
        require(now is None or now["metadata"]["uid"] == meta["uid"],
                f"refusing replacement {kind}/{name} after delete")
        return now is None
    wait_true(gone, f"owned {kind}/{name} deletion", deadline)


def remove_owned(milo: Kube, config: Path, run_id: str,
                 created: dict[str, str | None], deadline: float) -> list[str]:
    errors: list[str] = []
    proxy = APIProxy(milo, config.parent, "project-bootstrap")
    try:
        proxy.start()
        for name in reversed(PROJECTS):
            if name not in created:
                continue
            try:
                delete_owned(milo, proxy, PROJECT_RESOURCE, name,
                             f"{PROJECT_PATH}/{name}", created[name], run_id, deadline)
            except Exception as error:
                errors.append(f"Project/{name}: {error}")
        if not errors and ORG in created:
            try:
                delete_owned(milo, proxy, ORG_RESOURCE, ORG,
                             f"{ORG_PATH}/{ORG}", created[ORG], run_id, deadline)
            except Exception as error:
                errors.append(f"Organization/{ORG}: {error}")
    except Exception as error:
        errors.append(f"private Milo API cleanup: {error}")
    finally:
        proxy.close()
    return errors


def attempted_from_evidence(evidence: dict) -> dict[str, str | None]:
    attempted = evidence.get("attempted", evidence.get("uids", {}))
    require(isinstance(attempted, dict) and
            set(attempted).issubset({ORG, *PROJECTS}) and
            (not attempted or ORG in attempted) and
            all(value is None or isinstance(value, str) and bool(value)
                for value in attempted.values()),
            "bootstrap attempted-resource evidence invalid")
    return attempted


def cleanup_evidenced(milo: Kube, config: Path, evidence_file: Path,
                      evidence: dict, deadline: float) -> None:
    created = attempted_from_evidence(evidence)
    errors = remove_owned(milo, config, evidence["run_id"], created,
                          deadline) if created else []
    evidence["cleanup"] = "complete" if not errors else "incomplete"
    evidence["cleanup_errors"] = errors
    write_json_atomic(evidence_file, evidence)
    require(not errors, "owned Project cleanup incomplete: " + "; ".join(errors))


def source_guard(host: Kube, *, cleanup: bool = False) -> None:
    source = host.json("get", "gitrepository", "milo", "-n", "flux-system")
    core = host.json("get", "kustomization", "milo-core", "-n", "flux-system")
    # Creation remains bound to the original bootstrap. Cleanup may also use
    # the independently reviewed guard upgrade; never accept an arbitrary pin
    # or a source/image combination from different builds.
    reviewed = {MILO_SHA: MILO_IMAGE}
    if cleanup:
        reviewed["200de48a305732720377cb83308d57c84fd5f688"] = (
            "sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c")
    revision = source.get("spec", {}).get("ref", {}).get("commit")
    require(revision in reviewed, "unreviewed Milo revision for bootstrap mode")
    image = reviewed[revision]
    require(source["spec"].get("ref") == {"commit": revision} and
            source["spec"].get("url") == "https://github.com/milo-os/milo.git" and
            source.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + revision and
            core["spec"].get("path") == "./config/overlays/test-infra" and
            core["spec"].get("sourceRef") == {"kind": "GitRepository", "name": "milo"} and
            core["spec"].get("images") == [{"name": "ghcr.io/milo-os/milo",
                                             "digest": image}] and
            core.get("status", {}).get("lastAppliedRevision", "").endswith(revision) and
            ready(source) and ready(core) and not core["spec"].get("suspend", False),
            "host Milo Flux source, overlay, or image differs from reviewed pin")


def installer_cleanup_guard(host: Kube, milo: Kube, args, evidence: dict) -> None:
    """Accept only the dedicated installer's journaled suspension during teardown."""
    path = getattr(args, "installer_evidence_file", None)
    require(args.cleanup and isinstance(path, Path) and path.is_absolute() and
            not path.is_symlink(), "explicit private installer evidence required")
    require(getattr(args, "retain_root_kubeconfig", False) is True,
            "installer cleanup requires --retain-root-kubeconfig for dependent teardown")
    identity = scope_identity(host, milo, args.host_context, args.milo_port, projects_removing=True)
    require(evidence.get("uids") == identity["bootstrap_uids"] and
            attempted_from_evidence(evidence) == identity["bootstrap_uids"],
            "installer cleanup requires the complete original bootstrap UID set")
    _, state = active_catalog_installer(host, milo, identity, path)
    source = host.json("get", "gitrepository", "milo", "-n", "flux-system")
    core = host.json("get", "kustomization", "milo-core", "-n", "flux-system")
    revision = "200de48a305732720377cb83308d57c84fd5f688"
    image = "sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c"
    original = state.get("core_spec")
    require(isinstance(original, dict) and not original.get("suspend", False) and
            state.get("core_suspend_intent") is True and state.get("auth_intent") is True and
            type(state.get("core_generation")) is int and state["core_generation"] > 0 and
            state.get("suspended_generation") == state["core_generation"] + 1 and
            core["metadata"].get("uid") == state.get("core_uid") and
            core["metadata"].get("generation") == state["suspended_generation"] and
            not core["metadata"].get("deletionTimestamp") and
            core.get("spec") == {**original, "suspend": True} and
            original.get("path") == "./config/overlays/test-infra" and
            original.get("sourceRef") == {"kind": "GitRepository", "name": "milo"} and
            original.get("images") == [{"name": "ghcr.io/milo-os/milo", "digest": image}] and
            state.get("core_applied_revision") == "sha1:" + revision and
            core.get("status", {}).get("lastAppliedRevision") == state["core_applied_revision"],
            "installer-owned Milo core identity, full spec or applied source changed")
    require(isinstance(state.get("source_uid"), str) and state["source_uid"] and
            source["metadata"].get("uid") == state["source_uid"] and
            type(state.get("source_generation")) is int and state["source_generation"] > 0 and
            source["metadata"].get("generation") == state["source_generation"] and
            not source["metadata"].get("deletionTimestamp") and
            source.get("spec") == state.get("source_spec") and
            source["spec"].get("ref") == {"commit": revision} and
            source["spec"].get("url") == "https://github.com/milo-os/milo.git" and
            source.get("status", {}).get("artifact", {}).get("revision") == "sha1:" + revision and ready(source),
            "installer-owned Milo Git source identity or full spec changed")
    # Core Ready may refer to its pre-suspension generation. Fresh root/host
    # readiness and complete source/credential checks above replace only that
    # incompatible requirement; no suspension, credential or source is mutated.


def organization_context_server(port: int) -> str:
    return f"https://127.0.0.1:{port}{ORG_PATH}/{ORG}/control-plane"


def create_recorded(milo: Kube, manifest: dict, name: str,
                    created: dict[str, str | None], evidence: dict,
                    evidence_file: Path, *, project_server: str | None = None) -> str:
    """Persist this run's intent before POST, then the returned UID immediately."""
    require(name not in created, f"duplicate bootstrap create intent for {name}")
    created[name] = None  # POST may succeed although its response is lost.
    evidence["attempted"] = created.copy()
    write_json_atomic(evidence_file, evidence)
    if project_server is None:
        require(manifest["kind"] == "Organization",
                "Project creation requires the Organization API context")
        result = milo.create(manifest)
    else:
        require(manifest["kind"] == "Project" and
                project_server.startswith("https://127.0.0.1:") and
                project_server.endswith(f"{ORG_PATH}/{ORG}/control-plane"),
                "Project creation requires the pinned Organization API path")
        result = json.loads(milo.k("--server", project_server, "create", "-f", "-",
                                  "-o", "json", input_text=json.dumps(manifest)))
    uid = result["metadata"]["uid"]
    require(isinstance(uid, str) and bool(uid),
            f"{name} create response lacks UID")
    created[name] = uid
    evidence["attempted"] = created.copy()
    evidence["uids"] = {item: value for item, value in created.items() if value}
    write_json_atomic(evidence_file, evidence)
    return uid


def run(args: argparse.Namespace) -> dict:
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    private_file(args.host_kubeconfig)
    private_destination(args.root_kubeconfig, must_exist=args.cleanup)
    require(re.fullmatch(r"kind-apps-a1t-ci-[a-z0-9-]+", args.host_context) is not None,
            "host context must be task-owned kind-apps-a1t-ci-* context")
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    host_config = json.loads(host.k("config", "view", "--raw", "-o", "json",
                                    sensitive=True))
    exact_config(host_config, args.host_context, host_config["clusters"][0]["cluster"]["server"])
    require(re.fullmatch(r"https://127\.0\.0\.1:[0-9]+",
                         host_config["clusters"][0]["cluster"]["server"]) is not None,
            "host kubeconfig must use loopback Kind API")
    require(host.k("get", "--raw", "/readyz") == "ok", "host Kind API not ready")
    installer_path = getattr(args, "installer_evidence_file", None)
    require(args.cleanup or installer_path is None, "installer evidence is cleanup-only")
    if installer_path is None:
        source_guard(host, cleanup=args.cleanup)
    evidence_file = args.root_kubeconfig.parent / "apps-a1t-project-bootstrap-evidence.json"
    if args.cleanup:
        require(not evidence_file.is_symlink(), "bootstrap evidence symlink refused")
        private_file(evidence_file)
        evidence = json.loads(evidence_file.read_text())
        require(evidence.get("host_context") == args.host_context and
                evidence.get("organization") == ORG and
                evidence.get("projects") == list(PROJECTS) and
                re.fullmatch(r"[0-9a-f]{24}", evidence.get("run_id", "")),
                "bootstrap evidence does not match requested isolated scope")
        milo = Kube(commands, args.root_kubeconfig, "milo-root")
        config = json.loads(milo.k("config", "view", "--raw", "-o", "json",
                                    sensitive=True))
        exact_config(config, "milo-root", f"https://127.0.0.1:{args.milo_port}")
        wait_milo_ready(milo, min(commands.deadline, time.monotonic() + 30))
        if installer_path is not None:
            installer_cleanup_guard(host, milo, args, evidence)
        cleanup_evidenced(milo, args.root_kubeconfig, evidence_file, evidence,
                          commands.deadline)
        if getattr(args, "retain_root_kubeconfig", False):
            evidence["root_kubeconfig_retained"] = True
            write_json_atomic(evidence_file, evidence)
        else:
            args.root_kubeconfig.unlink()
        return evidence

    require(not evidence_file.exists() and not evidence_file.is_symlink(),
            "bootstrap evidence already exists; refusing overwrite")

    token, ca, server = bootstrap_token(host)
    verified_milo_ca(commands, args.root_kubeconfig.parent, ca, server)
    config = config_document(args.milo_port, ca, token)
    write_exclusive(args.root_kubeconfig, config)
    token = ""
    milo = Kube(commands, args.root_kubeconfig, "milo-root")
    created: dict[str, str | None] = {}
    run_id = secrets.token_hex(12)
    evidence = {"host_context": args.host_context, "milo_source": MILO_SHA,
                "organization": ORG, "projects": list(PROJECTS),
                "run_id": run_id, "attempted": {}, "uids": {},
                "cleanup": "not-requested"}
    try:
        wait_milo_ready(milo, min(commands.deadline, time.monotonic() + 30))
        require(milo.optional(ORG_RESOURCE, ORG) is None,
                "target Organization already exists; refusing adoption")
        require(milo.json("get", PROJECT_RESOURCE)["items"] == [],
                "Milo already has Projects; refusing ambiguous CI scope")
        write_json_atomic(evidence_file, evidence)
        create_recorded(milo, organization_manifest(run_id), ORG,
                        created, evidence, evidence_file)
        for name in PROJECTS:
            create_recorded(milo, project_manifest(name, run_id), name,
                           created, evidence, evidence_file,
                           project_server=organization_context_server(args.milo_port))
        for name in PROJECTS:
            wait_true(lambda n=name: project_ready(milo, host, n, created[n], run_id),
                      f"Project/{name} Ready and matching host ProjectControlPlane exists",
                      commands.deadline)
        proxy = APIProxy(milo, args.root_kubeconfig.parent, "project-bootstrap-discovery")
        try:
            proxy.start()
            for name in PROJECTS:
                discovery = proxy.get(f"{PROJECT_PATH}/{name}/control-plane/api/v1",
                                      retry_seconds=60)
                require(discovery.get("kind") == "APIResourceList",
                        f"Project/{name} control-plane public API discovery unavailable")
        finally:
            proxy.close()
        observed = milo.json("get", PROJECT_RESOURCE)["items"]
        require({item["metadata"]["name"]: item["metadata"]["uid"]
                 for item in observed} == {name: created[name] for name in PROJECTS},
                "Milo Project set or UID changed after readiness")
        evidence["project_control_planes"] = {
            name: project_control_plane_observation(host, name, created[name])
            for name in PROJECTS}
        evidence["attempted"] = created.copy()
        evidence["uids"] = {name: created[name] for name in (ORG, *PROJECTS)}
        write_json_atomic(evidence_file, evidence)
        return evidence
    except Exception:
        commands.deadline = time.monotonic() + 300
        errors = remove_owned(milo, args.root_kubeconfig, run_id, created,
                              commands.deadline) if created else []
        evidence["cleanup"] = "complete" if not errors else "incomplete"
        evidence["cleanup_errors"] = errors
        evidence["attempted"] = created.copy()
        evidence["uids"] = {name: uid for name, uid in created.items() if uid}
        write_json_atomic(evidence_file, evidence)
        if not errors:
            args.root_kubeconfig.unlink()
        raise


def write_json_atomic(path: Path, document: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"))
        os.replace(temporary, path)
        private_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--root-kubeconfig", required=True, type=Path)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--projects", required=True)
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--installer-evidence-file", type=Path,
                        help="exact active Catalog installer evidence for owned suspended-core cleanup")
    parser.add_argument("--retain-root-kubeconfig", action="store_true",
                        help="retain private root config after UID-safe Project cleanup for dependent API teardown")
    args = parser.parse_args()
    require(args.cleanup or args.installer_evidence_file is None, "installer evidence is cleanup-only")
    require(args.cleanup or not args.retain_root_kubeconfig,
            "--retain-root-kubeconfig is valid only with --cleanup")
    require(args.organization == ORG and args.projects.split(",") == list(PROJECTS),
            "only the reviewed Organization and three Projects are accepted")
    require(1 <= args.milo_port <= 65535 and 120 <= args.deadline_seconds <= 900,
            "invalid Milo port or bounded deadline")
    require(not args.host_kubeconfig.is_symlink(), "host kubeconfig symlink refused")
    args.host_kubeconfig = args.host_kubeconfig.resolve()
    args.root_kubeconfig = args.root_kubeconfig.absolute()
    old_umask = os.umask(0o077)
    try:
        result = run(args)
    finally:
        os.umask(old_umask)
    if args.cleanup:
        print("Milo Project bootstrap cleanup complete: recorded owned resources absent",
              flush=True)
    else:
        print("Milo Project bootstrap passed: exact source, CA, Organization, "
              "three Ready Project UIDs, matching host PCP records, and public "
              "Project API discovery verified", flush=True)


if __name__ == "__main__":
    main()
