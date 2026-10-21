#!/usr/bin/env python3
"""Start pinned Locations readiness controller against isolated Milo APIs.

The source-owned host manager uses a dedicated static-token User and the
source-derived readiness-only Milo role. Publisher/hub access remains off.
All writes have an exclusive private ownership and credential-recovery journal.
"""

from __future__ import annotations

import argparse
import base64
import csv
import copy
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
from apps_native_operator_flux import create_stage, delete_stage
from apps_native_fixture_scope import (active_catalog_installer, rollout_static_auth,
    scope_identity, require_same_identity, wait_authenticated_root)


ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_SHA = "507ac2cbd48c0228f09baea64199882eca91e865"
LOCATIONS_IMAGE = "sha256:c888576073f66e14a3401c450df6bcf47dcb69259914bbb93acc654084c4d400"
LOCATIONS_ARM64_MANIFEST = "sha256:208d9d9543cdc898969e33027d08a3097d12ac69030b65717ebfcd507306cf35"
LOCATIONS_ARM64_CONFIG = "sha256:5926007df439adea24d2af9b5a0e4d2f7f205e506b9a9c4f432a16a2eb63ccbf"
MILO_SHA = "200de48a305732720377cb83308d57c84fd5f688"
MILO_IMAGE = "sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c"
USER = "apps-a1t-locations-operator"
CLASS = "datum-managed"
LOCATION = "dfw"
CONTROLLER = "locations.miloapis.com/datum-managed"
ORG = "apps-a1t-ci-org"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
ROWS = (
    ("source", "gitrepository", "apps-a1t-locations", "flux-system",
     "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/apps-a1t-locations"),
    ("rbac-role", "clusterrole", "locations-manager", None,
     "/apis/rbac.authorization.k8s.io/v1/clusterroles/locations-manager"),
    ("namespace", "namespace", "locations-system", None,
     "/api/v1/namespaces/locations-system"),
    ("secret", "secret", "milo-kubeconfig", "locations-system",
     "/api/v1/namespaces/locations-system/secrets/milo-kubeconfig"),
    ("host", "kustomization", "apps-a1t-locations-host", "flux-system",
     "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/apps-a1t-locations-host"),
    ("class", "locationclass.locations.miloapis.com", CLASS, None,
     f"/apis/locations.miloapis.com/v1alpha1/locationclasses/{CLASS}"),
    ("location", "location.locations.miloapis.com", LOCATION, None,
     f"/apis/locations.miloapis.com/v1alpha1/locations/{LOCATION}"),
    ("rbac-binding", "clusterrolebinding", "locations-manager", None,
     "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/locations-manager"),
)


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


def checked_source(commands: Commands, source: Path) -> None:
    require(source.is_dir() and not source.is_symlink(), "clean Locations source required")
    require(commands.run("git", "-C", str(source), "rev-parse", "HEAD") == LOCATIONS_SHA and
            not commands.run("git", "-C", str(source), "status", "--porcelain",
                             "--untracked-files=all"),
            "Locations source must be exact and clean")


def offline_contract(commands: Commands, source: Path) -> dict:
    checked_source(commands, source)
    root = ROOT / "components/locations"
    source_manifest = yaml.safe_load((root / "source.yaml").read_text())
    host_manifest = yaml.safe_load((root / "host.yaml").read_text())
    require("kubeConfig" not in host_manifest["spec"] and not host_manifest["spec"].get("dependsOn"),
            "Locations host must use local API and native RBAC ordering")
    require(source_manifest["spec"]["ref"] == {"commit": LOCATIONS_SHA} and
            source_manifest["spec"]["url"] == "https://github.com/milo-os/locations.git",
            "Locations Git source pin changed")
    def render(name: str, path: str, file: str, namespace: str = "flux-system") -> list[dict]:
        result = commands.run("flux", "build", "kustomization", name,
                              "-n", namespace, "--path", str(source / path),
                              "--kustomization-file", str(root / file), "--dry-run")
        return [item for item in yaml.safe_load_all(result) if item]
    patches = yaml.safe_load((root / "readiness-role-patches.yaml").read_text())
    require(isinstance(patches, list) and len(patches) == 2, "exact readiness role patches required")
    with tempfile.TemporaryDirectory(prefix="apps-locations-role-") as directory:
        local = Path(directory).resolve()
        (local / "kustomization.yaml").write_text(yaml.safe_dump({
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
            "components": [os.path.relpath(source.resolve() / "config/components/controller_rbac", local)], "patches": patches}))
        role = [obj for obj in yaml.safe_load_all(commands.run("kustomize", "build", str(local),
                    "--load-restrictor", "LoadRestrictionsNone")) if obj]
    host = render("apps-a1t-locations-host", "config/base/manager", "host.yaml")
    require(len(role) == 2 and {item["kind"] for item in role} == {"ClusterRole", "ClusterRoleBinding"},
            "Locations RBAC render changed")
    role_obj = next(item for item in role if item["kind"] == "ClusterRole")
    binding = next(item for item in role if item["kind"] == "ClusterRoleBinding")
    require(role_obj["metadata"]["name"] == "locations-manager" and
            binding["metadata"]["name"] == "locations-manager" and
            binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "locations-manager"} and
            binding["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io",
                                     "kind": "User", "name": USER}] and
            {group for rule in role_obj["rules"] for group in rule["apiGroups"]} ==
                {"", "locations.miloapis.com"} and
            not ({"secrets", "clusterroles", "clusterrolebindings", "servinglocations"} &
                 {resource for rule in role_obj["rules"] for resource in rule["resources"]}),
            "Locations operator role exceeds reviewed readiness scope")
    kinds = {(item["kind"], item["metadata"]["name"]) for item in host}
    require(kinds == {("Deployment", "locations-manager"),
                      ("Service", "locations-webhook"),
                      ("ServiceAccount", "locations-manager"),
                      ("PodDisruptionBudget", "locations-manager"),
                      ("ConfigMap", "locations-config")},
            "Locations host render contains unexpected objects")
    deployment = next(item for item in host if item["kind"] == "Deployment")
    config = next(item for item in host if item["kind"] == "ConfigMap")
    pod = deployment["spec"]["template"]["spec"]
    manager = pod["containers"][0]
    settings = yaml.safe_load(config["data"]["config.yaml"])
    require(manager["image"] == "ghcr.io/milo-os/locations@" + LOCATIONS_IMAGE and
            deployment["spec"]["replicas"] == 1 and
            pod["automountServiceAccountToken"] is False and
            pod["securityContext"]["fsGroup"] == 65532 and
            pod["volumes"][-1]["secret"] == {"secretName": "milo-kubeconfig",
                                                   "defaultMode": 0o440} and
            manager["args"] == ["operator", "--health-probe-bind-address=:8081",
                                "--server-config=/config/config.yaml"] and
            settings["kubeconfigPath"] == "/etc/milo/kubeconfig" and
            settings["locationReadiness"]["controllerName"] == CONTROLLER and
            "locationPublisher" not in settings,
            "Locations host image, scoped mount or readiness contract changed")
    return {"source": source_manifest, "rbac": {"rbac-role": role_obj, "rbac-binding": binding},
            "host": yaml.safe_load((root / "host.yaml").read_text()),
            "flux_contracts": {name: {"manifest": yaml.safe_load((root / file).read_text()),
                "rendered": rendered, "revision": "sha1:" + LOCATIONS_SHA}
                for name, file, rendered in (("host", "host.yaml", host),)}}


def baseline_guard(host: Kube) -> None:
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    source = host.json("-n", "flux-system", "get", "gitrepository", "milo")
    require(source["spec"]["ref"] == {"commit": MILO_SHA} and
            source.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + MILO_SHA and
            core["spec"].get("images") == [{"name": "ghcr.io/milo-os/milo",
                                           "digest": MILO_IMAGE}] and
            ready(source) and core["spec"].get("suspend") is True,
            "Milo source/image pin changed before Locations trial")


def inventory_preflight(host: Kube, milo: Kube) -> None:
    for _, kind, name, namespace, _ in ROWS:
        cluster = milo if kind.startswith("location") or kind in ("clusterrole", "clusterrolebinding") else host
        require(cluster.optional(kind, name, namespace) is None,
                f"refusing to adopt existing {kind}/{name}")
    require(host.optional("kustomization", "apps-a1t-locations-rbac", "milo-system") is None,
            "legacy remote Locations RBAC stage must be absent")
    for name in ("locations.locations.miloapis.com",
                 "locationclasses.locations.miloapis.com"):
        crd = milo.json("get", "crd", name)
        require(any(c.get("type") == "Established" and c.get("status") == "True"
                    for c in crd.get("status", {}).get("conditions", [])),
                f"Location CRD/{name} is not Established")
    source = host.json("-n", "flux-system", "get", "gitrepository", "service-catalog")
    api = host.json("-n", "milo-system", "get", "kustomization", "service-catalog-api-crds")
    require(source["spec"]["ref"] == {"commit": "5f2b663b6c5e039c4c7b666ef3e959803851b675"} and
            ready(source) and ready(api), "source-owned Catalog API prerequisite absent")


def auth_rows(data: str) -> tuple[list[list[str]], str]:
    rows = list(csv.reader(io.StringIO(base64.b64decode(data, validate=True).decode())))
    require(len(rows) == 3 and {(row[1], row[2], row[3]) for row in rows if len(row) == 4} ==
            {("admin", "admin", "system:masters"),
             ("test-user", "test-user", "system:authenticated"),
             ("apps-a1t-catalog-installer", "apps-a1t-catalog-installer",
              "system:authenticated")} and
            len({row[0] for row in rows}) == 3 and all(row[0] for row in rows),
            "Milo static auth is not exact Catalog-installer three-user state")
    return rows, base64.b64decode(data).decode()


def scoped_config(ca: str, token: str, server: str) -> dict:
    return {"apiVersion": "v1", "kind": "Config", "current-context": "scoped",
            "clusters": [{"name": "milo", "cluster": {"server": server,
                        "certificate-authority-data": ca}}],
            "users": [{"name": USER, "user": {"token": token}}],
            "contexts": [{"name": "scoped", "context": {"cluster": "milo", "user": USER}}]}


def verified_milo_ca(commands: Commands, private: Path, ca_b64: str,
                     server_b64: str) -> None:
    ca = base64.b64decode(ca_b64, validate=True)
    server = base64.b64decode(server_b64, validate=True)
    require(b"BEGIN CERTIFICATE" in ca and b"BEGIN CERTIFICATE" in server,
            "Milo public CA or serving certificate absent")
    with tempfile.TemporaryDirectory(dir=private, prefix="locations-public-ca-") as directory:
        ca_path = Path(directory) / "ca.crt"
        server_path = Path(directory) / "server.crt"
        ca_path.write_bytes(ca)
        server_path.write_bytes(server)
        commands.run("openssl", "verify", "-CAfile", str(ca_path), str(server_path),
                     sensitive=True)
        commands.run("openssl", "x509", "-in", str(server_path), "-noout",
                     "-checkhost", "milo-apiserver.milo-system.svc", sensitive=True)
        commands.run("openssl", "x509", "-in", str(server_path), "-noout",
                     "-checkip", "127.0.0.1", sensitive=True)


def row_create(cluster: Kube, row: tuple, manifest: dict, evidence: dict,
               evidence_path: Path, *, sensitive: bool = False, flux_contracts=None, rbac_manifests=None) -> dict:
    key = row[0]
    if row[1] == "kustomization":
        return create_stage(cluster, key, manifest, flux_contracts, evidence, evidence_path, atomic_json)
    require(key not in evidence["attempted"], f"duplicate {key} create intent")
    if key in ("rbac-role", "rbac-binding"):
        require(manifest == (rbac_manifests or {}).get(key), "RBAC create differs from exact rendered source")
    manifest = copy.deepcopy(manifest)
    manifest.setdefault("metadata", {}).setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    evidence.setdefault("intents", {})[key] = rbac_digest(manifest) if key.startswith("rbac-") else intent_digest(manifest)
    evidence["attempted"][key] = None
    atomic_json(evidence_path, evidence)
    result = cluster.create(manifest, row[3], sensitive=sensitive)
    uid = result.get("metadata", {}).get("uid")
    require(isinstance(uid, str) and bool(uid), f"{key} create response lacks UID")
    evidence["attempted"][key] = uid
    atomic_json(evidence_path, evidence)
    if key.startswith("rbac-"):
        require(rbac_digest(result) == evidence["intents"][key], "RBAC response differs from exact source intent")
    return result


def create_root_rbac_after_reconnect(milo: Kube, manifests: dict,
                                     evidence: dict, path: Path,
                                     seconds: float = 45,
                                     mark_stage=None) -> None:
    """Gate the first Locations root writes after static-auth Pod replacement."""
    wait_authenticated_root(milo, seconds)
    for row in (ROWS[1], ROWS[7]):
        if mark_stage is not None:
            mark_stage("root-" + row[0] + "-create")
        row_create(milo, row, manifests[row[0]], evidence, path,
                   rbac_manifests=manifests)


def intent_digest(manifest: dict) -> str:
    meta = manifest.get("metadata", {})
    require(not meta.get("ownerReferences") and not meta.get("finalizers") and
            not meta.get("deletionTimestamp"), "Locations create intent has lifecycle metadata")
    labels = dict(meta.get("labels", {}))
    spec = manifest.get("spec", {})
    if manifest.get("kind") == "Namespace":
        # Kubernetes adds only its own name label and namespace finalizer.
        if labels.get("kubernetes.io/metadata.name") == meta.get("name"):
            del labels["kubernetes.io/metadata.name"]
        require(spec in ({}, {"finalizers": ["kubernetes"]}),
                "owned namespace has unexpected lifecycle spec")
        spec = {}
    projection = {"apiVersion": manifest.get("apiVersion"), "kind": manifest.get("kind"),
                  "name": meta.get("name"), "namespace": meta.get("namespace"),
                  "labels": labels, "annotations": meta.get("annotations", {}),
                  "spec": spec, "data": manifest.get("data", {}),
                  "type": manifest.get("type")}
    return hashlib.sha256(json.dumps(projection, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def rbac_digest(manifest: dict) -> str:
    """Exact public RBAC payload; exclude only API-owned metadata bookkeeping."""
    obj = copy.deepcopy(manifest)
    require(obj.get("apiVersion") == "rbac.authorization.k8s.io/v1" and
            obj.get("kind") in ("ClusterRole", "ClusterRoleBinding"), "exact RBAC kind required")
    meta = obj.get("metadata", {})
    require(not meta.get("ownerReferences") and not meta.get("finalizers") and
            not meta.get("deletionTimestamp") and not meta.get("namespace"),
            "RBAC lifecycle or scope changed")
    for field in ("uid", "resourceVersion", "creationTimestamp", "managedFields"):
        meta.pop(field, None)
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owned_delete(cluster: Kube, proxy: APIProxy, row: tuple, evidence: dict,
                 evidence_path: Path, flux_contracts=None, rbac_manifests=None) -> None:
    key = row[0]
    if row[1] == "kustomization":
        return delete_stage(cluster, proxy, key, flux_contracts, evidence, evidence_path, atomic_json)
    if key not in evidence["attempted"]:
        return
    obj = cluster.optional(row[1], row[2], row[3])
    if obj is None:
        return
    meta = obj["metadata"]
    if key in ("rbac-role", "rbac-binding"):
        expected = copy.deepcopy((rbac_manifests or {}).get(key))
        require(isinstance(expected, dict), "exact rendered RBAC source required for cleanup")
        expected.setdefault("metadata", {}).setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
        require(rbac_digest(expected) == evidence.get("intents", {}).get(key) == rbac_digest(obj),
                "known or unknown UID RBAC payload differs from source intent")
    known_uid = evidence["attempted"][key]
    if known_uid:
        require(meta["uid"] == known_uid, f"refusing replaced {key}")
    else:
        require(meta.get("labels", {}).get(RUN_LABEL) == evidence["run_id"] and
                (rbac_digest(obj) if key.startswith("rbac-") else intent_digest(obj)) == evidence.get("intents", {}).get(key),
                f"refusing foreign or changed unknown-UID {key}")
        evidence["attempted"][key] = meta["uid"]
        atomic_json(evidence_path, evidence)
    proxy.delete(row[4], meta["uid"], meta["resourceVersion"])
    def absent() -> bool:
        current = cluster.optional(row[1], row[2], row[3])
        require(current is None or current["metadata"]["uid"] == meta["uid"],
                f"refusing replacement {key} during cleanup")
        return current is None
    wait_for(absent, f"{key} deleted", 120)


def verified_retained_namespace(host: Kube, evidence: dict, path: Path) -> None:
    if "namespace" not in evidence["attempted"]:
        return
    namespace = host.optional("namespace", "locations-system")
    require(namespace is not None,
            "owned locations-system namespace disappeared; refusing cleanup")
    meta = namespace["metadata"]
    uid = evidence["attempted"]["namespace"]
    if uid:
        require(meta.get("uid") == uid and
                meta.get("labels", {}).get(RUN_LABEL) == evidence["run_id"],
                "locations-system namespace identity changed")
    else:
        require(meta.get("labels", {}).get(RUN_LABEL) == evidence["run_id"] and
                intent_digest(namespace) == evidence.get("intents", {}).get("namespace") and
                bool(meta.get("uid")),
                "unknown-UID locations-system namespace intent differs")
        evidence["attempted"]["namespace"] = meta["uid"]
        atomic_json(path, evidence)
    require(not meta.get("deletionTimestamp"),
            "locations-system namespace is terminating")


def condition_ready(obj: dict, condition: str) -> bool:
    generation = obj["metadata"].get("generation", 1)
    return any(c.get("type") == condition and c.get("status") == "True" and
               c.get("observedGeneration", 0) >= generation
               for c in obj.get("status", {}).get("conditions", []))


def patch_auth(host: Kube, private: Path, uid: str, expected: str, replacement: str) -> None:
    host.patch_json(private, "secret", "milo-apiserver-auth-tokens", [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/data/tokens.csv", "value": expected},
        {"op": "replace", "path": "/data/tokens.csv", "value": replacement},
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
    if not core["spec"].get("suspend", False):
        require(meta["generation"] == state["core_generation"],
                "milo-core suspend outcome ambiguous")
        return
    expected = state.get("suspended_generation", state["core_generation"] + 1)
    require(meta["generation"] == expected, "milo-core generation changed while suspended")
    operation = ("replace" if state["core_suspend_present"] else "remove")
    actions = [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/metadata/generation", "value": expected},
        {"op": "test", "path": "/spec/suspend", "value": True},
        {"op": operation, "path": "/spec/suspend",
         **({"value": False} if operation == "replace" else {})},
    ]
    host.patch_json(private, "kustomization", "milo-core", actions, namespace="flux-system")


def rollout_milo(host: Kube) -> dict:
    return rollout_static_auth(host)


def flux_ready(host: Kube, row: tuple, evidence: dict) -> bool:
    obj = host.optional(row[1], row[2], row[3])
    require(obj is not None and obj["metadata"]["uid"] == evidence["attempted"][row[0]],
            f"owned {row[0]} was replaced")
    return ready(obj)


def verified_running_manager(host: Kube) -> dict:
    deployment = host.json("-n", "locations-system", "get", "deployment", "locations-manager")
    meta = deployment["metadata"]
    status = deployment.get("status", {})
    image = "ghcr.io/milo-os/locations@" + LOCATIONS_IMAGE
    require(status.get("observedGeneration", 0) >= meta.get("generation", 1) and
            status.get("readyReplicas") == 1 and status.get("availableReplicas") == 1 and
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] == image,
            "Locations Deployment generation, readiness or image differs")
    replicasets = host.json("-n", "locations-system", "get", "replicasets", "-l",
                            "control-plane=locations").get("items", [])
    owned_rs = {rs["metadata"]["uid"] for rs in replicasets
                if any(ref.get("kind") == "Deployment" and
                       ref.get("uid") == meta["uid"] and ref.get("controller") is True
                       for ref in rs["metadata"].get("ownerReferences", []))}
    pods = host.json("-n", "locations-system", "get", "pods", "-l",
                     "control-plane=locations").get("items", [])
    require(len(pods) == 1 and len(owned_rs) == 1 and
            any(ref.get("kind") == "ReplicaSet" and ref.get("uid") in owned_rs and
                ref.get("controller") is True
                for ref in pods[0]["metadata"].get("ownerReferences", [])),
            "Ready Locations pod is not bound to the expected Deployment")
    pod = pods[0]
    require(pod.get("status", {}).get("phase") == "Running" and
            any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", [])) and
            len(pod.get("status", {}).get("containerStatuses", [])) == 1 and
            pod["status"]["containerStatuses"][0].get("ready") is True and
            pod["spec"]["containers"][0].get("image") == image,
            "Locations pod is not ready with the pinned spec image")
    node = host.json("get", "node", pod["spec"]["nodeName"])
    require(node["metadata"]["labels"].get("kubernetes.io/arch") == "arm64",
            "Locations pod was not scheduled to verified arm64 node")
    image_id = pod["status"]["containerStatuses"][0].get("imageID", "")
    require(image_id in {
        "ghcr.io/milo-os/locations@" + LOCATIONS_IMAGE,
        "ghcr.io/milo-os/locations@" + LOCATIONS_ARM64_MANIFEST,
        "docker-pullable://ghcr.io/milo-os/locations@" + LOCATIONS_ARM64_MANIFEST,
        "containerd://" + LOCATIONS_ARM64_CONFIG,
        "docker://" + LOCATIONS_ARM64_CONFIG,
    }, "Locations running image ID differs from verified OCI index/arm64 child/config")
    return {"deployment_uid": meta["uid"], "deployment_generation": meta["generation"],
            "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
            "image_id": image_id}


def cleanup(host: Kube, milo: Kube, evidence: dict, state: dict,
            evidence_path: Path, state_path: Path, private: Path, flux_contracts=None, rbac_manifests=None) -> None:
    require(set(evidence.get("attempted", {})).issubset({row[0] for row in ROWS}) and
            all(uid is None or isinstance(uid, str) and bool(uid)
                for uid in evidence.get("attempted", {}).values()),
            "Locations ownership journal is invalid")
    require_same_identity(evidence["scope_identity"],
                          scope_identity(host, milo, evidence["host_context"],
                                         evidence["milo_port"]))
    verified_retained_namespace(host, evidence, evidence_path)
    host_proxy = APIProxy(host, evidence_path.parent, "locations-host-cleanup")
    root_proxy = APIProxy(milo, evidence_path.parent, "locations-root-cleanup")
    probe_path = private / "probe.kubeconfig"
    errors: list[str] = []
    try:
        require(not probe_path.is_symlink(), "Locations probe kubeconfig symlink refused")
        if probe_path.exists():
            private_file(probe_path)
            probe_path.unlink()
        host_proxy.start()
        root_proxy.start()
        for row in (ROWS[6], ROWS[5]):
            try:
                owned_delete(milo, root_proxy, row, evidence, evidence_path)
            except Exception:
                errors.append(f"{row[0]}: UID-safe delete failed")
        if not errors:
            try:
                owned_delete(host, host_proxy, ROWS[4], evidence, evidence_path, flux_contracts)
                require(host.optional("deployment", "locations-manager", "locations-system") is None,
                        "Locations manager still exists after Flux stage deletion")
            except Exception:
                errors.append("host manager: delete or absence check failed")
        if not errors:
            try:
                for row in (ROWS[7], ROWS[1]):
                    owned_delete(milo, root_proxy, row, evidence, evidence_path, rbac_manifests=rbac_manifests)
                for kind in ("clusterrole", "clusterrolebinding"):
                    require(milo.optional(kind, "locations-manager") is None,
                            f"Milo {kind}/locations-manager remains after native UID/RV cleanup")
            except Exception:
                errors.append("Milo RBAC: delete or absence check failed")
        if not errors:
            # Retain the owned namespace. Kubernetes namespace deletion would
            # cascade contents this fixture has not fully enumerated.
            for row in (ROWS[3], ROWS[0]):
                try:
                    owned_delete(host, host_proxy, row, evidence, evidence_path)
                except Exception:
                    errors.append(f"{row[0]}: UID-safe delete failed")
        if not errors and state.get("auth_intent"):
            try:
                current = host.json("-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
                require(current["metadata"]["uid"] == state["auth_uid"],
                        "Milo static auth Secret was replaced")
                actual = current["data"]["tokens.csv"]
                require(actual in (state["old_b64"], state["new_b64"]),
                        "Milo static auth changed outside Locations trial")
                if actual == state["new_b64"]:
                    patch_auth(host, private, state["auth_uid"],
                               state["new_b64"], state["old_b64"])
                # Retrying after a successful CAS but failed/lost rollout
                # must still replace pods that may retain the added token.
                proof = rollout_milo(host)
                restored = host.json("-n", "milo-system", "get", "secret",
                                     "milo-apiserver-auth-tokens")
                require(restored["metadata"]["uid"] == state["auth_uid"] and
                        restored["data"]["tokens.csv"] == state["old_b64"],
                        "Milo auth changed during restored-pod rollout")
                state["restored_auth_rollout"] = proof
                atomic_json(state_path, state)
            except Exception:
                errors.append("Milo static auth restore failed")
    except Exception:
        errors.append("cleanup transport or recovery failed")
    finally:
        root_proxy.close()
        host_proxy.close()
    evidence["retained_namespace"] = {"name": "locations-system",
                                      "uid": evidence.get("attempted", {}).get("namespace")}
    evidence["cleanup"] = "complete-with-retained-namespace" if not errors else "incomplete"
    evidence["cleanup_errors"] = errors
    atomic_json(evidence_path, evidence)
    if not errors:
        state_path.unlink()
        (private / "cas-patch.json").unlink(missing_ok=True)
        private.rmdir()
    require(not errors, "Locations cleanup incomplete: " + "; ".join(errors))


def run(args: argparse.Namespace) -> None:
    for config in (args.host_kubeconfig, args.milo_kubeconfig):
        require(not config.is_symlink(), "kubeconfig symlink refused")
        private_file(config)
    require(args.evidence_file.is_absolute() and args.evidence_file.parent.is_dir() and
            args.evidence_file.parent.stat().st_mode & 0o077 == 0 and
            not args.evidence_file.is_symlink(), "private evidence path required")
    private = args.evidence_file.parent / "apps-a1t-locations-private"
    state_path = private / "state.json"
    probe_path = private / "probe.kubeconfig"
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    manifests = offline_contract(commands, args.locations_source)
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    identity = scope_identity(host, milo, args.host_context, args.milo_port)
    if args.cleanup:
        require(private.is_dir() and not private.is_symlink() and
                private.stat().st_mode & 0o077 == 0,
                "exclusive Locations private state directory required")
        private_file(args.evidence_file)
        private_file(state_path)
        evidence = json.loads(args.evidence_file.read_text())
        state = json.loads(state_path.read_text())
        require(evidence.get("host_context") == args.host_context and
                evidence.get("source") == LOCATIONS_SHA and
                re.fullmatch(r"[0-9a-f]{24}", evidence.get("run_id", "")) and
                state.get("run_id") == evidence["run_id"],
                "Locations cleanup journal differs from isolated run")
        require_same_identity(evidence["scope_identity"], identity)
        installer, _ = active_catalog_installer(host, milo, identity,
            args.installer_evidence_file,
            auth_allowed={state["old_b64"], state["new_b64"]})
        require(evidence.get("installer_run_id") == installer["run_id"],
                "Locations installer dependency changed before cleanup")
        cleanup(host, milo, evidence, state, args.evidence_file, state_path, private, manifests["flux_contracts"], manifests["rbac"])
        print("Locations fixture cleanup complete", flush=True)
        return
    baseline_guard(host)
    installer, _ = active_catalog_installer(host, milo, identity,
                                             args.installer_evidence_file)
    inventory_preflight(host, milo)
    require(not args.evidence_file.exists() and not private.exists() and
            not private.is_symlink(),
            "Locations private journal already exists")
    print("Locations preflight passed: source, schemas and names match", flush=True)
    if not args.execute:
        return
    private.mkdir(mode=0o700)
    auth = host.json("-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
    _, old_csv = auth_rows(auth["data"]["tokens.csv"])
    ca_secret = host.json("-n", "milo-system", "get", "secret", "milo-ca-tls")
    ca = ca_secret["data"]["ca.crt"]
    serving_secret = host.json("-n", "milo-system", "get", "secret", "milo-apiserver-tls")
    verified_milo_ca(commands, private, ca,
                     serving_secret["data"]["tls.crt"])
    token = secrets.token_urlsafe(32)
    new_csv = old_csv.rstrip("\n") + f'\n{token},{USER},{USER},"system:authenticated"\n'
    new_b64 = base64.b64encode(new_csv.encode()).decode()
    run_id = secrets.token_hex(12)
    state = {"run_id": run_id, "auth_uid": auth["metadata"]["uid"],
             "old_b64": auth["data"]["tokens.csv"], "new_b64": new_b64,
             "auth_intent": False}
    evidence = {"host_context": args.host_context, "source": LOCATIONS_SHA,
                "milo_port": args.milo_port, "scope_identity": identity,
                "installer_run_id": installer["run_id"],
                "image_index": LOCATIONS_IMAGE, "run_id": run_id,
                "attempted": {}, "intents": {}, "cleanup": "not-requested",
                "auth_before_sha256": hashlib.sha256(old_csv.encode()).hexdigest(),
                "setup_stage": "journal-written"}
    atomic_json(state_path, state)
    atomic_json(args.evidence_file, evidence)
    def stage(name: str) -> None:
        evidence["setup_stage"] = name
        atomic_json(args.evidence_file, evidence)
    try:
        stage("auth-cas-and-rollout")
        state["auth_intent"] = True
        atomic_json(state_path, state)
        patch_auth(host, private, state["auth_uid"],
                   state["old_b64"], state["new_b64"])
        rollout_milo(host)
        stage("host-git-source")
        row_create(host, ROWS[0], manifests["source"], evidence, args.evidence_file)
        wait_for(lambda: flux_ready(host, ROWS[0], evidence), "Locations Git source Ready", 180)
        source = host.json("-n", "flux-system", "get", "gitrepository", "apps-a1t-locations")
        require(source.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + LOCATIONS_SHA, "Locations Flux fetched unexpected revision")
        stage("authenticated-root-reconnect")
        create_root_rbac_after_reconnect(milo, manifests["rbac"], evidence,
                                         args.evidence_file, mark_stage=stage)
        stage("scoped-authorization-probes")
        atomic_json(probe_path, scoped_config(ca, token, f"https://127.0.0.1:{args.milo_port}"))
        probe = Kube(commands, probe_path, "scoped")
        try:
            for verb, resource, allowed in (
                ("get", "locationclasses.locations.miloapis.com", True),
                ("list", "locations.locations.miloapis.com", True),
                ("update", "locations.locations.miloapis.com/status", True),
                ("get", "secrets", False),
                ("create", "clusterrolebindings.rbac.authorization.k8s.io", False),
                ("impersonate", "users", False),
                ("create", "servinglocations.locations.miloapis.com", False),
            ):
                probe.can_i(verb, resource, allowed)
        finally:
            probe_path.unlink(missing_ok=True)
        stage("host-manager-and-credentials")
        row_create(host, ROWS[2], {"apiVersion": "v1", "kind": "Namespace",
                                  "metadata": {"name": "locations-system"}},
                   evidence, args.evidence_file)
        secret_manifest = {"apiVersion": "v1", "kind": "Secret",
                           "metadata": {"name": "milo-kubeconfig",
                                        "namespace": "locations-system"},
                           "type": "Opaque", "data": {"kubeconfig": base64.b64encode(
                               yaml.safe_dump(scoped_config(
                                   ca, token,
                                   "https://milo-apiserver.milo-system.svc:6443")).encode()).decode()}}
        row_create(host, ROWS[3], secret_manifest, evidence, args.evidence_file,
                   sensitive=True)
        token = ""
        row_create(host, ROWS[4], manifests["host"], evidence, args.evidence_file, flux_contracts=manifests["flux_contracts"])
        wait_for(lambda: flux_ready(host, ROWS[4], evidence), "Locations manager Ready", 240)
        running = verified_running_manager(host)
        evidence["running_manager"] = running
        atomic_json(args.evidence_file, evidence)
        stage("root-location-class-and-location")
        row_create(milo, ROWS[5], {"apiVersion": "locations.miloapis.com/v1alpha1",
                                     "kind": "LocationClass", "metadata": {"name": CLASS},
                                     "spec": {"controllerName": CONTROLLER}},
                   evidence, args.evidence_file)
        row_create(milo, ROWS[6], {"apiVersion": "locations.miloapis.com/v1alpha1",
                                     "kind": "Location", "metadata": {"name": LOCATION},
                                     "spec": {"locationClassRef": {"name": CLASS},
                                              "topology": {"topology.datum.net/city-code": "DFW"}}},
                   evidence, args.evidence_file)
        def controller_ready() -> bool:
            cls = milo.optional("locationclass.locations.miloapis.com", CLASS)
            loc = milo.optional("location.locations.miloapis.com", LOCATION)
            require(cls is not None and loc is not None and
                    cls["metadata"]["uid"] == evidence["attempted"]["class"] and
                    loc["metadata"]["uid"] == evidence["attempted"]["location"],
                    "owned LocationClass or Location changed")
            return condition_ready(cls, "Accepted") and condition_ready(loc, "Ready")
        wait_for(controller_ready, "controller-produced LocationClass Accepted and Location Ready", 180)
        evidence["running_manager"] = verified_running_manager(host)
        evidence["class_condition"] = "Accepted=True"
        evidence["location_condition"] = "Ready=True"
        evidence["publisher"] = "disabled: no hubKubeconfigPath"
        evidence["setup_stage"] = "ready"
        atomic_json(args.evidence_file, evidence)
        print("Locations readiness passed: real controller accepted class and marked dfw Ready", flush=True)
    except Exception as error:
        evidence["setup_failure"] = {"stage": evidence.get("setup_stage", "unknown"),
                                     "class": type(error).__name__}
        try:
            atomic_json(args.evidence_file, evidence)
        except Exception:
            pass
        commands.deadline = time.monotonic() + 300
        try:
            cleanup(host, milo, evidence, state, args.evidence_file, state_path, private, manifests["flux_contracts"], manifests["rbac"])
        except Exception:
            print("Locations cleanup incomplete; private journal retained", file=sys.stderr)
        raise RuntimeError("Locations setup failed; inspect sanitized evidence") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--locations-source", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--installer-evidence-file", required=True, type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    require(not (args.execute and args.cleanup) and 1 <= args.milo_port <= 65535 and
            180 <= args.deadline_seconds <= 1200, "invalid mode, port or deadline")
    old_umask = os.umask(0o077)
    try:
        run(args)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    main()
