#!/usr/bin/env python3
"""Run the pinned Apps authoring API through a private Milo test cluster.

The default mode is a read-only preflight. --execute creates short-lived,
scoped authority and author identities, installs the Apps API through Flux,
tests real Milo project traffic and durable history, then removes only its own
resources using UID/resourceVersion preconditions. No App is installed.
"""

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import yaml


ROOT = Path(__file__).resolve().parents[1]
APPS_SHA = "e0833804a92384502b9124e80002621cb6bc7549"
APPS_IMAGE = "ghcr.io/datum-labs/apps@sha256:6e6fa82647017fd80a3d15d62783b830a6664cbd990900b6f2afad191f110e28"
APPS_ARM_MANIFEST = "sha256:bbe3a144888b0e5231f586bd94fcf76cad10f3129ab5685abbf70d7da78bacf2"
APPS_ARM_CONFIG = "sha256:be402edc785398f23497a8daab9e373fb79be4716c8c37260f37b2fabf1033f7"
ETCD_IMAGE = "registry.k8s.io/etcd:3.5.21-0@sha256:d58c035df557080a27387d687092e3fc2b64c6d0e3162dc51453a115f847d121"
REGISTRY_IMAGE = "docker.io/library/registry@sha256:fa647fc1e5d5df7d8d923fb6332aab8e78783f8fca1a1394efb4011f68f5a793"
REVISION = f"sha1:{APPS_SHA}"
REGISTRY_REPO = "oci://apps-a1t-registry.flux-system.svc.cluster.local:5000/apps-authoring"
AUTHORITY = "apps-a1t-authoring-authority"
AUTHOR = "apps-a1t-template-author"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")
PROJECT_API = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
APPS_API = "/apis/apps.miloapis.com/v1alpha1"
LABEL = "apps-a1t.datum.net/run-id"
EXPECTED_RENDER = {
    ("Namespace", "", "apps-system"),
    ("ServiceAccount", "apps-system", "apps-authoring"),
    ("ServiceAccount", "apps-system", "apps-etcd"),
    ("Service", "apps-system", "apps-authoring"),
    ("Service", "apps-system", "apps-etcd"),
    ("Deployment", "apps-system", "apps-authoring"),
    ("StatefulSet", "apps-system", "apps-etcd"),
    ("PodDisruptionBudget", "apps-system", "apps-authoring"),
    *(("Certificate", "apps-system", name) for name in (
        "apps-etcd-ca", "apps-etcd-client-tls", "apps-etcd-server-tls",
        "apps-serving-ca", "apps-serving-tls")),
    *(("Issuer", "apps-system", name) for name in (
        "apps-etcd-issuer", "apps-serving-issuer", "apps-test-selfsigned")),
    *(("NetworkPolicy", "apps-system", name) for name in (
        "apps-authoring", "apps-default-deny", "apps-etcd")),
}
GLOBAL_DEADLINE = None


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*argv, input_text=None, sensitive=False, timeout=30, env=None):
    if GLOBAL_DEADLINE is not None:
        remaining = GLOBAL_DEADLINE - time.monotonic()
        require(remaining > 0, "Apps trial exceeded its 30-minute overall deadline")
        timeout = min(timeout, remaining)
    result = subprocess.run(argv, input=input_text, text=True, capture_output=True,
                            timeout=timeout, env=env, check=False)
    if result.returncode:
        detail = "details withheld" if sensitive else result.stderr.strip()
        if argv[0] == "gh" and not sensitive:
            detail += " " + result.stdout.strip()[:600]
        raise RuntimeError(f"{argv[0]} failed: {detail}")
    return result.stdout.strip()


def k(config, context, *args, **kwargs):
    return run("kubectl", "--kubeconfig", str(config), "--context", context,
               "--request-timeout=10s", *args, **kwargs)


def kjson(config, context, *args):
    return json.loads(k(config, context, *args, "-o", "json"))


def optional(config, context, namespace, kind, name):
    args = ("-n", namespace) if namespace else ()
    output = k(config, context, *args, "get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(output) if output else None


def ready(obj, condition="Ready"):
    generation = obj["metadata"]["generation"]
    status = obj.get("status", {})
    return status.get("observedGeneration", generation) >= generation and any(
        item.get("type") == condition and item.get("status") == "True"
        and item.get("observedGeneration", generation) >= generation
        for item in status.get("conditions", []))


def apiservice_available(obj):
    # This Milo APIService publishes no metadata.generation. Availability alone
    # is not a traffic proof; the caller also requires real authenticated
    # project-scoped discovery and authoring requests through Milo.
    return any(item.get("type") == "Available" and item.get("status") == "True"
               for item in obj.get("status", {}).get("conditions", []))


def private_file(path):
    require(path.is_absolute() and path.is_file(), f"absolute kubeconfig file required: {path}")
    require(path.stat().st_mode & 0o077 == 0, f"kubeconfig must be private: {path}")


def private_write(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def secret_field(args, namespace, name, key):
    secret = kjson(args.host_kubeconfig, args.host_context, "get", "secret", name,
                   "-n", namespace)
    value = secret.get("data", {}).get(key)
    require(value, f"missing {name} public certificate key {key}")
    return base64.b64decode(value)


def verify_certificate(private, ca_bytes, cert_bytes, hostname=None, ip=None, common_name=None):
    ca = private / f"verify-ca-{hashlib.sha256(ca_bytes).hexdigest()}.pem"
    cert = private / f"verify-cert-{hashlib.sha256(cert_bytes).hexdigest()}.pem"
    private_write(ca, ca_bytes.decode())
    private_write(cert, cert_bytes.decode())
    command = ["openssl", "verify", "-CAfile", str(ca)]
    if hostname:
        command += ["-verify_hostname", hostname]
    if ip:
        command += ["-verify_ip", ip]
    command.append(str(cert))
    require(run(*command).endswith(": OK"), "public certificate chain verification failed")
    if common_name:
        subject = run("openssl", "x509", "-in", str(cert), "-noout", "-subject",
                      "-nameopt", "RFC2253")
        require(subject == f"subject=CN={common_name}", "unexpected proxy certificate CN")


def render_source(args):
    require(args.source.is_absolute() and args.source.is_dir(), "absolute Apps PR14 checkout required")
    require(run("git", "-C", str(args.source), "rev-parse", "HEAD") == APPS_SHA,
            "Apps source HEAD differs from pinned PR14 commit")
    require(not run("git", "-C", str(args.source), "status", "--porcelain",
                    "--untracked-files=all"), "Apps source checkout has local changes")
    rendered = list(yaml.safe_load_all(run("kustomize", "build",
                                        str(args.source / "config/overlays/test-infra"), timeout=60)))
    actual = {(obj["kind"], obj["metadata"].get("namespace", ""), obj["metadata"]["name"])
              for obj in rendered}
    require(actual == EXPECTED_RENDER and len(rendered) == len(EXPECTED_RENDER),
            "Apps PR14 overlay differs from the expected 19-resource contract")
    by_key = {(obj["kind"], obj["metadata"]["name"]): obj for obj in rendered}
    api = by_key[("Deployment", "apps-authoring")]["spec"]["template"]["spec"]["containers"][0]
    etcd = by_key[("StatefulSet", "apps-etcd")]["spec"]["template"]["spec"]["containers"][0]
    require(api["image"] == APPS_IMAGE and etcd["image"] == ETCD_IMAGE,
            "Apps or test-etcd runtime image differs from pinned digest")
    required_flags = {
        "--health-listen=0.0.0.0:8081", "--requestheader-allowed-names=milo-front-proxy-client",
        "--milo-kubeconfig=/var/run/apps/milo/kubeconfig",
        "--authoring-max-project-templates=100", "--authoring-max-project-revisions=1000",
        "--authoring-max-project-bytes=4194304",
    }
    require(required_flags <= set(api["args"]), "Apps proxy, authority or quota flags changed")
    require(by_key[("Deployment", "apps-authoring")]["spec"]["replicas"] == 2,
            "Apps authoring must have two replicas for the restart trial")


def fixture_contract():
    source = yaml.safe_load((ROOT / "components/apps-authoring/source.yaml").read_text())
    runtime = yaml.safe_load((ROOT / "components/apps-authoring/runtime.yaml").read_text())
    registry = load_manifests(ROOT / "components/apps-authoring/registry.yaml")
    require(source["kind"] == "OCIRepository" and source["metadata"] ==
            {"name": "apps-authoring-a1t", "namespace": "flux-system"} and
            source["spec"]["ref"] == {"tag": APPS_SHA} and
            source["spec"]["url"] == REGISTRY_REPO and
            source["spec"]["insecure"] is True and
            "secretRef" not in source["spec"],
            "Apps OCI source bootstrap fixture differs from reviewed local registry contract")
    require(runtime["kind"] == "Kustomization" and runtime["metadata"] ==
            {"name": "apps-authoring-a1t-runtime", "namespace": "flux-system"} and
            runtime["spec"]["sourceRef"] == {"kind": "OCIRepository",
                                             "name": "apps-authoring-a1t",
                                             "namespace": "flux-system"} and
            runtime["spec"]["path"] == "./config/overlays/test-infra" and
            runtime["spec"]["prune"] is False and
            "postBuild" not in runtime["spec"] and "patches" not in runtime["spec"] and
            "dependsOn" not in runtime["spec"],
            "Apps Flux runtime fixture differs from reviewed overlay contract")
    require(len(registry) == 2 and [obj["kind"] for obj in registry] ==
            ["Deployment", "Service"] and
            all(obj["metadata"] == {"name": "apps-a1t-registry", "namespace": "flux-system"}
                for obj in registry) and
            registry[0]["spec"]["template"]["spec"]["containers"][0]["image"] == REGISTRY_IMAGE and
            registry[1]["spec"]["type"] == "ClusterIP",
            "local OCI registry fixture differs from pinned private-cluster contract")
    expected = {
        AUTHORITY: {("resourcemanager.miloapis.com", ("projects",), ("get",)),
                    ("authorization.k8s.io", ("subjectaccessreviews",), ("create",))},
        AUTHOR: {("apps.miloapis.com", ("apptemplates",),
                  ("get", "list", "create", "update")),
                 ("apps.miloapis.com", ("apptemplaterevisions",), ("get", "list"))},
    }
    for name, filename in ((AUTHORITY, "authority-rbac.yaml"),
                           (AUTHOR, "actor-rbac.yaml")):
        docs = load_manifests(ROOT / "components/apps-authoring" / filename)
        require(len(docs) == 2 and [doc["kind"] for doc in docs] ==
                ["ClusterRole", "ClusterRoleBinding"], f"unexpected {name} RBAC fixture")
        role, binding = docs
        rules = {(rule["apiGroups"][0], tuple(rule["resources"]), tuple(rule["verbs"]))
                 for rule in role["rules"]}
        require(role["metadata"]["name"] == name and rules == expected[name] and
                all(len(rule["apiGroups"]) == 1 and len(rule["resources"]) == 1
                    for rule in role["rules"]), f"{name} role exceeds exact reviewed verbs")
        require(binding["metadata"]["name"] == name and
                binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                       "kind": "ClusterRole", "name": name} and
                binding["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io",
                                         "kind": "User", "name": name}],
                f"{name} binding differs from dedicated User identity")


def preflight(args, private):
    render_source(args)
    fixture_contract()
    config_hash, config_count = tree_fingerprint(args.source / "config")
    private_file(args.host_kubeconfig)
    private_file(args.milo_kubeconfig)
    require(args.host_kubeconfig != args.milo_kubeconfig, "host and Milo kubeconfigs must differ")
    require(args.host_context.startswith("kind-apps-a1t-"), "expected task-owned Kind host context")
    require(1 <= args.milo_port <= 65535, "invalid private Milo port")
    require(1 <= args.direct_milo_port <= 65535 and args.direct_milo_port != args.milo_port,
            "invalid or colliding direct Milo loopback port")
    require(1 <= args.registry_port <= 65535 and
            args.registry_port not in (args.milo_port, args.direct_milo_port),
            "invalid or colliding local registry loopback port")
    for port in (args.direct_milo_port, args.registry_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    nodes = kjson(args.host_kubeconfig, args.host_context, "get", "nodes")["items"]
    require(len(nodes) == 1 and nodes[0]["status"]["nodeInfo"]["architecture"] == "arm64" and
            nodes[0]["status"]["nodeInfo"]["operatingSystem"] == "linux",
            "pinned registry manifest requires the one-node Linux/arm64 test cluster")
    available_images = {name for image in nodes[0]["status"].get("images", [])
                        for name in image.get("names", [])}
    require(APPS_IMAGE in available_images,
            "exact Apps image digest is not preloaded on the private Kind node")
    require(k(args.host_kubeconfig, args.host_context, "config", "current-context") == args.host_context,
            "host kubeconfig current context differs from explicit context")
    require(k(args.milo_kubeconfig, "milo-root", "config", "current-context") == "milo-root",
            "Milo kubeconfig current context differs from milo-root")
    for config, context in ((args.host_kubeconfig, args.host_context),
                            (args.milo_kubeconfig, "milo-root")):
        server = k(config, context, "config", "view", "--minify", "-o",
                   "jsonpath={.clusters[0].cluster.server}")
        expected = f"https://127.0.0.1:{args.milo_port}" if context == "milo-root" else None
        require(server == expected if expected else server.startswith("https://127.0.0.1:"),
                f"unexpected non-private {context} API endpoint")
        wait_for(lambda: k(config, context, "get", "--raw", "/readyz") == "ok",
                 f"{context} API readyz", 90)

    for namespace, kind, name in ((None, "namespace", "apps-system"),
                                  ("flux-system", "deployment", "apps-a1t-registry"),
                                  ("flux-system", "service", "apps-a1t-registry"),
                                  ("flux-system", "ocirepository", "apps-authoring-a1t"),
                                  ("flux-system", "kustomization", "apps-authoring-a1t-runtime")):
        require(optional(args.host_kubeconfig, args.host_context, namespace, kind, name) is None,
                f"refusing existing host {kind}/{name}")
    registry_pods = kjson(args.host_kubeconfig, args.host_context, "get", "pods",
                          "-n", "flux-system", "-l", "apps-a1t.datum.net/component=registry")
    require(not registry_pods["items"], "refusing existing registry-labeled Pods")
    for kind, name in (("apiservice", "v1alpha1.apps.miloapis.com"),
                       ("clusterrole", AUTHORITY), ("clusterrolebinding", AUTHORITY),
                       ("clusterrole", AUTHOR), ("clusterrolebinding", AUTHOR)):
        require(optional(args.milo_kubeconfig, "milo-root", None, kind, name) is None,
                f"refusing existing Milo {kind}/{name}")

    core = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization", "milo-core",
                 "-n", "flux-system")
    require(ready(core) and not core["spec"].get("suspend", False),
            "pinned Milo core is not Ready or is already suspended")
    manager = kjson(args.host_kubeconfig, args.host_context, "get", "deployment",
                    "milo-controller-manager", "-n", "milo-system")
    require(manager["status"].get("availableReplicas", 0) >= 1,
            "Milo availability controller-manager is not Available")
    cert_manager = kjson(args.host_kubeconfig, args.host_context, "get", "deployment",
                         "cert-manager", "-n", "cert-manager")
    require(cert_manager["status"].get("availableReplicas", 0) >= 1 and
            cert_manager["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":v1.16.2"),
            "cert-manager 1.16.2 prerequisite is not Available")
    classes = kjson(args.host_kubeconfig, args.host_context, "get", "storageclass")["items"]
    require(any(item["metadata"].get("annotations", {}).get(
        "storageclass.kubernetes.io/is-default-class") == "true" for item in classes),
        "default dynamic StorageClass is unavailable")

    auth = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                 "milo-apiserver-auth-tokens", "-n", "milo-system")
    old_b64 = auth["data"]["tokens.csv"]
    rows = list(csv.reader(base64.b64decode(old_b64).decode().splitlines()))
    identities = {(row[1], row[2], row[3]) for row in rows if len(row) == 4}
    require(len(rows) == 2 and identities == {
        ("admin", "admin", "system:masters"),
        ("test-user", "test-user", "system:authenticated"),
    } and all(len(row) == 4 and row[0] for row in rows),
            "Milo static auth Secret differs from the two-user isolated baseline")

    ca = secret_field(args, "milo-system", "milo-ca-tls", "ca.crt")
    front = secret_field(args, "milo-system", "milo-front-proxy-client", "tls.crt")
    server = secret_field(args, "milo-system", "milo-apiserver-tls", "tls.crt")
    verify_certificate(private, ca, front, common_name="milo-front-proxy-client")
    verify_certificate(private, ca, server, hostname="milo-apiserver.milo-system.svc")
    verify_certificate(private, ca, server, ip="127.0.0.1")
    projects = {name: kjson(args.milo_kubeconfig, "milo-root", "get", "project", name)
                for name in PROJECTS}
    require(all(obj["metadata"].get("uid") and not obj["metadata"].get("deletionTimestamp")
                for obj in projects.values()), "test Projects are missing or deleting")
    print("Apps preflight passed: exact PR14 overlay/images/quotas, private Milo and host APIs, "
          "cert-manager/storage, public proxy and serving chains, three live Projects, no collisions",
          flush=True)
    return {"core": core, "auth": auth, "auth_b64": old_b64, "ca": ca,
            "config_hash": config_hash, "config_file_count": config_count,
            "project_uids": {name: obj["metadata"]["uid"] for name, obj in projects.items()}}


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
            require(self.process.poll() is None, "private direct Milo port-forward exited")
            try:
                with socket.create_connection(("127.0.0.1", self.args.direct_milo_port),
                                              timeout=1) as raw:
                    with ssl.create_default_context(cafile=str(self.ca_file)).wrap_socket(
                            raw, server_hostname="127.0.0.1") as peer:
                        require(peer.getpeercert(), "direct Milo peer certificate missing")
                        return
            except (OSError, ssl.SSLError):
                time.sleep(0.2)
        raise RuntimeError("private direct Milo port-forward did not present verified serving TLS")

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


class LocalRegistryForward:
    def __init__(self, args):
        self.args = args
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", str(self.args.host_kubeconfig), "--context",
             self.args.host_context, "--request-timeout=10s", "-n", "flux-system",
             "port-forward", "service/apps-a1t-registry",
             f"{self.args.registry_port}:5000", "--address=127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "task-owned OCI registry port-forward exited")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.args.registry_port, timeout=2)
                conn.request("GET", "/v2/")
                response = conn.getresponse()
                response.read()
                conn.close()
                if response.status == 200:
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("task-owned OCI registry did not answer bounded /v2/ probe")

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


def create_owned(args, config, context, manifest, ref, run_id, sensitive=False):
    manifest.setdefault("metadata", {}).setdefault("labels", {})[LABEL] = run_id
    ref["intent"] = True
    argv = ("-n", ref["namespace"]) if ref["namespace"] else ()
    obj = json.loads(k(config, context, *argv, "create", "-f", "-", "-o", "json",
                       input_text=json.dumps(manifest), sensitive=sensitive))
    ref["uid"] = obj["metadata"]["uid"]
    return obj


def delete_owned(args, proxy, config, context, ref, run_id):
    if not ref["intent"]:
        return
    obj = optional(config, context, ref["namespace"], ref["kind"], ref["name"])
    if obj is None:
        return
    meta = obj["metadata"]
    require(meta["uid"] == ref["uid"] if ref["uid"] else
            meta.get("labels", {}).get(LABEL) == run_id,
            f"refusing to delete ownership-ambiguous {ref['kind']}/{ref['name']}")
    proxy.delete(ref["path"], meta["uid"], meta["resourceVersion"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        now = optional(config, context, ref["namespace"], ref["kind"], ref["name"])
        if now is None or now["metadata"]["uid"] != meta["uid"]:
            return
        time.sleep(1)
    raise RuntimeError(f"timed out deleting owned {ref['kind']}/{ref['name']}")


def patch_json(args, config, context, namespace, kind, name, ops, private, sensitive=False):
    patch_file = private / "cas-patch.json"
    private_write(patch_file, json.dumps(ops))
    argv = ("-n", namespace) if namespace else ()
    return json.loads(k(config, context, *argv, "patch", kind, name, "--type=json",
                        "--patch-file", str(patch_file), "-o", "json", sensitive=sensitive))


def set_core_suspend(args, snapshot, value, private, run_id):
    current = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                    "milo-core", "-n", "flux-system")
    old = snapshot["core"]
    meta = current["metadata"]
    require(meta["uid"] == old["metadata"]["uid"], "Milo core Flux ownership changed")
    require(current["spec"].get("suspend", False) == (not value),
            "Milo core Flux suspend state changed concurrently")
    marker = "apps-a1t.datum.net/suspend-run-id"
    marker_path = "/metadata/annotations/apps-a1t.datum.net~1suspend-run-id"
    original_annotations = old["metadata"].get("annotations", {})
    expected_annotations = dict(original_annotations)
    expected_annotations[marker] = run_id
    ops = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
           {"op": "test", "path": "/metadata/resourceVersion",
            "value": meta["resourceVersion"]},
           {"op": "test", "path": "/metadata/generation", "value": meta["generation"]}]
    if value:
        require(meta["generation"] == old["metadata"]["generation"],
                "Milo core Flux generation changed before suspension")
        require(meta.get("annotations", {}) == original_annotations and
                marker not in original_annotations,
                "Milo core Flux annotations changed before suspension")
        if "annotations" not in meta:
            ops.append({"op": "add", "path": "/metadata/annotations", "value": {}})
        ops.append({"op": "add", "path": marker_path, "value": run_id})
        ops.append({"op": "add", "path": "/spec/suspend", "value": True})
    else:
        expected_spec = dict(old["spec"])
        expected_spec["suspend"] = True
        require(meta["generation"] == old["metadata"]["generation"] + 1 and
                current["spec"] == expected_spec,
                "Milo core Flux spec changed while suspended; refusing restore")
        require(meta.get("annotations", {}) == expected_annotations,
                "Milo core Flux annotations changed while suspended; refusing restore")
        ops.append({"op": "test", "path": marker_path, "value": run_id})
        if "annotations" in old["metadata"]:
            ops.append({"op": "remove", "path": marker_path})
        else:
            ops.append({"op": "remove", "path": "/metadata/annotations"})
        ops.append({"op": "test", "path": "/spec/suspend", "value": True})
        ops.append({"op": "remove" if "suspend" not in old["spec"] else "replace",
                    "path": "/spec/suspend", **({} if "suspend" not in old["spec"]
                    else {"value": False})})
    return patch_json(args, args.host_kubeconfig, args.host_context, "flux-system",
                      "kustomization", "milo-core", ops, private)


def auth_csv(old_b64, tokens):
    original = base64.b64decode(old_b64).decode()
    suffix = "" if original.endswith("\n") else "\n"
    addition = "".join(f"{tokens[name]},{name},{name},system:authenticated\n"
                       for name in (AUTHORITY, AUTHOR))
    return base64.b64encode((original + suffix + addition).encode()).decode()


def patch_auth(args, snapshot, expected, replacement, private):
    current = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                    "milo-apiserver-auth-tokens", "-n", "milo-system")
    require(current["metadata"]["uid"] == snapshot["auth"]["metadata"]["uid"],
            "Milo static-token Secret UID changed")
    ops = [{"op": "test", "path": "/metadata/uid",
            "value": snapshot["auth"]["metadata"]["uid"]},
           {"op": "test", "path": "/data/tokens.csv", "value": expected},
           {"op": "replace", "path": "/data/tokens.csv", "value": replacement}]
    return patch_json(args, args.host_kubeconfig, args.host_context, "milo-system",
                      "secret", "milo-apiserver-auth-tokens", ops, private, sensitive=True)


def kubeconfig_text(server, ca, token):
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "current-context": "scoped",
                           "clusters": [{"name": "milo", "cluster": {"server": server,
                            "certificate-authority-data": base64.b64encode(ca).decode()}}],
                           "users": [{"name": "scoped", "user": {"token": token}}],
                           "contexts": [{"name": "scoped", "context": {"cluster": "milo",
                                          "user": "scoped"}}]})


def tree_fingerprint(root):
    require(root.is_dir(), f"missing source tree {root}")
    digest = hashlib.sha256()
    files = sorted(root.rglob("*"))
    require(files and all(not path.is_symlink() for path in files),
            "Apps config tree contains missing files or unsupported symlinks")
    count = 0
    for path in files:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        count += 1
    require(count > 0, "Apps config artifact is empty")
    return digest.hexdigest(), count


def publish_local_artifact(args, private, expected_hash):
    staged = private / "artifact"
    shutil.copytree(args.source / "config", staged / "config", symlinks=False)
    copied_hash, count = tree_fingerprint(staged / "config")
    require(copied_hash == expected_hash,
            "Apps config changed while copying exact PR14 source for OCI artifact")
    local_url = f"oci://127.0.0.1:{args.registry_port}/apps-authoring:{APPS_SHA}"
    raw = run("flux", "push", "artifact", local_url, "--path", str(staged),
              "--source", "https://github.com/datum-labs/apps.git",
              "--revision", f"pr14@{REVISION}", "--insecure-registry",
              "--ignore-paths=.git/",
              "--output", "json", timeout=120)
    published = json.loads(raw)
    digest = published.get("digest", "")
    require(digest.startswith("sha256:") and len(digest) == 71 and
            all(char in "0123456789abcdef" for char in digest[7:]),
            "local OCI publisher did not return an exact sha256 digest")
    pulled = private / "pulled"
    pulled.mkdir(mode=0o700)
    run("flux", "pull", "artifact",
        f"oci://127.0.0.1:{args.registry_port}/apps-authoring@{digest}",
        "--output", str(pulled), "--insecure-registry", timeout=120)
    require(sorted(path.name for path in pulled.iterdir()) == ["config"],
            "digest-pulled OCI artifact contains unexpected top-level paths")
    pulled_hash, pulled_count = tree_fingerprint(pulled / "config")
    if pulled_hash != expected_hash or pulled_count != count:
        staged_files = {path.relative_to(staged / "config").as_posix():
                        hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (staged / "config").rglob("*") if path.is_file()}
        pulled_files = {path.relative_to(pulled / "config").as_posix():
                        hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (pulled / "config").rglob("*") if path.is_file()}
        differing = sorted(name for name in staged_files.keys() | pulled_files.keys()
                           if staged_files.get(name) != pulled_files.get(name))
        raise RuntimeError(f"digest-pulled OCI config tree differs at paths: {differing}")
    require(pulled_hash == expected_hash and pulled_count == count,
            "digest-pulled OCI config tree differs from exact source checkout")
    return digest, count


def can_i(config, verb, resource, expected):
    result = subprocess.run(["kubectl", "--kubeconfig", str(config), "--context", "scoped",
                             "--request-timeout=10s", "auth", "can-i", verb, resource],
                            text=True, capture_output=True, timeout=20, check=False)
    actual = result.stdout.strip()
    detail = result.stderr.strip().splitlines()[-1:] if actual not in ("yes", "no") else []
    require(actual in ("yes", "no") and (actual == "yes") == expected,
            f"scoped Milo RBAC {verb} {resource}: expected {expected}, "
            f"received {actual!r}, exit={result.returncode}, diagnostic={detail}")
    print(f"Milo static RBAC {'allow' if expected else 'deny'}: {verb} {resource}", flush=True)


def assert_authority_scope(authority_config):
    for verb, resource in (("get", "projects.resourcemanager.miloapis.com"),
                           ("create", "subjectaccessreviews.authorization.k8s.io")):
        can_i(authority_config, verb, resource, True)
    for verb, resource in (("list", "projects.resourcemanager.miloapis.com"),
                           ("create", "projects.resourcemanager.miloapis.com"),
                           ("get", "secrets"), ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users"),
                           ("create", "apps.apps.miloapis.com")):
        can_i(authority_config, verb, resource, False)


def assert_author_scope(actor_config):
    for verb, resource in (("get", "apptemplates.apps.miloapis.com"),
                           ("list", "apptemplates.apps.miloapis.com"),
                           ("create", "apptemplates.apps.miloapis.com"),
                           ("update", "apptemplates.apps.miloapis.com"),
                           ("get", "apptemplaterevisions.apps.miloapis.com"),
                           ("list", "apptemplaterevisions.apps.miloapis.com")):
        can_i(actor_config, verb, resource, True)
    for verb, resource in (("delete", "apptemplates.apps.miloapis.com"),
                           ("create", "apptemplaterevisions.apps.miloapis.com"),
                           ("get", "projects.resourcemanager.miloapis.com"),
                           ("create", "subjectaccessreviews.authorization.k8s.io"),
                           ("get", "secrets"), ("create", "clusterroles.rbac.authorization.k8s.io"),
                           ("impersonate", "users"),
                           ("create", "apps.apps.miloapis.com")):
        can_i(actor_config, verb, resource, False)


def assert_http_denies(args, ca_file, tokens):
    denied = (
        (AUTHORITY, "GET", f"{PROJECT_API}", None),
        (AUTHORITY, "GET", "/api/v1/namespaces/milo-system/secrets/milo-apiserver-auth-tokens", None),
        (AUTHORITY, "POST", "/apis/rbac.authorization.k8s.io/v1/clusterroles", {}),
        (AUTHOR, "GET", f"{PROJECT_API}/{PROJECTS[0]}", None),
        (AUTHOR, "GET", "/api/v1/namespaces/milo-system/secrets/milo-apiserver-auth-tokens", None),
        (AUTHOR, "POST", "/apis/authorization.k8s.io/v1/subjectaccessreviews", {}),
    )
    for principal, method, path, body in denied:
        must_http(args.direct_milo_port, ca_file, tokens[principal], method, path, 403, body)
    print("Dedicated authority and author both denied unrelated real Milo API operations", flush=True)


def wait_for(predicate, description, seconds=120):
    deadline = time.monotonic() + seconds
    error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {description}: {error or 'condition false'}")


def restart_apiserver(args):
    k(args.host_kubeconfig, args.host_context, "rollout", "restart", "deployment/milo-apiserver",
      "-n", "milo-system")
    k(args.host_kubeconfig, args.host_context, "rollout", "status", "deployment/milo-apiserver",
      "-n", "milo-system", "--timeout=120s", timeout=130)
    # During a two-replica rollout the loopback service can briefly select a
    # terminating old pod even after Deployment rollout status is successful.
    wait_for(lambda: k(args.milo_kubeconfig, "milo-root", "get", "--raw", "/readyz") == "ok",
             "Milo API readyz after scoped token reload", 120)


def milo_request(port, ca_file, token, method, path, body=None):
    ctx = ssl.create_default_context(cafile=str(ca_file))
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(65536)
        require(len(raw) < 65536, "Milo API response exceeded diagnostic bound")
        return response.status, json.loads(raw) if raw and raw.startswith(b"{") else raw.decode()
    finally:
        conn.close()


def must_http(port, ca_file, token, method, path, status, body=None):
    actual, payload = milo_request(port, ca_file, token, method, path, body)
    require(actual == status, f"Milo {method} {path} returned HTTP {actual}, wanted {status}")
    print(f"Milo {method} {path} -> HTTP {actual}", flush=True)
    return payload


def load_manifests(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def flux(args, *argv, timeout=120):
    return run("flux", "--kubeconfig", str(args.host_kubeconfig), "--context",
               args.host_context, *argv, timeout=timeout)


def require_runtime(args, digest):
    source = kjson(args.host_kubeconfig, args.host_context, "get", "ocirepository",
                   "apps-authoring-a1t", "-n", "flux-system")
    runtime = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                    "apps-authoring-a1t-runtime", "-n", "flux-system")
    require(ready(source) and source["spec"].get("ref") == {"digest": digest} and
            source.get("status", {}).get("artifact", {}).get("revision") == digest,
            "Apps OCIRepository is not Ready at exact locally published OCI digest")
    require(ready(runtime) and runtime.get("status", {}).get("lastAppliedRevision") == digest,
            "Apps Flux runtime is not Ready at exact locally published OCI digest")
    deployment = kjson(args.host_kubeconfig, args.host_context, "get", "deployment",
                       "apps-authoring", "-n", "apps-system")
    stateful = kjson(args.host_kubeconfig, args.host_context, "get", "statefulset",
                     "apps-etcd", "-n", "apps-system")
    require(deployment["spec"]["replicas"] == 2 and deployment["status"].get("readyReplicas") == 2,
            "two Apps API replicas are not Ready")
    require(stateful["status"].get("readyReplicas") == 1, "persistent Apps etcd is not Ready")
    actual_image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    etcd_image = stateful["spec"]["template"]["spec"]["containers"][0]["image"]
    require(actual_image == APPS_IMAGE and etcd_image == ETCD_IMAGE,
            "deployed Apps or etcd image differs from immutable source pin")
    for name in ("apps-serving-tls", "apps-etcd-server-tls", "apps-etcd-client-tls"):
        require(ready(kjson(args.host_kubeconfig, args.host_context, "get", "certificate",
                            name, "-n", "apps-system")), f"Certificate/{name} not Ready")
    pvc = kjson(args.host_kubeconfig, args.host_context, "get", "pvc", "data-apps-etcd-0",
                "-n", "apps-system")
    require(pvc["status"]["phase"] == "Bound", "Apps etcd PVC is not Bound")
    print("Flux Apps source and 19-resource runtime Ready at exact PR14 SHA; two API pods, "
          "persistent etcd, Certificates and PVC Ready", flush=True)
    return {"source": source, "runtime": runtime, "deployment": deployment, "pvc": pvc}


def pod_state(args):
    pods = kjson(args.host_kubeconfig, args.host_context, "get", "pods", "-n", "apps-system",
                 "-l", "app.kubernetes.io/name=apps-authoring")["items"]
    active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    require(len(active) == 2, "expected exactly two active Apps API pods")
    observed = {}
    for pod in active:
        require(len(pod["spec"]["containers"]) == 1 and
                pod["spec"]["containers"][0]["image"] == APPS_IMAGE,
                "Apps API Pod does not declare the exact pinned image")
        statuses = pod.get("status", {}).get("containerStatuses", [])
        require(len(statuses) == 1 and statuses[0].get("ready") is True,
                "Apps API Pod container is not Ready")
        image_id = statuses[0].get("imageID", "")
        require(any(image_id.endswith(digest) for digest in
                    (APPS_IMAGE.split("@", 1)[1], APPS_ARM_MANIFEST, APPS_ARM_CONFIG)),
                "running Apps container imageID differs from verified index/arm64 manifest/config")
        observed[pod["metadata"]["uid"]] = image_id
    return observed


def rollout_snapshot(args):
    pods = kjson(args.host_kubeconfig, args.host_context, "get", "pods", "-n", "apps-system",
                 "-l", "app.kubernetes.io/name=apps-authoring")["items"]
    slices = kjson(args.host_kubeconfig, args.host_context, "get", "endpointslices",
                   "-n", "apps-system", "-l",
                   "kubernetes.io/service-name=apps-authoring")["items"]
    registration = kjson(args.milo_kubeconfig, "milo-root", "get", "apiservice",
                         "v1alpha1.apps.miloapis.com")
    return {"at_utc": datetime.now(timezone.utc).isoformat(),
            "pods": [{"uid": pod["metadata"]["uid"],
                      "created_at": pod["metadata"].get("creationTimestamp"),
                      "ready_transition": next((condition.get("lastTransitionTime")
                                                for condition in pod.get("status", {}).get("conditions", [])
                                                if condition.get("type") == "Ready"), None),
                      "running_since": next((status.get("state", {}).get("running", {}).get("startedAt")
                                             for status in pod.get("status", {}).get("containerStatuses", [])
                                             if status.get("name") == "apiserver"), None)}
                     for pod in pods],
            "endpoints": [{"uid": endpoint.get("targetRef", {}).get("uid"),
                           "ready": endpoint.get("conditions", {}).get("ready"),
                           "serving": endpoint.get("conditions", {}).get("serving"),
                           "terminating": endpoint.get("conditions", {}).get("terminating")}
                          for item in slices for endpoint in item.get("endpoints", [])],
            "apiservice_available": next((condition.get("status") for condition in
                                           registration.get("status", {}).get("conditions", [])
                                           if condition.get("type") == "Available"), None)}


def check_authoring(args, private, snapshot, tokens, evidence, record):
    ca_file = private / "public-ca.pem"
    private_write(ca_file, snapshot["ca"].decode())
    provider = PROJECTS[0]
    consumer = PROJECTS[1]
    root = f"{PROJECT_API}/{provider}/control-plane"
    other = f"{PROJECT_API}/{consumer}/control-plane"
    path = APPS_API + "/apptemplates"
    name = "apps-a1t-history-proof"
    body = {"apiVersion": "apps.miloapis.com/v1alpha1", "kind": "AppTemplate",
            "metadata": {"name": name},
            "spec": {"description": "initial", "inputSchema": {"type": "object",
                     "additionalProperties": False, "properties": {}},
                     "components": [], "outputs": []}}
    first = must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "POST", root + path, 201, body)
    require(first["metadata"].get("uid") and first["metadata"].get("resourceVersion") and
            first["metadata"].get("generation") == 1,
            "Milo authoring create lacked server UID, version or generation")
    must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "POST", root + path, 409, body)
    updated = json.loads(json.dumps(first))
    updated["spec"]["description"] = "updated"
    second = must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "PUT",
                       root + path + "/" + name, 200, updated)
    require(second["metadata"]["uid"] == first["metadata"]["uid"] and
            second["metadata"]["resourceVersion"] != first["metadata"]["resourceVersion"] and
            second["metadata"]["generation"] == 2,
            "Milo authoring update identity/version incorrect")
    history = must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
                        root + APPS_API + "/apptemplaterevisions", 200)
    require(len(history.get("items", [])) == 2, "two immutable template revisions not observed")
    require(all(item["spec"]["projectUID"] == snapshot["project_uids"][provider]
                and item["spec"]["templateUID"] == first["metadata"]["uid"]
                and item["spec"]["effectiveActorUID"] == AUTHOR
                for item in history["items"]), "revision Project or template attribution mismatch")
    must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
              other + path + "/" + name, 404)
    must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
              other + APPS_API + "/apptemplaterevisions/" + history["items"][0]["metadata"]["name"], 404)
    discovery = must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
                          root + APPS_API, 200)
    resources = {resource.get("name") for resource in discovery.get("resources", [])
                 if isinstance(resource.get("name"), str)}
    require(discovery.get("kind") == "APIResourceList" and
            {"apptemplates", "apptemplaterevisions"} <= resources and
            not any(name == "apps" or name.startswith("apps/") for name in resources),
            "Apps API discovery missing authoring resources or exposed App installation")
    must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "POST",
              root + APPS_API + "/apps", 403, {})
    before = pod_state(args)
    deploy = kjson(args.host_kubeconfig, args.host_context, "get", "deployment",
                   "apps-authoring", "-n", "apps-system")
    meta = deploy["metadata"]
    path_annotation = "/spec/template/metadata/annotations/apps-a1t.datum.net~1rollout"
    annotations = deploy["spec"]["template"].get("metadata", {}).get("annotations", {})
    require("apps-a1t.datum.net/rollout" not in annotations,
            "refusing to overwrite an existing Apps rollout annotation")
    operations = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                  {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
    if not annotations:
        operations.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {}})
    operations.append({"op": "add", "path": path_annotation, "value": secrets.token_hex(8)})
    patch_json(args, args.host_kubeconfig, args.host_context, "apps-system", "deployment",
               "apps-authoring", operations, private)
    k(args.host_kubeconfig, args.host_context, "rollout", "status", "deployment/apps-authoring",
      "-n", "apps-system", "--timeout=180s", timeout=190)
    after = pod_state(args)
    require(not (after.keys() & before.keys()),
            "two newly rolled Apps API pod UIDs were not observed")
    # Milo's aggregation transport can briefly retain a connection to an old
    # endpoint after both new Pods report Ready. Bound this real traffic gate.
    def post_rollout_probe():
        status, payload = milo_request(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
                                       root + path + "/" + name)
        if status != 200 and "first_non_200" not in evidence:
            message = payload.get("message", "") if isinstance(payload, dict) else str(payload)
            for token in tokens.values():
                message = message.replace(token, "[redacted]")
            evidence["first_non_200"] = {"http_status": status, "message": message[:300],
                                         "topology": rollout_snapshot(args)}
            record("first post-rollout non-200 captured with bounded endpoint chronology")
        return status == 200

    wait_for(post_rollout_probe, "aggregated Apps template endpoint after both Pods roll", 120)
    persisted = must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
                          root + path + "/" + name, 200)
    require(persisted == second,
            "full updated template changed across both Apps API pod replacements")
    persisted_history = must_http(args.direct_milo_port, ca_file, tokens[AUTHOR], "GET",
                                  root + APPS_API + "/apptemplaterevisions", 200)
    history_items = sorted(history.get("items", []), key=lambda item: item["metadata"]["name"])
    persisted_items = sorted(persisted_history.get("items", []),
                             key=lambda item: item["metadata"]["name"])
    require(persisted_items == history_items,
            "full revision history changed across both Apps API pod replacements")
    print("Actual Milo authoring create/update/history, consumer isolation, App install "
          "absent from discovery and denied to author, "
          "and two-replica history persistence passed", flush=True)
    return {"template_uid": first["metadata"]["uid"],
            "revision_names": sorted(item["metadata"]["name"] for item in history["items"]),
            "api_pod_image_ids_before": before, "api_pod_image_ids_after": after,
            "http": {"create": 201, "duplicate": 409, "update": 200,
                     "history": 200, "other_project": 404, "app_install_author_denied": 403,
                     "post_rollout_template": 200, "post_rollout_history": 200}}


def execute(args, private, snapshot):
    trial_started = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    global GLOBAL_DEADLINE
    run_id = "apps-authoring-" + secrets.token_hex(8)
    tokens = {AUTHORITY: secrets.token_urlsafe(48), AUTHOR: secrets.token_urlsafe(48)}
    new_b64 = auth_csv(snapshot["auth_b64"], tokens)
    refs = []
    host_proxy = ApiProxy(private, args.host_kubeconfig, args.host_context, "host")
    milo_proxy = ApiProxy(private, args.milo_kubeconfig, "milo-root", "milo")
    direct = None
    registry_forward = None
    oci_digest = None
    suspend_intent = False
    suspended = False
    auth_intent = False
    errors = []
    succeeded = False
    evidence_fd, evidence_name = tempfile.mkstemp(prefix="apps-a1t-authoring-evidence-",
                                                  suffix=".json")
    os.close(evidence_fd)
    evidence_path = Path(evidence_name)
    evidence = {"run_id": run_id, "started_at": started_at, "source_revision": REVISION,
                "source_config_sha256": snapshot["config_hash"],
                "source_config_file_count": snapshot["config_file_count"],
                "apps_image": APPS_IMAGE,
                "apps_arm64_manifest": APPS_ARM_MANIFEST,
                "apps_arm64_config": APPS_ARM_CONFIG,
                "etcd_image": ETCD_IMAGE,
                "private_registry_image": REGISTRY_IMAGE,
                "identity_mode": "isolated Milo static-token RBAC; production IAM/OpenFGA unverified",
                "project_uids": snapshot["project_uids"], "checks": [],
                "result": "incomplete", "cleanup": "pending"}

    def record(check):
        evidence["checks"].append(check)
        private_write(evidence_path, json.dumps(evidence, indent=2) + "\n")

    record("read-only preflight exact source/overlay, certificates, Projects and collision guards")

    def add(where, manifest, namespace, path, sensitive=False):
        config, context = ((args.host_kubeconfig, args.host_context) if where == "host"
                           else (args.milo_kubeconfig, "milo-root"))
        ref = reference(namespace, manifest["kind"].lower(), manifest["metadata"]["name"], path)
        refs.append((where, ref))
        create_owned(args, config, context, manifest, ref, run_id, sensitive=sensitive)
        return ref

    try:
        host_proxy.start()
        milo_proxy.start()
        for manifest in load_manifests(ROOT / "components/apps-authoring/registry.yaml"):
            kind = manifest["kind"]
            plural = "deployments" if kind == "Deployment" else "services"
            group = "/apis/apps/v1" if kind == "Deployment" else "/api/v1"
            add("host", manifest, "flux-system",
                f"{group}/namespaces/flux-system/{plural}/apps-a1t-registry")
        k(args.host_kubeconfig, args.host_context, "rollout", "status",
          "deployment/apps-a1t-registry", "-n", "flux-system", "--timeout=180s", timeout=190)
        registry_forward = LocalRegistryForward(args)
        registry_forward.start()
        oci_digest, file_count = publish_local_artifact(args, private, snapshot["config_hash"])
        require(file_count == snapshot["config_file_count"],
                "locally published Apps artifact file count changed")
        evidence["published_oci_digest"] = oci_digest
        record("private task-owned registry served exact PR14 config tree; digest pull hash matched")
        suspend_intent = True
        set_core_suspend(args, snapshot, True, private, run_id)
        suspended = True
        for filename in ("authority-rbac.yaml", "actor-rbac.yaml"):
            for manifest in load_manifests(ROOT / "components/apps-authoring" / filename):
                kind = manifest["kind"]
                name = manifest["metadata"]["name"]
                plural = "clusterroles" if kind == "ClusterRole" else "clusterrolebindings"
                add("milo", manifest, None,
                    f"/apis/rbac.authorization.k8s.io/v1/{plural}/{name}")
        auth_intent = True
        patch_auth(args, snapshot, snapshot["auth_b64"], new_b64, private)
        restart_apiserver(args)
        record("two dedicated random static-token Users installed with CAS; Milo API reloaded")
        public_ca_file = private / "public-ca.pem"
        private_write(public_ca_file, snapshot["ca"].decode())
        direct = DirectMilo(args, public_ca_file)
        direct.start()
        record("task-owned direct Milo port-forward presents CA-verified 127.0.0.1 serving TLS")
        authority_config = private / "authority.kubeconfig"
        actor_config = private / "author.kubeconfig"
        private_write(authority_config, kubeconfig_text(f"https://127.0.0.1:{args.direct_milo_port}",
                                                      snapshot["ca"], tokens[AUTHORITY]))
        private_write(actor_config, kubeconfig_text(f"https://127.0.0.1:{args.direct_milo_port}",
                                                  snapshot["ca"], tokens[AUTHOR]))
        authority_project = must_http(args.direct_milo_port, public_ca_file, tokens[AUTHORITY], "GET",
                                      f"{PROJECT_API}/{PROJECTS[0]}", 200)
        require(authority_project["metadata"]["uid"] == snapshot["project_uids"][PROJECTS[0]],
                "authority root Project GET returned unexpected UID")
        assert_authority_scope(authority_config)
        assert_http_denies(args, public_ca_file, tokens)
        sar = {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview",
               "spec": {"user": AUTHOR, "uid": AUTHOR,
                        "resourceAttributes": {"group": "apps.miloapis.com",
                                               "resource": "apptemplates", "verb": "create"}}}
        response = must_http(args.direct_milo_port, public_ca_file, tokens[AUTHORITY], "POST",
                             "/apis/authorization.k8s.io/v1/subjectaccessreviews", 201, sar)
        require(response.get("status", {}).get("allowed") is True,
                "authority SAR did not allow dedicated author template create")
        record("dedicated authority Milo RBAC allows/denies; actual Project GET and SAR; "
               "author unrelated-operation denials")
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "apps-system"}}
        add("host", namespace, None, "/api/v1/namespaces/apps-system")
        trust = {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": "apps-milo-proxy-trust", "namespace": "apps-system"},
                 "data": {"ca.crt": snapshot["ca"].decode()}}
        add("host", trust, "apps-system", "/api/v1/namespaces/apps-system/configmaps/apps-milo-proxy-trust")
        in_cluster = kubeconfig_text("https://milo-apiserver.milo-system.svc:6443",
                                     snapshot["ca"], tokens[AUTHORITY])
        credential = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                      "metadata": {"name": "apps-milo-authority", "namespace": "apps-system"},
                      "stringData": {"kubeconfig": in_cluster}}
        add("host", credential, "apps-system",
            "/api/v1/namespaces/apps-system/secrets/apps-milo-authority", sensitive=True)
        for filename in ("source.yaml", "runtime.yaml"):
            manifest = yaml.safe_load((ROOT / "components/apps-authoring" / filename).read_text())
            if filename == "source.yaml":
                require(manifest["spec"]["ref"] == {"tag": APPS_SHA},
                        "refusing unexpected mutable Apps OCI bootstrap tag")
                manifest["spec"]["ref"] = {"digest": oci_digest}
            plural = "ocirepositories" if manifest["kind"] == "OCIRepository" else "kustomizations"
            group = "source.toolkit.fluxcd.io" if plural == "ocirepositories" else "kustomize.toolkit.fluxcd.io"
            add("host", manifest, "flux-system",
                f"/apis/{group}/v1/namespaces/flux-system/{plural}/{manifest['metadata']['name']}")
        flux(args, "reconcile", "source", "oci", "apps-authoring-a1t", "-n", "flux-system",
             "--timeout=5m", timeout=310)
        flux(args, "reconcile", "kustomization", "apps-authoring-a1t-runtime", "-n", "flux-system",
             "--with-source", "--timeout=10m", timeout=620)
        runtime_state = require_runtime(args, oci_digest)
        evidence["observed_flux"] = {
            "source_revision": runtime_state["source"]["status"]["artifact"]["revision"],
            "runtime_revision": runtime_state["runtime"]["status"]["lastAppliedRevision"],
            "deployment_uid": runtime_state["deployment"]["metadata"]["uid"],
            "pvc_uid": runtime_state["pvc"]["metadata"]["uid"]}
        record("Flux source and runtime Ready at exact pinned revision; two API replicas and etcd PVC Ready")
        app_ca = secret_field(args, "apps-system", "apps-serving-tls", "ca.crt")
        app_cert = secret_field(args, "apps-system", "apps-serving-tls", "tls.crt")
        verify_certificate(private, app_ca, app_cert, hostname="apps-authoring.apps-system.svc")
        registration = {"apiVersion": "apiregistration.k8s.io/v1", "kind": "APIService",
                        "metadata": {"name": "v1alpha1.apps.miloapis.com"},
                        "spec": {"group": "apps.miloapis.com", "version": "v1alpha1",
                                 "groupPriorityMinimum": 1000, "versionPriority": 15,
                                 "caBundle": base64.b64encode(app_ca).decode(),
                                 "service": {"name": "apps-authoring", "namespace": "apps-system",
                                             "port": 443}}}
        add("milo", registration, None,
            "/apis/apiregistration.k8s.io/v1/apiservices/v1alpha1.apps.miloapis.com")
        wait_for(lambda: apiservice_available(kjson(args.milo_kubeconfig, "milo-root", "get",
                                                  "apiservice", "v1alpha1.apps.miloapis.com")),
                 "Milo manager-owned APIService Available", 180)
        discovery_path = f"{PROJECT_API}/{PROJECTS[0]}/control-plane{APPS_API}"
        wait_for(lambda: milo_request(args.direct_milo_port, public_ca_file, tokens[AUTHOR],
                                      "GET", discovery_path)[0] == 200,
                 "actual Milo project-scoped Apps discovery", 180)
        assert_author_scope(actor_config)
        record("dedicated author template/revision RBAC allows/denies after APIService registration")
        evidence["observed_authoring"] = check_authoring(args, private, snapshot, tokens,
                                                          evidence, record)
        record("real Milo authoring/history/isolation; two new API pod UIDs retain history")
        succeeded = True
    finally:
        cleanup_started = time.monotonic()
        evidence["test_duration_seconds"] = round(cleanup_started - trial_started, 3)
        # Retain a separate bounded cleanup window even if the test deadline expires.
        GLOBAL_DEADLINE = time.monotonic() + 300
        try:
            for where, ref in reversed(refs):
                # Revoke exposure at the APIService before deleting serving pods.
                config, context, proxy = ((args.host_kubeconfig, args.host_context, host_proxy)
                                          if where == "host" else
                                          (args.milo_kubeconfig, "milo-root", milo_proxy))
                try:
                    delete_owned(args, proxy, config, context, ref, run_id)
                except Exception as exc:
                    errors.append(f"{where} {ref['kind']}/{ref['name']}: {exc}")
            if any(ref["kind"] == "deployment" and ref["name"] == "apps-a1t-registry"
                   and ref["intent"] for _, ref in refs):
                try:
                    wait_for(lambda: not kjson(args.host_kubeconfig, args.host_context,
                                               "get", "pods", "-n", "flux-system", "-l",
                                               "apps-a1t.datum.net/component=registry")["items"],
                             "task-owned registry Pods and ephemeral artifact storage removed", 90)
                except Exception as exc:
                    errors.append(f"registry Pod/emptyDir removal: {exc}")
            if auth_intent:
                try:
                    current = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                                    "milo-apiserver-auth-tokens", "-n", "milo-system")
                    if current["data"]["tokens.csv"] == new_b64:
                        patch_auth(args, snapshot, new_b64, snapshot["auth_b64"], private)
                        restart_apiserver(args)
                    elif current["data"]["tokens.csv"] != snapshot["auth_b64"]:
                        errors.append("static auth Secret changed concurrently; refusing overwrite")
                except Exception as exc:
                    errors.append(f"static auth restore: {exc}")
            if suspend_intent:
                try:
                    current = kjson(args.host_kubeconfig, args.host_context, "get",
                                    "kustomization", "milo-core", "-n", "flux-system")
                    if current["metadata"]["uid"] != snapshot["core"]["metadata"]["uid"]:
                        errors.append("Milo core Flux UID changed; refusing adoption")
                    elif current["spec"].get("suspend", False):
                        if current["metadata"].get("annotations", {}).get(
                                "apps-a1t.datum.net/suspend-run-id") == run_id:
                            set_core_suspend(args, snapshot, False, private, run_id)
                        else:
                            errors.append("Milo core Flux suspend ownership ambiguous; manual restore required")
                    elif suspended:
                        errors.append("Milo core Flux unexpectedly resumed during trial")
                except Exception as exc:
                    errors.append(f"Milo core Flux resume: {exc}")
            if auth_intent or suspend_intent:
                try:
                    auth_now = kjson(args.host_kubeconfig, args.host_context, "get", "secret",
                                     "milo-apiserver-auth-tokens", "-n", "milo-system")
                    core_now = kjson(args.host_kubeconfig, args.host_context, "get",
                                     "kustomization", "milo-core", "-n", "flux-system")
                    require(auth_now["metadata"]["uid"] == snapshot["auth"]["metadata"]["uid"]
                            and auth_now["data"]["tokens.csv"] == snapshot["auth_b64"],
                            "Milo static-token Secret was not restored exactly")
                    require(core_now["metadata"]["uid"] == snapshot["core"]["metadata"]["uid"]
                            and core_now["spec"] == snapshot["core"]["spec"]
                            and core_now["metadata"].get("annotations", {}) ==
                            snapshot["core"]["metadata"].get("annotations", {}),
                            "Milo core Flux was not resumed")
                except Exception as exc:
                    errors.append(f"final token/Flux restoration verification: {exc}")
        finally:
            processes = [("host", host_proxy), ("Milo", milo_proxy)]
            if direct is not None:
                processes.append(("direct Milo", direct))
            if registry_forward is not None:
                processes.append(("local registry", registry_forward))
            for name, proxy in processes:
                try:
                    proxy.close()
                except Exception as exc:
                    errors.append(f"{name} private API proxy close: {exc}")
            evidence["cleanup_duration_seconds"] = round(time.monotonic() - cleanup_started, 3)
            evidence["total_duration_seconds"] = round(time.monotonic() - trial_started, 3)
            evidence["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            evidence["cleanup"] = "complete" if not errors else "incomplete: " + "; ".join(errors)
            evidence["result"] = "pass" if succeeded and not errors else "fail"
            private_write(evidence_path, json.dumps(evidence, indent=2) + "\n")
            print(f"Non-secret Apps trial evidence: {evidence_path}", flush=True)
        require(not errors, "Apps cleanup incomplete: " + "; ".join(errors))
    require(succeeded, "Apps trial did not complete")
    print("Apps authoring prerequisite PASS; owned APIService, runtime, dedicated tokens and "
          "namespace removed; Milo Flux resumed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--host-kubeconfig", type=Path, required=True)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", type=Path, required=True)
    parser.add_argument("--milo-port", type=int, default=41443)
    parser.add_argument("--direct-milo-port", type=int, default=41444)
    parser.add_argument("--registry-port", type=int, default=55000)
    parser.add_argument("--execute", action="store_true", help="run the reviewed live trial")
    args = parser.parse_args()
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="apps-a1t-authoring-") as directory:
            private = Path(directory)
            snapshot = preflight(args, private)
            if args.execute:
                global GLOBAL_DEADLINE
                GLOBAL_DEADLINE = time.monotonic() + 1800
                execute(args, private, snapshot)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Apps authoring prerequisite FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
