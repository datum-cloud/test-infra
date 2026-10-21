"""Exact source/render contract for the isolated native Catalog operator stage.

This module reads clean upstream source and the already-verified published OCI
layer. It never contacts a Kubernetes API or reads a credential.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile

import yaml

from apps_native_catalog_oci import (
    CATALOG_SHA, OCI_DIGEST, _files_from_layer, exact_manifest,
    exact_source, verify_tree,
)
from apps_native_support import Commands, RUN_LABEL, flux_inventory, require


ROOT = Path(__file__).resolve().parent.parent
CA_ROOT = ROOT / "components/service-catalog/operator-ca"
IMAGE_INDEX = "sha256:a5c5e4c31c9f89edac64d6a4c02a70320c04b4e1ca95aabab6567a0a1357b1e2"
SOURCE_ROLE_SHA = "eab5f85c4753294a6c5df3d67d65ef3f053a34c34890ab5cc7a25d08cdc26c1b"
SOURCE_OPERATOR_SHA = "350fd0c5127eddb856d540a8979980d8e2fd280ff22b9d077f53d2fb3a6c13f8"
CA_RENDER_SHA = "bee71e0b45a29ca6ca442f94839cb4dc504f572823c36dea252d4d8acfe36a4b"
USER = "apps-a1t-services-operator"
CA_SOURCE = "apps-a1t-services-bootstrap"
CA_STAGE = "apps-a1t-services-ca"
HOST_STAGE = "apps-a1t-services-host"
NAMESPACE = "services-system"
WEBHOOK_NAMES = {"services-mutating": 2, "services-validating": 6}


def _canonical(obj: dict | list) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def render(commands: Commands, path: Path) -> list[dict]:
    return [obj for obj in yaml.safe_load_all(
        commands.run("kustomize", "build", str(path), timeout=120)) if obj]


def flux_render(commands: Commands, stage: dict, path: Path) -> list[dict]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as file:
        yaml.safe_dump(stage, file)
        file.flush()
        output = commands.run("flux", "build", "kustomization",
                              stage["metadata"]["name"], "-n",
                              stage["metadata"]["namespace"], "--path", str(path),
                              "--kustomization-file", file.name, "--dry-run", timeout=120)
    return [obj for obj in yaml.safe_load_all(output) if obj]


def source_role(commands: Commands, source_config: Path) -> tuple[dict, dict]:
    docs = render(commands, source_config / "components/controller_rbac")
    require(len(docs) == 2 and
            {obj["kind"] for obj in docs} == {"ClusterRole", "ClusterRoleBinding"},
            "Catalog source RBAC render shape changed")
    role = next(obj for obj in docs if obj["kind"] == "ClusterRole")
    binding = next(obj for obj in docs if obj["kind"] == "ClusterRoleBinding")
    require(hashlib.sha256(_canonical(role).encode()).hexdigest() == SOURCE_ROLE_SHA and
            role["metadata"]["name"] == "services-controller-manager" and
            binding["metadata"]["name"] == "services-controller-manager" and
            binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                   "kind": "ClusterRole",
                                   "name": "services-controller-manager"} and
            len(binding.get("subjects", [])) == 1 and
            binding["subjects"][0]["kind"] == "ServiceAccount",
            "Catalog source RBAC identity or rules changed")
    require(not any(rule.get("apiGroups") == ["rbac.authorization.k8s.io"] or
                    "secrets" in rule.get("resources", []) or
                    "impersonate" in rule.get("verbs", [])
                    for rule in role["rules"]),
            "Catalog operator source role acquired privileged authority")
    binding = copy.deepcopy(binding)
    binding["subjects"] = [{"apiGroup": "rbac.authorization.k8s.io",
                            "kind": "User", "name": USER}]
    return role, binding


def source_operator(commands: Commands, config: Path) -> list[dict]:
    docs = render(commands, config / "overlays/e2e-milo/operator")
    require(len(docs) == 8 and
            hashlib.sha256(_canonical(docs).encode()).hexdigest() == SOURCE_OPERATOR_SHA,
            "Catalog upstream operator render changed")
    identities = {(obj["kind"], obj["metadata"]["name"]) for obj in docs}
    require(identities == {
        ("Namespace", NAMESPACE), ("ServiceAccount", "services-controller-manager"),
        ("ConfigMap", "services-config"), ("Service", "services-webhook"),
        ("Deployment", "services-controller-manager"),
        ("PodDisruptionBudget", "services-controller-manager"),
        ("Issuer", "selfsigned-issuer"), ("Certificate", "services-serving-cert")},
        "Catalog upstream operator contains an unexpected object")
    return docs


def modified_config(raw: str) -> str:
    config = yaml.safe_load(raw)
    require(config == {"apiVersion": "apiserver.config.miloapis.com/v1alpha1",
                       "kind": "ServicesOperator", "metricsServer": {"bindAddress": "0"},
                       "webhookServer": {},
                       "kubeconfigPath": "/etc/milo/kubeconfig"},
            "Catalog source config defaults changed")
    config["locationSource"] = "locations.miloapis.com/v1alpha1"
    return yaml.safe_dump(config, sort_keys=True)


def ca_contract(commands: Commands, revision: str, run_id: str) -> dict:
    ca = render(commands, CA_ROOT)
    require(len(ca) == 3 and
            hashlib.sha256(_canonical(ca).encode()).hexdigest() == CA_RENDER_SHA and
            {(obj["kind"], obj["metadata"]["name"]) for obj in ca} == {
                ("Issuer", "apps-a1t-services-selfsigned"),
                ("Certificate", "apps-a1t-services-ca"),
                ("Issuer", "apps-a1t-services-ca")},
            "task CA source must contain exactly one stable three-object chain")
    certificate = next(obj for obj in ca if obj["kind"] == "Certificate")
    require(certificate["spec"] == {
        "isCA": True, "commonName": "apps-a1t-services-ca",
        "secretName": "apps-a1t-services-ca",
        "privateKey": {"algorithm": "ECDSA", "size": 256},
        "issuerRef": {"kind": "Issuer", "name": "apps-a1t-services-selfsigned"}},
        "task CA Certificate changed")
    source = {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
              "metadata": {"name": CA_SOURCE, "namespace": "flux-system"},
              "spec": {"interval": "10m", "timeout": "5m",
                       "url": "https://github.com/datum-cloud/test-infra.git",
                       "ref": {"commit": revision},
                       "sparseCheckout": ["components/service-catalog/operator-ca"]}}
    stage = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
             "metadata": {"name": CA_STAGE, "namespace": "flux-system"},
             "spec": {"interval": "10m", "retryInterval": "1m", "timeout": "3m",
                      "prune": True,
                      "sourceRef": {"kind": "GitRepository", "name": CA_SOURCE,
                                    "namespace": "flux-system"},
                      "path": "./components/service-catalog/operator-ca",
                      "commonMetadata": {"labels": {RUN_LABEL: run_id}},
                      "healthChecks": [{"apiVersion": "cert-manager.io/v1",
                                        "kind": "Certificate", "name": "apps-a1t-services-ca",
                                        "namespace": NAMESPACE}]}}
    final = flux_render(commands, stage, CA_ROOT)
    require(len(final) == 3 and flux_inventory(final) == flux_inventory(ca) and
            all(obj["metadata"].get("labels", {}).get(RUN_LABEL) == run_id
                for obj in final), "task CA Flux render changed")
    return {"source": source, "stage": stage, "rendered": final,
            "flux_contract": {"manifest": stage, "rendered": final,
                              "revision": "sha1:" + revision}}


def operator_contract(commands: Commands, source_config: Path,
                      bundle_config: Path, run_id: str) -> dict:
    original = source_operator(commands, source_config)
    bundle_original = source_operator(commands, bundle_config)
    require(original == bundle_original,
            "published OCI operator root differs from source render")
    configmap = next(obj for obj in original if obj["kind"] == "ConfigMap")
    changed_config = modified_config(configmap["data"]["config.yaml"])
    patches = [
        {"target": {"kind": "Namespace", "name": NAMESPACE},
         "patch": "$patch: delete\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: services-system\n"},
        {"target": {"kind": "Issuer", "name": "selfsigned-issuer"},
         "patch": "$patch: delete\napiVersion: cert-manager.io/v1\nkind: Issuer\nmetadata:\n  name: selfsigned-issuer\n"},
        {"target": {"kind": "ConfigMap", "name": "services-config"},
         "patch": yaml.safe_dump([{"op": "replace", "path": "/data/config.yaml",
                                   "value": changed_config}], sort_keys=False)},
        {"target": {"kind": "Certificate", "name": "services-serving-cert"},
         "patch": yaml.safe_dump([{"op": "replace", "path": "/spec/issuerRef",
                                   "value": {"kind": "Issuer", "name": "apps-a1t-services-ca"}}],
                                 sort_keys=False)},
    ]
    stage = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
             "metadata": {"name": HOST_STAGE, "namespace": "flux-system"},
             "spec": {"interval": "10m", "retryInterval": "1m", "timeout": "3m",
                      "prune": True, "dependsOn": [{"name": CA_STAGE,
                                                     "namespace": "flux-system"}],
                      "sourceRef": {"kind": "OCIRepository",
                                    "name": "apps-a1t-catalog-bundle",
                                    "namespace": "flux-system"},
                      "path": "./overlays/e2e-milo/operator",
                      "commonMetadata": {"labels": {RUN_LABEL: run_id}},
                      "images": [{"name": "ghcr.io/milo-os/service-catalog",
                                  "digest": IMAGE_INDEX}],
                      "patches": patches,
                      "healthChecks": [
                          {"apiVersion": "apps/v1", "kind": "Deployment",
                           "name": "services-controller-manager", "namespace": NAMESPACE},
                          {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                           "name": "services-serving-cert", "namespace": NAMESPACE}]}}
    final = flux_render(commands, stage, bundle_config / "overlays/e2e-milo/operator")
    expected = []
    for item in original:
        if (item["kind"], item["metadata"]["name"]) in {
                ("Namespace", NAMESPACE), ("Issuer", "selfsigned-issuer")}:
            continue
        obj = copy.deepcopy(item)
        obj["metadata"].setdefault("labels", {}).update({
            RUN_LABEL: run_id,
            "kustomize.toolkit.fluxcd.io/name": HOST_STAGE,
            "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
        })
        if obj["kind"] == "ConfigMap":
            obj["data"]["config.yaml"] = changed_config
        elif obj["kind"] == "Certificate":
            obj["spec"]["issuerRef"] = {"kind": "Issuer", "name": "apps-a1t-services-ca"}
        elif obj["kind"] == "Deployment":
            obj["spec"]["template"]["spec"]["containers"][0]["image"] = (
                "ghcr.io/milo-os/service-catalog@" + IMAGE_INDEX)
        expected.append(obj)
    def keyed(items):
        return {(obj["apiVersion"], obj["kind"], obj["metadata"]["name"]): obj
                for obj in items}
    require(len(final) == 6 and keyed(final) == keyed(expected),
            "operator Flux rendered unexpected source objects or changes")
    return {"stage": stage, "rendered": final,
            "flux_contract": {"manifest": stage, "rendered": final,
                              "revision": OCI_DIGEST}}


def contract(commands: Commands, source: Path, manifest: Path,
             layer: Path, revision: str, run_id: str) -> dict:
    require(len(revision) == 40 and all(char in "0123456789abcdef" for char in revision) and
            len(run_id) == 24 and all(char in "0123456789abcdef" for char in run_id),
            "exact source revision and run ID required")
    source_config = exact_source(commands, source)
    exact_manifest(manifest)
    files = _files_from_layer(layer)
    verify_tree(source_config, files)
    with tempfile.TemporaryDirectory(prefix="apps-catalog-operator-bundle-") as directory:
        bundle = Path(directory)
        for name, data in files.items():
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        role, binding = source_role(commands, source_config)
        ca = ca_contract(commands, revision, run_id)
        operator = operator_contract(commands, source_config, bundle, run_id)
    return {"role": role, "binding": binding, "ca": ca, "operator": operator,
            "catalog_source": CATALOG_SHA, "catalog_oci_digest": OCI_DIGEST,
            "image_index": IMAGE_INDEX}
