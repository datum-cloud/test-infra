#!/usr/bin/env python3
"""Bounded native Apps authoring trial through Milo's public Project API.

Default mode is read-only live preflight; --offline never contacts a cluster.
Only --execute may create task-owned fixtures. No direct storage client is used.
"""

from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import quote

import yaml

from apps_native_support import (APIProxy, Commands, Forward, Kube, Owned, HeldLease,
                                 RUN_LABEL, kubeconfig_text, private_file,
                                 private_write, ready, require,
                                 tree_fingerprint, verify_cert, wait_for,
                                 certificate_ready)
from apps_native_cli.driver import (AppsCLI, CLI_SHA, build_private_cli,
                                    delete_with_cli, exercise_history_diff_restore)


ROOT = Path(__file__).resolve().parents[1]
APPS_BINARY_SHA = "0b1c574d8c7ee6e65ce165dee689a9af9bc1e6dc"
# Registry-verified exact-definition runtime; keep this fence source-matched.
CONTENT_SAFE_BINARY_SHA = "0b1c574d8c7ee6e65ce165dee689a9af9bc1e6dc"
APPS_IMAGE = "ghcr.io/datum-labs/apps@sha256:bfa9514b9fab3c36efff6cd1e9b700029ecce6d73e41da4b772ae5ddb8686ee9"
ARM_MANIFEST = "sha256:b945467041c253514d45c2b5e18784a35c80ed43c298b023a3109c2278cffea4"
ARM_CONFIG = "sha256:b14c53fa1f41f7579857fa5a25f8cbbd7b086f1e7a8f62845a3a41f6856faee3"
AMD_MANIFEST = "sha256:a6acb1a97d420d248a7ae1c98db1177b9a24fba61dcaf134c690296702900f6a"
AMD_CONFIG = "sha256:dc6608b84639ba1f618dddd671012f1809229d201fd874d337ca93bb00518b76"
PLATFORM_DIGESTS = {"arm64": (ARM_MANIFEST, ARM_CONFIG),
                    "amd64": (AMD_MANIFEST, AMD_CONFIG)}
REGISTRY_IMAGE = "docker.io/library/registry@sha256:fa647fc1e5d5df7d8d923fb6332aab8e78783f8fca1a1394efb4011f68f5a793"
MILO_SHA = "ba4b06e8dde99dd70e9a2f34e011d635e4408a76"
MILO_IMAGE = "sha256:3a89b1edb93eb3d4bb74808cc031897055e6130bf36aea56d978fcf252464afd"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
PROJECT_API = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
APPS_API = "/apis/apps.miloapis.com/v1alpha1"
USERS = ("apps-a1t-native-discovery", "apps-a1t-native-project",
         "apps-a1t-native-author", "apps-a1t-native-installer")
DISCOVERY, PROJECT, AUTHOR, INSTALLER = USERS
CRDS = ("apptemplates.apps.miloapis.com", "apptemplaterevisions.apps.miloapis.com")
WCS = (("mutatingwebhookconfiguration", "apps-templates.apps.miloapis.com"),
       ("validatingwebhookconfiguration", "apps-authoring.apps.miloapis.com"))
OCI_NAME = "apps-native-a1t"
FLUX_CRDS = "apps-native-a1t-crds"
FLUX_HOST = "apps-native-a1t-host"
FLUX_ADMISSION = "apps-native-a1t-admission"
INSTALLER_SECRET = "apps-native-a1t-milo-installer"
REGISTRY_NAME = "apps-a1t-native-registry"
SOURCE_REPO = "oci://apps-a1t-native-registry.flux-system.svc.cluster.local:5000/apps-native"
TEMPLATE_NAME = "apps-a1t-native-authoring-proof"
PRECISION_NAME = TEMPLATE_NAME + "-precision"
# Exact snapshot canonical encoding for the four values that exposed JSON
# float64 coercion. Never decode these numeric tokens through Python floats.
PRECISION_CANONICAL = (
    '{"description":"precision","inputSchema":{"additionalProperties":false,'
    '"properties":{"int53Plus":{"const":9007199254740993,"type":"integer"},'
    '"preciseDecimal":{"const":123456789012345678901e-21,"type":"number"},'
    '"trailingZeroInteger":{"const":9007199254740993e1,"type":"integer"},'
    '"uint64Max":{"const":18446744073709551615,"type":"integer"}},'
    '"type":"object"}}'
)
PRECISION_DIGEST = "sha256:c0f3fc1db9b5ea79b660a4809a126055cd364980439e2746f8fdc155562274c7"

def load_yaml(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text()) if item]


def render(commands: Commands, directory: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(
        commands.run("kustomize", "build", str(directory), timeout=90)) if item]


def key(obj: dict) -> tuple[str, str, str]:
    meta = obj["metadata"]
    return obj["kind"], meta.get("namespace", ""), meta["name"]


def expected_fixture() -> dict[str, dict]:
    manifests = {}
    for filename in ("registry", "source", "crds", "host", "admission", "milo-rbac"):
        items = load_yaml(ROOT / "components/apps-native" / f"{filename}.yaml")
        require(items, f"empty test-infra {filename} fixture")
        manifests[filename] = items
    return manifests


def source_contract(commands: Commands, source: Path, revision: str) -> dict:
    require(source.is_absolute() and source.is_dir(), "absolute Apps source checkout required")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
            "exact 40-character Apps config revision required")
    require(commands.run("git", "-C", str(source), "rev-parse", "HEAD") == revision,
            "Apps config checkout differs from explicit exact revision")
    require(commands.run("git", "-C", str(source), "status", "--porcelain",
                         "--untracked-files=all") == "", "Apps source checkout is dirty")
    require(commands.run("git", "-C", str(source), "merge-base", "--is-ancestor",
                         APPS_BINARY_SHA, revision) == "",
            "published Apps binary source is not an ancestor of config revision")
    runtime_changes = commands.run(
        "git", "-C", str(source), "diff", "--name-only", APPS_BINARY_SHA, revision,
        "--", "Dockerfile", ".dockerignore", "go.mod", "go.sum",
        "cmd/apps-controller", "internal", "pkg")
    require(not runtime_changes,
            "Apps runtime differs from published image source; publish and pin the new runtime")
    config_hash, config_count = tree_fingerprint(source / "config")
    crds = render(commands, source / "config/crd")
    require({key(item) for item in crds} ==
            {("CustomResourceDefinition", "", name) for name in CRDS} and len(crds) == 2,
            "Apps source CRD package differs from two native authoring kinds")
    for item in crds:
        require(item["spec"]["group"] == "apps.miloapis.com" and
                item["spec"]["scope"] == "Cluster" and
                item["metadata"].get("annotations", {}).get(
                    "discovery.miloapis.com/parent-contexts") == "Project",
                "Apps CRD scope or Project discovery contract changed")
        schema = item["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        spec_schema = schema["properties"]["spec"]
        if item["metadata"]["name"].startswith("apptemplates."):
            require(set(spec_schema.get("required", [])) == {"definitionJSON", "digest"} and
                    spec_schema["properties"]["definitionJSON"]["type"] == "string" and
                    spec_schema["properties"]["digest"]["type"] == "string" and
                    "definition" not in spec_schema["properties"],
                    "Apps template CRD lacks opaque definitionJSON/digest fidelity contract")
        else:
            require("definitionJSON" in spec_schema.get("required", []) and
                    spec_schema["properties"]["definitionJSON"]["type"] == "string" and
                    "definition" not in spec_schema["properties"],
                    "Apps revision CRD still exposes lossy structured definition")
    host = render(commands, source / "config/overlays/test-infra/host")
    host_keys = {key(item) for item in host}
    expected_host = {("ServiceAccount", "apps-system", "apps-controller"),
                     ("Role", "apps-system", "apps-controller-leader-election"),
                     ("RoleBinding", "apps-system", "apps-controller-leader-election"),
                     ("Service", "apps-system", "apps-controller"),
                     ("Deployment", "apps-system", "apps-controller"),
                     ("PodDisruptionBudget", "apps-system", "apps-controller"),
                     ("Certificate", "apps-system", "apps-serving-ca"),
                     ("Certificate", "apps-system", "apps-serving-tls"),
                     ("Issuer", "apps-system", "apps-serving-ca"),
                     ("Issuer", "apps-system", "apps-test-selfsigned")}
    require(len(host) == len(host_keys) and host_keys == expected_host,
            "Apps host overlay changed exact native manager/certificate topology")
    role = next(item for item in host if key(item) ==
                ("Role", "apps-system", "apps-controller-leader-election"))
    binding = next(item for item in host if key(item) ==
                   ("RoleBinding", "apps-system", "apps-controller-leader-election"))
    require(role["rules"] == [{"apiGroups": ["coordination.k8s.io"],
                               "resources": ["leases"],
                               "verbs": ["get", "create", "update"]}] and
            binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                    "kind": "Role",
                                    "name": "apps-controller-leader-election"} and
            binding["subjects"] == [{"kind": "ServiceAccount",
                                     "name": "apps-controller",
                                     "namespace": "apps-system"}],
            "Apps local manager gained host API rights beyond leader Lease")
    deployment = next(item for item in host if key(item) ==
                      ("Deployment", "apps-system", "apps-controller"))
    require(deployment["spec"].get("replicas") == 2,
            "native Apps trial requires two manager replicas")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    require(len(containers) == 1 and containers[0]["name"] == "manager" and
            containers[0]["image"].split("@")[0].split(":")[0] == "ghcr.io/datum-labs/apps",
            "Apps manager image transform target changed")
    admission = render(commands, source / "config/overlays/test-infra/milo-admission")
    require({key(item) for item in admission} == {
        ("MutatingWebhookConfiguration", "", WCS[0][1]),
        ("ValidatingWebhookConfiguration", "", WCS[1][1])} and len(admission) == 2,
        "Apps source Milo admission package differs from exact two WCs")
    hooks = {}
    for item in admission:
        for hook in item["webhooks"]:
            client = hook["clientConfig"]
            service = client.get("service") or {}
            require(service.get("name") == "apps-controller" and
                    service.get("namespace") == "apps-system" and
                    service.get("port", 443) == 443 and
                    service.get("path") in ("/mutate-template", "/validate-template",
                                             "/validate-revision") and
                    not client.get("caBundle") and
                    hook.get("failurePolicy") == "Fail" and
                    hook.get("sideEffects") == "None" and
                    hook.get("admissionReviewVersions") == ["v1"],
                    "source WC service, path or fail-closed contract changed")
            hooks[service["path"]] = hook
    require(set(hooks) == {"/mutate-template", "/validate-template",
                           "/validate-revision"}, "Apps source hook paths changed")
    expected_rules = {
        "/mutate-template": {("apptemplates", ("CREATE", "UPDATE"))},
        "/validate-template": {("apptemplates", ("CREATE", "UPDATE", "DELETE")),
                               ("apptemplates/status", ("UPDATE",))},
        "/validate-revision": {("apptemplaterevisions", ("CREATE", "UPDATE", "DELETE"))},
    }
    for path, hook in hooks.items():
        rules = {(resource, tuple(rule["operations"])) for rule in hook["rules"]
                 for resource in rule["resources"]}
        require(rules == expected_rules[path] and all(
            rule["apiGroups"] == ["apps.miloapis.com"] and
            rule["apiVersions"] == ["v1alpha1"] and
            rule["scope"] == "Cluster" for rule in hook["rules"]),
            f"Apps source hook rules changed: {path}")
    require(containers[0]["image"] == APPS_IMAGE,
            "Apps host overlay no longer pins the published exact OCI index")
    require(deployment["spec"]["template"]["metadata"]["annotations"].get(
        "org.opencontainers.image.revision") == APPS_BINARY_SHA and
        deployment["spec"]["template"]["metadata"]["annotations"].get(
        "org.opencontainers.image.source") == "https://github.com/datum-labs/apps",
        "Apps Deployment source provenance changed")
    require(deployment["spec"]["template"]["spec"]["securityContext"].get(
        "runAsNonRoot") is True and
        deployment["spec"]["template"]["spec"]["securityContext"].get(
            "fsGroup") == 65532 and
        deployment["spec"]["template"]["spec"]["securityContext"].get(
            "runAsUser") == 65532,
        "Apps scoped credential mounts are not nonroot-readable")
    podspec = deployment["spec"]["template"]["spec"]
    volume_secrets = {entry["name"]: entry["secret"] for entry in podspec["volumes"]
                      if "secret" in entry}
    require({name: secret["secretName"] for name, secret in volume_secrets.items()} == {
        "root-discovery-kubeconfig": "apps-root-discovery-kubeconfig",
        "project-service-kubeconfig": "apps-project-service-kubeconfig",
        "webhook-serving-cert": "apps-serving-tls"} and
        all(secret.get("defaultMode") == 0o440 for secret in volume_secrets.values()),
        "Apps manager Secret volumes or nonroot file modes changed")
    args = set(containers[0]["args"])
    require({"--root-kubeconfig=/var/run/apps/root/kubeconfig",
             "--project-kubeconfig=/var/run/apps/project/kubeconfig",
             "--leader-election-namespace=apps-system",
             "--webhook-cert-dir=/var/run/apps/webhook"} <= args and
            "--kubeconfig" not in " ".join(args),
            "Apps manager root/project credentials or local in-cluster API changed")
    return {"config_hash": config_hash, "config_count": config_count,
            "crds": crds, "host": host, "admission": admission,
            "host_keys": host_keys}


def fixture_contract(fixtures: dict[str, list[dict]]) -> None:
    registry = fixtures["registry"]
    require({key(item) for item in registry} == {
        ("Deployment", "flux-system", REGISTRY_NAME),
        ("Service", "flux-system", REGISTRY_NAME)} and
        registry[0]["spec"]["template"]["spec"]["containers"][0]["image"] == REGISTRY_IMAGE and
        registry[0]["spec"]["template"]["spec"].get(
            "automountServiceAccountToken") is False,
        "private registry fixture changed")
    source = fixtures["source"][0]
    require(key(source) == ("OCIRepository", "flux-system", OCI_NAME) and
            source["spec"]["url"] == SOURCE_REPO and
            source["spec"].get("insecure") is True and
            source["spec"]["ref"] == {"tag": "source-revision-injected-by-runner"},
            "native OCI bootstrap must be digest-replaced before reconciliation")
    expected = {"crds": ("milo-system", FLUX_CRDS, "./config/crd"),
                "host": ("flux-system", FLUX_HOST,
                         "./config/overlays/test-infra/host"),
                "admission": ("milo-system", FLUX_ADMISSION,
                              "./config/overlays/test-infra/milo-admission")}
    for label, (namespace, name, path) in expected.items():
        item = fixtures[label][0]
        require(key(item) == ("Kustomization", namespace, name) and
                item["spec"]["path"] == path and
                item["spec"]["sourceRef"] == {
                    "kind": "OCIRepository", "name": OCI_NAME,
                    "namespace": "flux-system"} and item["spec"]["prune"] is True,
                f"native Flux {label} fixture changed")
    require(fixtures["host"][0]["spec"]["images"] == [{
        "name": "ghcr.io/datum-labs/apps", "digest": APPS_IMAGE.split("@", 1)[1]}],
        "host Flux image is not the published exact OCI index")
    require(all(fixtures[label][0]["spec"].get("kubeConfig", {}).get(
        "secretRef") == {"name": INSTALLER_SECRET, "key": "kubeconfig"}
        for label in ("crds", "admission")),
        "remote Milo Flux stages lack dedicated scoped installer")
    rbac = fixtures["milo-rbac"]
    require(len(rbac) == 8 and {item["metadata"]["name"] for item in rbac} == set(USERS),
            "expected exactly four scoped Milo User roles and bindings")
    required = {
        DISCOVERY: {("resourcemanager.miloapis.com", "projects", "get"),
                    ("resourcemanager.miloapis.com", "projects", "list"),
                    ("resourcemanager.miloapis.com", "projects", "watch")},
        PROJECT: {("apps.miloapis.com", "apptemplates", verb) for verb in
                  ("get", "list", "watch", "patch")} |
                 {("apps.miloapis.com", "apptemplates/status", "update"),
                  ("apps.miloapis.com", "apptemplaterevisions", "get"),
                  ("apps.miloapis.com", "apptemplaterevisions", "create")},
        AUTHOR: {("apps.miloapis.com", "apptemplates", verb) for verb in
                 ("get", "list", "watch", "create", "update", "patch", "delete")} |
                {("apps.miloapis.com", "apptemplaterevisions", verb) for verb in
                 ("get", "list", "watch")},
    }
    for item in rbac:
        if item["kind"] != "ClusterRole" or item["metadata"]["name"] == INSTALLER:
            continue
        observed = {(group, resource, verb) for rule in item["rules"]
                    for group in rule["apiGroups"]
                    for resource in rule["resources"] for verb in rule["verbs"]}
        require(observed == required[item["metadata"]["name"]] and
                all("resourceNames" not in rule for rule in item["rules"]),
                "native runtime/customer RBAC changed from reviewed exact verbs")
    for item in rbac:
        if item["kind"] != "ClusterRoleBinding":
            continue
        name = item["metadata"]["name"]
        require(item["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                     "kind": "ClusterRole", "name": name} and
                item["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io",
                                       "kind": "User", "name": name}],
                "native User binding changed")


def image_contract(path: Path) -> dict:
    require(path.is_absolute() and path.is_file(),
            "absolute published OCI image evidence JSON is required")
    image = json.loads(path.read_text())
    require(image.get("image") == APPS_IMAGE,
            "published OCI index differs from exact runtime pin")
    platforms = {(entry["platform"]["os"], entry["platform"]["architecture"]): entry
                 for entry in image.get("platforms", [])}
    require(len(image.get("platforms", [])) == 2 and
            set(platforms) == {("linux", "amd64"), ("linux", "arm64")},
            "published Apps OCI index lacks exact Linux multiarch platform set")
    for entry in platforms.values():
        require(entry.get("revision") == APPS_BINARY_SHA and
                entry.get("source") == "https://github.com/datum-labs/apps" and
                entry.get("user") == "65532:65532" and
                entry.get("entrypoint") == ["/apps-controller"],
                "published Apps platform config labels/identity differ from source")
    for architecture, (manifest, config) in PLATFORM_DIGESTS.items():
        observed = platforms[("linux", architecture)]
        require(observed.get("manifest") == manifest and observed.get("config") == config,
                f"published {architecture} manifest/config differs from runtime image proof")
    return image


def validate_ports(args) -> None:
    ports = (args.milo_port, args.direct_milo_port, args.registry_port)
    require(len(set(ports)) == len(ports) and all(1 <= port <= 65535 for port in ports),
            "private port values are invalid or collide")
    for port in (args.direct_milo_port, args.registry_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


def assert_local_config(kube: Kube, expected_server: str | None) -> None:
    context = kube.k("config", "current-context")
    require(context == kube.context, "explicit kubeconfig current context changed")
    config = kube.json("config", "view", "--minify")
    clusters = config.get("clusters", [])
    require(len(clusters) == 1, "kubeconfig has no unique selected cluster")
    server = clusters[0]["cluster"]["server"]
    require(server == expected_server if expected_server else
            server.startswith("https://127.0.0.1:"),
            f"{kube.context} API is not the explicit private loopback server")
    wait_for(lambda: kube.k("get", "--raw", "/readyz") == "ok",
             f"{kube.context} API readyz", 60)


def absent(kube: Kube, resources: list[tuple[str, str, str | None]]) -> None:
    for kind, name, namespace in resources:
        require(kube.optional(kind, name, namespace) is None,
                f"refusing pre-existing {kube.context} {kind}/{name}")


def secret_bytes(kube: Kube, namespace: str, name: str, key_name: str) -> bytes:
    secret = kube.json("get", "secret", name, "-n", namespace)
    require(key_name in secret.get("data", {}), f"missing {name} public certificate field")
    return base64.b64decode(secret["data"][key_name], validate=True)


def node_images(node_items: list[dict], architecture: str) -> set[str]:
    require(architecture in PLATFORM_DIGESTS and len(node_items) == 1 and
            node_items[0]["status"]["nodeInfo"]["architecture"] == architecture and
            node_items[0]["status"]["nodeInfo"]["operatingSystem"] == "linux",
            f"exact Apps runtime requires one Linux/{architecture} Kind node")
    images = {name for image in node_items[0]["status"].get("images", [])
              for name in image.get("names", [])}
    require(APPS_IMAGE in images, "exact Apps OCI index not loaded on isolated Kind node")
    return images


def preflight(args, commands: Commands, contract: dict, fixtures: dict,
              private: Path) -> dict:
    private_file(args.host_kubeconfig)
    private_file(args.milo_kubeconfig)
    require(args.host_kubeconfig != args.milo_kubeconfig,
            "host and Milo kubeconfigs must be separate")
    require(args.host_context.startswith("kind-apps-a1t-"),
            "expected the isolated task-owned Kind context")
    validate_ports(args)
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    assert_local_config(host, None)
    assert_local_config(milo, f"https://127.0.0.1:{args.milo_port}")
    node_items = host.json("get", "nodes")["items"]
    images = node_images(node_items, args.expected_architecture)
    core = host.json("get", "kustomization", "milo-core", "-n", "flux-system")
    milo_source = host.json("get", "gitrepository", "milo", "-n", "flux-system")
    require(ready(core) and not core["spec"].get("suspend", False),
            "Milo core Flux is not Ready/unsuspended")
    require(ready(milo_source) and milo_source["spec"]["ref"] == {"commit": MILO_SHA} and
            milo_source["spec"]["url"] == "https://github.com/milo-os/milo.git" and
            core["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "milo"} and
            core["spec"]["path"] == "./config/overlays/test-infra" and
            core["spec"]["images"] == [{"name": "ghcr.io/milo-os/milo",
                                        "digest": MILO_IMAGE}] and
            core["status"].get("lastAppliedRevision", "").endswith(MILO_SHA),
            "Milo root source/overlay/image is not the reviewed exact baseline")
    auth = host.json("get", "secret", "milo-apiserver-auth-tokens", "-n", "milo-system")
    old_b64 = auth["data"]["tokens.csv"]
    rows = list(csv.reader(base64.b64decode(old_b64, validate=True).decode().splitlines()))
    identities = {(row[1], row[2], row[3]) for row in rows if len(row) == 4}
    require(len(rows) == 2 and identities == {
        ("admin", "admin", "system:masters"),
        ("test-user", "test-user", "system:authenticated")},
        "isolated Milo static auth differs from reviewed two-user baseline")
    ca = secret_bytes(host, "milo-system", "milo-ca-tls", "ca.crt")
    front = secret_bytes(host, "milo-system", "milo-front-proxy-client", "tls.crt")
    server = secret_bytes(host, "milo-system", "milo-apiserver-tls", "tls.crt")
    verify_cert(commands, private, ca, front, dns="milo-front-proxy-client")
    verify_cert(commands, private, ca, server, dns="milo-apiserver.milo-system.svc")
    verify_cert(commands, private, ca, server, ip="127.0.0.1")
    projects = {name: milo.json("get", "project", name) for name in PROJECTS}
    require(all(obj["metadata"].get("uid") and
                not obj["metadata"].get("deletionTimestamp") and
                any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in obj.get("status", {}).get("conditions", []))
                for obj in projects.values()),
            "three real Milo Projects must be Ready and active")
    all_projects = milo.json("get", "projects")["items"]
    all_project_uids = {item["metadata"]["name"]: item["metadata"]["uid"]
                        for item in all_projects}
    require(all_project_uids.keys() >= set(PROJECTS) and
            all_project_uids.keys() == {item["metadata"]["name"] for item in all_projects},
            "Milo Project inventory is incomplete/ambiguous")
    cert_controller = host.json("get", "deployment", "cert-manager", "-n", "cert-manager")
    require(cert_controller["status"].get("availableReplicas", 0) >= 1,
            "cert-manager issuer prerequisite unavailable")
    absent(host, [("namespace", "apps-system", None),
                  ("deployment", REGISTRY_NAME, "flux-system"),
                  ("service", REGISTRY_NAME, "flux-system"),
                  ("ocirepository", OCI_NAME, "flux-system"),
                  ("kustomization", FLUX_CRDS, "milo-system"),
                  ("kustomization", FLUX_HOST, "flux-system"),
                  ("kustomization", FLUX_ADMISSION, "milo-system"),
                  ("secret", INSTALLER_SECRET, "milo-system")])
    absent(host, [("clusterrole", name, None) for name in USERS] +
                 [("clusterrolebinding", name, None) for name in USERS])
    absent(milo, [("customresourcedefinition", name, None) for name in CRDS] +
                 [(kind, name, None) for kind, name in WCS] +
                 [("clusterrole", name, None) for name in USERS] +
                 [("clusterrolebinding", name, None) for name in USERS])
    require(not host.json("get", "pods", "-n", "flux-system", "-l",
                          "apps-a1t.datum.net/component=apps-native-registry")["items"],
            "registry-labeled Pods predate this run")
    return {"host": host, "milo": milo, "core": core, "auth": auth,
            "old_auth_b64": old_b64, "milo_ca": ca,
            "projects": all_project_uids,
            "node_image_names": sorted(images)}


def patch_auth(host: Kube, private: Path, snapshot: dict, before: str,
               after: str) -> None:
    current = host.json("get", "secret", "milo-apiserver-auth-tokens", "-n", "milo-system")
    require(current["metadata"]["uid"] == snapshot["auth"]["metadata"]["uid"] and
            current["data"]["tokens.csv"] == before,
            "Milo static auth changed concurrently; refusing overwrite")
    host.patch_json(private, "secret", "milo-apiserver-auth-tokens", [
        {"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion",
         "value": current["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/data/tokens.csv", "value": before},
        {"op": "replace", "path": "/data/tokens.csv", "value": after},
    ], "milo-system", sensitive=True)


def suspend_core(host: Kube, private: Path, snapshot: dict,
                 run_id: str, suspend: bool) -> None:
    current = host.json("get", "kustomization", "milo-core", "-n", "flux-system")
    original = snapshot["core"]
    meta = current["metadata"]
    marker = "apps-a1t.datum.net/suspend-run-id"
    marker_path = "/metadata/annotations/apps-a1t.datum.net~1suspend-run-id"
    require(meta["uid"] == original["metadata"]["uid"],
            "Milo core Flux UID changed")
    ops = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
           {"op": "test", "path": "/metadata/resourceVersion",
            "value": meta["resourceVersion"]},
           {"op": "test", "path": "/metadata/generation",
            "value": meta["generation"]}]
    if suspend:
        require(meta["generation"] == original["metadata"]["generation"] and
                current["spec"] == original["spec"] and
                meta.get("annotations", {}) == original["metadata"].get("annotations", {}) and
                not current["spec"].get("suspend", False),
                "Milo core Flux changed before suspension")
        if "annotations" not in meta:
            ops.append({"op": "add", "path": "/metadata/annotations", "value": {}})
        ops.extend(({"op": "add", "path": marker_path, "value": run_id},
                    {"op": "add", "path": "/spec/suspend", "value": True}))
    else:
        expected = dict(original["spec"])
        expected["suspend"] = True
        annotations = dict(original["metadata"].get("annotations", {}))
        annotations[marker] = run_id
        require(current["spec"] == expected and
                meta["generation"] == original["metadata"]["generation"] + 1 and
                meta.get("annotations", {}) == annotations,
                "Milo core Flux changed while suspended; refusing restore")
        ops.extend(({"op": "test", "path": marker_path, "value": run_id},
                    {"op": "remove", "path": marker_path},
                    {"op": "test", "path": "/spec/suspend", "value": True},
                    {"op": "remove" if "suspend" not in original["spec"] else "replace",
                     "path": "/spec/suspend", **({} if "suspend" not in original["spec"]
                     else {"value": False})}))
        if "annotations" not in original["metadata"]:
            ops[-3] = {"op": "remove", "path": "/metadata/annotations"}
    host.patch_json(private, "kustomization", "milo-core", ops, "flux-system")


def restart_milo(host: Kube) -> None:
    host.k("rollout", "restart", "deployment/milo-apiserver", "-n", "milo-system")
    host.k("rollout", "status", "deployment/milo-apiserver", "-n", "milo-system",
           "--timeout=180s", timeout=190)


def token_csv(old_b64: str, tokens: dict[str, str]) -> str:
    original = base64.b64decode(old_b64, validate=True).decode()
    suffix = "" if original.endswith("\n") else "\n"
    extra = "".join(f"{token},{user},{user},system:authenticated\n"
                    for user, token in tokens.items())
    return base64.b64encode((original + suffix + extra).encode()).decode()


def owned(kube: Kube, namespace: str | None, manifest: dict,
          plural: str, api_prefix: str) -> Owned:
    name = manifest["metadata"]["name"]
    path = (f"{api_prefix}/namespaces/{namespace}/{plural}/{name}" if namespace else
            f"{api_prefix}/{plural}/{name}")
    return Owned(kube, namespace, manifest["kind"].lower(), name, path)


def publish_artifact(commands: Commands, args, private: Path,
                     expected_hash: str, count: int) -> str:
    staged = private / "artifact"
    shutil.copytree(args.apps_source / "config", staged / "config", symlinks=False)
    digest, copied_count = tree_fingerprint(staged / "config")
    require(digest == expected_hash and copied_count == count,
            "Apps config changed during private OCI packaging")
    tag = f"oci://127.0.0.1:{args.registry_port}/apps-native:{args.apps_revision}"
    response = json.loads(commands.run("flux", "push", "artifact", tag,
                                      "--path", str(staged),
                                      "--source", "https://github.com/datum-labs/apps.git",
                                      "--revision", f"git@{args.apps_revision}",
                                      "--insecure-registry", "--ignore-paths=.git/",
                                      "--output", "json", timeout=120))
    digest = response.get("digest", "")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
            "private OCI publication did not return exact sha256 digest")
    pulled = private / "pulled"
    pulled.mkdir(mode=0o700)
    commands.run("flux", "pull", "artifact",
                 f"oci://127.0.0.1:{args.registry_port}/apps-native@{digest}",
                 "--output", str(pulled), "--insecure-registry", timeout=120)
    require(sorted(path.name for path in pulled.iterdir()) == ["config"] and
            tree_fingerprint(pulled / "config") == (expected_hash, count),
            "digest-pulled OCI artifact differs from clean source config")
    return digest


def flux_cluster(kube: Kube, *args: str, timeout: float = 30) -> str:
    return kube.commands.run("flux", "--kubeconfig", str(kube.config),
                             "--context", kube.context, *args, timeout=timeout)


def flux_reconcile(kube: Kube, kind: str, name: str,
                   namespace: str, seconds: int) -> None:
    flux_cluster(kube, "reconcile", kind, name, "-n", namespace,
                 "--with-source", f"--timeout={seconds}s", timeout=seconds + 10)


def exact_flux(kube: Kube, name: str, namespace: str, digest: str) -> dict:
    obj = kube.json("get", "kustomization", name, "-n", namespace)
    applied = obj.get("status", {}).get("lastAppliedRevision", "")
    require(ready(obj) and applied == digest,
            f"Flux {name} not Ready at exact artifact digest")
    return obj


def milo_request(port: int, ca_file: Path, token: str, method: str,
                 path: str, body: dict | None = None) -> tuple[int, dict | str]:
    ctx = ssl.create_default_context(cafile=str(ca_file))
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=payload,
                           headers={"Authorization": "Bearer " + token,
                                    "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(65537)
        require(len(raw) <= 65536, "Milo public API response exceeded bounded diagnostic size")
        return response.status, (json.loads(raw) if raw.startswith(b"{") else
                                 raw.decode(errors="replace"))
    finally:
        connection.close()


def must_http(port: int, ca: Path, token: str, method: str,
              path: str, expected: int, body: dict | None = None) -> dict | str:
    status, value = milo_request(port, ca, token, method, path, body)
    require(status == expected,
            f"Milo {method} {path.split('?')[0]} returned HTTP {status}, expected {expected}")
    return value


def authorization_probes(configs: dict[str, Path], commands: Commands,
                         *, author_enabled: bool) -> None:
    matrix = {
        DISCOVERY: [("get", "projects.resourcemanager.miloapis.com", True),
                    ("list", "projects.resourcemanager.miloapis.com", True),
                    ("create", "projects.resourcemanager.miloapis.com", False),
                    ("get", "secrets", False), ("create", "clusterroles", False),
                    ("impersonate", "users", False)],
        PROJECT: [("get", "apptemplates.apps.miloapis.com", True),
                  ("patch", "apptemplates.apps.miloapis.com", True),
                  ("update", "apptemplates.apps.miloapis.com/status", True),
                  ("create", "apptemplaterevisions.apps.miloapis.com", True),
                  ("update", "apptemplaterevisions.apps.miloapis.com", False),
                  ("get", "secrets", False), ("create", "clusterroles", False),
                  ("impersonate", "users", False)],
        AUTHOR: [("create", "apptemplates.apps.miloapis.com", author_enabled),
                 ("update", "apptemplates.apps.miloapis.com", author_enabled),
                 ("delete", "apptemplates.apps.miloapis.com", author_enabled),
                 ("create", "apptemplaterevisions.apps.miloapis.com", False),
                 ("update", "apptemplates.apps.miloapis.com/status", False),
                 ("get", "secrets", False), ("create", "clusterroles", False),
                 ("impersonate", "users", False)],
        INSTALLER: [("create", "customresourcedefinitions.apiextensions.k8s.io", True),
                    ("create", "validatingwebhookconfigurations.admissionregistration.k8s.io", True),
                    ("create", "mutatingwebhookconfigurations.admissionregistration.k8s.io", True),
                    ("create", "apptemplates.apps.miloapis.com", False),
                    ("get", "secrets", False), ("create", "clusterroles", False),
                    ("impersonate", "users", False)],
    }
    for user, probes in matrix.items():
        scoped = Kube(commands, configs[user], "scoped")
        for verb, resource, permitted in probes:
            scoped.can_i(verb, resource, permitted)


def source_role_guards(fixtures: dict) -> None:
    require("sha256:" + hashlib.sha256(PRECISION_CANONICAL.encode()).hexdigest() ==
            PRECISION_DIGEST, "four-value opaque numeric fixture digest changed")
    role = next(obj for obj in fixtures["milo-rbac"] if obj["kind"] == "ClusterRole"
                and obj["metadata"]["name"] == INSTALLER)
    grants = {(tuple(rule["apiGroups"]), tuple(rule["resources"]),
               tuple(rule.get("resourceNames", [])), tuple(rule["verbs"]))
              for rule in role["rules"]}
    expected = {
        (("apiextensions.k8s.io",), ("customresourcedefinitions",), (), ("create",)),
        (("apiextensions.k8s.io",), ("customresourcedefinitions",), CRDS,
         ("get", "patch", "delete")),
        (("admissionregistration.k8s.io",), ("mutatingwebhookconfigurations",), (),
         ("create",)),
        (("admissionregistration.k8s.io",), ("mutatingwebhookconfigurations",),
         (WCS[0][1],), ("get", "patch", "delete")),
        (("admissionregistration.k8s.io",), ("validatingwebhookconfigurations",), (),
         ("create",)),
        (("admissionregistration.k8s.io",), ("validatingwebhookconfigurations",),
         (WCS[1][1],), ("get", "patch", "delete")),
    }
    require(grants == expected, "temporary installer grants changed from exact native resources")


def check_crds(milo: Kube, source: dict) -> dict[str, str]:
    rendered = {item["metadata"]["name"]: item for item in source["crds"]}
    schema_hashes = {}
    for name in CRDS:
        live = milo.json("get", "customresourcedefinition", name)
        wanted = rendered[name]["spec"]
        source_schemas = [version["schema"] for version in wanted["versions"]]
        live_schemas = [version["schema"] for version in live["spec"]["versions"]]
        source_bytes = json.dumps(source_schemas, sort_keys=True,
                                  separators=(",", ":")).encode()
        live_bytes = json.dumps(live_schemas, sort_keys=True,
                                separators=(",", ":")).encode()
        require(any(c.get("type") == "Established" and c.get("status") == "True"
                    for c in live.get("status", {}).get("conditions", [])) and
                live["spec"]["group"] == wanted["group"] and
                live["spec"]["scope"] == wanted["scope"] and
                live["spec"]["names"] == wanted["names"] and
                source_bytes == live_bytes and
                live["metadata"].get("annotations", {}).get(
                    "discovery.miloapis.com/parent-contexts") == "Project",
                f"native CRD {name} is not Established with exact source schema")
        schema_hashes[name] = hashlib.sha256(live_bytes).hexdigest()
    return schema_hashes


def project_path(project: str, resource: str, name: str = "") -> str:
    result = f"{PROJECT_API}/{project}/control-plane{APPS_API}/{resource}"
    return result + ("/" + quote(name, safe="") if name else "")


def template_object(name: str, description: str) -> dict:
    canonical = json.dumps({"description": description,
                            "inputSchema": {"type": "object", "properties": {},
                                            "additionalProperties": False}},
                           sort_keys=True, separators=(",", ":"))
    return {"apiVersion": "apps.miloapis.com/v1alpha1", "kind": "AppTemplate",
            "metadata": {"name": name},
            "spec": {"definitionJSON": canonical,
                     "digest": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()}}


def precision_object() -> dict:
    return {"apiVersion": "apps.miloapis.com/v1alpha1", "kind": "AppTemplate",
            "metadata": {"name": PRECISION_NAME},
            "spec": {"definitionJSON": PRECISION_CANONICAL,
                     "digest": PRECISION_DIGEST}}


def revision_list(port: int, ca: Path, token: str, project: str) -> list[dict]:
    value = must_http(port, ca, token, "GET", project_path(project,
                                                           "apptemplaterevisions"), 200)
    require(isinstance(value, dict) and isinstance(value.get("items"), list),
            "Milo revision List did not return native resource items")
    return value["items"]


def wait_template(port: int, ca: Path, token: str, project: str,
                  name: str, sequence: int, seconds: float = 120) -> dict:
    def acknowledged():
        status, value = milo_request(port, ca, token, "GET",
                                     project_path(project, "apptemplates", name))
        if status != 200 or not isinstance(value, dict):
            return False
        annotation = value["metadata"].get("annotations", {}).get(
            "apps.miloapis.com/change-generation")
        return value if annotation and value.get("status", {}).get(
            "observedGeneration") == int(annotation) and value["status"].get(
                "latestSequence") == sequence and value["status"].get("latestRevision") else False
    return wait_for(acknowledged, f"real {project} template history sequence {sequence}", seconds)


def pod_snapshot(host: Kube, architecture: str) -> dict:
    require(architecture in PLATFORM_DIGESTS, "unsupported exact Apps platform")
    manifest, config = PLATFORM_DIGESTS[architecture]
    deployment = host.json("get", "deployment", "apps-controller", "-n", "apps-system")
    pods = host.json("get", "pods", "-n", "apps-system", "-l",
                     "app.kubernetes.io/name=apps-controller")["items"]
    require(deployment["spec"].get("replicas") == 2 and
            deployment["status"].get("availableReplicas", 0) == 2 and len(pods) == 2,
            "two native Apps controller replicas are not Available")
    statuses = []
    for pod in pods:
        require(pod["status"].get("phase") == "Running" and
                any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod["status"].get("conditions", [])),
                "native Apps controller Pod is not Ready")
        observed = pod["status"].get("containerStatuses", [])
        require(len(observed) == 1 and observed[0].get("ready") is True and
                observed[0].get("imageID"), "native Apps image ID/readiness missing")
        statuses.append({"uid": pod["metadata"]["uid"],
                         "image_id": observed[0]["imageID"]})
    require({pod["image_id"] for pod in statuses} <= {
        "docker-pullable://" + APPS_IMAGE, APPS_IMAGE,
        "docker-pullable://ghcr.io/datum-labs/apps@" + manifest,
        "ghcr.io/datum-labs/apps@" + manifest,
        "docker://" + config},
        f"running Apps image IDs do not map to verified OCI index/{architecture} manifest/config")
    return {"deployment_uid": deployment["metadata"]["uid"],
            "pods": sorted(statuses, key=lambda item: item["uid"])}


def assert_admission(milo: Kube, source: dict, ca: bytes) -> None:
    expected = {key(item): item for item in source["admission"]}
    encoded = base64.b64encode(ca).decode()
    for kind, name in WCS:
        live = milo.json("get", kind, name)
        original = expected[("MutatingWebhookConfiguration" if kind.startswith("mutating")
                             else "ValidatingWebhookConfiguration", "", name)]
        require(len(live["webhooks"]) == len(original["webhooks"]),
                f"source webhook count changed in {name}")
        for observed, declared in zip(live["webhooks"], original["webhooks"]):
            copy = json.loads(json.dumps(observed))
            for selector in ("namespaceSelector", "objectSelector"):
                if selector not in declared and copy.get(selector) == {}:
                    copy.pop(selector)
            require(copy["clientConfig"].pop("caBundle", None) == encoded and
                    copy == declared,
                    f"root Milo WC {name} differs from source rules plus verified CA")


def check_discovery(port: int, ca: Path, token: str) -> dict:
    for project in PROJECTS[1:]:
        path = f"{PROJECT_API}/{project}/control-plane{APPS_API}"
        def discovered():
            status, response = milo_request(port, ca, token, "GET", path)
            return response if status == 200 else False
        value = wait_for(discovered, f"native Apps discovery in {project}", 120)
        resources = {item.get("name") for item in value.get("resources", [])}
        require({"apptemplates", "apptemplaterevisions"} <= resources,
                f"Apps CRDs not discovered through real Project {project}")
    root_status, root_value = milo_request(port, ca, token, "GET", APPS_API)
    # Milo's Project annotation adds scoped discovery; it does not hide root
    # discovery or grant write authority. post_admission tests root writes.
    require(root_status in (200, 403, 404), "unexpected root discovery response")
    return {"root_status": root_status,
            "root_resource_names": sorted(item.get("name", "") for item in
                root_value.get("resources", [])) if isinstance(root_value, dict) else []}



def compare_history(first: dict, second: dict) -> None:
    a = json.loads(json.dumps(first))
    b = json.loads(json.dumps(second))
    for obj in (a, b):
        obj.get("metadata", {}).pop("resourceVersion", None)
        obj.get("metadata", {}).pop("managedFields", None)
    require(a == b, "immutable revision content changed across Apps replica rollout")


def test_authoring(args, state: dict, tokens: dict, ca_file: Path,
                   lease: Owned, host_proxy: APIProxy, record) -> dict:
    port = args.direct_milo_port
    author = tokens[AUTHOR]
    project = PROJECTS[1]
    path = project_path(project, "apptemplates")
    created = must_http(port, ca_file, author, "POST", path, 201,
                        template_object(TEMPLATE_NAME, "native-one"))
    state["allowed_template_uids"][created["metadata"]["uid"]] = TEMPLATE_NAME
    require(created["metadata"].get("uid") and
            created["metadata"].get("annotations", {}).get(
                "apps.miloapis.com/change-actor-uid") == AUTHOR and
            created["metadata"]["annotations"].get(
                "apps.miloapis.com/project-uid") == state["projects"][project] and
            created["metadata"]["annotations"].get(
                "apps.miloapis.com/change-generation") == "1" and
            "apps.miloapis.com/template-history" in created["metadata"].get("finalizers", []),
            "native admission did not stamp authenticated actor, Project UID or history finalizer")
    current = must_http(port, ca_file, author, "GET",
                        project_path(project, "apptemplates", TEMPLATE_NAME), 200)
    require(current.get("status", {}).get("observedGeneration", 0) == 0 and
            not current.get("status", {}).get("latestRevision") and
            len(revision_list(port, ca_file, author, project)) == 0,
            "held leader Lease did not keep a deterministic pending revision")
    edited = json.loads(json.dumps(current))
    edited["spec"] = template_object(TEMPLATE_NAME, "overwritten-pending")["spec"]
    status, denial = milo_request(port, ca_file, author, "PUT",
                                  project_path(project, "apptemplates", TEMPLATE_NAME), edited)
    require(status == 403 and isinstance(denial, dict) and
            "pending template revision" in denial.get("message", ""),
            "admission did not reject second fresh-RV spec edit while history pending")
    require(must_http(port, ca_file, author, "GET",
                      project_path(project, "apptemplates", TEMPLATE_NAME), 200) == current,
            "denied pending edit changed the persisted template")
    record("held Lease kept one admitted pending edit; validator denied a second spec edit")
    lease.delete(host_proxy, state["run_id"])
    accepted = wait_template(port, ca_file, author, project, TEMPLATE_NAME, 1)
    revisions = revision_list(port, ca_file, author, project)
    require(len(revisions) == 1 and revisions[0]["metadata"]["name"] ==
            accepted["status"]["latestRevision"] and
            revisions[0]["spec"]["templateUID"] == created["metadata"]["uid"] and
            revisions[0]["spec"]["projectUID"] == state["projects"][project] and
            revisions[0]["spec"]["effectiveActorUID"] == AUTHOR and
            revisions[0]["spec"]["sequence"] == 1 and
            not revisions[0]["metadata"].get("ownerReferences"),
            "native controller failed to persist exact immutable first history")
    record("released Lease; native controller wrote one immutable revision then status")
    # A mutating admission write with identical semantic JSON cannot append history.
    noop = json.loads(json.dumps(accepted))
    noop["spec"]["definitionJSON"] = (
        '{ "inputSchema": { "properties": {}, "additionalProperties": false, '
        '"type": "object" }, "description": "native-one" }')
    must_http(port, ca_file, author, "PUT",
              project_path(project, "apptemplates", TEMPLATE_NAME), 200, noop)
    after_noop = must_http(port, ca_file, author, "GET",
                           project_path(project, "apptemplates", TEMPLATE_NAME), 200)
    require(after_noop["metadata"]["generation"] == accepted["metadata"]["generation"] and
            after_noop["metadata"]["annotations"]["apps.miloapis.com/change-generation"] ==
            accepted["metadata"]["annotations"]["apps.miloapis.com/change-generation"] and
            len(revision_list(port, ca_file, author, project)) == 1,
            "semantic no-op advanced native history")
    # The same name in another Project must not read this Project's object/history.
    other = PROJECTS[2]
    must_http(port, ca_file, author, "GET",
              project_path(other, "apptemplates", TEMPLATE_NAME), 404)
    require(not revision_list(port, ca_file, author, other),
            "native Project-path history leaked to the second Project")
    return {"template_uid": created["metadata"]["uid"],
            "first_revision": revisions[0], "accepted": after_noop}


def bad_ca_probe(args, milo: Kube, private: Path, tokens: dict,
                 ca_file: Path, true_ca: bytes, false_ca: bytes) -> None:
    kind, name = WCS[1]
    original = milo.json("get", kind, name)
    meta = original["metadata"]
    true_b64 = base64.b64encode(true_ca).decode()
    false_b64 = base64.b64encode(false_ca).decode()
    require(true_b64 != false_b64 and all(
        hook["clientConfig"].get("caBundle") == true_b64
        for hook in original["webhooks"]),
        "root admission CA is absent/changed before negative TLS probe")
    modified = False
    try:
        ops = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
               {"op": "test", "path": "/metadata/resourceVersion",
                "value": meta["resourceVersion"]}]
        for index in range(len(original["webhooks"])):
            path = f"/webhooks/{index}/clientConfig/caBundle"
            ops.extend(({"op": "test", "path": path, "value": true_b64},
                        {"op": "replace", "path": path, "value": false_b64}))
        milo.patch_json(private, kind, name, ops)
        modified = True
        dryrun = project_path(PROJECTS[1], "apptemplates") + "?dryRun=All"
        status, response = milo_request(args.direct_milo_port, ca_file, tokens[AUTHOR],
                                        "POST", dryrun,
                                        template_object(TEMPLATE_NAME + "-bad-ca", "tls-denial"))
        message = response.get("message", "") if isinstance(response, dict) else str(response)
        require(status == 500 and "x509" in message.lower() and
                "validate-templates.apps.miloapis.com" in message,
                "wrong public CA did not fail closed as the named webhook TLS validation")
    finally:
        if modified:
            current = milo.json("get", kind, name)
            require(current["metadata"]["uid"] == meta["uid"] and
                    all(hook["clientConfig"].get("caBundle") == false_b64
                        for hook in current["webhooks"]),
                    "CA probe changed concurrently; refusing unsafe restoration")
            ops = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                   {"op": "test", "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"]}]
            for index in range(len(current["webhooks"])):
                path = f"/webhooks/{index}/clientConfig/caBundle"
                ops.extend(({"op": "test", "path": path, "value": false_b64},
                            {"op": "replace", "path": path, "value": true_b64}))
            milo.patch_json(private, kind, name, ops)
            restored = milo.json("get", kind, name)
            require(restored["metadata"]["uid"] == meta["uid"] and
                    all(hook["clientConfig"].get("caBundle") == true_b64
                        for hook in restored["webhooks"]),
                    "root admission CA did not restore exactly")
    must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
              project_path(PROJECTS[1], "apptemplates", TEMPLATE_NAME + "-bad-ca"), 404)


def post_admission(args, state: dict, tokens: dict, ca_file: Path,
                   host: Kube, first: dict, record) -> dict:
    port, author, project = args.direct_milo_port, tokens[AUTHOR], PROJECTS[1]
    name = TEMPLATE_NAME
    status_path = project_path(project, "apptemplates", name) + "/status"
    forged = json.loads(json.dumps(first["accepted"]))
    forged["status"]["latestRevision"] = "forged"
    status, _ = milo_request(port, ca_file, author, "PUT", status_path, forged)
    require(status == 403, "customer identity could write Apps status")
    revision = first["first_revision"]
    status, _ = milo_request(port, ca_file, author, "POST",
                             project_path(project, "apptemplaterevisions"), revision)
    require(status == 403, "customer identity could forge revision Create")
    root_status, _ = milo_request(port, ca_file, author, "POST",
                                  APPS_API + "/apptemplates",
                                  template_object(name + "-root", "root-forgery"))
    require(root_status in (403, 404), "customer root authoring write unexpectedly succeeded")
    record("customer cannot write status/history or bypass Project routing at Milo root")
    next_template = json.loads(json.dumps(first["accepted"]))
    next_template["spec"] = template_object(TEMPLATE_NAME, "native-two")["spec"]
    must_http(port, ca_file, author, "PUT",
              project_path(project, "apptemplates", name), 200, next_template)
    accepted = wait_template(port, ca_file, author, project, name, 2)
    revisions = sorted(revision_list(port, ca_file, author, project),
                       key=lambda item: item["metadata"]["name"])
    require(len(revisions) == 2 and
            {item["spec"]["sequence"] for item in revisions} == {1, 2} and
            accepted["status"]["latestRevision"] in {
                item["metadata"]["name"] for item in revisions},
            "second real spec edit did not append exactly one immutable revision")
    before_pods = pod_snapshot(host, args.expected_architecture)
    before_template = must_http(port, ca_file, author, "GET",
                                project_path(project, "apptemplates", name), 200)
    host.k("rollout", "restart", "deployment/apps-controller", "-n", "apps-system")
    host.k("rollout", "status", "deployment/apps-controller", "-n", "apps-system",
           "--timeout=180s", timeout=190)
    def replaced_pods():
        observed = pod_snapshot(host, args.expected_architecture)
        old_uids = {item["uid"] for item in before_pods["pods"]}
        new_uids = {item["uid"] for item in observed["pods"]}
        return observed if old_uids.isdisjoint(new_uids) else False
    after_pods = wait_for(replaced_pods, "two replacement Apps replicas", 120)
    after_template = must_http(port, ca_file, author, "GET",
                               project_path(project, "apptemplates", name), 200)
    after_revisions = sorted(revision_list(port, ca_file, author, project),
                             key=lambda item: item["metadata"]["name"])
    compare_history(before_template, after_template)
    require(len(after_revisions) == len(revisions),
            "revision count changed across Apps replica restart")
    for before, after in zip(revisions, after_revisions):
        compare_history(before, after)
    record("two replacement native manager replicas retained full template and immutable history")
    return {"before_pods": before_pods, "after_pods": after_pods,
            "template": after_template, "revisions": after_revisions}


def delete_history_test(args, state: dict, tokens: dict, ca_file: Path, prior: dict,
                        record, cli: AppsCLI | None = None) -> dict:
    port, author, project = args.direct_milo_port, tokens[AUTHOR], PROJECTS[1]
    path = project_path(project, "apptemplates", TEMPLATE_NAME)
    old_uid = prior["template"]["metadata"]["uid"]
    old_revisions = {item["metadata"]["name"] for item in prior["revisions"]}
    if cli is None:
        delete_options = {"apiVersion": "v1", "kind": "DeleteOptions",
                          "preconditions": {"uid": old_uid,
                                            "resourceVersion": prior["template"]["metadata"][
                                                "resourceVersion"]}}
        status, _ = milo_request(port, ca_file, author, "DELETE", path, delete_options)
        require(status in (200, 202), "native template UID/RV deletion failed")
        wait_for(lambda: milo_request(port, ca_file, author, "GET", path)[0] == 404,
                 "history-protected template deletion", 120)
    else:
        def snapshot():
            status, value = milo_request(port, ca_file, author, "GET", path)
            require(status in (200, 404), "CLI snapshot template GET failed")
            return {"template": value if status == 200 else None,
                    "revisions": revision_list(port, ca_file, author, project)}
        def wait_deleted():
            wait_for(lambda: milo_request(port, ca_file, author, "GET", path)[0] == 404,
                     "CLI history-protected template deletion", 120)
        result = delete_with_cli(cli, snapshot, wait_deleted)
        require(result["template_uid"] == old_uid and
                set(result["retained_revision_names"]) == old_revisions,
                "CLI deletion changed exact expected native history")
    retained = revision_list(port, ca_file, author, project)
    require({item["metadata"]["name"] for item in retained} == old_revisions,
            "template deletion erased immutable history")
    replacement = must_http(port, ca_file, author, "POST",
                            project_path(project, "apptemplates"), 201,
                            template_object(TEMPLATE_NAME, "replacement-uid"))
    state["allowed_template_uids"][replacement["metadata"]["uid"]] = TEMPLATE_NAME
    require(replacement["metadata"]["uid"] != old_uid,
            "same-name replacement did not receive a fresh API UID")
    accepted = wait_template(port, ca_file, author, project, TEMPLATE_NAME, 1)
    after = revision_list(port, ca_file, author, project)
    require(len(after) == len(retained) + 1 and
            old_revisions < {item["metadata"]["name"] for item in after} and
            accepted["status"]["latestRevision"] not in old_revisions and
            next(item for item in after if item["metadata"]["name"] ==
                 accepted["status"]["latestRevision"])["spec"]["templateUID"] ==
            replacement["metadata"]["uid"],
            "same-name replacement reused the prior immutable history chain")
    current = must_http(port, ca_file, author, "GET", path, 200)
    opts = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
        "uid": current["metadata"]["uid"],
        "resourceVersion": current["metadata"]["resourceVersion"]}}
    status, _ = milo_request(port, ca_file, author, "DELETE", path, opts)
    require(status in (200, 202), "replacement UID/RV deletion failed")
    wait_for(lambda: milo_request(port, ca_file, author, "GET", path)[0] == 404,
             "replacement template deletion", 120)
    require({item["metadata"]["name"] for item in revision_list(
        port, ca_file, author, project)} == {item["metadata"]["name"] for item in after},
            "replacement deletion did not retain both history chains")
    record("deletion retained immutable history; same name with new UID made a separate chain")
    return {"original_uid": old_uid, "replacement_uid": replacement["metadata"]["uid"],
            "revision_names": sorted(item["metadata"]["name"] for item in after)}


def precision_test(args, state: dict, tokens: dict, ca_file: Path, record) -> dict:
    port, author, project = args.direct_milo_port, tokens[AUTHOR], PROJECTS[1]
    path = project_path(project, "apptemplates")
    invalid = precision_object()
    invalid["spec"]["digest"] = "sha256:" + "0" * 64
    status, denied = milo_request(port, ca_file, author, "POST", path, invalid)
    message = denied.get("message", "") if isinstance(denied, dict) else str(denied)
    require(status == 403 and "digest" in message.lower(),
            "native admission did not reject an otherwise valid opaque definition digest mismatch")
    must_http(port, ca_file, author, "GET",
              project_path(project, "apptemplates", PRECISION_NAME), 404)
    fixture = precision_object()
    created = must_http(port, ca_file, author, "POST", path, 201, fixture)
    uid = created["metadata"]["uid"]
    state["allowed_template_uids"][uid] = PRECISION_NAME
    accepted = wait_template(port, ca_file, author, project, PRECISION_NAME, 1)
    revision = must_http(port, ca_file, author, "GET",
                         project_path(project, "apptemplaterevisions",
                                      accepted["status"]["latestRevision"]), 200)
    expected = fixture["spec"]
    require(created["spec"] == expected and accepted["spec"] == expected and
            revision["spec"]["definitionJSON"] == PRECISION_CANONICAL and
            revision["spec"]["digest"] == expected["digest"] and
            revision["spec"]["templateUID"] == uid,
            "real Milo API or native history changed one of four exact numeric values/digest")
    opts = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
        "uid": uid, "resourceVersion": accepted["metadata"]["resourceVersion"]}}
    delete_status, _ = milo_request(port, ca_file, author, "DELETE",
                                    project_path(project, "apptemplates", PRECISION_NAME), opts)
    require(delete_status in (200, 202), "precision template UID/RV deletion failed")
    wait_for(lambda: milo_request(port, ca_file, author, "GET",
                                  project_path(project, "apptemplates", PRECISION_NAME))[0] == 404,
             "precision template deletion", 120)
    retained = must_http(port, ca_file, author, "GET",
                         project_path(project, "apptemplaterevisions",
                                      revision["metadata"]["name"]), 200)
    require(retained["spec"] == revision["spec"],
            "four-value precision history was lost after template deletion")
    record("digest mismatch denied; four exact numeric strings/digest survived Milo API and history")
    return {"template_uid": uid, "revision_name": revision["metadata"]["name"],
            "digest": expected["digest"]}


def register_owned(refs: dict[str, list[Owned]], group: str, kube: Kube,
                   manifest: dict, namespace: str | None, run_id: str,
                   plural: str, prefix: str, *, sensitive: bool = False) -> dict:
    ref = owned(kube, namespace, manifest, plural, prefix)
    refs.setdefault(group, []).append(ref)
    return ref.create(manifest, run_id, sensitive=sensitive)


def delete_group(refs: dict[str, list[Owned]], group: str,
                 proxy: APIProxy, run_id: str) -> None:
    for ref in reversed(refs.get(group, [])):
        ref.delete(proxy, run_id)


def stage_prune_gate(refs: dict[str, list[Owned]], group: str,
                     host: Kube, proxy: APIProxy, run_id: str,
                     name: str, namespace: str) -> None:
    delete_group(refs, group, proxy, run_id)
    require(host.optional("kustomization", name, namespace) is None,
            f"Flux Kustomization/{name} still exists and may recreate resources")


def resource_empty(proxy: APIProxy, project: str, resource: str) -> list[dict]:
    value = proxy.get(project_path(project, resource))
    require(isinstance(value.get("items"), list),
            f"{project} {resource} list unavailable during cleanup")
    return value["items"]


def census_owned_data(milo_proxy: APIProxy, initial_projects: dict[str, str],
                      allowed_template_uids: dict[str, str]) -> dict[str, dict[str, list[dict]]]:
    """Read-only ownership census; failure must precede webhook/manager teardown."""
    projects = milo_proxy.get(PROJECT_API)["items"]
    actual = {item["metadata"]["name"]: item["metadata"]["uid"] for item in projects}
    require(actual == initial_projects,
            "Project set/UID changed; refusing destructive Apps CRD cleanup")
    census: dict[str, dict[str, list[dict]]] = {}
    for project in initial_projects:
        templates = resource_empty(milo_proxy, project, "apptemplates")
        revisions = resource_empty(milo_proxy, project, "apptemplaterevisions")
        require(all(allowed_template_uids.get(obj["metadata"]["uid"]) ==
                    obj["metadata"]["name"]
                    for obj in templates),
                f"foreign AppTemplate present in {project}; retaining CRDs")
        require(all(allowed_template_uids.get(obj["spec"].get("templateUID")) ==
                    obj["spec"].get("templateName") and
                    obj["spec"].get("projectUID") == initial_projects[project]
                    for obj in revisions),
                f"foreign AppTemplateRevision present in {project}; retaining CRDs")
        require(all(obj["metadata"].get("uid") and
                    obj["metadata"].get("resourceVersion") for obj in
                    templates + revisions),
                f"Apps authoring data in {project} lacks UID/RV for safe cleanup")
        census[project] = {"templates": templates, "revisions": revisions}
    return census


def remove_owned_data(milo_proxy: APIProxy, initial_projects: dict[str, str],
                      allowed_template_uids: dict[str, str]) -> None:
    # Recheck immediately before destructive operations. This Kubernetes API
    # census cannot make concurrent writes atomic; UID/RV DELETEs still fence
    # each object, and any newly discovered foreign object aborts cleanup.
    census = census_owned_data(milo_proxy, initial_projects, allowed_template_uids)
    for project, found in census.items():
        templates = found["templates"]
        revisions = found["revisions"]
        for obj in templates:
            meta = obj["metadata"]
            milo_proxy.delete(project_path(project, "apptemplates", meta["name"]),
                              meta["uid"], meta["resourceVersion"])
        wait_for(lambda: not resource_empty(milo_proxy, project, "apptemplates"),
                 f"owned templates absent in {project}", 90)
        for obj in revisions:
            meta = obj["metadata"]
            milo_proxy.delete(project_path(project, "apptemplaterevisions", meta["name"]),
                              meta["uid"], meta["resourceVersion"])
        wait_for(lambda: not resource_empty(milo_proxy, project, "apptemplaterevisions"),
                 f"owned revisions absent in {project}", 90)
    final = census_owned_data(milo_proxy, initial_projects, allowed_template_uids)
    require(all(not entry["templates"] and not entry["revisions"]
                for entry in final.values()),
            "authoring objects appeared during UID/RV cleanup; retaining CRDs")


def cleanup(args, commands: Commands, state: dict, refs: dict[str, list[Owned]],
            host_proxy: APIProxy, milo_proxy: APIProxy,
            forwards: list[Forward], evidence: dict, evidence_file: Path) -> list[str]:
    errors: list[str] = []
    commands.deadline = time.monotonic() + 600
    host, milo = state["host"], state["milo"]

    def attempt(label, fn) -> bool:
        try:
            fn()
            evidence.setdefault("cleanup_actions", []).append(label)
            return True
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            return False

    try:
        # Refuse to disable fail-closed admission while any project contains
        # data that is not unquestionably this run's. Preserve the serving
        # manager/CA/credentials for manual recovery on ambiguous inventory.
        if state.get("crds_installed") and not attempt(
                "read-only ownership and Project UID census before WC teardown",
                lambda: census_owned_data(milo_proxy, state["projects"],
                                          state["allowed_template_uids"])):
            errors.append("retaining root WCs, manager, CA and credentials after foreign/ambiguous data census")
            return errors
        # Root registration must disappear before any serving certificate or
        # manager can be removed. A partial Flux apply may have no inventory.
        if not attempt("delete root WC Flux stage and verify absent", lambda:
                       stage_prune_gate(refs, "admission", host, host_proxy,
                                        state["run_id"], FLUX_ADMISSION, "milo-system")):
            errors.append("retaining manager, CA and credentials while WC Flux stage may recreate WCs")
            return errors
        wc_absent = attempt("verify both root WCs absent", lambda: absent(
            milo, [(kind, name, None) for kind, name in WCS]))
        if not wc_absent:
            errors.append("retaining manager, CA, scoped auth and Flux after active WC cleanup failure")
            return errors
        if not attempt("revoke dedicated author binding after WC removal", lambda:
                       delete_group(refs, "author-binding", milo_proxy, state["run_id"])):
            errors.append("retaining manager/CRDs after uncertain author-rights revocation")
            return errors
        attempt("release held leader Lease", lambda: delete_group(
            refs, "lease", host_proxy, state["run_id"]))
        if state.get("crds_installed"):
            if not attempt("remove only owned Project authoring data", lambda:
                           remove_owned_data(milo_proxy, state["projects"],
                                             state["allowed_template_uids"])):
                errors.append("retaining CRDs and scoped installer after foreign/ambiguous data")
                return errors
        if not attempt("delete host controller Flux stage and verify absent", lambda:
                       stage_prune_gate(refs, "host", host, host_proxy,
                                        state["run_id"], FLUX_HOST, "flux-system")):
            errors.append("retaining manager CA/credentials while host Flux stage may recreate them")
            return errors
        manager_gone = attempt("verify manager Deployment absent", lambda: absent(
            host, [("deployment", "apps-controller", "apps-system")]))
        if not manager_gone:
            errors.append("retaining controller CA and CRDs while manager Deployment exists")
            return errors
        if not attempt("delete root CRD Flux stage and verify absent", lambda:
                       stage_prune_gate(refs, "crds", host, host_proxy,
                                        state["run_id"], FLUX_CRDS, "milo-system")):
            errors.append("retaining remote installer while CRD Flux stage may recreate CRDs")
            return errors
        crds_absent = attempt("verify owned CRDs absent", lambda: absent(
            milo, [("customresourcedefinition", name, None) for name in CRDS]))
        if not crds_absent:
            errors.append("retaining remote Flux installer identity until CRD prune succeeds")
            return errors
        for group, proxy in (("namespace", host_proxy), ("installer-secret", host_proxy),
                             ("rbac", milo_proxy), ("source", host_proxy)):
            attempt(f"delete {group}", lambda g=group, p=proxy: delete_group(
                refs, g, p, state["run_id"]))
        if state.get("auth_intent"):
            def restore_auth():
                current = host.json("get", "secret", "milo-apiserver-auth-tokens",
                                    "-n", "milo-system")
                now = current["data"]["tokens.csv"]
                require(now in (state["old_auth_b64"], state["new_auth_b64"]),
                        "static auth changed concurrently; refusing overwrite")
                if now == state["new_auth_b64"]:
                    patch_auth(host, state["private"], state, now,
                               state["old_auth_b64"])
                    restart_milo(host)
                require(host.json("get", "secret", "milo-apiserver-auth-tokens",
                                  "-n", "milo-system")["data"]["tokens.csv"] ==
                        state["old_auth_b64"], "Milo static auth did not restore")
            attempt("restore dedicated static auth exactly", restore_auth)
        if state.get("suspend_intent"):
            def resume():
                current = host.json("get", "kustomization", "milo-core", "-n", "flux-system")
                if current["spec"].get("suspend", False):
                    suspend_core(host, state["private"], state, state["run_id"], False)
                require(host.json("get", "kustomization", "milo-core", "-n",
                                  "flux-system")["spec"] == state["core"]["spec"],
                        "Milo core Flux was not restored")
            attempt("resume Milo core Flux by CAS", resume)
        for group in ("registry",):
            attempt(f"delete {group}", lambda g=group: delete_group(
                refs, g, host_proxy, state["run_id"]))
    finally:
        for forward in reversed(forwards):
            attempt("stop private port-forward", forward.close)
        attempt("close host API proxy", host_proxy.close)
        attempt("close Milo API proxy", milo_proxy.close)
        evidence["cleanup_actions"] = evidence.get("cleanup_actions", [])
        evidence["cleanup"] = "complete" if not errors else "incomplete"
        evidence["cleanup_errors"] = errors
        private_write(evidence_file, json.dumps(evidence, indent=2) + "\n")
    return errors


def diagnostics(host: Kube) -> dict:
    # Select status only; never serialize credential-bearing objects or Pod spec.
    result = {}
    targets = [("kustomization", FLUX_CRDS, "milo-system"),
               ("kustomization", FLUX_HOST, "flux-system"),
               ("kustomization", FLUX_ADMISSION, "milo-system"),
               ("ocirepository", OCI_NAME, "flux-system"),
               ("deployment", "apps-controller", "apps-system"),
               ("certificate", "apps-serving-tls", "apps-system")]
    for kind, name, namespace in targets:
        label = f"{namespace}/{kind}/{name}"
        try:
            obj = host.optional(kind, name, namespace)
            result[label] = None if obj is None else {
                "uid": obj["metadata"]["uid"],
                "generation": obj["metadata"].get("generation"),
                "status": obj.get("status", {})}
        except Exception as error:
            result[label] = {"unavailable": str(error)[:300]}
    return result


def execute(args, commands: Commands, state: dict, source: dict,
            fixtures: dict, private: Path) -> None:
    run_id = "apps-native-" + secrets.token_hex(8)
    started = time.monotonic()
    evidence_fd, evidence_name = tempfile.mkstemp(prefix="apps-a1t-native-evidence-",
                                                   suffix=".json")
    os.close(evidence_fd)
    evidence_file = Path(evidence_name)
    evidence_file.chmod(0o600)
    evidence = {"run_id": run_id, "source_revision": args.apps_revision,
                "binary_revision": APPS_BINARY_SHA, "image_index": APPS_IMAGE,
                "node_architecture": args.expected_architecture,
                "platform_manifest": PLATFORM_DIGESTS[args.expected_architecture][0],
                "platform_config": PLATFORM_DIGESTS[args.expected_architecture][1],
                "source_config_sha256": source["config_hash"],
                "source_config_file_count": source["config_count"],
                "identity_mode": "isolated Milo static-token RBAC; production IAM/OpenFGA unverified",
                "project_uids": state["projects"], "checks": [],
                "result": "incomplete", "cleanup": "pending"}
    if args.datumctl_binary is not None:
        evidence["datumctl_source_revision"] = CLI_SHA
        evidence["datumctl_checks"] = []
    def record(check: str) -> None:
        evidence["checks"].append(check)
        private_write(evidence_file, json.dumps(evidence, indent=2) + "\n")
        print(f"PASS: {check}", flush=True)

    host, milo = state["host"], state["milo"]
    state.update({"run_id": run_id, "private": private, "allowed_template_uids": {},
                  "auth_intent": False, "suspend_intent": False,
                  "crds_installed": False})
    refs: dict[str, list[Owned]] = {}
    host_proxy = APIProxy(host, private, "host")
    milo_proxy = APIProxy(milo, private, "milo")
    forwards: list[Forward] = []
    tokens = {user: secrets.token_urlsafe(48) for user in USERS}
    state["new_auth_b64"] = token_csv(state["old_auth_b64"], tokens)
    primary_error: Exception | None = None
    commands.deadline = started + args.deadline_seconds
    record("read-only preflight: exact source, isolated APIs, Projects, CA, image and collisions")
    try:
        host_proxy.start()
        milo_proxy.start()
        for item in fixtures["registry"]:
            kind = item["kind"]
            register_owned(refs, "registry", host, item, "flux-system", run_id,
                           "deployments" if kind == "Deployment" else "services",
                           "/apis/apps/v1" if kind == "Deployment" else "/api/v1")
        host.k("rollout", "status", f"deployment/{REGISTRY_NAME}", "-n", "flux-system",
               "--timeout=180s", timeout=190)
        registry_forward = Forward(host, "flux-system", REGISTRY_NAME,
                                   args.registry_port, 5000)
        registry_forward.start()
        forwards.append(registry_forward)
        digest = publish_artifact(commands, args, private, source["config_hash"],
                                  source["config_count"])
        evidence["private_oci_digest"] = digest
        record("private OCI source exact digest pull matched clean Apps config tree")
        state["suspend_intent"] = True
        suspend_core(host, private, state, run_id, True)
        deferred_author_binding = None
        for item in fixtures["milo-rbac"]:
            if (item["kind"] == "ClusterRoleBinding" and
                    item["metadata"]["name"] == AUTHOR):
                deferred_author_binding = item
                continue
            plural = "clusterroles" if item["kind"] == "ClusterRole" else "clusterrolebindings"
            register_owned(refs, "rbac", milo, item, None, run_id,
                           plural, "/apis/rbac.authorization.k8s.io/v1")
        state["auth_intent"] = True
        patch_auth(host, private, state, state["old_auth_b64"], state["new_auth_b64"])
        restart_milo(host)
        ca_file = private / "milo-public-ca.pem"
        private_write(ca_file, state["milo_ca"])
        direct = Forward(host, "milo-system", "milo-apiserver",
                         args.direct_milo_port, 6443)
        direct.start()
        direct.verified_tls(ca_file)
        forwards.append(direct)
        scoped_configs: dict[str, Path] = {}
        for user in USERS:
            file = private / f"{user}.kubeconfig"
            private_write(file, kubeconfig_text(
                f"https://127.0.0.1:{args.direct_milo_port}", state["milo_ca"],
                tokens[user]))
            scoped_configs[user] = file
        authorization_probes(scoped_configs, commands, author_enabled=False)
        for name, uid in state["projects"].items():
            got = must_http(args.direct_milo_port, ca_file, tokens[DISCOVERY],
                            "GET", f"{PROJECT_API}/{name}", 200)
            require(got["metadata"]["uid"] == uid,
                    "dedicated discovery identity observed changed Project UID")
        record("four fresh scoped Milo Users pass deny matrix; author writes remain withheld")
        namespace = {"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": "apps-system"}}
        register_owned(refs, "namespace", host, namespace, None, run_id,
                       "namespaces", "/api/v1")
        in_cluster = "https://milo-apiserver.milo-system.svc:6443"
        secrets_to_create = (
            ("milo-system", INSTALLER_SECRET, INSTALLER),
            ("apps-system", "apps-root-discovery-kubeconfig", DISCOVERY),
            ("apps-system", "apps-project-service-kubeconfig", PROJECT),
        )
        for namespace_name, name, identity in secrets_to_create:
            manifest = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": name, "namespace": namespace_name},
                        "stringData": {"kubeconfig": kubeconfig_text(
                            in_cluster, state["milo_ca"], tokens[identity])}}
            register_owned(refs, "installer-secret" if identity == INSTALLER else
                           "namespace", host, manifest, namespace_name, run_id,
                           "secrets", "/api/v1", sensitive=True)
        oci = json.loads(json.dumps(fixtures["source"][0]))
        oci["spec"]["ref"] = {"digest": digest}
        register_owned(refs, "source", host, oci, "flux-system", run_id,
                       "ocirepositories", "/apis/source.toolkit.fluxcd.io/v1")
        flux_cluster(host, "reconcile", "source", "oci", OCI_NAME, "-n",
                     "flux-system", "--timeout=180s", timeout=190)
        observed_source = host.json("get", "ocirepository", OCI_NAME, "-n", "flux-system")
        require(ready(observed_source) and observed_source["spec"]["ref"] ==
                {"digest": digest} and observed_source["status"]["artifact"]["revision"] ==
                digest, "Flux OCI source not Ready at exact private artifact digest")
        crd_kust = json.loads(json.dumps(fixtures["crds"][0]))
        crd_kust["spec"]["commonMetadata"] = {"labels": {RUN_LABEL: run_id}}
        register_owned(refs, "crds", host, crd_kust, "milo-system", run_id,
                       "kustomizations", "/apis/kustomize.toolkit.fluxcd.io/v1")
        flux_reconcile(host, "kustomization", FLUX_CRDS, "milo-system", 300)
        exact_flux(host, FLUX_CRDS, "milo-system", digest)
        evidence["observed_crd_schema_sha256"] = check_crds(milo, source)
        state["crds_installed"] = True
        for project in state["projects"]:
            require(not resource_empty(milo_proxy, project, "apptemplates") and
                    not resource_empty(milo_proxy, project, "apptemplaterevisions"),
                    f"foreign Apps authoring data appeared in {project} before trial")
        for project in PROJECTS[1:]:
            path = f"{PROJECT_API}/{project}/control-plane{APPS_API}"
            wait_for(lambda p=path: milo_proxy.get(p),
                     f"administrator observes native Apps discovery in {project}", 120)
        record("remote Flux installed exact two source CRDs; established schemas and two real Project paths")
        held_lease = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                      "metadata": {"name": "apps-authoring.apps.miloapis.com",
                                   "namespace": "apps-system"},
                      "spec": {"holderIdentity": run_id, "leaseDurationSeconds": 1800,
                               "acquireTime": datetime.now(timezone.utc).isoformat()}}
        lease_ref = HeldLease(host, "apps-system", "lease",
                              held_lease["metadata"]["name"],
                              "/apis/coordination.k8s.io/v1/namespaces/apps-system/leases/" +
                              held_lease["metadata"]["name"])
        refs.setdefault("lease", []).append(lease_ref)
        lease_ref.create(held_lease, run_id)
        host_kust = json.loads(json.dumps(fixtures["host"][0]))
        host_kust["spec"]["commonMetadata"] = {"labels": {RUN_LABEL: run_id}}
        register_owned(refs, "host", host, host_kust, "flux-system", run_id,
                       "kustomizations", "/apis/kustomize.toolkit.fluxcd.io/v1")
        flux_reconcile(host, "kustomization", FLUX_HOST, "flux-system", 600)
        exact_flux(host, FLUX_HOST, "flux-system", digest)
        before = pod_snapshot(host, args.expected_architecture)
        evidence["initial_pods"] = before
        for name in ("apps-serving-ca", "apps-serving-tls"):
            certificate = host.json("get", "certificate", name, "-n", "apps-system")
            require(certificate_ready(certificate), f"native Apps Certificate/{name} is not Ready")
        app_ca = secret_bytes(host, "apps-system", "apps-serving-ca", "tls.crt")
        app_cert = secret_bytes(host, "apps-system", "apps-serving-tls", "tls.crt")
        verify_cert(commands, private, app_ca, app_cert,
                    dns="apps-controller.apps-system.svc")
        service = host.json("get", "service", "apps-controller", "-n", "apps-system")
        require(service["spec"]["ports"][0]["port"] == 443 and
                service["spec"]["ports"][0]["targetPort"] == "webhook",
                "Apps source webhook Service changed")
        endpoints = host.json("get", "endpoints", "apps-controller", "-n", "apps-system")
        require(sum(len(entry.get("addresses", [])) for entry in
                    endpoints.get("subsets", [])) == 2,
                "both Apps webhook replicas are not reachable through host Service")
        record("host Flux manager Ready: two verified imageIDs and CA-validated webhook endpoints")
        admission = json.loads(json.dumps(fixtures["admission"][0]))
        encoded_ca = base64.b64encode(app_ca).decode()
        admission["spec"]["commonMetadata"] = {"labels": {RUN_LABEL: run_id}}
        admission["spec"]["patches"] = []
        for item in source["admission"]:
            steps = [{"op": "add", "path": f"/webhooks/{i}/clientConfig/caBundle",
                      "value": encoded_ca} for i in range(len(item["webhooks"]))]
            admission["spec"]["patches"].append({
                "target": {"kind": item["kind"], "name": item["metadata"]["name"]},
                "patch": yaml.safe_dump(steps, sort_keys=False)})
        register_owned(refs, "admission", host, admission, "milo-system", run_id,
                       "kustomizations", "/apis/kustomize.toolkit.fluxcd.io/v1")
        flux_reconcile(host, "kustomization", FLUX_ADMISSION, "milo-system", 300)
        exact_flux(host, FLUX_ADMISSION, "milo-system", digest)
        assert_admission(milo, source, app_ca)
        require(deferred_author_binding is not None,
                "source fixture lacks deferred author User binding")
        register_owned(refs, "author-binding", milo, deferred_author_binding, None, run_id,
                       "clusterrolebindings", "/apis/rbac.authorization.k8s.io/v1")
        authorization_probes(scoped_configs, commands, author_enabled=True)
        evidence["discovery"] = check_discovery(args.direct_milo_port, ca_file, tokens[AUTHOR])
        record("root Milo Flux installed only source-owned fail-closed WCs with verified public CA")
        prior = test_authoring(args, state, tokens, ca_file, lease_ref, host_proxy, record)
        bad_ca_probe(args, milo, private, tokens, ca_file, app_ca, state["milo_ca"])
        record("wrong public CA rejected a named webhook TLS request; exact CA restored")
        history = post_admission(args, state, tokens, ca_file, host, prior, record)
        cli = None
        if args.datumctl_binary is not None:
            project = PROJECTS[1]
            cli_config = private / "apps-native-customer-project.kubeconfig"
            private_write(cli_config, kubeconfig_text(
                f"https://127.0.0.1:{args.direct_milo_port}{PROJECT_API}/{project}/control-plane",
                state["milo_ca"], tokens[AUTHOR]))
            cli = AppsCLI(args.datumctl_binary, cli_config, project,
                          args.direct_milo_port)
            path = project_path(project, "apptemplates", TEMPLATE_NAME)
            def snapshot():
                return {"template": must_http(args.direct_milo_port, ca_file,
                                               tokens[AUTHOR], "GET", path, 200),
                        "revisions": revision_list(args.direct_milo_port, ca_file,
                                                   tokens[AUTHOR], project)}
            result = exercise_history_diff_restore(
                cli, snapshot,
                lambda sequence: wait_template(args.direct_milo_port, ca_file,
                                               tokens[AUTHOR], project,
                                               TEMPLATE_NAME, sequence))
            history["template"], history["revisions"] = (
                result["template"], result["revisions"])
            evidence["datumctl_checks"] = result["checks"]
            record("exported datumctl history/diff/dry-run and exact-definition restore used real Project API")
        deletion = delete_history_test(args, state, tokens, ca_file, history, record, cli)
        if cli is not None:
            evidence["datumctl_checks"].extend(
                ["server-dry-run-delete", "uid-rv-delete", "history-after-delete"])
            record("exported datumctl UID/RV delete retained immutable history by UID")
        precision = precision_test(args, state, tokens, ca_file, record)
        evidence["history"] = {"template_uids": [deletion["original_uid"],
                                                  deletion["replacement_uid"],
                                                  precision["template_uid"]],
                               "revision_names": deletion["revision_names"] +
                               [precision["revision_name"]],
                               "precision_digest": precision["digest"]}
        evidence["pods_before_after"] = {"before": history["before_pods"],
                                         "after": history["after_pods"]}
    except Exception as exc:
        primary_error = exc
        evidence["failure"] = str(exc)[:900]
        private_write(evidence_file, json.dumps(evidence, indent=2) + "\n")
        print(f"FAIL: {evidence['failure']}; starting bounded cleanup", flush=True)
        evidence["failure_diagnostics"] = diagnostics(host)
    finally:
        cleanup_started = time.monotonic()
        evidence["test_duration_seconds"] = round(cleanup_started - started, 3)
        cleanup_errors = cleanup(args, commands, state, refs, host_proxy, milo_proxy,
                                 forwards, evidence, evidence_file)
        evidence["cleanup_duration_seconds"] = round(time.monotonic() - cleanup_started, 3)
        evidence["total_duration_seconds"] = round(time.monotonic() - started, 3)
        evidence["result"] = "pass" if primary_error is None and not cleanup_errors else "fail"
        if primary_error is not None:
            evidence["failure"] = str(primary_error)[:900]
        private_write(evidence_file, json.dumps(evidence, indent=2) + "\n")
        print(f"Non-secret native Apps evidence: {evidence_file}", flush=True)
    require(primary_error is None and not cleanup_errors,
            f"native Apps trial failed: {primary_error}; cleanup: {cleanup_errors}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps-source", type=Path, required=True,
                        help="absolute clean Apps checkout at --apps-revision")
    parser.add_argument("--datumctl-source", type=Path,
                        help=f"optional clean pinned datumctl checkout at {CLI_SHA}; export Apps Command")
    parser.add_argument("--apps-revision", required=True,
                        help="exact 40-character source commit including deployment config")
    parser.add_argument("--image-evidence", type=Path,
                        help="published multiarch OCI manifest/label evidence JSON")
    parser.add_argument("--expected-architecture", choices=sorted(PLATFORM_DIGESTS),
                        default="arm64", help="exact Linux Kind node and Apps runtime platform")
    parser.add_argument("--host-kubeconfig", type=Path)
    parser.add_argument("--host-context")
    parser.add_argument("--milo-kubeconfig", type=Path)
    parser.add_argument("--milo-port", type=int, default=18443)
    parser.add_argument("--direct-milo-port", type=int, default=19443)
    parser.add_argument("--registry-port", type=int, default=19500)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true",
                      help="source and fixture checks only; zero cluster calls")
    mode.add_argument("--execute", action="store_true",
                      help="run bounded owned-cluster trial after lead review")
    args = parser.parse_args()
    args.apps_source = args.apps_source.resolve()
    if args.datumctl_source is not None:
        args.datumctl_source = args.datumctl_source.resolve()
    args.datumctl_binary = None
    commands = Commands()
    fixtures = expected_fixture()
    fixture_contract(fixtures)
    source_role_guards(fixtures)
    source = source_contract(commands, args.apps_source, args.apps_revision)
    if args.image_evidence:
        image_contract(args.image_evidence.resolve())
    print("Apps native offline contract PASS: exact source revision, two Project CRDs, "
          "two fail-closed webhook configurations, two-replica host overlay, scoped fixture",
          flush=True)
    if args.offline:
        if args.datumctl_source is not None:
            with tempfile.TemporaryDirectory(prefix="apps-a1t-native-cli-offline-") as temporary:
                build_private_cli(args.datumctl_source, Path(temporary))
            print("Pinned exported datumctl Apps Command build PASS", flush=True)
        return
    require(args.image_evidence is not None,
            "read-only/live preflight requires exact multiarch image evidence")
    require(all((args.host_kubeconfig, args.host_context, args.milo_kubeconfig)),
            "explicit host/Milo kubeconfig and host context are required")
    require(600 <= args.deadline_seconds <= 3600, "deadline must be between 600 and 3600s")
    args.host_kubeconfig = args.host_kubeconfig.resolve()
    args.milo_kubeconfig = args.milo_kubeconfig.resolve()
    if args.execute:
        require(CONTENT_SAFE_BINARY_SHA == APPS_BINARY_SHA and
                re.fullmatch(r"[0-9a-f]{40}", CONTENT_SAFE_BINARY_SHA) is not None,
                "live native trial fenced until exact-content binary and image pins are published")
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="apps-a1t-native-private-") as temporary:
            private = Path(temporary)
            if args.datumctl_source is not None:
                args.datumctl_binary = build_private_cli(args.datumctl_source, private)
            state = preflight(args, commands, source, fixtures, private)
            print("Native Apps read-only preflight PASS: isolated contexts, exact node image, "
                  "real Projects and CA, no object collisions", flush=True)
            if args.execute:
                execute(args, commands, state, source, fixtures, private)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    main()
