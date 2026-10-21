#!/usr/bin/env python3
"""Bounded isolated Compute management startup fixture.

Default mode is read-only preflight; --offline verifies source/render contracts
without cluster access. --execute requires reviewed, explicit private contexts.
It never persists a Workload, Instance, ResourceClaim, or tenant route.
"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "88a7101c2b91f7195c2bb5433c50f54c384a90ce"
NSO_SHA = "79245f00d6220c128df3d024475ff7b1566ce305"
IMAGE_INDEX = "sha256:1e213d5dcff0be340788d80df6fbdc235b691830667526234ee131240a89e679"
ARM_MANIFEST = "sha256:d288691dee2c3935712fe70e3f4b43af063c16db27bd7bc9ab38b1951ec3ba77"
ARM_CONFIG = "sha256:5d230dc6141ad919d653540d6a23ed2ac5b679622a1acbbb890d1f14a0bc98ae"
IMAGE = "ghcr.io/datum-cloud/compute@" + IMAGE_INDEX
USERS = ("apps-a1t-compute-discovery", "apps-a1t-compute-project", "apps-a1t-compute-quota")
ADMISSION_USER = "apps-a1t-compute-admission-installer"
ADMISSION_NAMES = {"MutatingWebhookConfiguration": "compute-mutating",
                   "ValidatingWebhookConfiguration": "compute-validating"}
ADMISSION_PATHS = {
    "minstanceconsolesession.kb.io": "/mutate-compute-datumapis-com-v1alpha-instanceconsolesession",
    "mworkload.kb.io": "/mutate-compute-datumapis-com-v1alpha-workload",
    "vinstanceconsolesession.kb.io": "/validate-compute-datumapis-com-v1alpha-instanceconsolesession",
    "vinstancetype.kb.io": "/validate-compute-datumapis-com-v1alpha-instancetype",
    "vworkload.kb.io": "/validate-compute-datumapis-com-v1alpha-workload",
    "vworkloaddeployment.kb.io": "/validate-compute-datumapis-com-v1alpha-workloaddeployment",
}
PROBE_IMAGE = "docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
HUB_SA = "apps-a1t-compute-manager"
INSTALLER_SA = "apps-a1t-hub-crd-installer"
INSTALLER_KUBECONFIG_SECRET = "compute-a1t-hub-crd-installer-kubeconfig"
HUB_NAMESPACE = "default"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
PROJECT_API = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
RUN_LABEL = "apps-a1t.datum.net/run-id"
FLUX_NAME_LABEL = "kustomize.toolkit.fluxcd.io/name"
FLUX_NAMESPACE_LABEL = "kustomize.toolkit.fluxcd.io/namespace"
MANAGEMENT_FLUX_NAME = "compute-a1t-management"
MANAGEMENT_CLUSTER_PATHS = {
    "ClusterRole": "/apis/rbac.authorization.k8s.io/v1/clusterroles/",
    "ClusterRoleBinding": "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/",
    "MutatingWebhookConfiguration": "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/",
    "ValidatingWebhookConfiguration": "/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/",
}
MANAGEMENT_CLUSTER_KEYS = {
    ("MutatingWebhookConfiguration", "compute-mutating"),
    ("ValidatingWebhookConfiguration", "compute-validating"),
    ("ClusterRole", "compute"),
    ("ClusterRoleBinding", "compute"),
    ("ClusterRole", "compute-metrics-auth-role"),
    ("ClusterRoleBinding", "compute-metrics-auth-rolebinding"),
    ("ClusterRole", "compute-metrics-reader"),
    ("ClusterRole", "compute-workloaddeployment-editor-role"),
    ("ClusterRole", "compute-workloaddeployment-viewer-role"),
    ("ClusterRole", "compute-workload-editor-role"),
    ("ClusterRole", "compute-workload-viewer-role"),
}
WEBHOOK_CA_SECRET = "compute-a1t-webhook-ca"
GLOBAL_DEADLINE = None
HUB_SCHEMA_LOCK = {
    "instanceconsolesessions.compute.datumapis.com": "d3107f43691590dbf0e60f7d3290021d03d00d18e8d71ec217d7ac5cf3fadb08",
    "instances.compute.datumapis.com": "502505981f8fb8679fbd89ff8fca583213a8c408ddec4f7b4235225dd3b18f6f",
    "instancetypes.compute.datumapis.com": "e82e80bddbdae1f4208651724fa55e3ea3895356283937c75cf873afcf8734a3",
    "runtimeclasses.compute.datumapis.com": "abe8a33bd12096fca5f819db6bc8f12b69edf265b0f0e49a756ef289e95c3c5d",
    "workloaddeployments.compute.datumapis.com": "1bdd66d483043b6374084670788de265e2f028d7a9be10b2195dead46004937c",
    "workloads.compute.datumapis.com": "615e5bcd6d2e6e31b61bdb642599686f612f03a5f9caf2abd7cd9a27b36b9618",
    "connectors.networking.datumapis.com": "e951743746e79b67c415bfc3b754ab62665a6c719f627f3fd01c59460e23a8fd",
    "edgereachabilities.networking.datumapis.com": "dfe2384d5442028cbdfdcd06ca15ce2ad03e1c63fce204c9c0b1f23ccd382a18",
    "httpproxies.networking.datumapis.com": "a03faa2b5a77373610ba9000c95b70f8eddaeee498cd42947e7e26ab90fa8c00",
    "networkbindings.networking.datumapis.com": "2fb532967dc32881c399573465c66f93797a8b3af05ae4072b4d29c77268f31f",
    "networkcontexts.networking.datumapis.com": "c9096bcc375aba957eb5c2ba9a2e99fbf28e2cf1c523eeb3b93743a826244afe",
    "networkinterfaceclaims.networking.datumapis.com": "cd5252be09ecbabe9a8e355906955ffd6999204a57b6c87edaeccddea73b2696",
    "networkinterfaces.networking.datumapis.com": "230cad6bcce1ae2ffff9a3a8cdb2e09f3868bb281aae0c6f6fbe8f5253c886d6",
    "networks.networking.datumapis.com": "1ea0551a5479a2511f6f83c40674386900d4e02cc88408ad0460b7fd44d0c775",
    "subnets.networking.datumapis.com": "c6f7bb5d8bcecfb4004af0e5e7bb400a702eb4cbe0d82aa1b058439baa516a82",
    "trafficprotectionpolicies.networking.datumapis.com": "cb69b40a49158c1815580d67c767699f00e8e533da37bfd3d8d8ea56b19f0f2c",
}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*argv, input_text=None, sensitive=False, timeout=30, env=None):
    if GLOBAL_DEADLINE is not None:
        remaining = GLOBAL_DEADLINE - time.monotonic()
        require(remaining > 0, "Compute startup exceeded its 30-minute deadline")
        timeout = min(timeout, remaining)
    result = subprocess.run(argv, input=input_text, text=True, capture_output=True,
                            timeout=timeout, env=env, check=False)
    if result.returncode:
        detail = "details withheld" if sensitive else result.stderr.strip()[:1000]
        raise RuntimeError(f"{argv[0]} failed: {detail}")
    return result.stdout.strip()


def k(config, context, *argv, **kwargs):
    return run("kubectl", "--kubeconfig", str(config), "--context", context,
               "--request-timeout=10s", *argv, **kwargs)


def kjson(config, context, *argv):
    return json.loads(k(config, context, *argv, "-o", "json"))


def optional(config, context, namespace, kind, name):
    scope = ("-n", namespace) if namespace else ()
    output = k(config, context, *scope, "get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(output) if output else None


def ready(obj, condition="Ready"):
    generation = obj["metadata"]["generation"]
    status = obj.get("status", {})
    return status.get("observedGeneration", generation) >= generation and any(
        item.get("type") == condition and item.get("status") == "True" and
        item.get("observedGeneration", generation) >= generation
        for item in status.get("conditions", []))


def private_file(path):
    require(path.is_absolute() and path.is_file(), f"absolute private file required: {path}")
    require(path.stat().st_mode & 0o077 == 0, f"private file has group/other access: {path}")


def private_write(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def wait_for(predicate, description, seconds=120):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {description}: {last or 'condition false'}")


def manifests(path):
    return [item for item in yaml.safe_load_all(path.read_text()) if item]


def check_secret_access(deployment, expected_names):
    pod = deployment["spec"]["template"]["spec"]
    security = pod.get("securityContext", {})
    require(security.get("runAsNonRoot") is True and
            security.get("runAsUser") == security.get("runAsGroup") ==
            security.get("fsGroup") == 65532,
            "Compute Pod no longer runs as nonroot UID/GID/fsGroup 65532")
    volume_names = {item["name"] for item in pod["volumes"]}
    require(all(mount["name"] in volume_names for container in pod["containers"]
                for mount in container.get("volumeMounts", [])),
            "Compute container mount references an absent volume")
    secrets_by_name = {item["name"]: item["secret"] for item in pod["volumes"]
                       if "secret" in item}
    require(set(secrets_by_name) == set(expected_names) and
            all(secret.get("defaultMode") == 0o440 and
                len(secret.get("items", [])) == 1 and
                secret["items"][0].get("key") == "kubeconfig"
                for secret in secrets_by_name.values()),
            "Compute scoped kubeconfig Secrets are not group-readable only")


def schema_hash(crd):
    schemas = [{"name": version["name"], "schema": version.get("schema", {})}
               for version in crd["spec"]["versions"]]
    return hashlib.sha256(json.dumps(schemas, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def source_contract(args):
    require(args.source.is_absolute() and args.source.is_dir(), "absolute pinned Compute checkout required")
    require(run("git", "-C", str(args.source), "rev-parse", "HEAD") == SOURCE_SHA,
            "Compute source checkout differs from pinned 88a7101 commit")
    require(not run("git", "-C", str(args.source), "status", "--porcelain", "--untracked-files=all"),
            "Compute source checkout is dirty")
    require(args.nso_source.is_absolute() and args.nso_source.is_dir(),
            "absolute pinned NSO checkout required")
    require(run("git", "-C", str(args.nso_source), "rev-parse", "HEAD") == NSO_SHA and
            not run("git", "-C", str(args.nso_source), "status", "--porcelain",
                    "--untracked-files=all"),
            "NSO source checkout differs from clean pinned 79245f0 commit")
    source = yaml.safe_load((ROOT / "components/compute-controller/source.yaml").read_text())
    management = yaml.safe_load((ROOT / "components/compute-controller/management.yaml").read_text())
    nso_source = yaml.safe_load((ROOT / "components/compute-controller/hub-nso-source.yaml").read_text())
    hub_compute = yaml.safe_load((ROOT / "components/compute-controller/hub-compute-crds.yaml").read_text())
    hub_nso = yaml.safe_load((ROOT / "components/compute-controller/hub-nso-crds.yaml").read_text())
    bundle = manifests_text(run("kustomize", "build",
                                str(ROOT / "components/compute-controller"), timeout=60))
    require({(item["kind"], item["metadata"]["name"]) for item in bundle} == {
        ("GitRepository", "compute-a1t-controller"),
        ("GitRepository", "compute-a1t-hub-nso-source"),
        ("Kustomization", "compute-a1t-hub-compute-crds"),
        ("Kustomization", "compute-a1t-hub-nso-crds"),
        ("Kustomization", "compute-a1t-management"),
        ("Kustomization", "compute-a1t-dfw-cell"),
    } and len(bundle) == 6, "optional Compute Flux graph shape changed")
    require(source["spec"]["ref"] == {"commit": SOURCE_SHA} and
            source["spec"]["url"] == "https://github.com/datum-cloud/compute.git" and
            management["spec"]["sourceRef"] == {"kind": "GitRepository",
                                                 "name": "compute-a1t-controller"} and
            management["spec"]["path"] == "./config/overlays/management-plane" and
            management["spec"]["prune"] is True and
            management["spec"]["dependsOn"] == [
                {"name": "compute-a1t-milo-api-crds", "namespace": "milo-system"},
                {"name": "compute-a1t-hub-compute-crds"},
                {"name": "compute-a1t-hub-nso-crds"}] and
            management["spec"]["images"] == [{"name": "ghcr.io/datum-cloud/compute",
                                                  "digest": IMAGE_INDEX}],
            "prepared Compute Flux source/management contract changed")
    require(nso_source["spec"]["url"] ==
            "https://github.com/datum-cloud/network-services-operator.git" and
            nso_source["spec"]["ref"] == {"commit": NSO_SHA} and
            nso_source["spec"]["sparseCheckout"] == ["config"],
            "prepared NSO hub GitRepository source pin changed")
    for item, source_name, path in (
            (hub_compute, "compute-a1t-controller", "./config/base/crd"),
            (hub_nso, "compute-a1t-hub-nso-source", "./config/crd/downstream")):
        spec = item["spec"]
        require(spec["sourceRef"] == {"kind": "GitRepository", "name": source_name} and
                spec["path"] == path and spec["prune"] is True and
                spec["kubeConfig"]["secretRef"] ==
                {"name": INSTALLER_KUBECONFIG_SECRET, "key": "kubeconfig"},
                f"remote Flux hub CRD contract changed: {item['metadata']['name']}")
    hub_crds = []
    for item, tree, expected_suffix in (
            (hub_compute, args.source / "config/base/crd", ".compute.datumapis.com"),
            (hub_nso, args.nso_source / "config/crd/downstream", ".networking.datumapis.com")):
        docs = manifests_text(run("flux", "build", "kustomization",
                                  item["metadata"]["name"], "--path", str(tree),
                                  "--kustomization-file",
                                  str(ROOT / "components/compute-controller" /
                                      ("hub-compute-crds.yaml" if item is hub_compute else
                                       "hub-nso-crds.yaml")), "--dry-run", timeout=60))
        expected = {name for name in HUB_SCHEMA_LOCK if name.endswith(expected_suffix)}
        actual = {doc["metadata"]["name"] for doc in docs}
        require(len(docs) == len(expected) == len(actual) and
                all(doc["kind"] == "CustomResourceDefinition" for doc in docs) and
                actual == expected and
                all(schema_hash(doc) == HUB_SCHEMA_LOCK[doc["metadata"]["name"]]
                    for doc in docs),
                f"pinned {item['metadata']['name']} CRD names or schema hashes changed")
        hub_crds.extend(docs)
    rendered = manifests_text(run(
        "flux", "build", "kustomization", "compute-a1t-management", "--path",
        str(args.source / "config/overlays/management-plane"), "--kustomization-file",
        str(ROOT / "components/compute-controller/management.yaml"), "--dry-run", timeout=60))
    keys = [(item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"])
            for item in rendered]
    require(len(keys) == 18 and len(keys) == len(set(keys)) and
            ("Namespace", None, "compute-system") not in keys,
            "Compute management render object count, uniqueness, or namespace changed")
    management_cluster_refs(rendered)
    deployment = next((item for item in rendered if item["kind"] == "Deployment" and
                       item["metadata"]["name"] == "compute-manager"), None)
    config = next((item for item in rendered if item["kind"] == "ConfigMap" and
                   item["metadata"]["name"] == "compute-config"), None)
    require(deployment and config and
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE and
            deployment["spec"]["template"]["spec"]["serviceAccountName"] == "compute-manager",
            "Compute management manager image or service account changed")
    check_secret_access(deployment, ("downstream-kubeconfig", "milo-discovery-credentials",
                                     "milo-project-credentials", "quota-credentials"))
    webhook_volume = next((item for item in deployment["spec"]["template"]["spec"]["volumes"]
                           if item["name"] == "webhook-server-tls"), None)
    require(webhook_volume == {
        "name": "webhook-server-tls",
        "csi": {"driver": "csi.cert-manager.io", "readOnly": True,
                "volumeAttributes": {
                    "csi.cert-manager.io/dns-names":
                    "webhook-service.compute-system.svc,webhook-service.compute-system.svc.cluster.local",
                    "csi.cert-manager.io/fs-group": "65532",
                    "csi.cert-manager.io/issuer-kind": "Issuer",
                    "csi.cert-manager.io/issuer-name": WEBHOOK_CA_SECRET}},
    }, "Compute CSI webhook issuer or DNS contract changed")
    cert_bootstrap = manifests(ROOT / "components/compute-controller/cert-bootstrap.yaml")
    expected_certs = [
        {"apiVersion": "cert-manager.io/v1", "kind": "Issuer",
         "metadata": {"name": "compute-a1t-selfsigned", "namespace": "compute-system"},
         "spec": {"selfSigned": {}}},
        {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
         "metadata": {"name": WEBHOOK_CA_SECRET, "namespace": "compute-system"},
         "spec": {"isCA": True, "commonName": WEBHOOK_CA_SECRET,
                  "secretName": WEBHOOK_CA_SECRET,
                  "privateKey": {"algorithm": "ECDSA", "size": 256},
                  "issuerRef": {"group": "cert-manager.io", "kind": "Issuer",
                                "name": "compute-a1t-selfsigned"}}},
        {"apiVersion": "cert-manager.io/v1", "kind": "Issuer",
         "metadata": {"name": WEBHOOK_CA_SECRET, "namespace": "compute-system"},
         "spec": {"ca": {"secretName": WEBHOOK_CA_SECRET}}},
    ]
    require(cert_bootstrap == expected_certs,
            "task-owned Compute webhook CA/Issuer bootstrap contract changed")
    admission_configs = [item for item in rendered if item["kind"] in ADMISSION_NAMES]
    require({(item["kind"], item["metadata"]["name"]) for item in admission_configs} ==
            set(ADMISSION_NAMES.items()) and len(admission_configs) == 2,
            "source-rendered Compute admission configurations changed")
    seen_hooks = {}
    for item in admission_configs:
        for hook in item["webhooks"]:
            name = hook["name"]
            require(name not in seen_hooks and name in ADMISSION_PATHS,
                    f"unreviewed Compute admission hook {name}")
            seen_hooks[name] = item["kind"]
            require(hook["clientConfig"] == {"service": {
                        "name": "webhook-service", "namespace": "compute-system",
                        "path": ADMISSION_PATHS[name]}} and
                    hook["failurePolicy"] == "Fail" and
                    hook["sideEffects"] == "None" and
                    hook["admissionReviewVersions"] == ["v1"] and
                    hook["rules"] == next(source_hook["rules"] for source_doc in
                        manifests(args.source / "config/base/webhook/manifests.yaml")
                        for source_hook in source_doc["webhooks"] if source_hook["name"] == name),
                    f"Compute admission hook {name} diverged from pinned source")
    require(seen_hooks == {
        "minstanceconsolesession.kb.io": "MutatingWebhookConfiguration",
        "mworkload.kb.io": "MutatingWebhookConfiguration",
        "vinstanceconsolesession.kb.io": "ValidatingWebhookConfiguration",
        "vinstancetype.kb.io": "ValidatingWebhookConfiguration",
        "vworkload.kb.io": "ValidatingWebhookConfiguration",
        "vworkloaddeployment.kb.io": "ValidatingWebhookConfiguration",
    }, "Compute admission hook count or kinds changed")
    probe = None
    if args.native_admission:
        try:
            import jsonschema
        except ImportError as exc:
            raise RuntimeError("--native-admission offline schema check needs python jsonschema") from exc
        probe = yaml.safe_load((args.source / "test/e2e/instance-types/workload-valid.yaml").read_text())
        require(probe["apiVersion"] == "compute.datumapis.com/v1alpha" and
                probe["kind"] == "Workload" and
                len(probe["spec"]["placements"]) == 1 and
                len(probe["spec"]["template"]["spec"]["runtime"]["sandbox"]["containers"]) == 1,
                "pinned Workload admission probe fixture shape changed")
        probe["metadata"] = {"name": "apps-a1t-admission-probe", "namespace": "default"}
        probe["spec"]["template"]["spec"]["runtime"]["sandbox"]["containers"][0]["image"] = PROBE_IMAGE
        # The CRD defaults this required field at the API server; explicitly
        # materialize that same default for offline JSON Schema validation.
        probe["spec"]["placements"][0]["scaleSettings"]["instanceManagementPolicy"] = "OrderedReady"
        workload_crd = next(item for item in hub_crds if item["metadata"]["name"] ==
                            "workloads.compute.datumapis.com")
        schema = workload_crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(probe)
    cell_rendered = manifests_text(run(
        "flux", "build", "kustomization", "compute-a1t-dfw-cell", "--path",
        str(args.source / "config/overlays/cell"), "--kustomization-file",
        str(ROOT / "components/compute-controller/cell.yaml"), "--dry-run", timeout=60))
    cell_manager = next((item for item in cell_rendered if item["kind"] == "Deployment" and
                         item["metadata"]["name"] == "compute-manager"), None)
    require(cell_manager and
            cell_manager["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE,
            "unexecuted Compute cell image changed")
    check_secret_access(cell_manager, ("quota-credentials",))
    config_data = yaml.safe_load(config["data"]["config.yaml"])["discovery"]
    require(config_data == {"mode": "milo", "clusterName": "apps-a1t-pop-dfw-20261020",
                            "discoveryKubeconfigPath": "/etc/milo-discovery/kubeconfig",
                            "projectKubeconfigPath": "/etc/milo-project/kubeconfig",
                            "quotaKubeconfigPath": "/etc/quota-credentials/kubeconfig"},
            "Compute manager no longer mounts distinct Milo discovery/project/quota configs")
    roles = manifests_text(run("kustomize", "build", str(ROOT / "components/compute-controller/milo-rbac")))
    role_keys = [(item["kind"], item["metadata"]["name"]) for item in roles]
    require(len(roles) == 6 and len(role_keys) == len(set(role_keys)),
            "Compute Milo RBAC bundle differs from six reviewed objects")
    for user in USERS:
        # Binding names are intentionally distinct from User names.
        binding = next(item for item in roles if item["kind"] == "ClusterRoleBinding" and
                       item["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io",
                                             "kind": "User", "name": user}])
        role = next(item for item in roles if item["kind"] == "ClusterRole" and
                    item["metadata"]["name"] == binding["roleRef"]["name"])
        require(binding["metadata"]["name"] == role["metadata"]["name"] and
                all("*" not in rule.get("apiGroups", []) + rule.get("resources", []) +
                    rule.get("verbs", []) for rule in role["rules"]),
                f"unexpected broad Milo RBAC for {user}")
        forbidden = {"bind", "escalate", "impersonate"}
        require(all(not forbidden.intersection(rule["verbs"]) and
                    not {"clusterroles", "clusterrolebindings", "roles", "rolebindings"}
                    .intersection(rule["resources"])
                    for rule in role["rules"]),
                f"Milo RBAC management or impersonation grant for {user}")
        if user != USERS[1]:
            require(all("secrets" not in rule["resources"] for rule in role["rules"]),
                    f"non-project Milo identity can read Secrets: {user}")
        else:
            require(all(not ({"secrets", "configmaps"}.intersection(rule["resources"]) and
                              {"create", "update", "patch", "delete"}.intersection(rule["verbs"]))
                        for rule in role["rules"]),
                    "project Milo identity can mutate referenced Secrets or ConfigMaps")
    hub = manifests_text(run("kustomize", "build", str(args.source / "config/base/downstream-rbac")))
    require(len(hub) == 2 and {item["kind"] for item in hub} ==
            {"ClusterRole", "ClusterRoleBinding"} and
            all(item["metadata"]["name"] == "compute-manager" for item in hub),
            "upstream Karmada downstream RBAC shape changed")
    return {"source": source, "management": management, "rendered": rendered,
            "cert_bootstrap": cert_bootstrap,
            "admission_configs": admission_configs, "admission_probe": probe,
            "nso_source": nso_source, "hub_compute": hub_compute, "hub_nso": hub_nso,
            "hub_crds": hub_crds, "milo_roles": roles,
            "hub_role": next(x for x in hub if x["kind"] == "ClusterRole")}


def manifests_text(text):
    return [item for item in yaml.safe_load_all(text) if item]


def public_secret_field(args, namespace, name, key):
    escaped = key.replace(".", "\\.")
    encoded = k(args.host_kubeconfig, args.host_context, "-n", namespace,
                "get", "secret", name, "-o", f"jsonpath={{.data.{escaped}}}")
    require(encoded, f"missing public certificate {namespace}/{name}:{key}")
    return base64.b64decode(encoded)


def verify_milo_cert(private, ca, cert, hostname=None, ip=None):
    ca_file = private / f"ca-{hashlib.sha256(ca).hexdigest()}.pem"
    cert_file = private / f"cert-{hashlib.sha256(cert).hexdigest()}.pem"
    private_write(ca_file, ca.decode())
    private_write(cert_file, cert.decode())
    argv = ["openssl", "verify", "-CAfile", str(ca_file)]
    if hostname:
        argv += ["-verify_hostname", hostname]
    if ip:
        argv += ["-verify_ip", ip]
    require(run(*argv, str(cert_file)).endswith(": OK"),
            "public Milo certificate chain/SAN verification failed")


def preflight(args, private, contract):
    for path in (args.host_kubeconfig, args.milo_kubeconfig, args.hub_host_kubeconfig,
                 args.karmada_admin_kubeconfig):
        private_file(path)
    require(len({args.host_kubeconfig, args.milo_kubeconfig, args.hub_host_kubeconfig,
                 args.karmada_admin_kubeconfig}) == 4,
            "host, Milo, hub-host and Karmada admin kubeconfigs must be separate")
    require(args.host_context.startswith("kind-apps-a1t-") and
            args.hub_host_context.startswith("kind-apps-a1t-hub-"),
            "expected explicit task-owned Kind contexts")
    for config, context in ((args.host_kubeconfig, args.host_context),
                            (args.milo_kubeconfig, "milo-root"),
                            (args.hub_host_kubeconfig, args.hub_host_context),
                            (args.karmada_admin_kubeconfig, args.karmada_admin_context)):
        require(k(config, context, "config", "current-context") == context,
                f"explicit {context} differs from kubeconfig current context")
    for config, context in ((args.host_kubeconfig, args.host_context),
                            (args.milo_kubeconfig, "milo-root"),
                            (args.hub_host_kubeconfig, args.hub_host_context),
                            (args.karmada_admin_kubeconfig, args.karmada_admin_context)):
        server = k(config, context, "config", "view", "--minify", "-o",
                   "jsonpath={.clusters[0].cluster.server}")
        require(server.startswith("https://127.0.0.1:"),
                f"{context} bootstrap endpoint is not private localhost")
    require(1 <= args.direct_milo_port <= 65535, "invalid direct Milo loopback port")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.direct_milo_port))
    parsed = urlparse(args.karmada_internal_server)
    require(parsed.scheme == "https" and parsed.port and not parsed.path and
            not parsed.username and not parsed.password and
            ipaddress.ip_address(parsed.hostname).is_private,
            "Karmada runtime server must be a private HTTPS IP:port")
    require(args.docker_host.startswith("unix://"), "explicit private Docker socket required")
    docker_socket = Path(args.docker_host.removeprefix("unix://"))
    require(docker_socket.is_absolute() and docker_socket.is_socket(),
            "dedicated Docker socket missing")
    hub_node = args.hub_host_context.removeprefix("kind-") + "-control-plane"
    hub_ip = run("docker", "-H", args.docker_host, "inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", hub_node)
    require(parsed.hostname == hub_ip,
            "Karmada runtime bridge IP differs from the dedicated hub Kind node")
    host_image = json.loads(run("docker", "-H", args.docker_host, "image", "inspect", IMAGE))[0]
    labels = host_image.get("Config", {}).get("Labels") or {}
    require(IMAGE in host_image.get("RepoDigests", []) and
            host_image.get("Architecture") == "arm64" and
            labels.get("org.opencontainers.image.revision") == SOURCE_SHA and
            labels.get("org.opencontainers.image.source") ==
            "https://github.com/datum-cloud/compute",
            "host Compute OCI image digest, architecture or source labels differ from pin")

    nodes = kjson(args.host_kubeconfig, args.host_context, "get", "nodes")["items"]
    require(len(nodes) == 1 and nodes[0]["status"]["nodeInfo"]["architecture"] == "arm64",
            "expected dedicated one-node arm64 Compute management cluster")
    node_images = {name for image in nodes[0]["status"].get("images", [])
                   for name in image.get("names", [])}
    require(IMAGE in node_images,
            "exact Compute index image is not preloaded on management Kind node")
    core = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization", "milo-core",
                 "-n", "flux-system")
    compute_crds = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                         "compute-a1t-milo-api-crds", "-n", "milo-system")
    require(ready(core) and not core["spec"].get("suspend", False) and ready(compute_crds) and
            compute_crds["status"].get("lastAppliedRevision") == "sha1:" + SOURCE_SHA,
            "Milo core or pinned Compute API CRDs not Ready")
    manager = kjson(args.host_kubeconfig, args.host_context, "get", "deployment",
                    "milo-controller-manager", "-n", "milo-system")
    require(manager["status"].get("availableReplicas", 0) >= 1,
            "Milo controller-manager not Available")
    require(optional(args.host_kubeconfig, args.host_context, None, "csidriver",
                     "csi.cert-manager.io") is not None,
            "Compute webhook certificate requires cert-manager CSI driver")
    for name in ("issuers.cert-manager.io", "certificates.cert-manager.io"):
        crd = kjson(args.host_kubeconfig, args.host_context, "get", "crd", name)
        require(any(item.get("type") == "Established" and item.get("status") == "True"
                    for item in crd.get("status", {}).get("conditions", [])),
                f"cert-manager CRD {name} is not Established")
    for name in ("cert-manager", "cert-manager-csi-driver"):
        release = kjson(args.host_kubeconfig, args.host_context, "get", "helmrelease",
                        name, "-n", "cert-manager")
        require(ready(release), f"cert-manager HelmRelease {name} is not Ready")
    auth = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                 "milo-apiserver-auth-tokens", "-n", "milo-system")
    old_b64 = auth["data"]["tokens.csv"]
    rows = list(csv.reader(base64.b64decode(old_b64).decode().splitlines()))
    identities = {(row[1], row[2], row[3]) for row in rows if len(row) == 4}
    require(len(rows) == 2 and identities == {
        ("admin", "admin", "system:masters"),
        ("test-user", "test-user", "system:authenticated")
    } and all(len(row) == 4 and row[0] for row in rows),
            "Milo static auth differs from two-user isolated baseline")
    milo_ca = public_secret_field(args, "milo-system", "milo-ca-tls", "ca.crt")
    milo_cert = public_secret_field(args, "milo-system", "milo-apiserver-tls", "tls.crt")
    verify_milo_cert(private, milo_ca, milo_cert,
                     hostname="milo-apiserver.milo-system.svc")
    verify_milo_cert(private, milo_ca, milo_cert, ip="127.0.0.1")
    projects = {name: kjson(args.milo_kubeconfig, "milo-root", "get", "project", name)
                for name in PROJECTS}
    require(all(item["metadata"].get("uid") and not item["metadata"].get("deletionTimestamp")
                for item in projects.values()), "one or more fixture Projects missing/deleting")
    for name in PROJECTS:
        discovery = json.loads(k(args.milo_kubeconfig, "milo-root", "get", "--raw",
                                f"{PROJECT_API}/{name}/control-plane/apis/quota.miloapis.com/v1alpha1"))
        require(any(item.get("name") == "resourceclaims" for item in
                    discovery.get("resources", [])),
                f"native ResourceClaim API unavailable in Project {name}")

    host_collisions = [(None, "namespace", "compute-system"),
                       ("flux-system", "gitrepository", "compute-a1t-controller"),
                       ("flux-system", "gitrepository", "compute-a1t-hub-nso-source"),
                       ("flux-system", "kustomization", "compute-a1t-management"),
                       ("flux-system", "kustomization", "compute-a1t-hub-compute-crds"),
                       ("flux-system", "kustomization", "compute-a1t-hub-nso-crds"),
                       ("flux-system", "secret", INSTALLER_KUBECONFIG_SECRET)]
    for kind, name in (("secret", "compute-a1t-milo-discovery-kubeconfig"),
                       ("secret", "compute-a1t-milo-project-kubeconfig"),
                       ("secret", "compute-a1t-quota-kubeconfig"),
                       ("secret", "compute-a1t-karmada-kubeconfig")):
        host_collisions.append(("compute-system", kind, name))
    host_collisions += [(item["metadata"].get("namespace"), item["kind"].lower(),
                         item["metadata"]["name"]) for item in contract["rendered"]]
    for namespace, kind, name in host_collisions:
        # Namespace absence is asserted first. A namespaced object cannot exist
        # under that absent namespace; some API servers return Namespace NotFound
        # rather than an empty get --ignore-not-found result.
        if namespace == "compute-system":
            continue
        require(optional(args.host_kubeconfig, args.host_context, namespace, kind, name) is None,
                f"refusing existing host {kind}/{name}")
    for item in contract["milo_roles"]:
        require(optional(args.milo_kubeconfig, "milo-root", None, item["kind"].lower(),
                         item["metadata"]["name"]) is None,
                f"refusing existing Milo {item['kind']}/{item['metadata']['name']}")
    if args.native_admission:
        for kind, name in ADMISSION_NAMES.items():
            require(optional(args.milo_kubeconfig, "milo-root", None,
                             kind.lower(), name) is None,
                    f"refusing existing Milo admission {kind}/{name}")
        for kind in ("clusterrole", "clusterrolebinding"):
            require(optional(args.milo_kubeconfig, "milo-root", None, kind,
                             ADMISSION_USER) is None,
                    f"refusing existing Milo admission {kind}/{ADMISSION_USER}")

    hub_ca_encoded = k(args.hub_host_kubeconfig, args.hub_host_context, "-n", "karmada-system",
                       "get", "secret", "karmada-cert", "-o",
                       "jsonpath={.data.server-ca\\.crt}")
    require(hub_ca_encoded, "Karmada public server CA is missing from pinned chart Secret")
    hub_ca = base64.b64decode(hub_ca_encoded)
    hub_ca_file = private / "karmada-public-ca.pem"
    private_write(hub_ca_file, hub_ca.decode())
    # macOS cannot dial a Colima Docker-bridge IP directly. Authenticate the
    # same Karmada API over its published loopback endpoint here; the scoped
    # runtime config uses the bridge address and must prove itself by startup.
    hub_host_server = k(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                        "config", "view", "--minify", "-o",
                        "jsonpath={.clusters[0].cluster.server}")
    host_parsed = urlparse(hub_host_server)
    require(parsed.port == host_parsed.port,
            "Karmada runtime NodePort differs from host-published port")
    hub_tls = ssl.create_default_context(cafile=str(hub_ca_file))
    # The pinned Karmada chart generates a CA with BasicConstraints CA:TRUE
    # but no KeyUsage extension. Python 3.14 enables X509_STRICT by default;
    # Go's Kubernetes client accepts this CA. Keep chain and hostname checks.
    hub_tls.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with socket.create_connection(("127.0.0.1", host_parsed.port), timeout=5) as raw:
        with hub_tls.wrap_socket(raw, server_hostname="localhost") as peer:
            require(peer.getpeercert(), "Karmada API lacks verified localhost SAN")
    cluster = kjson(args.karmada_admin_kubeconfig, args.karmada_admin_context, "get", "cluster",
                    "apps-a1t-pop-dfw-20261020")
    conditions = cluster.get("status", {}).get("conditions", [])
    labels = cluster["metadata"].get("labels", {})
    require(any(x.get("type") == "Ready" and x.get("status") == "True" for x in conditions) and
            labels.get("topology.datum.net/city-code") == "dfw" and
            labels.get("topology.datum.net/location") == "dfw" and
            labels.get("infra.datum.net/gateways") == "enabled",
            "dedicated DFW Karmada member is not Ready with reviewed placement labels")
    for item in contract["hub_crds"]:
        name = item["metadata"]["name"]
        crd = optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                       None, "crd", name)
        require(crd is None, f"refusing pre-existing Karmada hub CRD {name}; Flux may not adopt it")
    core_discovery = json.loads(k(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                                  "get", "--raw", "/api/v1"))
    require(any(item.get("name") == "serviceaccounts/token" for item in
                core_discovery.get("resources", [])),
            "Karmada hub TokenRequest subresource unavailable for scoped identity")
    require(optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                     HUB_NAMESPACE, "serviceaccount", HUB_SA) is None,
            "refusing existing Karmada runtime ServiceAccount")
    require(optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                     HUB_NAMESPACE, "serviceaccount", INSTALLER_SA) is None,
            "refusing existing Karmada hub CRD installer ServiceAccount")
    for kind in ("clusterrole", "clusterrolebinding"):
        require(optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                         None, kind, "compute-manager") is None,
                f"refusing existing Karmada {kind}/compute-manager")
        require(optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                         None, kind, INSTALLER_SA) is None,
                f"refusing existing Karmada {kind}/{INSTALLER_SA}")
    print("Compute management preflight passed: pinned source/render/image, isolated APIs, "
          "Milo CRDs/auth, Karmada CA/SAN/member, CSI driver and hub CRD absence guards", flush=True)
    return {"core": core, "auth": auth, "auth_b64": old_b64, "milo_ca": milo_ca,
            "hub_ca": hub_ca, "hub_host_server": hub_host_server,
            "projects": {name: item["metadata"]["uid"]
                                         for name, item in projects.items()}}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


class ApiProxy:
    def __init__(self, private, config, context, name):
        self.path = private / f"{name}-api.sock"
        self.config = config
        self.context = context
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", str(self.config), "--context", self.context,
             "--request-timeout=10s", "proxy", f"--unix-socket={self.path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.path.is_socket():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("private UID-safe API proxy did not start")

    def delete(self, path, uid, rv):
        body = json.dumps({"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": uid, "resourceVersion": rv}})
        conn = UnixHTTPConnection(str(self.path))
        try:
            conn.request("DELETE", path, body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            require(response.status in (200, 202),
                    f"UID/resourceVersion-preconditioned DELETE returned HTTP {response.status}")
        finally:
            conn.close()

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


class DirectMilo:
    def __init__(self, args, ca_file):
        self.args = args
        self.ca_file = ca_file
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", str(self.args.host_kubeconfig), "--context",
             self.args.host_context, "--request-timeout=10s", "-n", "milo-system",
             "port-forward", "service/milo-apiserver", f"{self.args.direct_milo_port}:6443",
             "--address=127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "private Milo port-forward exited")
            try:
                with socket.create_connection(("127.0.0.1", self.args.direct_milo_port),
                                              timeout=1) as raw:
                    with ssl.create_default_context(cafile=str(self.ca_file)).wrap_socket(
                            raw, server_hostname="127.0.0.1") as peer:
                        require(peer.getpeercert(), "direct Milo peer certificate missing")
                        return
            except (OSError, ssl.SSLError):
                time.sleep(0.2)
        raise RuntimeError("private Milo port-forward lacks verified serving TLS")

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


def reference(namespace, kind, name, path):
    return {"namespace": namespace, "kind": kind, "name": name, "path": path,
            "intent": False, "uid": None}


def management_cluster_refs(rendered):
    """Exact source-rendered cluster-scoped objects eligible for orphan cleanup."""
    keys = {(item["kind"], item["metadata"]["name"])
            for item in rendered if not item["metadata"].get("namespace")}
    require(keys == MANAGEMENT_CLUSTER_KEYS,
            "management cluster-scoped render changed; refusing partial-apply cleanup")
    refs = []
    for kind, name in sorted(keys):
        require(kind in MANAGEMENT_CLUSTER_PATHS,
                f"unreviewed management cluster-scoped kind {kind}")
        refs.append(reference(None, kind.lower(), name,
                              MANAGEMENT_CLUSTER_PATHS[kind] + name))
    return refs


def management_orphan_owned(obj, run_id):
    labels = obj.get("metadata", {}).get("labels") or {}
    return (labels.get(RUN_LABEL) == run_id and
            labels.get(FLUX_NAME_LABEL) == MANAGEMENT_FLUX_NAME and
            labels.get(FLUX_NAMESPACE_LABEL) == "flux-system")


def cleanup_partial_management(args, host_proxy, contract, run_id, evidence):
    """Delete only known cluster-scoped Flux orphans with live UID/RV CAS."""
    for ref in management_cluster_refs(contract["rendered"]):
        obj = optional(args.host_kubeconfig, args.host_context, None,
                       ref["kind"], ref["name"])
        if obj is None:
            continue
        require(management_orphan_owned(obj, run_id),
                f"refusing foreign management {ref['kind']}/{ref['name']}")
        meta = obj["metadata"]
        uid, rv = meta["uid"], meta["resourceVersion"]
        try:
            host_proxy.delete(ref["path"], uid, rv)
        except RuntimeError:
            # Flux may finish pruning between our GET and UID/RV DELETE.
            # Only absence is benign; a changed object must fail closed.
            if optional(args.host_kubeconfig, args.host_context, None,
                        ref["kind"], ref["name"]) is None:
                continue
            raise
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = optional(args.host_kubeconfig, args.host_context, None,
                               ref["kind"], ref["name"])
            if current is None:
                evidence["partial_management_cleanup"].append(
                    {"kind": ref["kind"], "name": ref["name"], "uid": uid})
                break
            require(current["metadata"]["uid"] == uid,
                    f"replacement appeared for {ref['kind']}/{ref['name']}; refusing adoption")
            time.sleep(1)
        else:
            raise RuntimeError(f"timed out deleting management {ref['kind']}/{ref['name']}")


def create_owned(args, config, context, manifest, ref, run_id, sensitive=False):
    manifest.setdefault("metadata", {}).setdefault("labels", {})[RUN_LABEL] = run_id
    ref["intent"] = True
    scope = ("-n", ref["namespace"]) if ref["namespace"] else ()
    output = k(config, context, *scope, "create", "-f", "-", "-o", "json",
               input_text=json.dumps(manifest), sensitive=sensitive)
    ref["uid"] = json.loads(output)["metadata"]["uid"]


def delete_owned(proxy, config, context, ref, run_id):
    if not ref["intent"]:
        return
    obj = optional(config, context, ref["namespace"], ref["kind"], ref["name"])
    if obj is None:
        return
    meta = obj["metadata"]
    if ref["uid"]:
        require(meta["uid"] == ref["uid"],
                f"refusing replaced {ref['kind']}/{ref['name']}")
    else:
        require(meta.get("labels", {}).get(RUN_LABEL) == run_id,
                f"ambiguous create for {ref['kind']}/{ref['name']}; refusing adoption")
    proxy.delete(ref["path"], meta["uid"], meta["resourceVersion"])
    deadline = time.monotonic() + (360 if ref["kind"] == "kustomization" else 120)
    while time.monotonic() < deadline:
        current = optional(config, context, ref["namespace"], ref["kind"], ref["name"])
        if current is None or current["metadata"]["uid"] != meta["uid"]:
            return
        time.sleep(1)
    raise RuntimeError(f"timed out deleting owned {ref['kind']}/{ref['name']}")


def patch_json(args, config, context, namespace, kind, name, operations, private,
               sensitive=False):
    patch = private / "patch.json"
    private_write(patch, json.dumps(operations))
    scope = ("-n", namespace) if namespace else ()
    return json.loads(k(config, context, *scope, "patch", kind, name, "--type=json",
                        "--patch-file", str(patch), "-o", "json", sensitive=sensitive))


def set_core_suspend(args, snapshot, value, private, run_id):
    current = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                    "milo-core", "-n", "flux-system")
    old = snapshot["core"]
    meta = current["metadata"]
    require(meta["uid"] == old["metadata"]["uid"] and
            current["spec"].get("suspend", False) == (not value),
            "Milo core Flux UID or suspend state changed")
    marker = "apps-a1t.datum.net/suspend-run-id"
    marker_path = "/metadata/annotations/apps-a1t.datum.net~1suspend-run-id"
    old_annotations = old["metadata"].get("annotations", {})
    expected_annotations = dict(old_annotations)
    expected_annotations[marker] = run_id
    ops = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
           {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
           {"op": "test", "path": "/metadata/generation", "value": meta["generation"]}]
    if value:
        require(meta["generation"] == old["metadata"]["generation"] and
                meta.get("annotations", {}) == old_annotations and marker not in old_annotations,
                "Milo core changed before suspension")
        if "annotations" not in meta:
            ops.append({"op": "add", "path": "/metadata/annotations", "value": {}})
        ops += [{"op": "add", "path": marker_path, "value": run_id},
                {"op": "add", "path": "/spec/suspend", "value": True}]
    else:
        expected_spec = dict(old["spec"])
        expected_spec["suspend"] = True
        require(meta["generation"] == old["metadata"]["generation"] + 1 and
                current["spec"] == expected_spec and
                meta.get("annotations", {}) == expected_annotations,
                "Milo core spec/annotations changed while suspended")
        ops += [{"op": "test", "path": marker_path, "value": run_id},
                {"op": "remove", "path": marker_path if "annotations" in old["metadata"]
                 else "/metadata/annotations"},
                {"op": "test", "path": "/spec/suspend", "value": True},
                {"op": "remove" if "suspend" not in old["spec"] else "replace",
                 "path": "/spec/suspend", **({} if "suspend" not in old["spec"]
                                      else {"value": False})}]
    return patch_json(args, args.host_kubeconfig, args.host_context, "flux-system",
                      "kustomization", "milo-core", ops, private)


def patch_auth(args, snapshot, expected, replacement, private):
    current = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                    "milo-apiserver-auth-tokens", "-n", "milo-system")
    require(current["metadata"]["uid"] == snapshot["auth"]["metadata"]["uid"],
            "Milo static auth Secret UID changed")
    ops = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
           {"op": "test", "path": "/data/tokens.csv", "value": expected},
           {"op": "replace", "path": "/data/tokens.csv", "value": replacement}]
    return patch_json(args, args.host_kubeconfig, args.host_context, "milo-system",
                      "secret", "milo-apiserver-auth-tokens", ops, private, sensitive=True)


def append_auth(old_b64, tokens):
    original = base64.b64decode(old_b64).decode()
    suffix = "" if original.endswith("\n") else "\n"
    additions = "".join(f"{token},{user},{user},system:authenticated\n"
                        for user, token in tokens.items())
    return base64.b64encode((original + suffix + additions).encode()).decode()


def installer_role():
    names = sorted(HUB_SCHEMA_LOCK)
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": INSTALLER_SA},
            "rules": [
                {"apiGroups": ["apiextensions.k8s.io"],
                 "resources": ["customresourcedefinitions"], "verbs": ["create"]},
                {"apiGroups": ["apiextensions.k8s.io"],
                 "resources": ["customresourcedefinitions"],
                 "resourceNames": names,
                 "verbs": ["get", "update", "patch", "delete"]},
            ]}


def admission_installer_manifests():
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": ADMISSION_USER},
            "rules": [
                {"apiGroups": ["admissionregistration.k8s.io"],
                 "resources": ["mutatingwebhookconfigurations", "validatingwebhookconfigurations"],
                 "verbs": ["create"]},
                {"apiGroups": ["admissionregistration.k8s.io"],
                 "resources": ["mutatingwebhookconfigurations"],
                 "resourceNames": ["compute-mutating"], "verbs": ["get", "delete"]},
                {"apiGroups": ["admissionregistration.k8s.io"],
                 "resources": ["validatingwebhookconfigurations"],
                 "resourceNames": ["compute-validating"], "verbs": ["get", "delete"]},
            ]}
    binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
               "metadata": {"name": ADMISSION_USER},
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                           "name": ADMISSION_USER},
               "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User",
                             "name": ADMISSION_USER}]}
    return (role, binding)


def admission_observed_matches(manifest, observed, uid, run_id):
    """Allow only API-server defaults beyond the exact source webhooks + public CA."""
    if (not observed or observed["metadata"].get("uid") != uid or
            observed["metadata"].get("labels", {}).get(RUN_LABEL) != run_id or
            len(observed.get("webhooks", [])) != len(manifest["webhooks"])):
        return False
    defaults = {"matchPolicy": "Equivalent", "timeoutSeconds": 10,
                "namespaceSelector": {}, "objectSelector": {},
                "reinvocationPolicy": "Never"}
    for wanted, actual in zip(manifest["webhooks"], observed["webhooks"]):
        if (actual.get("name") != wanted["name"] or
                any(actual.get(key) != value for key, value in wanted.items()
                    if key not in ("clientConfig", "rules")) or
                any(actual.get(key) != value for key, value in defaults.items()
                    if key in actual and key not in wanted)):
            return False
        actual_rules = actual.get("rules", [])
        if len(actual_rules) != len(wanted["rules"]):
            return False
        for wanted_rule, actual_rule in zip(wanted["rules"], actual_rules):
            observed_rule = dict(actual_rule)
            if "scope" not in wanted_rule and observed_rule.get("scope") == "*":
                observed_rule.pop("scope")
            if observed_rule != wanted_rule:
                return False
        if set(actual) - set(wanted) - set(defaults):
            return False
        client = actual.get("clientConfig", {})
        expected_client = wanted["clientConfig"]
        if (client.get("caBundle") != expected_client["caBundle"] or
                set(client) != {"service", "caBundle"}):
            return False
        service = client.get("service", {})
        expected_service = expected_client["service"]
        if (any(service.get(key) != value for key, value in expected_service.items()) or
                set(service) - set(expected_service) - {"port"} or
                service.get("port", 443) != 443):
            return False
    return True


def kubeconfig_text(server, ca, token, tls_server_name=None):
    cluster = {"server": server, "certificate-authority-data": base64.b64encode(ca).decode()}
    if tls_server_name:
        cluster["tls-server-name"] = tls_server_name
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "current-context": "scoped",
                           "clusters": [{"name": "api", "cluster": cluster}],
                           "users": [{"name": "scoped", "user": {"token": token}}],
                           "contexts": [{"name": "scoped", "context": {"cluster": "api",
                                                                    "user": "scoped"}}]})


def can_i(config, verb, resource, expected, name=None, subresource=None):
    target = f"{resource}/{name}" if name else resource
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(config), "--context", "scoped",
         "--request-timeout=10s", "auth", "can-i", verb, target] +
        (["--subresource", subresource] if subresource else []),
        text=True, capture_output=True, timeout=20, check=False)
    answer = result.stdout.strip().splitlines()[-1:]
    require(answer in (["yes"], ["no"]),
            f"authorization review inconclusive for {verb} {target}")
    require((answer == ["yes"]) == expected,
            f"authorization review unexpected for {verb} {target}")
    print(f"{'allow' if expected else 'deny'}: {verb} {target}", flush=True)


def milo_http(port, ca_file, token, method, path, body=None):
    context = ssl.create_default_context(cafile=str(ca_file))
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        response.read(65537)
        return response.status
    finally:
        conn.close()


def milo_json_response(port, ca_file, token, method, path, body=None):
    """Bound one authenticated project request and its response body."""
    context = ssl.create_default_context(cafile=str(ca_file))
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(65537)
        require(len(raw) <= 65536, "Milo admission response exceeded 64 KiB")
        return response.status, json.loads(raw) if raw else {}
    finally:
        conn.close()


def probe_admission_installer(args, private, snapshot, token):
    config = private / "milo-admission-installer.yaml"
    private_write(config, kubeconfig_text(f"https://127.0.0.1:{args.direct_milo_port}",
                                        snapshot["milo_ca"], token))
    for kind, name in (("mutatingwebhookconfigurations", "compute-mutating"),
                       ("validatingwebhookconfigurations", "compute-validating")):
        resource = f"{kind}.admissionregistration.k8s.io"
        can_i(config, "create", resource, True)
        for verb in ("get", "delete"):
            can_i(config, verb, resource, True, name=name)
            can_i(config, verb, resource, False, name="apps-a1t-foreign")
        for verb in ("list", "watch", "update", "patch"):
            can_i(config, verb, resource, False)
    for verb, resource in (("get", "secrets"), ("create", "secrets"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("create", "clusterrolebindings.rbac.authorization.k8s.io"),
                           ("impersonate", "users"),
                           ("create", "workloads.compute.datumapis.com")):
        can_i(config, verb, resource, False)
    ca_file = private / "milo-direct-ca.pem"
    require(milo_http(args.direct_milo_port, ca_file, token, "GET",
                      "/api/v1/namespaces/default/secrets/apps-a1t-denial-probe") == 403,
            "admission installer unexpectedly read a Milo Secret")
    return config


def native_admission_probe(args, contract, snapshot, tokens, ca_file, run_id,
                           stage, evidence):
    """Compare genuine project dry-runs before and after native webhook registration."""
    project = PROJECTS[1]
    project_uid = snapshot["projects"][project]
    require(kjson(args.milo_kubeconfig, "milo-root", "get", "project", project)
            ["metadata"]["uid"] == project_uid, "Project UID changed before admission probe")
    probe = json.loads(json.dumps(contract["admission_probe"]))
    name = "apps-a1t-admission-" + run_id.rsplit("-", 1)[-1]
    probe["metadata"] = {"name": name, "namespace": "default"}
    evidence["native_admission"]["probe_workload_name"] = name
    root = f"{PROJECT_API}/{project}/control-plane/apis/compute.datumapis.com/v1alpha"
    collection = root + "/namespaces/default/workloads"
    item = collection + "/" + name
    token = tokens[USERS[1]]

    location_path = (f"{PROJECT_API}/{project}/control-plane"
                     "/apis/locations.miloapis.com/v1alpha1/locations")
    location_status, locations = milo_json_response(args.direct_milo_port, ca_file, token,
                                                    "GET", location_path)
    require(location_status == 200 and locations.get("kind") == "LocationList" and
            all(location.get("metadata", {}).get("name") != "dfw" for location in
                locations.get("items", [])),
            "genuine Project Location registry is unavailable or dfw already exists")
    location_hash = hashlib.sha256(json.dumps(locations.get("items", []),
                                              sort_keys=True).encode()).hexdigest()
    if stage == "baseline":
        evidence["native_admission"]["existing_locations_sha256"] = location_hash
    else:
        require(location_hash == evidence["native_admission"]["existing_locations_sha256"],
                "Project Locations changed between absent/present webhook probes")

    list_status, current_list = milo_json_response(args.direct_milo_port, ca_file, token,
                                                   "GET", collection)
    require(list_status == 200 and current_list.get("kind") == "WorkloadList",
            "could not snapshot genuine Project Workload collection")
    before_items = current_list.get("items", [])
    before_hash = hashlib.sha256(json.dumps(before_items, sort_keys=True).encode()).hexdigest()
    if stage == "baseline":
        evidence["native_admission"]["existing_workload_count"] = len(before_items)
        evidence["native_admission"]["existing_workloads_sha256"] = before_hash
    else:
        require(before_hash == evidence["native_admission"]["existing_workloads_sha256"],
                "Project Workloads changed between absent/present webhook probes")

    def absent():
        status, _ = milo_json_response(args.direct_milo_port, ca_file, token, "GET", item)
        require(status == 404, f"Workload probe {name} unexpectedly persisted: HTTP {status}")

    absent()
    status, result = milo_json_response(args.direct_milo_port, ca_file, token, "POST",
                                        collection + "?dryRun=All&fieldValidation=Strict", probe)
    if stage == "baseline":
        require(status == 201 and result.get("kind") == "Workload" and
                result.get("metadata", {}).get("name") == name,
                f"absent-webhook Workload dry-run did not pass: HTTP {status}")
        evidence["native_admission"]["absent_webhook_dryrun_status"] = status
    else:
        message = str(result.get("message", ""))
        lower = message.lower()
        require(400 <= status < 500 and
                'admission webhook "vworkload.kb.io" denied the request' in lower and
                "location" in lower and "dfw" in lower and
                not any(failure in lower for failure in
                        ("failed calling webhook", "x509:", "no such host", "connection refused")),
                f"native Workload validator did not semantically reject missing Location: HTTP {status}")
        evidence["native_admission"]["validator_dryrun_status"] = status
        evidence["native_admission"]["validator_hook"] = "vworkload.kb.io"
        evidence["native_admission"]["denial_category"] = "missing genuine Location dfw"
    absent()
    after_status, after_list = milo_json_response(args.direct_milo_port, ca_file, token,
                                                  "GET", collection)
    require(after_status == 200 and
            hashlib.sha256(json.dumps(after_list.get("items", []), sort_keys=True).encode())
            .hexdigest() == before_hash,
            "Project Workload collection changed after dry-run")
    location_status, after_locations = milo_json_response(args.direct_milo_port, ca_file,
                                                         token, "GET", location_path)
    require(location_status == 200 and hashlib.sha256(json.dumps(
            after_locations.get("items", []), sort_keys=True).encode()).hexdigest() ==
            location_hash, "Project Locations changed during admission dry-run")
    require(kjson(args.milo_kubeconfig, "milo-root", "get", "project", project)
            ["metadata"]["uid"] == project_uid, "Project UID changed during admission probe")
    evidence["native_admission"]["workload_persisted"] = False


def probe_identities(args, private, snapshot, tokens, hub_config):
    ca_file = private / "milo-direct-ca.pem"
    private_write(ca_file, snapshot["milo_ca"].decode())
    direct_server = f"https://127.0.0.1:{args.direct_milo_port}"
    configs = {}
    for user in USERS:
        config = private / f"{user}.yaml"
        private_write(config, kubeconfig_text(direct_server, snapshot["milo_ca"], tokens[user]))
        configs[user] = config
    discovery, project, quota = (configs[user] for user in USERS)
    for verb in ("get", "list", "watch"):
        can_i(discovery, verb, "projects.resourcemanager.miloapis.com", True)
    for verb, resource in (("get", "secrets"), ("create", "workloads.compute.datumapis.com"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users")):
        can_i(discovery, verb, resource, False)
    for verb, resource in (("get", "secrets"), ("list", "secrets"),
                           ("get", "workloads.compute.datumapis.com"),
                           ("create", "workloads.compute.datumapis.com"),
                           ("create", "subjectaccessreviews.authorization.k8s.io")):
        can_i(project, verb, resource, True)
    for verb, resource in (("create", "secrets"), ("update", "secrets"),
                           ("delete", "secrets"),
                           ("create", "resourceclaims.quota.miloapis.com"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users")):
        can_i(project, verb, resource, False)
    for verb in ("create", "get", "list", "watch", "delete"):
        can_i(quota, verb, "resourceclaims.quota.miloapis.com", True)
    can_i(quota, "update", "resourceclaims.quota.miloapis.com", False, subresource="status")
    for verb, resource in (("get", "secrets"),
                           ("create", "workloads.compute.datumapis.com"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users")):
        can_i(quota, verb, resource, False)
    for verb, resource in (("list", "clusters.cluster.karmada.io"),
                           ("create", "networkbindings.networking.datumapis.com"),
                           ("get", "secrets")):
        can_i(hub_config, verb, resource, True)
    for verb, resource in (("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users"),
                           ("create", "serviceaccounts")):
        can_i(hub_config, verb, resource, False)

    provider = f"{PROJECT_API}/{PROJECTS[0]}/control-plane"
    consumer = f"{PROJECT_API}/{PROJECTS[1]}/control-plane"
    require(milo_http(args.direct_milo_port, ca_file, tokens[USERS[0]], "GET",
                      f"{PROJECT_API}/{PROJECTS[0]}") == 200,
            "discovery identity could not GET a real Project")
    require(milo_http(args.direct_milo_port, ca_file, tokens[USERS[0]], "GET",
                      provider + "/api/v1/namespaces/default/secrets") == 403,
            "discovery identity unexpectedly read Project Secrets")
    require(milo_http(args.direct_milo_port, ca_file, tokens[USERS[1]], "POST",
                      provider + "/api/v1/namespaces/default/secrets", {}) == 403,
            "project identity unexpectedly wrote Project Secrets")
    require(milo_http(args.direct_milo_port, ca_file, tokens[USERS[1]], "POST",
                      provider + "/apis/quota.miloapis.com/v1alpha1/namespaces/default/resourceclaims",
                      {}) == 403,
            "project identity unexpectedly created quota claims")
    for user in (USERS[0], USERS[2]):
        require(milo_http(args.direct_milo_port, ca_file, tokens[user], "GET",
                          provider + "/api/v1/namespaces/default/secrets") == 403,
                f"{user} unexpectedly read Project Secrets")
    # Static RBAC grants project-controller Secret reads across all Projects.
    # Probe a missing named Secret only; never retrieve or print Secret values.
    for root in (provider, consumer):
        status = milo_http(args.direct_milo_port, ca_file, tokens[USERS[1]], "GET",
                           root + "/api/v1/namespaces/default/secrets/apps-a1t-never-created")
        require(status == 404, "project controller did not receive expected cross-Project read grant")
    result = subprocess.run(["kubectl", "--kubeconfig", str(hub_config), "--context", "scoped",
                             "--request-timeout=10s", "get", "clusterroles", "-o", "name"],
                            text=True, capture_output=True, timeout=20, check=False)
    require(result.returncode != 0 and "Forbidden" in result.stderr,
            "Karmada runtime identity unexpectedly read ClusterRoles")
    print("Distinct Milo/Karmada identities passed direct allow and denial probes", flush=True)


def probe_installer(config):
    crd = "customresourcedefinitions.apiextensions.k8s.io"
    can_i(config, "create", crd, True)
    for name in ("instances.compute.datumapis.com",
                 "networkbindings.networking.datumapis.com"):
        for verb in ("get", "update", "patch", "delete"):
            can_i(config, verb, crd, True, name=name)
    can_i(config, "get", crd, False, name="clusters.cluster.karmada.io")
    can_i(config, "patch", crd, False, name="clusters.cluster.karmada.io")
    for verb, resource in (("get", "secrets"), ("create", "secrets"),
                           ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("create", "workloads.compute.datumapis.com"),
                           ("impersonate", "users")):
        can_i(config, verb, resource, False)
    for resource, name in (("crd", "clusters.cluster.karmada.io"),
                           ("secret", "apps-a1t-installer-deny-probe")):
        cmd = ["kubectl", "--kubeconfig", str(config), "--context", "scoped",
               "--request-timeout=10s", "get", resource, name]
        if resource == "secret":
            cmd += ["-n", HUB_NAMESPACE]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=20,
                                check=False)
        require(result.returncode != 0 and "Forbidden" in result.stderr,
                f"hub CRD installer did not receive actual {resource} GET denial")
    print("Dedicated hub CRD installer passed named allow and actual denial probes", flush=True)


def hub_crd_state(args, contract, run_id):
    revisions = {}
    for kind, name, expected_sha in (
            ("gitrepository", "compute-a1t-controller", SOURCE_SHA),
            ("gitrepository", "compute-a1t-hub-nso-source", NSO_SHA),
            ("kustomization", "compute-a1t-hub-compute-crds", SOURCE_SHA),
            ("kustomization", "compute-a1t-hub-nso-crds", NSO_SHA)):
        obj = kjson(args.host_kubeconfig, args.host_context, "get", kind, name,
                    "-n", "flux-system")
        revision = (obj.get("status", {}).get("artifact", {}).get("revision") if
                    kind == "gitrepository" else
                    obj.get("status", {}).get("lastAppliedRevision"))
        require(ready(obj) and revision == "sha1:" + expected_sha,
                f"host Flux {kind}/{name} not Ready at exact pinned source")
        revisions[name] = revision
    uids = {}
    for expected in contract["hub_crds"]:
        name = expected["metadata"]["name"]
        live = kjson(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                     "get", "crd", name)
        require(live["metadata"].get("labels", {}).get(RUN_LABEL) == run_id and
                schema_hash(live) == HUB_SCHEMA_LOCK[name] and
                any(item.get("type") == "Established" and item.get("status") == "True"
                    for item in live.get("status", {}).get("conditions", [])),
                f"remote Flux CRD {name} lacks owned label, exact schema, or Established")
        uids[name] = live["metadata"]["uid"]
    return {"revisions": revisions, "schema_hashes": HUB_SCHEMA_LOCK,
            "crd_uids": uids}


def management_state(args):
    source = kjson(args.host_kubeconfig, args.host_context, "get", "gitrepository",
                   "compute-a1t-controller", "-n", "flux-system")
    runtime = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                    "compute-a1t-management", "-n", "flux-system")
    revision = "sha1:" + SOURCE_SHA
    require(ready(source) and source["status"]["artifact"]["revision"] == revision and
            ready(runtime) and runtime["status"]["lastAppliedRevision"] == revision,
            "Compute Flux source/runtime not Ready at exact pinned SHA")
    deployment = kjson(args.host_kubeconfig, args.host_context, "get", "deployment",
                       "compute-manager", "-n", "compute-system")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    require(container["image"] == IMAGE and
            container.get("readinessProbe", {}).get("httpGet", {}).get("path") == "/readyz" and
            deployment["status"].get("readyReplicas") == deployment["spec"]["replicas"] and
            deployment["status"].get("availableReplicas") == deployment["spec"]["replicas"],
            "Compute management manager image, source readiness gate, or replicas changed")
    pods = kjson(args.host_kubeconfig, args.host_context, "get", "pods", "-n", "compute-system",
                 "-l", "control-plane=compute")["items"]
    active = [item for item in pods if not item["metadata"].get("deletionTimestamp")]
    require(len(active) == deployment["spec"]["replicas"],
            "Compute manager running Pod count differs from Deployment")
    image_ids = {}
    hub_watch_synced = False
    for pod in active:
        containers = pod["spec"]["containers"]
        statuses = pod.get("status", {}).get("containerStatuses", [])
        require(len(containers) == 1 and containers[0]["image"] == IMAGE and
                len(statuses) == 1 and statuses[0].get("ready") is True,
                "Compute manager Pod image or Ready state changed")
        image_id = statuses[0].get("imageID", "")
        require(any(image_id.endswith(digest) for digest in
                    (IMAGE_INDEX, ARM_MANIFEST, ARM_CONFIG)),
                "Compute manager running imageID differs from pinned OCI index/arm64 chain")
        image_ids[pod["metadata"]["uid"]] = image_id
        require(any(item.get("type") == "Ready" and item.get("status") == "True"
                    for item in pod.get("status", {}).get("conditions", [])),
                "Compute Pod did not pass /readyz project discovery and engagement checks")
        logs = k(args.host_kubeconfig, args.host_context, "logs",
                 "pod/" + pod["metadata"]["name"], "-n", "compute-system", "-c", "manager",
                 "--since=30m", "--tail=5000", timeout=30)
        hub_watch_synced = hub_watch_synced or any(
            "Starting workers" in line and "instance-projector" in line for line in logs.splitlines())
    require(hub_watch_synced,
            "active Karmada Instance controller has no post-cache-sync worker marker")
    for kind, name in (("mutatingwebhookconfiguration", "compute-mutating"),
                       ("validatingwebhookconfiguration", "compute-validating")):
        require(optional(args.host_kubeconfig, args.host_context, None, kind, name) is not None,
                f"Compute {kind}/{name} missing after Flux Ready")
    return {"source_revision": source["status"]["artifact"]["revision"],
            "runtime_revision": runtime["status"]["lastAppliedRevision"],
            "deployment_uid": deployment["metadata"]["uid"], "pod_image_ids": image_ids,
            "watch_sync": {"milo": "Pod /readyz includes project-discovery-synced and projects-engaged",
                           "hub": "instance-projector Starting workers follows source.Kind cache sync"}}


def failure_diagnostics(args):
    """Bounded, whitelisted status snapshot taken before failure teardown."""
    targets = {
        "compute_source": ("flux-system", "gitrepository", "compute-a1t-controller"),
        "nso_source": ("flux-system", "gitrepository", "compute-a1t-hub-nso-source"),
        "compute_hub_crds": ("flux-system", "kustomization", "compute-a1t-hub-compute-crds"),
        "nso_hub_crds": ("flux-system", "kustomization", "compute-a1t-hub-nso-crds"),
        "management": ("flux-system", "kustomization", "compute-a1t-management"),
        "deployment": ("compute-system", "deployment", "compute-manager"),
        "pods": ("compute-system", "pods", "-l=control-plane=compute"),
    }

    def clean(value):
        value = " ".join(str(value or "").split())
        value = re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", value)
        value = re.sub(r"(?i)\b(token|password|authorization|private.key)(\s*[:=]\s*)\S+",
                       r"\1\2[redacted]", value)
        return value[:220]

    def fetch(name, target):
        namespace, kind, object_name = target
        command = ["kubectl", "--kubeconfig", str(args.host_kubeconfig),
                   "--context", args.host_context, "--request-timeout=3s",
                   "-n", namespace, "get", kind, object_name, "-o", "json"]
        try:
            result = subprocess.run(command, text=True, capture_output=True,
                                    timeout=4, check=False)
        except subprocess.TimeoutExpired:
            return name, {"unavailable": "timeout"}
        if result.returncode:
            stderr = result.stderr
            reason = ("NotFound" if "NotFound" in stderr else
                      "Forbidden" if "Forbidden" in stderr else "api_error")
            return name, {"unavailable": reason}
        try:
            obj = json.loads(result.stdout)
        except json.JSONDecodeError:
            return name, {"unavailable": "invalid_json"}
        if kind in ("gitrepository", "kustomization"):
            status = obj.get("status") or {}
            return name, {
                "generation": obj.get("metadata", {}).get("generation"),
                "observed_generation": status.get("observedGeneration"),
                "revision": ((status.get("artifact") or {}).get("revision") if
                             kind == "gitrepository" else status.get("lastAppliedRevision")),
                "conditions": [{"type": clean(item.get("type")),
                                "status": clean(item.get("status")),
                                "reason": clean(item.get("reason")),
                                "message": clean(item.get("message"))}
                               for item in (status.get("conditions") or [])[:5]],
            }
        if kind == "deployment":
            status = obj.get("status") or {}
            return name, {"desired": obj.get("spec", {}).get("replicas"),
                          "ready": status.get("readyReplicas", 0),
                          "available": status.get("availableReplicas", 0)}
        selected = []
        for pod in obj.get("items", [])[:8]:
            status = pod.get("status") or {}
            selected.append({
                "name": pod.get("metadata", {}).get("name"),
                "phase": status.get("phase"),
                "containers": [{"name": item.get("name"),
                                "image_id": item.get("imageID"),
                                "waiting_reason": clean(((item.get("state") or {}).get("waiting") or {}).get("reason")),
                                "waiting_message": clean(((item.get("state") or {}).get("waiting") or {}).get("message"))}
                               for item in (status.get("containerStatuses") or [])[:4]],
            })
        return name, {"items": selected, "total_count": len(obj.get("items", []))}

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, name, target) for name, target in targets.items()]
        try:
            for future in as_completed(futures, timeout=18):
                try:
                    name, value = future.result()
                    results[name] = value
                except Exception:
                    # A broken diagnostic must never obscure the startup failure.
                    continue
        except TimeoutError:
            results["collection"] = {"unavailable": "partial_timeout"}
    return results


def restart_milo(args):
    k(args.host_kubeconfig, args.host_context, "rollout", "restart",
      "deployment/milo-apiserver", "-n", "milo-system")
    k(args.host_kubeconfig, args.host_context, "rollout", "status",
      "deployment/milo-apiserver", "-n", "milo-system", "--timeout=180s", timeout=190)
    wait_for(lambda: k(args.milo_kubeconfig, "milo-root", "get", "--raw", "/readyz") == "ok",
             "Milo API after scoped auth reload", 120)


def execute(args, private, contract, snapshot, preflight_seconds):
    global GLOBAL_DEADLINE
    started = time.monotonic()
    GLOBAL_DEADLINE = time.monotonic() + 1800
    run_id = "apps-compute-management-" + secrets.token_hex(8)
    tokens = {user: secrets.token_urlsafe(48) for user in USERS}
    if args.native_admission:
        tokens[ADMISSION_USER] = secrets.token_urlsafe(48)
    original_csv = snapshot["auth_b64"]
    new_csv = append_auth(original_csv, tokens)
    manager_csv = append_auth(original_csv, {user: tokens[user] for user in USERS})
    refs = []
    errors = []
    succeeded = False
    suspend_intent = False
    auth_intent = False
    admission_retained = False
    admission_stage_started = False
    direct = None
    host_proxy = ApiProxy(private, args.host_kubeconfig, args.host_context, "host")
    milo_proxy = ApiProxy(private, args.milo_kubeconfig, "milo-root", "milo")
    hub_proxy = ApiProxy(private, args.karmada_admin_kubeconfig, args.karmada_admin_context,
                         "karmada")
    fd, evidence_name = tempfile.mkstemp(prefix="apps-a1t-compute-management-evidence-",
                                         suffix=".json")
    os.close(fd)
    evidence_path = Path(evidence_name)
    evidence = {"run_id": run_id, "source_revision": "sha1:" + SOURCE_SHA,
                "nso_crd_source_revision": "sha1:" + NSO_SHA,
                "compute_image": IMAGE, "arm64_manifest": ARM_MANIFEST,
                "arm64_config": ARM_CONFIG,
                "identity_mode": ("three static Milo RBAC Users, one optional admission installer User, "
                                  "and two distinct short-lived Karmada ServiceAccounts"),
                "project_uids": snapshot["projects"], "checks": [],
                "preflight_seconds": round(preflight_seconds, 3),
                "result": "incomplete", "cleanup": "pending",
                "partial_management_cleanup": []}
    if args.native_admission:
        evidence["native_admission"] = {"mode": "source-rendered Milo registration, project dry-run only",
                                        "workload_persisted": "unverified",
                                        "project": PROJECTS[1],
                                        "project_uid": snapshot["projects"][PROJECTS[1]]}

    def record(check):
        evidence["checks"].append(check)
        private_write(evidence_path, json.dumps(evidence, indent=2) + "\n")

    def add(surface, manifest, namespace, path, sensitive=False):
        config, context = {
            "host": (args.host_kubeconfig, args.host_context),
            "milo": (args.milo_kubeconfig, "milo-root"),
            "hub": (args.karmada_admin_kubeconfig, args.karmada_admin_context),
        }[surface]
        ref = reference(namespace, manifest["kind"].lower(), manifest["metadata"]["name"], path)
        refs.append((surface, ref))
        create_owned(args, config, context, manifest, ref, run_id, sensitive=sensitive)

    record("offline exact source and live read-only prerequisite/collision preflight")
    try:
        host_proxy.start()
        milo_proxy.start()
        hub_proxy.start()
        installer_sa = {"apiVersion": "v1", "kind": "ServiceAccount",
                        "metadata": {"name": INSTALLER_SA, "namespace": HUB_NAMESPACE},
                        "automountServiceAccountToken": False}
        add("hub", installer_sa, HUB_NAMESPACE,
            f"/api/v1/namespaces/{HUB_NAMESPACE}/serviceaccounts/{INSTALLER_SA}")
        add("hub", installer_role(), None,
            f"/apis/rbac.authorization.k8s.io/v1/clusterroles/{INSTALLER_SA}")
        installer_binding = {"apiVersion": "rbac.authorization.k8s.io/v1",
                             "kind": "ClusterRoleBinding",
                             "metadata": {"name": INSTALLER_SA},
                             "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                                         "kind": "ClusterRole", "name": INSTALLER_SA},
                             "subjects": [{"kind": "ServiceAccount", "name": INSTALLER_SA,
                                           "namespace": HUB_NAMESPACE}]}
        add("hub", installer_binding, None,
            f"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{INSTALLER_SA}")
        installer_token = k(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                            "-n", HUB_NAMESPACE, "create", "token", INSTALLER_SA,
                            "--duration=55m", sensitive=True)
        require(len(installer_token) > 80,
                "Karmada TokenRequest produced no dedicated hub CRD installer token")
        installer_probe = private / "karmada-crd-installer-host-probe.yaml"
        private_write(installer_probe, kubeconfig_text(snapshot["hub_host_server"],
                      snapshot["hub_ca"], installer_token, tls_server_name="localhost"))
        probe_installer(installer_probe)
        record("hub CRD installer has only exact-name CRD change grants and passed denials")
        installer_runtime = kubeconfig_text(args.karmada_internal_server,
                                            snapshot["hub_ca"], installer_token,
                                            tls_server_name="localhost")
        installer_secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                            "metadata": {"name": INSTALLER_KUBECONFIG_SECRET,
                                         "namespace": "flux-system"},
                            "stringData": {"kubeconfig": installer_runtime}}
        add("host", installer_secret, "flux-system",
            f"/api/v1/namespaces/flux-system/secrets/{INSTALLER_KUBECONFIG_SECRET}",
            sensitive=True)
        add("host", json.loads(json.dumps(contract["source"])), "flux-system",
            "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/compute-a1t-controller")
        add("host", json.loads(json.dumps(contract["nso_source"])), "flux-system",
            "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/compute-a1t-hub-nso-source")
        flux_args = ("flux", "--kubeconfig", str(args.host_kubeconfig),
                     "--context", args.host_context)
        for name in ("compute-a1t-controller", "compute-a1t-hub-nso-source"):
            run(*flux_args, "reconcile", "source", "git", name,
                "-n", "flux-system", "--timeout=5m", timeout=310)
        # The initial preflight refuses adoption; repeat immediately before
        # creating either remote Flux Kustomization to narrow the race window.
        for item in contract["hub_crds"]:
            name = item["metadata"]["name"]
            require(optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                             None, "crd", name) is None,
                    f"hub CRD {name} appeared before remote Flux creation; refusing adoption")
        for key in ("hub_compute", "hub_nso"):
            item = json.loads(json.dumps(contract[key]))
            item["spec"]["commonMetadata"] = {"labels": {RUN_LABEL: run_id}}
            name = item["metadata"]["name"]
            add("host", item, "flux-system",
                f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/{name}")
            run(*flux_args, "reconcile", "kustomization", name, "-n", "flux-system",
                "--timeout=6m", timeout=370)
        wait_for(lambda: hub_crd_state(args, contract, run_id),
                 "exact pinned Compute/NSO Karmada hub CRDs Established", 180)
        evidence["observed_hub_crds"] = hub_crd_state(args, contract, run_id)
        record("two remote Flux source bundles installed exact 16 hub CRD schemas")

        service_account = {"apiVersion": "v1", "kind": "ServiceAccount",
                           "metadata": {"name": HUB_SA, "namespace": HUB_NAMESPACE},
                           "automountServiceAccountToken": False}
        add("hub", service_account, HUB_NAMESPACE,
            f"/api/v1/namespaces/{HUB_NAMESPACE}/serviceaccounts/{HUB_SA}")
        hub_role = json.loads(json.dumps(contract["hub_role"]))
        add("hub", hub_role, None,
            "/apis/rbac.authorization.k8s.io/v1/clusterroles/compute-manager")
        hub_binding = {"apiVersion": "rbac.authorization.k8s.io/v1",
                       "kind": "ClusterRoleBinding", "metadata": {"name": "compute-manager"},
                       "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                                   "kind": "ClusterRole", "name": "compute-manager"},
                       "subjects": [{"kind": "ServiceAccount", "name": HUB_SA,
                                     "namespace": HUB_NAMESPACE}]}
        add("hub", hub_binding, None,
            "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/compute-manager")
        hub_token = k(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                      "-n", HUB_NAMESPACE, "create", "token", HUB_SA, "--duration=55m",
                      sensitive=True)
        require(len(hub_token) > 80, "Karmada TokenRequest produced no scoped token")
        hub_config = private / "karmada-runtime.yaml"
        private_write(hub_config, kubeconfig_text(args.karmada_internal_server,
                      snapshot["hub_ca"], hub_token, tls_server_name="localhost"))
        hub_probe_config = private / "karmada-host-probe.yaml"
        private_write(hub_probe_config, kubeconfig_text(snapshot["hub_host_server"],
                      snapshot["hub_ca"], hub_token, tls_server_name="localhost"))
        record("Karmada upstream ClusterRole bound only to fresh short-lived ServiceAccount")

        suspend_intent = True
        set_core_suspend(args, snapshot, True, private, run_id)
        for manifest in contract["milo_roles"]:
            plural = "clusterroles" if manifest["kind"] == "ClusterRole" else "clusterrolebindings"
            add("milo", json.loads(json.dumps(manifest)), None,
                f"/apis/rbac.authorization.k8s.io/v1/{plural}/{manifest['metadata']['name']}")
        if args.native_admission:
            for manifest in admission_installer_manifests():
                plural = "clusterroles" if manifest["kind"] == "ClusterRole" else "clusterrolebindings"
                add("milo", manifest, None,
                    f"/apis/rbac.authorization.k8s.io/v1/{plural}/{ADMISSION_USER}")
        auth_intent = True
        patch_auth(args, snapshot, original_csv, new_csv, private)
        restart_milo(args)
        milo_ca_file = private / "milo-direct-ca.pem"
        private_write(milo_ca_file, snapshot["milo_ca"].decode())
        direct = DirectMilo(args, milo_ca_file)
        direct.start()
        probe_identities(args, private, snapshot, tokens, hub_probe_config)
        admission_config = None
        if args.native_admission:
            admission_config = probe_admission_installer(args, private, snapshot,
                                                         tokens[ADMISSION_USER])
        record("three distinct Milo Users and Karmada SA passed actual allow/deny probes before mounts")

        namespace = {"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": "compute-system"}}
        add("host", namespace, None, "/api/v1/namespaces/compute-system")
        wait_for(lambda: kjson(args.host_kubeconfig, args.host_context, "get", "namespace",
                               "compute-system").get("status", {}).get("phase") == "Active",
                 "owned compute-system Namespace Active", 60)
        # Compute's source-owned CSI volume requires a namespaced CA Issuer.
        # This is a test-infra bootstrap exception: only the three reviewed
        # cert-manager objects are created directly, and only their public
        # certificate fields are read for trust verification.
        for manifest in contract["cert_bootstrap"]:
            kind = manifest["kind"].lower()
            name = manifest["metadata"]["name"]
            plural = "certificates" if kind == "certificate" else "issuers"
            add("host", json.loads(json.dumps(manifest)), "compute-system",
                f"/apis/cert-manager.io/v1/namespaces/compute-system/{plural}/{name}")
            wait_for(lambda kind=kind, name=name: ready(kjson(
                args.host_kubeconfig, args.host_context, "get", kind, name,
                "-n", "compute-system")), f"owned Compute {kind}/{name} Ready", 120)
        webhook_ca = public_secret_field(args, "compute-system", WEBHOOK_CA_SECRET, "ca.crt")
        webhook_cert = public_secret_field(args, "compute-system", WEBHOOK_CA_SECRET, "tls.crt")
        ca_file = private / "compute-webhook-public-ca.pem"
        cert_file = private / "compute-webhook-public-cert.pem"
        private_write(ca_file, webhook_ca.decode())
        private_write(cert_file, webhook_cert.decode())
        require(run("openssl", "verify", "-CAfile", str(ca_file), str(cert_file))
                .endswith(": OK") and
                "CA:TRUE" in run("openssl", "x509", "-in", str(cert_file), "-noout", "-text"),
                "task-owned Compute webhook public CA certificate failed verification")
        run("openssl", "x509", "-in", str(cert_file), "-checkend", "3600", "-noout")
        evidence["webhook_ca_public_sha256"] = hashlib.sha256(webhook_cert).hexdigest()
        record("task-owned cert-manager CA Certificate and CA Issuer Ready; public CA verified")
        secret_tokens = (("compute-a1t-milo-discovery-kubeconfig", USERS[0]),
                         ("compute-a1t-milo-project-kubeconfig", USERS[1]),
                         ("compute-a1t-quota-kubeconfig", USERS[2]))
        for name, user in secret_tokens:
            scoped = kubeconfig_text("https://milo-apiserver.milo-system.svc:6443",
                                     snapshot["milo_ca"], tokens[user])
            manifest = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": name, "namespace": "compute-system"},
                        "stringData": {"kubeconfig": scoped}}
            add("host", manifest, "compute-system",
                f"/api/v1/namespaces/compute-system/secrets/{name}", sensitive=True)
        hub_secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                      "metadata": {"name": "compute-a1t-karmada-kubeconfig",
                                   "namespace": "compute-system"},
                      "stringData": {"kubeconfig": hub_config.read_text()}}
        add("host", hub_secret, "compute-system",
            "/api/v1/namespaces/compute-system/secrets/compute-a1t-karmada-kubeconfig",
            sensitive=True)
        runtime = json.loads(json.dumps(contract["management"]))
        runtime["spec"]["commonMetadata"] = {"labels": {RUN_LABEL: run_id}}
        if args.fault_invalid_runtime:
            runtime["spec"]["patches"].append({
                "target": {"group": "apps", "version": "v1", "kind": "Deployment", "name": "compute-manager"},
                "patch": json.dumps([{ "op": "add", "path": "/spec/template/spec/containers/0/volumeMounts/-",
                                       "value": {"name": "apps-fault-missing-volume", "mountPath": "/apps-fault"}}])})
        add("host", runtime, "flux-system",
            "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/compute-a1t-management")
        if args.fault_invalid_runtime:
            def rejected_runtime():
                obj = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                            "compute-a1t-management", "-n", "flux-system")
                return any(condition.get("type") == "Ready" and condition.get("status") == "False" and
                           condition.get("reason") == "ReconciliationFailed" and
                           "apps-fault-missing-volume" in condition.get("message", "")
                           for condition in obj.get("status", {}).get("conditions", []))
            wait_for(rejected_runtime, "injected Deployment rejection after partial Flux apply", 90)
            partial = optional(args.host_kubeconfig, args.host_context, None, "clusterrole", "compute")
            require(partial is not None and management_orphan_owned(partial, run_id),
                    "fault did not produce the expected owned partial-apply ClusterRole")
            record("injected invalid Deployment was rejected after an owned ClusterRole was created")
            raise RuntimeError("expected injected invalid Deployment rejection; inspect cleanup evidence")
        run(*flux_args, "reconcile", "kustomization", "compute-a1t-management",
            "-n", "flux-system", "--with-source", "--timeout=8m", timeout=490)
        wait_for(lambda: management_state(args),
                 "all Compute replicas Ready and active Instance controller watch synchronized", 120)
        evidence["observed_management"] = management_state(args)
        record("native Compute management Flux, webhooks and actual pinned-image Pods Ready")
        if args.native_admission:
            # The management overlay already deploys these two source-rendered
            # configurations to the host API. Milo owns a separate API server;
            # register exact copies there with only the task-owned public CA.
            admission_stage_started = True
            admission_started = time.monotonic()
            for kind, name in ADMISSION_NAMES.items():
                require(optional(args.milo_kubeconfig, "milo-root", None,
                                 kind.lower(), name) is None,
                        f"Milo {kind}/{name} appeared before baseline; refusing adoption")
            native_admission_probe(args, contract, snapshot, tokens, milo_ca_file,
                                   run_id, "baseline", evidence)
            record("absent Milo WCs: authenticated Project Workload dry-run admitted, no persistence")
            for source_manifest in contract["admission_configs"]:
                manifest = json.loads(json.dumps(source_manifest))
                for hook in manifest["webhooks"]:
                    hook["clientConfig"]["caBundle"] = base64.b64encode(webhook_ca).decode()
                kind, name = manifest["kind"], manifest["metadata"]["name"]
                path = MANAGEMENT_CLUSTER_PATHS[kind] + name
                ref = reference(None, kind.lower(), name, path)
                refs.append(("milo", ref))
                create_owned(args, admission_config, "scoped", manifest, ref, run_id)
                observed = optional(admission_config, "scoped", None, kind.lower(), name)
                require(admission_observed_matches(manifest, observed, ref["uid"], run_id),
                        f"Milo {kind}/{name} differs from pinned source plus public CA")
            evidence["native_admission"]["public_ca_sha256"] = hashlib.sha256(webhook_ca).hexdigest()
            evidence["native_admission"]["webhook_uids"] = {
                ref["name"]: ref["uid"] for surface, ref in refs
                if surface == "milo" and ref["name"] in ADMISSION_NAMES.values()}
            native_admission_probe(args, contract, snapshot, tokens, milo_ca_file,
                                   run_id, "registered", evidence)
            evidence["native_admission"]["probe_seconds"] = round(
                time.monotonic() - admission_started, 3)
            record("Milo native fail-closed WCs: named Workload validator rejected absent genuine Location; no persistence")
        succeeded = True
    except Exception:
        try:
            evidence["failure_diagnostics"] = failure_diagnostics(args)
            private_write(evidence_path, json.dumps(evidence, indent=2) + "\n")
        except Exception:
            # Diagnostics are optional evidence; the original failure and
            # the unconditional cleanup take precedence.
            evidence["failure_diagnostics"] = {"unavailable": "diagnostic_error"}
        raise
    finally:
        cleanup_started = time.monotonic()
        evidence["startup_seconds"] = round(cleanup_started - started, 3)
        # Teardown has its own deadline even when the startup deadline expires.
        GLOBAL_DEADLINE = time.monotonic() + 600
        try:
            admission_refs = [ref for surface, ref in refs if surface == "milo" and
                              (ref["kind"], ref["name"]) in
                              {(kind.lower(), name) for kind, name in ADMISSION_NAMES.items()}]
            if admission_stage_started:
                # Milo admission invokes the host manager with failurePolicy
                # Fail. Preserve that manager and its CA unless both Milo
                # registrations are certainly absent.
                for ref in reversed(admission_refs):
                    try:
                        delete_owned(milo_proxy, args.milo_kubeconfig, "milo-root", ref, run_id)
                    except Exception as exc:
                        admission_retained = True
                        errors.append(f"Milo admission {ref['kind']}/{ref['name']}: {exc}")
                for kind, name in ADMISSION_NAMES.items():
                    try:
                        if optional(args.milo_kubeconfig, "milo-root", None,
                                    kind.lower(), name) is not None:
                            admission_retained = True
                            errors.append(f"Milo {kind}/{name} remains after UID/RV cleanup")
                    except Exception as exc:
                        admission_retained = True
                        errors.append(f"Milo {kind}/{name} absence unknown: {exc}")
                if admission_retained:
                    evidence["recovery"] = (
                        "Native fail-closed Milo webhook absence unconfirmed. Host Compute manager, "
                        "compute-system CA, runtime Flux, Milo core suspension, and hub CRDs "
                        "are retained. Inspect exact Milo webhook UIDs and remove them before "
                        "tearing down the manager or CA. Admission installer revocation is attempted "
                        "separately; Karmada CRD installer access is also revoked. See cleanup errors.")
                    raise RuntimeError("Milo admission removal incomplete; retaining manager and CA")
                evidence["native_admission"]["milo_webhooks_absent_after_cleanup"] = True
                if evidence["native_admission"].get("existing_workloads_sha256"):
                    try:
                        project = PROJECTS[1]
                        root = f"{PROJECT_API}/{project}/control-plane"
                        collection = root + "/apis/compute.datumapis.com/v1alpha/namespaces/default/workloads"
                        token = tokens[USERS[1]]
                        code, listing = milo_json_response(args.direct_milo_port, milo_ca_file,
                                                           token, "GET", collection)
                        current_hash = hashlib.sha256(json.dumps(
                            listing.get("items", []), sort_keys=True).encode()).hexdigest()
                        require(code == 200 and current_hash == evidence["native_admission"]
                                ["existing_workloads_sha256"],
                                "Project Workloads changed before final teardown")
                        code, _ = milo_json_response(args.direct_milo_port, milo_ca_file, token,
                                                     "GET", collection + "/" + evidence
                                                     ["native_admission"]["probe_workload_name"])
                        require(code == 404 and kjson(args.milo_kubeconfig, "milo-root",
                                                      "get", "project", project)["metadata"]["uid"] ==
                                snapshot["projects"][project],
                                "probe Workload persisted or Project UID changed before teardown")
                        evidence["native_admission"]["final_project_state_unchanged"] = True
                    except Exception as exc:
                        errors.append(f"native Project final-state verification: {exc}")
            # Stop the Flux-owned manager first. Even if its prune stalls,
            # revoke both runtime identities before the slower namespace and
            # source cleanup. An incomplete deletion is reported, not adopted.
            runtime_ref = next((ref for surface, ref in refs
                                if surface == "host" and ref["kind"] == "kustomization" and
                                ref["name"] == "compute-a1t-management"), None)
            manager_removed = runtime_ref is None
            if runtime_ref is not None:
                try:
                    delete_owned(host_proxy, args.host_kubeconfig, args.host_context,
                                 runtime_ref, run_id)
                    manager_removed = True
                except Exception as exc:
                    errors.append(f"host kustomization/{runtime_ref['name']}: {exc}")
            # A partial first reconcile may leave no Flux inventory, so deleting
            # the Kustomization is not proof that its Deployment was pruned.
            try:
                live_manager = optional(args.host_kubeconfig, args.host_context,
                                        "compute-system", "deployment", "compute-manager")
                if live_manager is not None:
                    manager_removed = False
                    errors.append("management Deployment remains; refusing cluster-scoped "
                                  "fallback and hub CRD prune")
            except Exception as exc:
                manager_removed = False
                errors.append(f"management Deployment absence verification: {exc}")
            hub_binding_ref = next((ref for surface, ref in refs
                                    if surface == "hub" and ref["kind"] == "clusterrolebinding" and
                                    ref["name"] == "compute-manager"), None)
            if hub_binding_ref is not None:
                try:
                    delete_owned(hub_proxy, args.karmada_admin_kubeconfig,
                                 args.karmada_admin_context, hub_binding_ref, run_id)
                except Exception as exc:
                    errors.append(f"hub clusterrolebinding/compute-manager: {exc}")
            if auth_intent:
                try:
                    current = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                                    "milo-apiserver-auth-tokens", "-n", "milo-system")
                    if current["data"]["tokens.csv"] == new_csv:
                        patch_auth(args, snapshot, new_csv, original_csv, private)
                        restart_milo(args)
                    elif current["data"]["tokens.csv"] != original_csv:
                        errors.append("Milo token CSV changed concurrently; refusing overwrite")
                except Exception as exc:
                    errors.append(f"Milo static auth restoration: {exc}")
            if manager_removed:
                try:
                    cleanup_partial_management(args, host_proxy, contract, run_id, evidence)
                except Exception as exc:
                    manager_removed = False
                    errors.append(f"management cluster-scoped partial-apply cleanup: {exc}")
            try:
                if optional(args.host_kubeconfig, args.host_context, "compute-system",
                            "deployment", "compute-manager") is not None:
                    manager_removed = False
                    errors.append("management Deployment appeared before hub CRD prune")
            except Exception as exc:
                manager_removed = False
                errors.append(f"management Deployment pre-CRD verification: {exc}")
            hub_crd_refs = [ref for surface, ref in refs if surface == "host" and
                            ref["kind"] == "kustomization" and ref["name"] in
                            ("compute-a1t-hub-compute-crds", "compute-a1t-hub-nso-crds")]
            hub_crd_prune_safe = True
            observed_uids = evidence.get("observed_hub_crds", {}).get("crd_uids", {})
            for expected in contract["hub_crds"]:
                name = expected["metadata"]["name"]
                try:
                    live = optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                                    None, "crd", name)
                    if live and (live["metadata"].get("labels", {}).get(RUN_LABEL) != run_id or
                                 (name in observed_uids and
                                  live["metadata"]["uid"] != observed_uids[name])):
                        hub_crd_prune_safe = False
                        errors.append(f"hub CRD {name} ownership/UID changed; refusing Flux prune")
                except Exception as exc:
                    hub_crd_prune_safe = False
                    errors.append(f"hub CRD {name} pre-prune verification: {exc}")
            if manager_removed and hub_crd_prune_safe:
                for ref in reversed(hub_crd_refs):
                    try:
                        delete_owned(host_proxy, args.host_kubeconfig, args.host_context,
                                     ref, run_id)
                    except Exception as exc:
                        errors.append(f"host kustomization/{ref['name']} prune: {exc}")
            elif hub_crd_refs:
                errors.append("hub CRD prune skipped because manager remains or CRD ownership changed")
            remaining_hub_crds = []
            for expected in contract["hub_crds"]:
                name = expected["metadata"]["name"]
                try:
                    if optional(args.karmada_admin_kubeconfig, args.karmada_admin_context,
                                None, "crd", name):
                        remaining_hub_crds.append(name)
                except Exception as exc:
                    errors.append(f"hub CRD {name} cleanup verification: {exc}")
            if remaining_hub_crds:
                errors.append("remote Flux hub CRDs remain: " + ", ".join(remaining_hub_crds))
            else:
                evidence["hub_crds_absent_after_cleanup"] = True
            installer_binding_ref = next((ref for surface, ref in refs
                                          if surface == "hub" and
                                          ref["kind"] == "clusterrolebinding" and
                                          ref["name"] == INSTALLER_SA), None)
            installer_secret_ref = next((ref for surface, ref in refs
                                         if surface == "host" and ref["kind"] == "secret" and
                                         ref["name"] == INSTALLER_KUBECONFIG_SECRET), None)
            for ref, config, context, proxy in (
                    (installer_binding_ref, args.karmada_admin_kubeconfig,
                     args.karmada_admin_context, hub_proxy),
                    (installer_secret_ref, args.host_kubeconfig,
                     args.host_context, host_proxy)):
                if ref is not None:
                    try:
                        delete_owned(proxy, config, context, ref, run_id)
                    except Exception as exc:
                        errors.append(f"early installer {ref['kind']}/{ref['name']} revocation: {exc}")
            for surface, ref in reversed(refs):
                if (ref is runtime_ref or ref is hub_binding_ref or
                        ref is installer_binding_ref or ref is installer_secret_ref or
                        ref in hub_crd_refs or ref in admission_refs):
                    continue
                config, context, proxy = {
                    "host": (args.host_kubeconfig, args.host_context, host_proxy),
                    "milo": (args.milo_kubeconfig, "milo-root", milo_proxy),
                    "hub": (args.karmada_admin_kubeconfig, args.karmada_admin_context, hub_proxy),
                }[surface]
                try:
                    delete_owned(proxy, config, context, ref, run_id)
                except Exception as exc:
                    errors.append(f"{surface} {ref['kind']}/{ref['name']}: {exc}")
            try:
                compute_namespace = optional(args.host_kubeconfig, args.host_context,
                                             None, "namespace", "compute-system")
            except Exception as exc:
                compute_namespace = None
                errors.append(f"compute-system namespace verification: {exc}")
            for item in contract["rendered"]:
                namespace = item["metadata"].get("namespace")
                kind, name = item["kind"].lower(), item["metadata"]["name"]
                try:
                    if namespace == "compute-system" and compute_namespace is None:
                        continue
                    if optional(args.host_kubeconfig, args.host_context, namespace, kind, name):
                        errors.append(f"Flux-owned {kind}/{name} remains after teardown")
                except Exception as exc:
                    errors.append(f"Flux-owned {kind}/{name} verification: {exc}")
            if suspend_intent:
                try:
                    current = kjson(args.host_kubeconfig, args.host_context, "get",
                                    "kustomization", "milo-core", "-n", "flux-system")
                    if current["metadata"]["uid"] != snapshot["core"]["metadata"]["uid"]:
                        errors.append("Milo core Flux UID changed; refusing adoption")
                    elif current["spec"].get("suspend", False) and current["metadata"].get(
                            "annotations", {}).get("apps-a1t.datum.net/suspend-run-id") == run_id:
                        set_core_suspend(args, snapshot, False, private, run_id)
                    elif current["spec"].get("suspend", False):
                        errors.append("Milo core Flux suspend ownership ambiguous")
                except Exception as exc:
                    errors.append(f"Milo core Flux resume: {exc}")
            if auth_intent or suspend_intent:
                try:
                    auth_now = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                                     "milo-apiserver-auth-tokens", "-n", "milo-system")
                    core_now = kjson(args.host_kubeconfig, args.host_context, "get",
                                     "kustomization", "milo-core", "-n", "flux-system")
                    require(auth_now["metadata"]["uid"] == snapshot["auth"]["metadata"]["uid"]
                            and auth_now["data"]["tokens.csv"] == original_csv,
                            "Milo token CSV not restored exactly")
                    require(core_now["metadata"]["uid"] == snapshot["core"]["metadata"]["uid"]
                            and core_now["spec"] == snapshot["core"]["spec"] and
                            core_now["metadata"].get("annotations", {}) ==
                            snapshot["core"]["metadata"].get("annotations", {}),
                            "Milo core Flux not restored exactly")
                except Exception as exc:
                    errors.append(f"final Milo auth/Flux restoration: {exc}")
        finally:
            # A slow remote prune must not leave the scoped installer grant
            # alive merely because the normal cleanup deadline expired.
            # This is a separate bounded emergency revocation attempt; if
            # remote CRDs remain, the evidence stays failed/incomplete.
            GLOBAL_DEADLINE = time.monotonic() + (300 if admission_retained else 60)
            if admission_retained and auth_intent:
                # Keep manager identities for the retained serving path, but
                # revoke the admission installer User and its binding.
                try:
                    current = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                                    "milo-apiserver-auth-tokens", "-n", "milo-system")
                    if current["data"]["tokens.csv"] == new_csv:
                        patch_auth(args, snapshot, new_csv, manager_csv, private)
                        restart_milo(args)
                    else:
                        require(current["data"]["tokens.csv"] == manager_csv,
                                "Milo token CSV changed; admission installer revocation refused")
                    evidence["native_admission"]["installer_token_revoked_after_retention"] = True
                except Exception as exc:
                    errors.append(f"retained-stage admission installer token revocation: {exc}")
                for kind in ("clusterrolebinding", "clusterrole"):
                    ref = next((item for surface, item in refs if surface == "milo" and
                                item["kind"] == kind and item["name"] == ADMISSION_USER), None)
                    if ref is not None:
                        try:
                            delete_owned(milo_proxy, args.milo_kubeconfig, "milo-root",
                                         ref, run_id)
                        except Exception as exc:
                            errors.append(f"retained-stage admission {kind} revocation: {exc}")
            for surface, kind, name, config, context, proxy in (
                    ("hub", "clusterrolebinding", INSTALLER_SA,
                     args.karmada_admin_kubeconfig, args.karmada_admin_context, hub_proxy),
                    ("host", "secret", INSTALLER_KUBECONFIG_SECRET,
                     args.host_kubeconfig, args.host_context, host_proxy)):
                ref = next((item for s, item in refs if s == surface and
                            item["kind"] == kind and item["name"] == name), None)
                if ref is not None:
                    try:
                        delete_owned(proxy, config, context, ref, run_id)
                    except Exception as exc:
                        errors.append(f"emergency installer {surface} {kind}/{name} revocation: {exc}")
            for name, process in (("host API", host_proxy), ("Milo API", milo_proxy),
                                  ("Karmada API", hub_proxy), ("direct Milo", direct)):
                if process is not None:
                    try:
                        process.close()
                    except Exception as exc:
                        errors.append(f"{name} private proxy close: {exc}")
            evidence["cleanup"] = "complete" if not errors else "incomplete: " + "; ".join(errors)
            evidence["result"] = "pass" if succeeded and not errors else "fail"
            evidence["cleanup_seconds"] = round(time.monotonic() - cleanup_started, 3)
            evidence["total_seconds"] = round(time.monotonic() - started + preflight_seconds, 3)
            private_write(evidence_path, json.dumps(evidence, indent=2) + "\n")
            print(f"Non-secret Compute management evidence: {evidence_path}", flush=True)
        require(not errors, "Compute management cleanup incomplete: " + "; ".join(errors))
    require(succeeded, "Compute management startup did not complete")
    if args.native_admission:
        print("Compute management plus native Milo admission dry-run PASS; scoped identities and runtime removed", flush=True)
    else:
        print("Compute management startup PASS; scoped identities and native runtime removed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--nso-source", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="verify exact source/render only")
    parser.add_argument("--execute", action="store_true", help="run reviewed isolated fixture")
    parser.add_argument("--native-admission", action="store_true",
                        help="opt in to real Milo native webhook registration and Project dry-run comparison")
    parser.add_argument("--fault-invalid-runtime", action="store_true",
                        help="inject a rejected Deployment to prove partial-apply cleanup; exits nonzero")
    parser.add_argument("--host-kubeconfig", type=Path)
    parser.add_argument("--host-context")
    parser.add_argument("--milo-kubeconfig", type=Path)
    parser.add_argument("--hub-host-kubeconfig", type=Path)
    parser.add_argument("--hub-host-context")
    parser.add_argument("--karmada-admin-kubeconfig", type=Path)
    parser.add_argument("--karmada-admin-context", default="karmada-apiserver")
    parser.add_argument("--karmada-internal-server")
    parser.add_argument("--docker-host")
    parser.add_argument("--direct-milo-port", type=int, default=41445)
    args = parser.parse_args()
    require(not args.fault_invalid_runtime or args.execute, "fault injection requires --execute")
    require(not (args.fault_invalid_runtime and args.native_admission),
            "fault injection and native admission are mutually exclusive")
    require(not (args.offline and args.execute), "--offline and --execute are mutually exclusive")
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="apps-a1t-compute-management-") as directory:
            private = Path(directory)
            contract = source_contract(args)
            if args.offline:
                mode = "Compute management plus native admission" if args.native_admission else "Compute management"
                print(f"{mode} offline source/render/RBAC contract PASS", flush=True)
                return
            for name in ("host_kubeconfig", "host_context", "milo_kubeconfig",
                         "hub_host_kubeconfig", "hub_host_context",
                         "karmada_admin_kubeconfig", "karmada_internal_server", "docker_host"):
                require(getattr(args, name), f"explicit --{name.replace('_', '-')} required")
            preflight_started = time.monotonic()
            snapshot = preflight(args, private, contract)
            preflight_seconds = time.monotonic() - preflight_started
            print(f"Read-only Compute preflight duration: {preflight_seconds:.3f}s", flush=True)
            if args.execute:
                execute(args, private, contract, snapshot, preflight_seconds)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Compute management prerequisite FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
