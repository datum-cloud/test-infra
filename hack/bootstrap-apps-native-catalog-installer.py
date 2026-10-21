#!/usr/bin/env python3
"""Retain a narrow CA-verified Milo installer only while Catalog Flux is active.

The User is added by CAS to the isolated test API's static-token fixture.
Its token exists only in a private recovery journal and one task-owned host
Secret. The existing broad, insecure source Secret is never copied or used.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

import yaml

from apps_native_support import APIProxy, Commands, Kube, RUN_LABEL, private_file, ready, require, wait_for
from apps_native_fixture_scope import (catalog_installer_intent_digest,
    rollout_static_auth, scope_identity, require_same_identity,
    verified_flux_remote_secret, wait_authenticated_root)


ROOT = Path(__file__).resolve().parent.parent
MILO_SHA = "200de48a305732720377cb83308d57c84fd5f688"
MILO_IMAGE = "sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c"
USER = "apps-a1t-catalog-installer"
SECRET = "apps-a1t-catalog-installer-kubeconfig"
ROWS = (
    ("role", "clusterrole", USER, None,
     "/apis/rbac.authorization.k8s.io/v1/clusterroles/" + USER),
    ("binding", "clusterrolebinding", USER, None,
     "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/" + USER),
    ("secret", "secret", SECRET, "milo-system",
     "/api/v1/namespaces/milo-system/secrets/" + SECRET),
)
GRAPH = (("kustomization", "service-catalog-api-crds", "milo-system"),
         ("kustomization", "service-catalog-billing-crds", "milo-system"),
         ("gitrepository", "service-catalog", "flux-system"),
         ("ocirepository", "catalog-billing", "flux-system"))
TARGET_SUFFIXES = (".services.miloapis.com", ".locations.miloapis.com",
                   ".billing.miloapis.com")
EXACT_CRDS = {
    "locationclasses.locations.miloapis.com", "locations.locations.miloapis.com",
    "servinglocations.locations.miloapis.com",
    "serviceactivationrequests.services.miloapis.com",
    "serviceagentconfigurations.services.miloapis.com",
    "serviceagents.services.miloapis.com", "serviceavailabilities.services.miloapis.com",
    "serviceconfigurations.services.miloapis.com",
    "serviceconsumers.services.miloapis.com",
    "serviceentitlements.services.miloapis.com", "services.services.miloapis.com",
    "billingaccountbindings.billing.miloapis.com",
    "billingaccounts.billing.miloapis.com",
    "billingarrangements.billing.miloapis.com",
    "billingentitlements.billing.miloapis.com", "invoices.billing.miloapis.com",
    "meterdefinitions.billing.miloapis.com",
    "monitoredresourcetypes.billing.miloapis.com", "offers.billing.miloapis.com",
    "paymentmethodclasses.billing.miloapis.com",
    "paymentmethods.billing.miloapis.com", "servicepricings.billing.miloapis.com",
}
WEBHOOKS = (("mutatingwebhookconfiguration", "services-mutating"),
            ("validatingwebhookconfiguration", "services-validating"))


def atomic_json(path: Path, document: dict) -> None:
    fd, pending = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        private_file(path)
    finally:
        Path(pending).unlink(missing_ok=True)


def exact_role_manifests() -> list[dict]:
    path = ROOT / "components/service-catalog/installer-rbac.yaml"
    manifests = list(yaml.safe_load_all(path.read_text()))
    require(len(manifests) == 2 and
            [(m["kind"], m["metadata"]["name"]) for m in manifests] ==
            [("ClusterRole", USER), ("ClusterRoleBinding", USER)],
            "Catalog installer RBAC identity differs from reviewed manifest")
    role, binding = manifests
    require(role["apiVersion"] == binding["apiVersion"] == "rbac.authorization.k8s.io/v1" and
            not role["metadata"].get("namespace") and
            not binding["metadata"].get("namespace") and
            binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                   "kind": "ClusterRole", "name": USER} and
            binding["subjects"] == [{"kind": "User", "name": USER,
                                     "apiGroup": "rbac.authorization.k8s.io"}],
            "Catalog installer RBAC binding is broader than reviewed User")
    rules = role["rules"]
    require(len(rules) == 5 and
            rules[0] == {"apiGroups": ["apiextensions.k8s.io"],
                         "resources": ["customresourcedefinitions"],
                         "verbs": ["create", "list", "watch"]} and
            rules[2] == {"apiGroups": ["admissionregistration.k8s.io"],
                         "resources": ["mutatingwebhookconfigurations",
                                       "validatingwebhookconfigurations"],
                         "verbs": ["create", "list", "watch"]} and
            rules[3] == {"apiGroups": ["admissionregistration.k8s.io"],
                         "resources": ["mutatingwebhookconfigurations"],
                         "resourceNames": ["services-mutating"],
                         "verbs": ["get", "patch", "update", "delete"]} and
            rules[4] == {"apiGroups": ["admissionregistration.k8s.io"],
                         "resources": ["validatingwebhookconfigurations"],
                         "resourceNames": ["services-validating"],
                         "verbs": ["get", "patch", "update", "delete"]} and
            rules[1]["apiGroups"] == ["apiextensions.k8s.io"] and
            rules[1]["resources"] == ["customresourcedefinitions"] and
            rules[1]["verbs"] == ["get", "patch", "update", "delete"] and
            len(rules[1]["resourceNames"]) == 22 and
            set(rules[1]["resourceNames"]) == EXACT_CRDS,
            "Catalog installer role is not exact source-owned CRD/WC scope")
    return manifests


def intent_digest(obj: dict) -> str:
    return catalog_installer_intent_digest(obj)


def create_owned(kube: Kube, row: tuple, manifest: dict, evidence: dict,
                 path: Path, *, sensitive: bool = False) -> None:
    key = row[0]
    manifest.setdefault("metadata", {}).setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    evidence["attempted"][key] = None
    evidence["intents"][key] = intent_digest(manifest)
    atomic_json(path, evidence)
    result = kube.create(manifest, row[3], sensitive=sensitive)
    uid = result.get("metadata", {}).get("uid")
    require(isinstance(uid, str) and uid, f"{key} create response lacks UID")
    evidence["attempted"][key] = uid
    atomic_json(path, evidence)


def create_root_role_after_reconnect(milo: Kube, role: dict, evidence: dict,
                                     path: Path, seconds: float = 45,
                                     mark_stage=None) -> None:
    """Gate the first root write after a Milo auth rollout on a read-only GET."""
    wait_authenticated_root(milo, seconds)
    if mark_stage is not None:
        mark_stage("root-role-create")
    create_owned(milo, ROWS[0], role, evidence, path)


def delete_owned(kube: Kube, proxy: APIProxy, row: tuple, evidence: dict,
                 path: Path) -> None:
    key = row[0]
    if key not in evidence["attempted"]:
        return
    obj = kube.optional(row[1], row[2], row[3])
    if obj is None:
        return
    meta = obj["metadata"]
    uid = evidence["attempted"][key]
    if uid:
        require(meta.get("uid") == uid, f"Catalog installer {key} was replaced")
    else:
        require(meta.get("labels", {}).get(RUN_LABEL) == evidence["run_id"] and
                intent_digest(obj) == evidence["intents"].get(key),
                f"Catalog installer unknown-UID {key} differs from create intent")
        evidence["attempted"][key] = meta["uid"]
        atomic_json(path, evidence)
    proxy.delete(row[4], meta["uid"], meta["resourceVersion"])
    def absent() -> bool:
        current = kube.optional(row[1], row[2], row[3])
        require(current is None or current["metadata"]["uid"] == meta["uid"],
                f"Catalog installer {key} replaced during cleanup")
        return current is None
    wait_for(absent, f"Catalog installer {key} absent", 120)


def auth_csv(data: str) -> str:
    try:
        decoded = base64.b64decode(data, validate=True).decode()
        rows = list(csv.reader(io.StringIO(decoded)))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("Milo static auth CSV malformed") from error
    require(len(rows) == 2 and all(len(row) == 4 and row[0] for row in rows) and
            {(row[1], row[2], row[3]) for row in rows} ==
            {("admin", "admin", "system:masters"),
             ("test-user", "test-user", "system:authenticated")},
            "Milo static auth is not exact two-user test baseline")
    return decoded


def patch_auth(host: Kube, private: Path, uid: str, old: str, new: str) -> None:
    host.patch_json(private, "secret", "milo-apiserver-auth-tokens", [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/data/tokens.csv", "value": old},
        {"op": "replace", "path": "/data/tokens.csv", "value": new},
    ], namespace="milo-system", sensitive=True)


def suspend_core(host: Kube, private: Path, state: dict) -> None:
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    meta = core["metadata"]
    require(meta["uid"] == state["core_uid"] and
            meta["generation"] == state["core_generation"] and
            not core["spec"].get("suspend", False), "milo-core changed before suspension")
    result = host.patch_json(private, "kustomization", "milo-core", [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/metadata/generation", "value": meta["generation"]},
        {"op": "add", "path": "/spec/suspend", "value": True},
    ], namespace="flux-system")
    state["suspended_generation"] = result["metadata"]["generation"]


def resume_core(host: Kube, private: Path, state: dict) -> None:
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    meta = core["metadata"]
    require(meta["uid"] == state["core_uid"], "milo-core UID changed")
    original = state["core_spec"]
    suspended = state.get("suspended_generation", state["core_generation"] + 1)
    if not core["spec"].get("suspend", False):
        # The restore PATCH may have succeeded while its response or the
        # subsequent reconcile failed. Accept only the original exact spec at
        # the original or the single expected post-restore generation.
        require(core["spec"] == original and
                meta["generation"] in (state["core_generation"], suspended + 1),
                "milo-core resume outcome differs from exact original spec/generation")
        state["resumed_generation"] = meta["generation"]
        return
    require(meta["generation"] == suspended and
            core["spec"] == {**original, "suspend": True},
            "milo-core changed while suspended")
    remove = not state["core_suspend_present"]
    actions = [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/metadata/generation", "value": suspended},
        {"op": "test", "path": "/spec/suspend", "value": True},
        {"op": "remove" if remove else "replace", "path": "/spec/suspend",
         **({} if remove else {"value": False})},
    ]
    result = host.patch_json(private, "kustomization", "milo-core", actions,
                             namespace="flux-system")
    require(result["metadata"]["uid"] == state["core_uid"] and
            result["metadata"]["generation"] == suspended + 1 and
            result["spec"] == original,
            "milo-core restore response differs from exact original spec/generation")
    state["resumed_generation"] = result["metadata"]["generation"]


def rollout_milo(host: Kube) -> dict:
    return rollout_static_auth(host)


def kubeconfig(ca: str, token: str) -> dict:
    return {"apiVersion": "v1", "kind": "Config", "current-context": "catalog-installer",
            "clusters": [{"name": "milo", "cluster": {
                "server": "https://milo-apiserver.milo-system.svc.cluster.local:6443",
                "certificate-authority-data": ca}}],
            "users": [{"name": USER, "user": {"token": token}}],
            "contexts": [{"name": "catalog-installer", "context": {
                "cluster": "milo", "user": USER}}]}


def preflight(host: Kube, milo: Kube, identity: dict) -> None:
    exact_role_manifests()
    source = host.json("-n", "flux-system", "get", "gitrepository", "milo")
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    require(source["spec"].get("ref") == {"commit": MILO_SHA} and
            source.get("status", {}).get("artifact", {}).get("revision") == "sha1:" + MILO_SHA and
            core["spec"].get("images") == [{"name": "ghcr.io/milo-os/milo",
                                           "digest": MILO_IMAGE}] and
            ready(source) and ready(core) and not core["spec"].get("suspend", False),
            "Milo #870 source/image/core differs from reviewed baseline")
    for row in ROWS:
        cluster = host if row[0] == "secret" else milo
        require(cluster.optional(row[1], row[2], row[3]) is None,
                f"Catalog installer {row[0]} already exists")
    require(host.optional("namespace", "locations-system") is None and
            host.optional("namespace", "services-system") is None,
            "operator namespace already exists; use a fresh isolated fixture")
    absence_census(host, milo)
    auth = host.json("-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
    auth_csv(auth["data"]["tokens.csv"])
    require(identity["milo"]["ca_sha256"] == hashlib.sha256(
        base64.b64decode(host.json("-n", "milo-system", "get", "secret", "milo-ca-tls")
                         ["data"]["ca.crt"], validate=True)).hexdigest(),
            "Milo root public CA differs from source Secret")
    cert = base64.b64decode(host.json("-n", "milo-system", "get", "secret",
        "milo-apiserver-tls")["data"]["tls.crt"], validate=True)
    ca = base64.b64decode(milo.json("config", "view", "--raw")["clusters"][0]
        ["cluster"]["certificate-authority-data"], validate=True)
    with tempfile.TemporaryDirectory(prefix="catalog-public-cert-") as directory:
        ca_path = Path(directory) / "ca.crt"
        cert_path = Path(directory) / "server.crt"
        ca_path.write_bytes(ca)
        cert_path.write_bytes(cert)
        host.commands.run("openssl", "verify", "-CAfile", str(ca_path),
                          str(cert_path), sensitive=True)
        host.commands.run("openssl", "x509", "-in", str(cert_path), "-noout",
                          "-checkhost", "milo-apiserver.milo-system.svc.cluster.local",
                          sensitive=True)
        host.commands.run("openssl", "x509", "-in", str(cert_path), "-noout",
                          "-checkip", "127.0.0.1", sensitive=True)


def absence_census(host: Kube, milo: Kube) -> None:
    for kind, name, namespace in GRAPH:
        require(host.optional(kind, name, namespace) is None,
                f"Catalog Flux graph {kind}/{name} remains active")
    require(host.optional("kustomization", "apps-a1t-locations-host", "flux-system") is None and
            host.optional("kustomization", "apps-a1t-locations-rbac", "milo-system") is None and
            host.optional("deployment", "locations-manager", "locations-system") is None,
            "Locations runtime still depends on installer state")
    crds = milo.json("get", "crd").get("items", [])
    require(not any(item["metadata"]["name"].endswith(TARGET_SUFFIXES) for item in crds) and
            all(milo.optional(kind, name) is None for kind, name in WEBHOOKS),
            "Catalog API schemas or webhooks remain; retain installer")


def cleanup(host: Kube, milo: Kube, evidence: dict, state: dict,
            evidence_path: Path, state_path: Path, private: Path) -> None:
    require(set(evidence.get("attempted", {})).issubset({row[0] for row in ROWS}) and
            all(uid is None or isinstance(uid, str) and uid
                for uid in evidence.get("attempted", {}).values()),
            "Catalog installer journal invalid")
    # All dependency/collision checks precede a destructive credential change.
    absence_census(host, milo)
    host_proxy = APIProxy(host, private, "catalog-installer-host-cleanup")
    root_proxy = APIProxy(milo, private, "catalog-installer-root-cleanup")
    errors: list[str] = []
    try:
        host_proxy.start()
        root_proxy.start()
        for row in reversed(ROWS):
            cluster, proxy = (host, host_proxy) if row[0] == "secret" else (milo, root_proxy)
            try:
                delete_owned(cluster, proxy, row, evidence, evidence_path)
            except Exception:
                errors.append(f"Catalog installer {row[0]} UID-safe delete failed")
                break
        if not errors and state.get("auth_intent"):
            try:
                auth = host.json("-n", "milo-system", "get", "secret",
                                 "milo-apiserver-auth-tokens")
                actual = auth["data"]["tokens.csv"]
                require(auth["metadata"]["uid"] == state["auth_uid"] and
                        actual in (state["old_b64"], state["new_b64"]),
                        "Milo auth changed outside Catalog installer")
                if actual == state["new_b64"]:
                    patch_auth(host, private, state["auth_uid"],
                               state["new_b64"], state["old_b64"])
                # A prior cleanup may have patched the Secret before the
                # rollout failed or its response was lost. Old CSV alone does
                # not prove the running authenticator dropped the token.
                proof = rollout_milo(host)
                restored = host.json("-n", "milo-system", "get", "secret",
                                     "milo-apiserver-auth-tokens")
                require(restored["metadata"]["uid"] == state["auth_uid"] and
                        restored["data"]["tokens.csv"] == state["old_b64"],
                        "Milo auth changed during restored-pod rollout")
                state["restored_auth_rollout"] = proof
                atomic_json(state_path, state)
            except Exception:
                errors.append("Catalog installer auth restore failed")
        if not errors and state.get("core_suspend_intent"):
            try:
                resume_core(host, private, state)
                atomic_json(state_path, state)
                host.commands.run("flux", "reconcile", "kustomization", "milo-core",
                                  "-n", "flux-system", "--kubeconfig", str(host.config),
                                  "--context", host.context, "--timeout=180s", timeout=200)
                require(ready(host.json("-n", "flux-system", "get", "kustomization",
                                        "milo-core")), "milo-core not Ready after resume")
            except Exception:
                errors.append("Catalog installer milo-core resume failed")
    except Exception:
        errors.append("Catalog installer cleanup transport failed")
    finally:
        root_proxy.close()
        host_proxy.close()
    evidence["cleanup"] = "complete" if not errors else "incomplete"
    evidence["cleanup_errors"] = errors
    atomic_json(evidence_path, evidence)
    if not errors:
        state_path.unlink()
        (private / "cas-patch.json").unlink(missing_ok=True)
        (private / "probe.kubeconfig").unlink(missing_ok=True)
        private.rmdir()
    require(not errors, "Catalog installer cleanup incomplete: " + "; ".join(errors))


def run(args: argparse.Namespace) -> None:
    for config in (args.host_kubeconfig, args.milo_kubeconfig):
        require(not config.is_symlink(), "kubeconfig symlink refused")
        private_file(config)
    require(args.evidence_file.is_absolute() and args.evidence_file.parent.is_dir() and
            args.evidence_file.parent.stat().st_mode & 0o077 == 0 and
            not args.evidence_file.is_symlink(), "private evidence path required")
    private = args.evidence_file.parent / "apps-a1t-catalog-installer-private"
    state_path = private / "state.json"
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    identity = scope_identity(host, milo, args.host_context, args.milo_port,
                              projects_removed=args.cleanup and
                              json.loads((milo.config.parent /
                                "apps-a1t-project-bootstrap-evidence.json").read_text())
                              .get("cleanup") == "complete")
    if args.cleanup:
        require(private.is_dir() and not private.is_symlink() and
                private.stat().st_mode & 0o077 == 0,
                "private Catalog installer recovery directory required")
        private_file(args.evidence_file)
        private_file(state_path)
        evidence = json.loads(args.evidence_file.read_text())
        state = json.loads(state_path.read_text())
        require(evidence.get("host_context") == args.host_context and
                evidence.get("milo_source") == MILO_SHA and
                re.fullmatch(r"[0-9a-f]{24}", evidence.get("run_id", "")) and
                state.get("run_id") == evidence["run_id"],
                "Catalog installer cleanup journal differs from isolated run")
        require_same_identity(evidence["scope_identity"], identity)
        cleanup(host, milo, evidence, state, args.evidence_file, state_path, private)
        print("Catalog installer cleanup complete", flush=True)
        return
    require(not args.evidence_file.exists() and not private.exists() and
            not private.is_symlink(), "Catalog installer journal already exists")
    preflight(host, milo, identity)
    print("Catalog installer preflight passed: exact source, role and collisions", flush=True)
    if not args.execute:
        return
    private.mkdir(mode=0o700)
    auth = host.json("-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
    old_csv = auth_csv(auth["data"]["tokens.csv"])
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    source = host.json("-n", "flux-system", "get", "gitrepository", "milo")
    token = secrets.token_urlsafe(32)
    new_csv = old_csv.rstrip("\n") + f'\n{token},{USER},{USER},"system:authenticated"\n'
    ca = milo.json("config", "view", "--raw")["clusters"][0]["cluster"][
        "certificate-authority-data"]
    state = {"run_id": secrets.token_hex(12), "auth_uid": auth["metadata"]["uid"],
             "old_b64": auth["data"]["tokens.csv"],
             "new_b64": base64.b64encode(new_csv.encode()).decode(),
             "core_uid": core["metadata"]["uid"],
             "core_generation": core["metadata"]["generation"],
             "core_spec": core["spec"],
             "core_applied_revision": core.get("status", {}).get("lastAppliedRevision"),
             "source_uid": source["metadata"]["uid"],
             "source_generation": source["metadata"]["generation"],
             "source_spec": source["spec"],
             "core_suspend_present": "suspend" in core["spec"],
             "core_suspend_intent": False, "auth_intent": False}
    evidence = {"host_context": args.host_context, "milo_source": MILO_SHA,
                "milo_port": args.milo_port, "scope_identity": identity,
                "run_id": state["run_id"], "attempted": {}, "intents": {},
                "cleanup": "not-requested", "setup_stage": "journal-written"}
    atomic_json(state_path, state)
    atomic_json(args.evidence_file, evidence)
    def stage(name: str) -> None:
        evidence["setup_stage"] = name
        atomic_json(args.evidence_file, evidence)
    try:
        stage("suspend-core")
        state["core_suspend_intent"] = True
        atomic_json(state_path, state)
        suspend_core(host, private, state)
        atomic_json(state_path, state)
        stage("auth-cas-and-rollout")
        state["auth_intent"] = True
        atomic_json(state_path, state)
        patch_auth(host, private, state["auth_uid"], state["old_b64"], state["new_b64"])
        rollout_milo(host)
        role, binding = exact_role_manifests()
        stage("authenticated-root-reconnect")
        create_root_role_after_reconnect(milo, role, evidence, args.evidence_file,
                                         mark_stage=stage)
        stage("root-binding-create")
        create_owned(milo, ROWS[1], binding, evidence, args.evidence_file)
        stage("scoped-authorization-probes")
        probe_path = private / "probe.kubeconfig"
        probe = kubeconfig(ca, token)
        probe["clusters"][0]["cluster"]["server"] = f"https://127.0.0.1:{args.milo_port}"
        probe["current-context"] = "catalog-installer"
        probe_path.write_text(yaml.safe_dump(probe))
        probe_path.chmod(0o600)
        scoped = Kube(commands, probe_path, "catalog-installer")
        for verb, resource, expected, name in (
            ("create", "customresourcedefinitions.apiextensions.k8s.io", True, None),
            ("patch", "customresourcedefinitions.apiextensions.k8s.io", True,
             "locations.locations.miloapis.com"),
            ("delete", "customresourcedefinitions.apiextensions.k8s.io", False,
             "unreviewed.example.com"),
            ("create", "mutatingwebhookconfigurations.admissionregistration.k8s.io", True, None),
            ("get", "secrets", False, None),
            ("create", "clusterroles.rbac.authorization.k8s.io", False, None),
            ("impersonate", "users", False, None),
        ):
            scoped.can_i(verb, resource, expected, name)
        probe_path.unlink()
        stage("host-flux-secret-create")
        secret = {"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": SECRET, "namespace": "milo-system"},
                  "type": "Opaque", "data": {"kubeconfig": base64.b64encode(
                      yaml.safe_dump(kubeconfig(ca, token)).encode()).decode()}}
        create_owned(host, ROWS[2], secret, evidence, args.evidence_file, sensitive=True)
        token = ""
        stage("host-flux-secret-verify")
        verified_flux_remote_secret(host, identity)
        evidence["complete"] = True
        evidence["setup_stage"] = "ready"
        atomic_json(args.evidence_file, evidence)
        print("Catalog scoped installer Ready: CA-verified remote Flux identity", flush=True)
    except Exception as error:
        evidence["setup_failure"] = {"stage": evidence.get("setup_stage", "unknown"),
                                     "class": type(error).__name__}
        try:
            atomic_json(args.evidence_file, evidence)
        except Exception:
            pass
        commands.deadline = time.monotonic() + 300
        try:
            cleanup(host, milo, evidence, state, args.evidence_file, state_path, private)
        except Exception:
            print("Catalog installer cleanup incomplete; private journal retained", file=sys.stderr)
        raise RuntimeError("Catalog installer setup failed; inspect sanitized evidence") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    require(not (args.execute and args.cleanup) and 1 <= args.milo_port <= 65535 and
            180 <= args.deadline_seconds <= 1200, "invalid mode, port or deadline")
    previous_umask = os.umask(0o077)
    try:
        run(args)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    main()
