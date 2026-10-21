#!/usr/bin/env python3
"""Install one source-verified NSO LocationBinding CRD through scoped Flux.

This prerequisite keeps the existing Catalog installer credential unchanged.
An exact-name supplemental role permits its Flux controller to prune this one
CRD; the role is removed only after the CRD and Flux stage are absent.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
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
from apps_native_fixture_scope import active_catalog_installer, require_same_identity, scope_identity
from apps_native_catalog_oci import transition_contract as catalog_oci_contract, verified_live_transition


ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "components/service-catalog"
SCHEMA_ROOT = COMPONENT / "locationbinding-source"
NSO_SHA = "79245f00d6220c128df3d024475ff7b1566ce305"
SCHEMA_SHA256 = "73fc1945ec88f651ee6ace8518a4593792c005af0d52dbc8da3e22d1f173f875"
CATALOG_SHA = "5f2b663b6c5e039c4c7b666ef3e959803851b675"
BILLING_DIGEST = "sha256:9e772461787d0b0f2561ed348109a6d80e171d04eeea9fee36a4e5a67d37f20c"
LOCATIONS_SHA = "507ac2cbd48c0228f09baea64199882eca91e865"
CRD_NAME = "locationbindings.networking.datumapis.com"
ROLE_NAME = "apps-a1t-catalog-locationbinding-installer"
SOURCE_NAME = "apps-a1t-catalog-support"
STAGE_NAME = "apps-a1t-locationbinding-crd"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
ROWS = {
    "role": ("clusterrole", ROLE_NAME, None,
             f"/apis/rbac.authorization.k8s.io/v1/clusterroles/{ROLE_NAME}"),
    "binding": ("clusterrolebinding", ROLE_NAME, None,
                f"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{ROLE_NAME}"),
    "source": ("gitrepository", SOURCE_NAME, "flux-system",
               f"/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/{SOURCE_NAME}"),
    "stage": ("kustomization", STAGE_NAME, "milo-system",
              f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/milo-system/kustomizations/{STAGE_NAME}"),
}


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


def clean_revision(commands: Commands) -> str:
    require(ROOT.is_dir() and not ROOT.is_symlink(), "isolated test-infra source required")
    revision = commands.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None and
            not commands.run("git", "-C", str(ROOT), "status", "--porcelain",
                             "--untracked-files=all"),
            "exact clean signed test-infra source required")
    commands.run("git", "-C", str(ROOT), "verify-commit", "HEAD", sensitive=True)
    return revision


def manifests(commands: Commands, revision: str, run_id: str) -> dict:
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
            "exact test-infra source revision required")
    require(re.fullmatch(r"[0-9a-f]{24}", run_id) is not None,
            "exact trial run label required")
    schema_path = SCHEMA_ROOT / "networking.datumapis.com_locationbindings.yaml"
    require(schema_path.is_file() and not schema_path.is_symlink() and
            hashlib.sha256(schema_path.read_bytes()).hexdigest() == SCHEMA_SHA256,
            "vendored NSO 79245f0 CRD bytes changed")
    wrapper = yaml.safe_load((SCHEMA_ROOT / "kustomization.yaml").read_text())
    require(wrapper == {"apiVersion": "kustomize.config.k8s.io/v1beta1",
                        "kind": "Kustomization",
                        "resources": [schema_path.name]},
            "single-CRD Kustomize root changed")
    source_crd = yaml.safe_load(schema_path.read_text())
    rendered_source = [obj for obj in yaml.safe_load_all(
        commands.run("kustomize", "build", str(SCHEMA_ROOT))) if obj]
    require(rendered_source == [source_crd], "NSO CRD wrapper emitted unexpected content")
    rendered = copy.deepcopy(rendered_source)
    rendered[0]["metadata"].setdefault("labels", {}).update({
        RUN_LABEL: run_id,
        "kustomize.toolkit.fluxcd.io/name": STAGE_NAME,
        "kustomize.toolkit.fluxcd.io/namespace": "milo-system",
    })
    require(len(rendered) == 1 and
            source_crd["apiVersion"] == "apiextensions.k8s.io/v1" and
            source_crd["kind"] == "CustomResourceDefinition" and
            source_crd["metadata"]["name"] == CRD_NAME and
            source_crd["spec"]["group"] == "networking.datumapis.com" and
            source_crd["spec"]["scope"] == "Cluster" and
            source_crd["spec"]["names"]["kind"] == "LocationBinding" and
            [v["name"] for v in source_crd["spec"]["versions"] if v["served"]] == ["v1alpha"],
            "source render differs from exact NSO LocationBinding schema")
    schema = source_crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    spec = schema["properties"]["spec"]
    require("locationRef" in spec["required"] and
            {"locationRef", "locationClassName", "displayName", "topology"} <=
            set(spec["properties"]) and
            "conditions" in schema["properties"]["status"]["properties"],
            "NSO schema no longer accepts Catalog 5f2b projection")
    rbac = [obj for obj in yaml.safe_load_all(
        (COMPONENT / "locationbinding-installer-rbac.yaml").read_text()) if obj]
    require(len(rbac) == 2 and {obj["kind"] for obj in rbac} ==
            {"ClusterRole", "ClusterRoleBinding"}, "supplemental RBAC shape changed")
    role = next(obj for obj in rbac if obj["kind"] == "ClusterRole")
    binding = next(obj for obj in rbac if obj["kind"] == "ClusterRoleBinding")
    require(role["metadata"] == {"name": ROLE_NAME} and role["rules"] == [{
        "apiGroups": ["apiextensions.k8s.io"], "resources": ["customresourcedefinitions"],
        "resourceNames": [CRD_NAME], "verbs": ["get", "patch", "update", "delete"]}] and
        binding["metadata"] == {"name": ROLE_NAME} and
        binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                               "kind": "ClusterRole", "name": ROLE_NAME} and
        binding["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io",
                                 "kind": "User", "name": "apps-a1t-catalog-installer"}],
        "supplemental grant exceeds exact named NSO CRD")
    source = {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
              "metadata": {"name": SOURCE_NAME, "namespace": "flux-system"},
              "spec": {"interval": "10m", "timeout": "5m",
                       "url": "https://github.com/datum-cloud/test-infra.git",
                       "ref": {"commit": revision},
                       "sparseCheckout": ["components/service-catalog/locationbinding-source"]}}
    stage = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
             "metadata": {"name": STAGE_NAME, "namespace": "milo-system"},
             "spec": {"interval": "10m", "retryInterval": "1m", "timeout": "3m",
                      "prune": True,
                      "dependsOn": [{"name": "service-catalog-api-crds", "namespace": "milo-system"}],
                      "sourceRef": {"kind": "GitRepository", "name": SOURCE_NAME,
                                    "namespace": "flux-system"},
                      "path": "./components/service-catalog/locationbinding-source",
                      "commonMetadata": {"labels": {RUN_LABEL: run_id}},
                      "kubeConfig": {"secretRef": {
                          "name": "apps-a1t-catalog-installer-kubeconfig", "key": "kubeconfig"}},
                      "healthChecks": [{"apiVersion": "apiextensions.k8s.io/v1",
                                        "kind": "CustomResourceDefinition", "name": CRD_NAME}]}}
    with tempfile.TemporaryDirectory(prefix="apps-locationbinding-render-") as directory:
        stage_file = Path(directory) / "stage.yaml"
        stage_file.write_text(yaml.safe_dump(stage))
        actual = [obj for obj in yaml.safe_load_all(commands.run(
            "flux", "build", "kustomization", STAGE_NAME, "-n", "milo-system",
            "--path", str(SCHEMA_ROOT), "--kustomization-file", str(stage_file),
            "--dry-run")) if obj]
    require(actual == rendered, "Flux stage render differs from one run-labelled NSO CRD")
    return {"schema": source_crd, "role": role, "binding": binding,
            "source": source, "stage": stage,
            "flux_contracts": {"stage": {"manifest": stage, "rendered": rendered,
                                         "revision": "sha1:" + revision}}}


def live_dependencies(host: Kube, milo: Kube, identity: dict, installer_path: Path,
                      catalog_api_path: Path,
                      locations_path: Path, transition_path: Path,
                      oci_contract: dict) -> tuple[dict, dict, dict, dict]:
    require(not locations_path.is_symlink(), "Locations journal symlink refused")
    private_file(locations_path)
    locations = json.loads(locations_path.read_text())
    state_path = locations_path.parent / "apps-a1t-locations-private/state.json"
    private_file(state_path)
    state = json.loads(state_path.read_text())
    require(locations.get("source") == LOCATIONS_SHA and
            locations.get("cleanup") == "not-requested" and
            locations.get("setup_stage") == "ready" and
            locations.get("scope_identity") == identity and
            state.get("run_id") == locations.get("run_id") and
            state.get("auth_intent") is True,
            "active exact Locations fixture required")
    installer, installer_state = active_catalog_installer(host, milo, identity,
                                                          installer_path,
                                                          auth_allowed={state["new_b64"]})
    require(state["old_b64"] == installer_state["new_b64"] and
            locations.get("installer_run_id") == installer["run_id"],
            "Locations auth does not extend exact Catalog installer state")
    require(not catalog_api_path.is_symlink(), "Catalog API journal symlink refused")
    private_file(catalog_api_path)
    catalog_api = json.loads(catalog_api_path.read_text())
    require(catalog_api.get("complete") is True and
            catalog_api.get("cleanup") == "not-requested" and
            catalog_api.get("catalog_source") == CATALOG_SHA and
            catalog_api.get("billing_digest") == BILLING_DIGEST and
            catalog_api.get("scope_identity") == identity and
            catalog_api.get("installer_run_id") == installer["run_id"],
            "active source-owned Catalog API graph required")
    require(not transition_path.is_symlink(), "Catalog OCI transition journal symlink refused")
    private_file(transition_path)
    transition = json.loads(transition_path.read_text())
    require(transition.get("locations_run_id") == locations.get("run_id"),
            "Catalog OCI transition does not bind the retained Locations run")
    verified_live_transition(host, identity, oci_contract, catalog_api, transition)
    billing_predecessor(host, catalog_api)
    host_stage = host.json("-n", "flux-system", "get", "kustomization",
                           "apps-a1t-locations-host")
    cls = milo.json("get", "locationclass.locations.miloapis.com", "datum-managed")
    loc = milo.json("get", "location.locations.miloapis.com", "dfw")
    def fresh_condition(obj, kind):
        return any(c.get("type") == kind and c.get("status") == "True" and
                   c.get("observedGeneration") == obj["metadata"].get("generation")
                   for c in obj.get("status", {}).get("conditions", []))
    require(host_stage["metadata"].get("uid") == locations.get("attempted", {}).get("host") and
            ready(host_stage) and
            cls["metadata"].get("uid") == locations.get("attempted", {}).get("class") and
            loc["metadata"].get("uid") == locations.get("attempted", {}).get("location") and
            fresh_condition(cls, "Accepted") and fresh_condition(loc, "Ready"),
            "live source-owned Locations readiness prerequisite changed")
    return installer, catalog_api, locations, transition


def billing_predecessor(host: Kube, catalog_api: dict) -> None:
    """Report fixed, noncredential predicate names on a predecessor mismatch."""
    source = host.json("-n", "flux-system", "get", "ocirepository", "catalog-billing")
    stage = host.json("-n", "milo-system", "get", "kustomization",
                      "service-catalog-billing-crds")
    checks = {
        "source_uid": source.get("metadata", {}).get("uid") ==
            catalog_api.get("attempted", {}).get("flux-system/catalog-billing"),
        "source_digest": source.get("status", {}).get("artifact", {}).get("revision") ==
            BILLING_DIGEST,
        "source_ready_current": ready(source),
        "stage_uid": stage.get("metadata", {}).get("uid") ==
            catalog_api.get("attempted", {}).get("milo-system/service-catalog-billing-crds"),
        "stage_ready_current": ready(stage),
    }
    require(all(checks.values()), "source-owned Catalog Billing predecessor changed: " +
            json.dumps(checks, sort_keys=True, separators=(",", ":")))


def inventory_preflight(host: Kube, milo: Kube) -> None:
    for key, (kind, name, namespace, _) in ROWS.items():
        cluster = milo if key in ("role", "binding") else host
        require(cluster.optional(kind, name, namespace) is None,
                f"refusing existing {kind}/{name}")
    require(milo.optional("crd", CRD_NAME) is None,
            "refusing existing LocationBinding CRD")


def intent_digest(obj: dict) -> str:
    meta = obj.get("metadata", {})
    allowed_finalizers = (["finalizers.fluxcd.io"] if obj.get("kind") == "GitRepository"
                          else [])
    require(not meta.get("ownerReferences") and not meta.get("deletionTimestamp") and
            meta.get("finalizers", []) in ([], allowed_finalizers),
            "supplemental object has unexpected lifecycle")
    return hashlib.sha256(json.dumps({
        "apiVersion": obj.get("apiVersion"), "kind": obj.get("kind"),
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "labels": meta.get("labels", {}),
        "spec": obj.get("spec"), "rules": obj.get("rules"),
        "roleRef": obj.get("roleRef"), "subjects": obj.get("subjects")},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_owned(cluster: Kube, key: str, manifest: dict, evidence: dict,
                 path: Path) -> dict:
    require(key not in evidence["attempted"], "duplicate prerequisite create intent")
    item = copy.deepcopy(manifest)
    item["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    evidence["intents"][key] = intent_digest(item)
    evidence["attempted"][key] = None
    atomic_json(path, evidence)
    result = cluster.create(item, ROWS[key][2])
    uid = result.get("metadata", {}).get("uid")
    require(isinstance(uid, str) and bool(uid) and
            intent_digest(result) == evidence["intents"][key],
            "created prerequisite differs from exact source intent")
    evidence["attempted"][key] = uid
    atomic_json(path, evidence)
    return result


def delete_owned(cluster: Kube, proxy: APIProxy, key: str, manifest: dict,
                 evidence: dict, path: Path) -> None:
    if key not in evidence["attempted"]:
        return
    kind, name, namespace, url = ROWS[key]
    current = cluster.optional(kind, name, namespace)
    if current is None:
        return
    expected = copy.deepcopy(manifest)
    expected["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    require(intent_digest(current) == evidence["intents"].get(key) == intent_digest(expected),
            f"changed {key} payload blocks UID-safe cleanup")
    uid = evidence["attempted"][key]
    if uid is None:
        require(current["metadata"].get("labels", {}).get(RUN_LABEL) == evidence["run_id"],
                f"unknown-UID {key} lacks exact run marker")
        uid = current["metadata"]["uid"]
        evidence["attempted"][key] = uid
        atomic_json(path, evidence)
    require(current["metadata"]["uid"] == uid, f"refusing replaced {key}")
    proxy.delete(url, uid, current["metadata"]["resourceVersion"])
    def absent() -> bool:
        obj = cluster.optional(kind, name, namespace)
        require(obj is None or obj["metadata"]["uid"] == uid,
                f"refusing replacement during {key} cleanup")
        return obj is None
    wait_for(absent, f"{key} absent", 120)


def installer_probe_config(host: Kube, identity: dict, private: Path,
                           milo_port: int) -> tuple[Path, Path, str]:
    secret = host.json("-n", "milo-system", "get", "secret",
                       "apps-a1t-catalog-installer-kubeconfig")
    config = yaml.safe_load(base64.b64decode(
        secret["data"]["kubeconfig"], validate=True))
    require(config.get("current-context") == "catalog-installer" and
            len(config.get("clusters", [])) == 1 and
            len(config.get("contexts", [])) == 1 and
            len(config.get("users", [])) == 1,
            "scoped Catalog installer config shape changed")
    require(config["contexts"][0] == {"name": "catalog-installer",
            "context": {"cluster": "milo", "user": "apps-a1t-catalog-installer"}} and
            config["clusters"][0].get("name") == "milo" and
            config["users"][0].get("name") == "apps-a1t-catalog-installer" and
            set(config["users"][0].get("user", {})) == {"token"},
            "scoped Catalog installer context or user changed")
    settings = config["clusters"][0]["cluster"]
    require(settings.get("server") ==
            "https://milo-apiserver.milo-system.svc.cluster.local:6443" and
            hashlib.sha256(base64.b64decode(
                settings["certificate-authority-data"], validate=True)).hexdigest() ==
            identity["milo"]["ca_sha256"] and
            not settings.get("insecure-skip-tls-verify", False),
            "scoped Catalog installer CA or origin changed")
    config["clusters"][0]["cluster"]["server"] = f"https://127.0.0.1:{milo_port}"
    probe_dir = Path(tempfile.mkdtemp(prefix="catalog-installer-probe-", dir=private))
    probe_path = probe_dir / "kubeconfig"
    try:
        atomic_json(probe_path, config)
    except Exception:
        probe_path.unlink(missing_ok=True)
        probe_dir.rmdir()
        raise
    return probe_dir, probe_path, config["current-context"]


def authorization_probes(probe: Kube, record=None) -> None:
    def check(verb: str, resource: str, expected: bool,
              name: str | None = None) -> None:
        operation = {"verb": verb, "resource": resource, "expected": expected,
                     "name": name}
        if record is not None:
            record({**operation, "state": "started"})
        if name is None:
            probe.can_i(verb, resource, expected)
        else:
            probe.can_i(verb, resource, expected, name)
        if record is not None:
            record({**operation, "state": "passed"})

    for verb in ("get", "patch", "update", "delete"):
        check(verb, "customresourcedefinitions.apiextensions.k8s.io", True,
              CRD_NAME)
        check(verb, "customresourcedefinitions.apiextensions.k8s.io", False,
              "foreign.networking.datumapis.com")
    for verb, resource in (("get", "secrets"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users")):
        check(verb, resource, False)


def no_locationbindings(milo: Kube) -> None:
    observed_projects = milo.json("get", "projects.resourcemanager.miloapis.com")
    require({item["metadata"]["name"] for item in observed_projects.get("items", [])} ==
            set(PROJECTS), "foreign or missing Project blocks CRD prune")
    for name in (None, *PROJECTS):
        path = ("/apis/networking.datumapis.com/v1alpha/locationbindings" if name is None else
                "/apis/resourcemanager.miloapis.com/v1alpha1/projects/" + name +
                "/control-plane/apis/networking.datumapis.com/v1alpha/locationbindings")
        result = json.loads(milo.k("get", "--raw", path))
        require(result.get("apiVersion") == "networking.datumapis.com/v1alpha" and
                result.get("kind") == "LocationBindingList" and
                result.get("items") == [],
                f"existing LocationBinding blocks CRD prune in {name or 'root'}")


def writer_quiescent(host: Kube, milo: Kube,
                     expected_webhook_uids: dict | None = None) -> dict:
    """Refuse CRD prune while a Catalog operator can project new bindings."""
    for name, namespace in (("service-catalog-operator", "flux-system"),
                            ("service-catalog-operator-rbac", "milo-system")):
        require(host.optional("kustomization", name, namespace) is None,
                "Catalog operator Flux stage still can start a writer")
    namespace = host.optional("namespace", "services-system")
    if namespace is not None:
        resources = host.json("-n", "services-system", "get",
                              "deployment,replicaset,pod,statefulset,daemonset,job,cronjob,replicationcontroller")
        require(resources.get("items") == [],
                "Catalog host namespace contains active or terminating workloads")
        require(host.optional("secret", "milo-kubeconfig", "services-system") is None,
                "Catalog operator credential Secret remains mounted or reusable")
    selected = host.json(
        "get", "deployment,replicaset,pod,statefulset,daemonset,job,cronjob,replicationcontroller",
        "-A", "-l", "control-plane=services")
    require(selected.get("items") == [],
            "Catalog manager workload still exists in host cluster")
    observed = {}
    for kind, name, count in (("mutatingwebhookconfiguration", "services-mutating", 2),
                              ("validatingwebhookconfiguration", "services-validating", 6)):
        obj = milo.json("get", kind, name)
        metadata = obj.get("metadata", {})
        hooks = obj.get("webhooks", [])
        require(len(hooks) == count and isinstance(metadata.get("uid"), str) and
                bool(metadata["uid"]) and not metadata.get("deletionTimestamp") and
                all(hook.get("failurePolicy") == "Fail" and
                    hook.get("clientConfig", {}).get("service", {}).get("name") ==
                    "services-webhook" and
                    hook["clientConfig"]["service"].get("namespace") == "services-system" and
                    hook["clientConfig"]["service"].get("port") == 443 and
                    not hook["clientConfig"].get("caBundle") and
                    not hook["clientConfig"].get("url") for hook in hooks),
                "Catalog webhook registration is not original fail-closed empty-CA state")
        observed[name] = metadata["uid"]
    if expected_webhook_uids is not None:
        require(observed == expected_webhook_uids,
                "Catalog webhook UID changed before CRD prune")
    return observed


def validate_owned_crd(crd: dict, evidence: dict, manifest_set: dict,
                       path: Path) -> None:
    metadata = crd.get("metadata", {})
    expected = manifest_set["flux_contracts"]["stage"]["rendered"][0]
    spec = copy.deepcopy(crd.get("spec", {}))
    # The pinned Kubernetes apiextensions server defaults only this absent
    # conversion stanza. Every other spec field must match source exactly.
    if "conversion" not in expected["spec"] and spec.get("conversion") == {"strategy": "None"}:
        spec.pop("conversion")
    require(crd.get("apiVersion") == "apiextensions.k8s.io/v1" and
            crd.get("kind") == "CustomResourceDefinition" and
            metadata.get("name") == CRD_NAME and
            metadata.get("labels") == expected["metadata"]["labels"] and
            metadata.get("annotations") == expected["metadata"].get("annotations") and
            not metadata.get("ownerReferences") and
            not metadata.get("deletionTimestamp") and
            metadata.get("finalizers", []) == [] and
            spec == expected["spec"],
            "foreign or changed LocationBinding CRD blocks prune")
    uid = metadata.get("uid")
    require(isinstance(uid, str) and bool(uid) and
            (evidence.get("crd_uid") is None or uid == evidence["crd_uid"]),
            "replaced LocationBinding CRD blocks prune")
    if evidence.get("crd_uid") is None:
        evidence["crd_uid"] = uid
        atomic_json(path, evidence)


def cleanup(host: Kube, milo: Kube, evidence: dict, path: Path,
            manifests_by_key: dict, identity: dict) -> None:
    require_same_identity(evidence["scope_identity"], identity)
    require(set(evidence.get("webhook_uids", {})) ==
            {"services-mutating", "services-validating"} and
            all(isinstance(uid, str) and bool(uid)
                for uid in evidence["webhook_uids"].values()),
            "original Catalog webhook UID census missing; retain CRD")
    errors = []
    host_proxy = APIProxy(host, path.parent, "catalog-locationbinding-host")
    root_proxy = APIProxy(milo, path.parent, "catalog-locationbinding-root")
    try:
        host_proxy.start()
        root_proxy.start()
        if "stage" in evidence["attempted"]:
            try:
                # A partial apply may have created the CRD before Flux recorded inventory.
                # Never prune in the presence of even one live projected object.
                writer_quiescent(host, milo, evidence["webhook_uids"])
                if milo.optional("crd", CRD_NAME) is not None:
                    no_locationbindings(milo)
                    crd = milo.json("get", "crd", CRD_NAME)
                    validate_owned_crd(crd, evidence, manifests_by_key, path)
                delete_stage(host, host_proxy, "stage",
                             manifests_by_key["flux_contracts"], evidence, path, atomic_json)
                require(host.optional("kustomization", STAGE_NAME, "milo-system") is None,
                        "remote Flux stage still exists after delete")
                # Flux may apply the CRD but fail before writing inventory.
                # Only after its source-bound Kustomization is absent can a
                # directly UID/RV-conditional fallback no longer race Flux.
                remaining = milo.optional("crd", CRD_NAME)
                if remaining is not None:
                    writer_quiescent(host, milo, evidence["webhook_uids"])
                    no_locationbindings(milo)
                    validate_owned_crd(remaining, evidence, manifests_by_key, path)
                    evidence["fallback_crd_delete_intent"] = evidence["crd_uid"]
                    atomic_json(path, evidence)
                    root_proxy.delete(
                        f"/apis/apiextensions.k8s.io/v1/customresourcedefinitions/{CRD_NAME}",
                        evidence["crd_uid"], remaining["metadata"]["resourceVersion"])
                    def crd_absent() -> bool:
                        obj = milo.optional("crd", CRD_NAME)
                        require(obj is None or obj["metadata"]["uid"] == evidence["crd_uid"],
                                "LocationBinding CRD replaced during fallback cleanup")
                        return obj is None
                    wait_for(crd_absent, "exact partial-apply CRD absent", 120)
                require(milo.optional("crd", CRD_NAME) is None,
                        "LocationBinding CRD remains after Flux prune")
            except Exception:
                errors.append("source-bound LocationBinding Flux prune failed")
        if not errors:
            for key in ("binding", "role", "source"):
                try:
                    cluster, proxy = ((milo, root_proxy) if key in ("role", "binding") else
                                      (host, host_proxy))
                    delete_owned(cluster, proxy, key, manifests_by_key[key], evidence, path)
                except Exception:
                    errors.append(f"UID-safe {key} teardown failed")
                    break
    except Exception:
        errors.append("private API cleanup transport failed")
    finally:
        root_proxy.close()
        host_proxy.close()
    evidence["cleanup"] = "complete" if not errors else "incomplete"
    evidence["cleanup_errors"] = errors
    atomic_json(path, evidence)
    require(not errors, "LocationBinding prerequisite cleanup incomplete: " + "; ".join(errors))


def run(args: argparse.Namespace) -> None:
    for path in (args.host_kubeconfig, args.milo_kubeconfig):
        require(not path.is_symlink(), "kubeconfig symlink refused")
        private_file(path)
    require(args.evidence_file.is_absolute() and args.evidence_file.parent.is_dir() and
            not args.evidence_file.is_symlink() and
            args.evidence_file.parent.stat().st_mode & 0o077 == 0,
            "private evidence path required")
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    revision = clean_revision(commands)
    oci_contract = catalog_oci_contract(commands, args.catalog_source,
                                        args.catalog_oci_manifest,
                                        args.catalog_oci_layer)
    if args.cleanup:
        private_file(args.evidence_file)
        evidence = json.loads(args.evidence_file.read_text())
        run_id = evidence.get("run_id")
    else:
        run_id = secrets.token_hex(12)
    manifest_set = manifests(commands, revision, run_id)
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    identity = scope_identity(host, milo, args.host_context, args.milo_port)
    installer, catalog_api, locations, transition = live_dependencies(
        host, milo, identity, args.installer_evidence_file,
        args.catalog_api_evidence_file, args.locations_evidence_file,
        args.catalog_oci_evidence_file, oci_contract)
    if args.cleanup:
        require(evidence.get("source_revision") == revision and
                evidence.get("nso_revision") == NSO_SHA and
                evidence.get("installer_run_id") == installer["run_id"] and
                evidence.get("catalog_api_run_id") == catalog_api["run_id"] and
                evidence.get("catalog_oci_run_id") == transition["run_id"] and
                evidence.get("locations_run_id") == locations["run_id"] and
                evidence.get("host_context") == args.host_context and
                evidence.get("milo_port") == args.milo_port and
                re.fullmatch(r"[0-9a-f]{24}", evidence.get("run_id", "")),
                "LocationBinding cleanup journal differs from exact prerequisites")
        cleanup(host, milo, evidence, args.evidence_file, manifest_set, identity)
        print("LocationBinding prerequisite cleanup complete", flush=True)
        return
    inventory_preflight(host, milo)
    webhook_uids = writer_quiescent(host, milo)
    require(not args.evidence_file.exists(), "LocationBinding evidence already exists")
    print("LocationBinding prerequisite preflight passed: exact source and scope", flush=True)
    if not args.execute:
        return
    evidence = {"run_id": run_id, "source_revision": revision,
                "nso_revision": NSO_SHA, "schema_sha256": SCHEMA_SHA256,
                "host_context": args.host_context, "milo_port": args.milo_port,
                "scope_identity": identity, "installer_run_id": installer["run_id"],
                "catalog_api_run_id": catalog_api["run_id"],
                "catalog_oci_run_id": transition["run_id"],
                "locations_run_id": locations["run_id"],
                "webhook_uids": webhook_uids,
                "attempted": {}, "intents": {}, "flux_stages": {},
                "cleanup": "not-requested", "setup_stage": "journal-written"}
    atomic_json(args.evidence_file, evidence)
    def stage(name: str) -> None:
        evidence["setup_stage"] = name
        atomic_json(args.evidence_file, evidence)
    try:
        stage("supplemental-role")
        create_owned(milo, "role", manifest_set["role"], evidence, args.evidence_file)
        create_owned(milo, "binding", manifest_set["binding"], evidence, args.evidence_file)
        stage("installer-probe-config")
        probe_dir, probe_path, probe_context = installer_probe_config(
            host, identity, args.evidence_file.parent, args.milo_port)
        try:
            stage("installer-authorization-probes")
            def record_probe(operation: dict) -> None:
                evidence["authorization_probe"] = operation
                atomic_json(args.evidence_file, evidence)
            authorization_probes(Kube(commands, probe_path, probe_context), record_probe)
        finally:
            probe_path.unlink(missing_ok=True)
            probe_dir.rmdir()
        stage("pinned-testinfra-source")
        create_owned(host, "source", manifest_set["source"], evidence, args.evidence_file)
        def source_ready():
            obj = host.optional("gitrepository", SOURCE_NAME, "flux-system")
            require(obj is not None and obj["metadata"]["uid"] == evidence["attempted"]["source"],
                    "owned test-infra source changed")
            return (ready(obj) and
                    obj.get("status", {}).get("artifact", {}).get("revision") ==
                    "sha1:" + revision)
        wait_for(source_ready, "exact test-infra Git artifact", 180)
        stage("remote-flux-crd")
        create_stage(host, "stage", manifest_set["stage"], manifest_set["flux_contracts"],
                     evidence, args.evidence_file, atomic_json)
        def crd_ready():
            obj = host.optional("kustomization", STAGE_NAME, "milo-system")
            crd = milo.optional("crd", CRD_NAME)
            require(obj is not None and obj["metadata"]["uid"] == evidence["attempted"]["stage"],
                    "owned remote Flux CRD stage changed")
            return (ready(obj) and
                obj.get("status", {}).get("lastAppliedRevision") ==
                "sha1:" + revision and crd is not None and any(
                    c.get("type") == "Established" and c.get("status") == "True"
                    for c in crd.get("status", {}).get("conditions", [])))
        wait_for(crd_ready, "exact source-owned LocationBinding CRD", 240)
        crd = milo.json("get", "crd", CRD_NAME)
        validate_owned_crd(crd, evidence, manifest_set, args.evidence_file)
        evidence["setup_stage"] = "ready"
        atomic_json(args.evidence_file, evidence)
        print("LocationBinding CRD Ready at pinned NSO schema and test-infra Flux revision", flush=True)
    except Exception as error:
        evidence["setup_failure"] = {"stage": evidence.get("setup_stage", "unknown"),
                                     "class": type(error).__name__}
        atomic_json(args.evidence_file, evidence)
        commands.deadline = time.monotonic() + 300
        try:
            cleanup(host, milo, evidence, args.evidence_file, manifest_set, identity)
        except Exception:
            print("LocationBinding prerequisite cleanup incomplete; journal retained", file=sys.stderr)
        raise RuntimeError("LocationBinding prerequisite setup failed; inspect sanitized evidence") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--installer-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-api-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-oci-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-source", required=True, type=Path)
    parser.add_argument("--catalog-oci-manifest", required=True, type=Path)
    parser.add_argument("--catalog-oci-layer", required=True, type=Path)
    parser.add_argument("--locations-evidence-file", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
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
