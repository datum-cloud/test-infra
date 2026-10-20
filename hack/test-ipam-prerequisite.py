#!/usr/bin/env python3
"""Exercise pinned IPAM through Milo project paths in a private kind cluster.

This is an opt-in, destructive-to-its-own-fixtures trial. It creates resources
only after checking their names are vacant, then removes them with UID and
resourceVersion preconditions. It never copies an admin credential into IPAM.
"""

import base64
import csv
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent.parent
HOST_KUBECONFIG = os.environ.get("KUBECONFIG", "")
HOST_CONTEXT = os.environ.get("IPAM_HOST_CONTEXT", "")
MILO_KUBECONFIG = os.environ.get("IPAM_MILO_KUBECONFIG", "")
MILO_PORT = os.environ.get("IPAM_MILO_HTTPS_PORT", "")
PROBE_USER = "test-user"
PROBE_ROLE = "apps-a1t-ipam-probe"
PROVIDER = "apps-a1t-provider"
CONSUMERS = ("apps-a1t-consumer-a", "apps-a1t-consumer-b")
IPAM_API = "/apis/ipam.miloapis.com/v1alpha1"
PROJECT_API = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
IPAM_REVISION = "sha256:f006b5604a924b0ce33adf98644cb106234b75a80fcc937214f3f7e327f8c2eb"
IPAM_IMAGE = "ghcr.io/milo-os/ipam@sha256:9811093733ccc8e0e56ab86ba959dda73d8035a08a6feb7d3a91c5cd10011a3e"
CNPG_IMAGE = "ghcr.io/cloudnative-pg/cloudnative-pg@sha256:a2701eb97cdd2a34b1fdb2cb51987f544b706e40bec72ae7146cd8580efefebb"
CNPG_OCI_DIGEST = "sha256:209c588b902982bf283a0073db83edd422d9710a2c8a670fe57c0329abe789a4"
CNPG_CHART_DIGEST = "sha256:668e065ff53508d58238788fd35b355a925060843629a951df0e6a9362e6d32f"
PG_IMAGE = "ghcr.io/cloudnative-pg/postgresql:17.0@sha256:0a198589e4edca8f169399dd4d082e18de47d289c5f2b7670c9d7bbd9e6f323b"


def run(*args, input_text=None, sensitive=False, timeout=30, env=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True,
                            check=False, timeout=timeout, env=env)
    if result.returncode:
        detail = "details withheld" if sensitive else result.stderr.strip()
        raise RuntimeError(f"{args[0]} failed: {detail}")
    return result.stdout


def host(*args, **kwargs):
    return run("kubectl", "--kubeconfig", HOST_KUBECONFIG, "--context", HOST_CONTEXT,
               "--request-timeout=10s", *args, **kwargs)


def milo(*args, **kwargs):
    return run("kubectl", "--kubeconfig", MILO_KUBECONFIG, "--context", "milo-root",
               "--request-timeout=10s", *args, **kwargs)


def flux(*args, timeout=300):
    return run("flux", *args, env={**os.environ, "KUBECONFIG": HOST_KUBECONFIG},
               timeout=timeout)


def optional(getter, *args):
    # --ignore-not-found does not mask RBAC, network, or API errors.
    output = getter("get", *args, "--ignore-not-found", "-o", "json")
    return json.loads(output) if output.strip() else None


def manifest(path):
    # A single-object local conversion avoids depending on PyYAML in CI.
    return json.loads(host("create", "--dry-run=client", "--validate=false",
                           "-f", str(path), "-o", "json"))


def new_ref(api, kind, name, namespace, path):
    return {"api": api, "kind": kind, "name": name, "namespace": namespace,
            "path": path, "uid": None, "intent": False}


def create_owned(getter, obj, ref, run_id, sensitive=False):
    obj.setdefault("metadata", {}).setdefault("labels", {})["apps-a1t.datum.net/run-id"] = run_id
    ref["intent"] = True
    reply = json.loads(getter("create", "--validate=false", "-f", "-", "-o", "json",
                              input_text=json.dumps(obj), sensitive=sensitive))
    ref["uid"] = reply["metadata"]["uid"]
    return reply


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


class Proxy:
    def __init__(self, private, config, context, label):
        self.path = str(private / f"{label}.sock")
        self.config = config
        self.context = context
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", self.config, "--context", self.context,
             "proxy", f"--unix-socket={self.path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if Path(self.path).is_socket():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError(f"{self.context} private API proxy did not start")

    def delete(self, path, uid, rv):
        body = json.dumps({"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": uid, "resourceVersion": rv}})
        conn = UnixConnection(self.path)
        try:
            conn.request("DELETE", path, body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            if response.status not in (200, 202):
                raise RuntimeError(f"preconditioned DELETE returned HTTP {response.status}")
        finally:
            conn.close()

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


def current(ref):
    if ref.get("probe_config"):
        output = probe(ref["probe_config"], ref["context"], "get", ref["kind"],
                       ref["name"], "--ignore-not-found", "-o", "json")
        return json.loads(output) if output.strip() else None
    getter = host if ref["api"] == "host" else milo
    args = ["-n", ref["namespace"]] if ref["namespace"] else []
    return optional(getter, *args, ref["kind"], ref["name"])


def delete_owned(proxy, ref, run_id):
    if not ref["intent"]:
        return
    obj = current(ref)
    if obj is None:
        return
    meta = obj["metadata"]
    if ref["uid"]:
        if meta["uid"] != ref["uid"]:
            raise RuntimeError(f"replaced {ref['kind']}/{ref['name']}; refusing delete")
    elif meta.get("labels", {}).get("apps-a1t.datum.net/run-id") != run_id:
        raise RuntimeError(f"ownership unknown for {ref['kind']}/{ref['name']}")
    proxy.delete(ref["path"], meta["uid"], meta["resourceVersion"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        now = current(ref)
        if now is None or now["metadata"]["uid"] != meta["uid"]:
            return
        time.sleep(1)
    raise RuntimeError(f"timed out deleting {ref['kind']}/{ref['name']}")


def wait_absent(ref, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if current(ref) is None:
            return
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {ref['kind']}/{ref['name']} to disappear")


def setup_probe_kubeconfig(private):
    secret = json.loads(host("-n", "milo-system", "get", "secret",
                             "milo-apiserver-auth-tokens", "-o", "json"))
    rows = list(csv.reader(base64.b64decode(secret["data"]["tokens.csv"]).decode().splitlines()))
    matches = [r for r in rows if len(r) == 4 and r[1:] ==
               [PROBE_USER, PROBE_USER, "system:authenticated"]]
    if len(matches) != 1:
        raise RuntimeError("expected one scoped test-user token in Milo auth baseline")
    sources = json.loads(run("kubectl", "--kubeconfig", MILO_KUBECONFIG,
                             "config", "view", "-o", "json"))
    by_name = {c["name"]: c["cluster"]["server"] for c in sources["clusters"]}
    contexts = {}
    for c in sources["contexts"]:
        if c["name"] in ("milo-root", PROVIDER, *CONSUMERS):
            contexts[c["name"]] = by_name[c["context"]["cluster"]]
    expected = {"milo-root": f"https://127.0.0.1:{MILO_PORT}"}
    expected.update({name: f"https://127.0.0.1:{MILO_PORT}{PROJECT_API}/{name}/control-plane"
                     for name in (PROVIDER, *CONSUMERS)})
    if contexts != expected:
        raise RuntimeError("private Milo kubeconfig has missing or unexpected project URLs")
    out = {"apiVersion": "v1", "kind": "Config", "current-context": "milo-root",
           "clusters": [{"name": name, "cluster": {"server": server,
                         "insecure-skip-tls-verify": True}} for name, server in contexts.items()],
           "users": [{"name": PROBE_USER, "user": {"token": matches[0][0]}}],
           "contexts": [{"name": name, "context": {"cluster": name, "user": PROBE_USER}}
                        for name in contexts]}
    path = private / "probe-kubeconfig"
    path.write_text(json.dumps(out), encoding="utf-8")
    path.chmod(0o600)
    return path


def probe(config, context, *args, **kwargs):
    return run("kubectl", "--kubeconfig", str(config), "--context", context,
               "--request-timeout=10s", *args, **kwargs)


def require_denied(config, context, verb, resource):
    # kubectl auth can-i intentionally exits 1 when it prints "no".
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(config), "--context", context,
         "--request-timeout=10s", "auth", "can-i", verb, resource],
        text=True, capture_output=True, check=False, timeout=30)
    if result.returncode != 1 or result.stdout.strip() != "no":
        raise RuntimeError(f"scoped IPAM probe denial unproven for {verb} {resource}")


def sample_role():
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": PROBE_ROLE},
            "rules": [{"apiGroups": ["ipam.miloapis.com"],
                       "resources": ["ipclasses", "ippools"],
                       "verbs": ["get", "list", "create", "delete"]}]}


def sample_binding():
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": PROBE_ROLE},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                        "name": PROBE_ROLE},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User",
                          "name": PROBE_USER}]}


def require_deployment_image(namespace, name, repository, expected):
    deployment = json.loads(host("-n", namespace, "get", "deployment", name, "-o", "json"))
    pod = deployment["spec"]["template"]["spec"]
    images = [c["image"] for c in pod.get("initContainers", []) + pod.get("containers", [])
              if c["image"].startswith(repository)]
    if not images or any(image != expected for image in images):
        raise RuntimeError(f"{name} is not using the pinned {repository} image")


def preflight():
    if not (HOST_KUBECONFIG.startswith("/tmp/") and Path(HOST_KUBECONFIG).is_file()):
        raise RuntimeError("explicit private /tmp KUBECONFIG required")
    if not (MILO_KUBECONFIG.startswith("/tmp/") and Path(MILO_KUBECONFIG).is_file()):
        raise RuntimeError("explicit private /tmp IPAM_MILO_KUBECONFIG required")
    if not HOST_CONTEXT.startswith("kind-apps-a1t-"):
        raise RuntimeError("IPAM_HOST_CONTEXT must name a dedicated apps-a1t kind cluster")
    if not MILO_PORT.isdecimal() or not 1 <= int(MILO_PORT) <= 65535:
        raise RuntimeError("IPAM_MILO_HTTPS_PORT must be a valid port")
    if run("kubectl", "--kubeconfig", HOST_KUBECONFIG, "config", "current-context").strip() != HOST_CONTEXT:
        raise RuntimeError("private host kubeconfig has a different current context")
    if run("kubectl", "--kubeconfig", MILO_KUBECONFIG, "config", "current-context").strip() != "milo-root":
        raise RuntimeError("private Milo kubeconfig must select milo-root")
    servers = json.loads(run("kubectl", "--kubeconfig", MILO_KUBECONFIG,
                             "config", "view", "--minify", "-o", "json"))
    if servers["clusters"][0]["cluster"]["server"] != f"https://127.0.0.1:{MILO_PORT}":
        raise RuntimeError("Milo kubeconfig points at an unexpected server")
    for kind, name, ns in [
        ("namespace", "ipam-system", None), ("namespace", "cnpg-system", None),
        ("ocirepository", "ipam", "flux-system"),
        ("ocirepository", "cnpg", "cnpg-system"),
        ("helmrelease", "cnpg", "cnpg-system"),
        ("kustomization", "ipam-ca", "flux-system"),
        ("kustomization", "ipam-runtime", "flux-system"),
        ("clusterrole", "ipam-apiserver", None),
        ("clusterrolebinding", "ipam-apiserver", None),
        ("clusterrolebinding", "ipam-apiserver:system:auth-delegator", None),
        ("clusterrole", PROBE_ROLE, None),
        ("clusterrolebinding", PROBE_ROLE, None),
    ]:
        args = ["-n", ns] if ns else []
        if optional(host, *args, kind, name) is not None:
            raise RuntimeError(f"refusing existing host {kind}/{name}")
    for kind, name in [("apiservice", "v1alpha1.ipam.miloapis.com"),
                       ("clusterrole", PROBE_ROLE), ("clusterrolebinding", PROBE_ROLE)]:
        if optional(milo, kind, name) is not None:
            raise RuntimeError(f"refusing existing Milo {kind}/{name}")
    if optional(host, "clusterissuer", "selfsigned-cluster-issuer") is None:
        raise RuntimeError("selfsigned-cluster-issuer is not installed")
    proxy_cert = optional(host, "-n", "milo-system", "certificate", "milo-front-proxy-client")
    if proxy_cert is None or not any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in proxy_cert.get("status", {}).get("conditions", [])
    ):
        raise RuntimeError("Milo aggregator front-proxy client Certificate is not Ready")
    deployment = optional(host, "-n", "milo-system", "deployment", "milo-apiserver")
    if deployment is None:
        raise RuntimeError("Milo apiserver Deployment is absent")
    pod = deployment["spec"]["template"]["spec"]
    containers = [c for c in pod["containers"] if c["name"] == "milo-apiserver"]
    if len(containers) != 1:
        raise RuntimeError("expected one Milo apiserver container")
    env = {item["name"]: item.get("value") for item in containers[0].get("env", [])}
    if env.get("PROXY_CLIENT_CERT_FILE") != "/etc/kubernetes/pki/front-proxy/tls.crt" or \
       env.get("PROXY_CLIENT_KEY_FILE") != "/etc/kubernetes/pki/front-proxy/tls.key":
        raise RuntimeError("Milo aggregator does not use the front-proxy client certificate")
    mounts = {m["name"]: m for m in containers[0].get("volumeMounts", [])}
    volumes = {v["name"]: v for v in pod.get("volumes", [])}
    if mounts.get("front-proxy", {}).get("mountPath") != "/etc/kubernetes/pki/front-proxy" or \
       volumes.get("front-proxy", {}).get("secret", {}).get("secretName") != "milo-front-proxy-client":
        raise RuntimeError("Milo aggregator client certificate Secret is not mounted")


def main():
    preflight()
    run_id = secrets.token_hex(16)
    refs = []
    host_proxy = None
    milo_proxy = None
    with tempfile.TemporaryDirectory(prefix="apps-a1t-ipam-", dir="/tmp") as tmp:
        private = Path(tmp)
        host_proxy = Proxy(private, HOST_KUBECONFIG, HOST_CONTEXT, "host")
        milo_proxy = Proxy(private, MILO_KUBECONFIG, "milo-root", "milo")
        def add(api, getter, obj, namespace, path, sensitive=False):
            ref = new_ref(api, obj["kind"].lower(), obj["metadata"]["name"], namespace, path)
            refs.append(ref)
            create_owned(getter, obj, ref, run_id, sensitive=sensitive)
            return ref
        try:
            host_proxy.start()
            milo_proxy.start()
            for name in ("ipam-system", "cnpg-system"):
                add("host", host, {"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": name}}, None, f"/api/v1/namespaces/{name}")
            add("host", host, manifest(ROOT / "components/ipam/source.yaml"), "flux-system",
                "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/ocirepositories/ipam")
            add("host", host, manifest(ROOT / "components/ipam/cnpg-source.yaml"), "cnpg-system",
                "/apis/source.toolkit.fluxcd.io/v1/namespaces/cnpg-system/ocirepositories/cnpg")
            flux("reconcile", "source", "oci", "cnpg", "-n", "cnpg-system",
                 "--timeout=180s", timeout=210)
            chart = json.loads(host("-n", "cnpg-system", "get", "ocirepository", "cnpg",
                                    "-o", "json"))
            if chart["spec"].get("ref", {}).get("digest") != CNPG_OCI_DIGEST or \
               chart.get("status", {}).get("artifact", {}).get("revision") != CNPG_OCI_DIGEST or \
               chart.get("status", {}).get("artifact", {}).get("digest") != CNPG_CHART_DIGEST:
                raise RuntimeError("CNPG OCI chart content differs from pinned digest before install")
            add("host", host, manifest(ROOT / "components/ipam/cnpg-release.yaml"), "cnpg-system",
                "/apis/helm.toolkit.fluxcd.io/v2/namespaces/cnpg-system/helmreleases/cnpg")
            flux("reconcile", "source", "oci", "ipam", "-n", "flux-system", "--timeout=120s", timeout=150)
            flux("reconcile", "helmrelease", "cnpg", "-n", "cnpg-system",
                 "--timeout=600s", timeout=630)
            host("wait", "crd/clusters.postgresql.cnpg.io", "--for=condition=Established",
                 "--timeout=120s", timeout=140)
            host("-n", "cnpg-system", "rollout", "status", "deployment/cnpg-cloudnative-pg",
                 "--timeout=180s", timeout=200)
            require_deployment_image("cnpg-system", "cnpg-cloudnative-pg",
                                     "ghcr.io/cloudnative-pg/cloudnative-pg", CNPG_IMAGE)
            password = secrets.token_urlsafe(32)
            dsn = ("postgres://ipam:" + quote(password, safe="") +
                   "@ipam-db-rw.ipam-system.svc.cluster.local:5432/ipam?sslmode=disable")
            credentials = {"apiVersion": "v1", "kind": "Secret",
                           "metadata": {"name": "postgres-credentials", "namespace": "ipam-system"},
                           "type": "kubernetes.io/basic-auth",
                           "stringData": {"username": "ipam", "password": password, "dsn": dsn}}
            add("host", host, credentials, "ipam-system",
                "/api/v1/namespaces/ipam-system/secrets/postgres-credentials", sensitive=True)
            password = dsn = None
            add("host", host, manifest(ROOT / "components/ipam/database.yaml"), "ipam-system",
                "/apis/postgresql.cnpg.io/v1/namespaces/ipam-system/clusters/ipam-db")
            host("-n", "ipam-system", "wait", "cluster.postgresql.cnpg.io/ipam-db",
                 "--for=condition=Ready", "--timeout=420s", timeout=450)
            db = json.loads(host("-n", "ipam-system", "get", "cluster.postgresql.cnpg.io",
                                 "ipam-db", "-o", "json"))
            if db["spec"].get("imageName") != PG_IMAGE:
                raise RuntimeError("IPAM database is not using the pinned PostgreSQL image")
            milo_ca = json.loads(host("-n", "milo-system", "get", "secret", "milo-ca-tls",
                                      "-o", "json"))["data"]["ca.crt"]
            if not milo_ca:
                raise RuntimeError("Milo front-proxy CA is empty")
            add("host", host, {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": "control-plane-ca", "namespace": "ipam-system"},
                 "data": {"ca.crt": base64.b64decode(milo_ca).decode()}}, "ipam-system",
                "/api/v1/namespaces/ipam-system/configmaps/control-plane-ca", sensitive=True)
            add("host", host, manifest(ROOT / "components/ipam/ca.yaml"), "flux-system",
                "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/ipam-ca")
            flux("reconcile", "kustomization", "ipam-ca", "-n", "flux-system",
                 "--timeout=180s", timeout=210)
            add("host", host, manifest(ROOT / "components/ipam/runtime.yaml"), "flux-system",
                "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/ipam-runtime")
            flux("reconcile", "kustomization", "ipam-runtime", "-n", "flux-system",
                 "--timeout=360s", timeout=390)
            runtime = json.loads(host("-n", "flux-system", "get", "kustomization",
                                      "ipam-runtime", "-o", "json"))
            if runtime.get("status", {}).get("lastAppliedRevision") != IPAM_REVISION:
                raise RuntimeError("IPAM Flux applied revision differs from the pinned OCI digest")
            require_deployment_image("ipam-system", "ipam-apiserver", "ghcr.io/milo-os/ipam",
                                     IPAM_IMAGE)
            cert_name = json.loads(host("-n", "ipam-system", "get", "certificate", "ipam-ca",
                                        "-o", "json"))["spec"]["secretName"]
            ipam_ca = json.loads(host("-n", "ipam-system", "get", "secret", cert_name,
                                      "-o", "json"))["data"]["ca.crt"]
            if not ipam_ca:
                raise RuntimeError("IPAM serving CA is empty")
            api_service = {"apiVersion": "apiregistration.k8s.io/v1", "kind": "APIService",
                           "metadata": {"name": "v1alpha1.ipam.miloapis.com"},
                           "spec": {"group": "ipam.miloapis.com", "version": "v1alpha1",
                                    "groupPriorityMinimum": 1000, "versionPriority": 15,
                                    "caBundle": ipam_ca,
                                    "service": {"name": "ipam-apiserver", "namespace": "ipam-system",
                                                "port": 443}}}
            add("milo", milo, api_service, None,
                "/apis/apiregistration.k8s.io/v1/apiservices/v1alpha1.ipam.miloapis.com")
            for api, getter in (("host", host), ("milo", milo)):
                for kind, obj in (("clusterrole", sample_role()),
                                  ("clusterrolebinding", sample_binding())):
                    plural = "clusterroles" if kind == "clusterrole" else "clusterrolebindings"
                    add(api, getter, obj, None,
                        f"/apis/rbac.authorization.k8s.io/v1/{plural}/{PROBE_ROLE}")
            probe_config = setup_probe_kubeconfig(private)
            # The actual proxy request, not the APIService condition, is the readiness gate.
            deadline = time.monotonic() + 180
            last_error = "no response"
            while True:
                try:
                    reply = json.loads(probe(probe_config, PROVIDER, "get", "--raw",
                                             f"{IPAM_API}/ippools"))
                    if reply.get("kind") == "IPPoolList":
                        break
                    last_error = f"unexpected list kind {reply.get('kind')}"
                except Exception as exc:
                    last_error = str(exc)
                if time.monotonic() >= deadline:
                    raise RuntimeError("IPAM did not serve an IPPoolList through provider project: " + last_error)
                time.sleep(3)
            for verb, resource in (("get", "secrets"), ("create", "clusterrolebindings"),
                                   ("impersonate", "users")):
                require_denied(probe_config, PROVIDER, verb, resource)
            ipclass = {"apiVersion": "ipam.miloapis.com/v1alpha1", "kind": "IPClass",
                       "metadata": {"name": "apps-a1t-probe-v4"},
                       "spec": {"ipFamily": "IPv4", "allowedPrefixLengths": {"min": 32, "max": 32},
                                "reclaimPolicy": "Delete", "routing": {"internal": "Host"}}}
            pool = {"apiVersion": "ipam.miloapis.com/v1alpha1", "kind": "IPPool",
                    "metadata": {"name": "apps-a1t-probe-pool"},
                    "spec": {"cidr": "198.19.0.0/24", "ipFamily": "IPv4",
                             "visibility": "consumer", "classNames": ["apps-a1t-probe-v4"],
                             "allocation": {"minPrefixLength": 32, "maxPrefixLength": 32,
                                            "strategy": "FirstFit"}}}
            for obj, plural in ((ipclass, "ipclasses"), (pool, "ippools")):
                ref = new_ref("milo", obj["kind"].lower(), obj["metadata"]["name"], None,
                              f"{PROJECT_API}/{PROVIDER}/control-plane{IPAM_API}/{plural}/{obj['metadata']['name']}")
                ref["probe_config"] = str(probe_config)
                ref["context"] = PROVIDER
                refs.append(ref)
                obj["metadata"].setdefault("labels", {})["apps-a1t.datum.net/run-id"] = run_id
                ref["intent"] = True
                reply = json.loads(probe(probe_config, PROVIDER, "create", "--validate=false",
                                         "-f", "-", "-o", "json", input_text=json.dumps(obj)))
                ref["uid"] = reply["metadata"]["uid"]
            for context, should_contain in ((PROVIDER, True), (CONSUMERS[0], False),
                                            (CONSUMERS[1], False), ("milo-root", False)):
                listing = json.loads(probe(probe_config, context, "get", "--raw",
                                           f"{IPAM_API}/ippools"))
                names = {item["metadata"]["name"] for item in listing.get("items", [])}
                if ("apps-a1t-probe-pool" in names) != should_contain:
                    raise RuntimeError(f"IPPool project isolation mismatch at {context}")
                print(f"IPAM {context}: provider probe pool {'present' if should_contain else 'absent'}", flush=True)
            print("IPAM prerequisite passed: exact source/image, CNPG, Milo aggregation, scoped project reads", flush=True)
        finally:
            errors = []
            for ref in reversed(refs):
                try:
                    delete_owned(host_proxy if ref["api"] == "host" else milo_proxy, ref, run_id)
                    print(f"removed {ref['api']} {ref['kind']}/{ref['name']}", flush=True)
                    if ref["name"] == "ipam-runtime" and ref["kind"] == "kustomization":
                        wait_absent(new_ref("host", "deployment", "ipam-apiserver", "ipam-system",
                                            ""))
                    if ref["name"] == "cnpg" and ref["kind"] == "helmrelease":
                        wait_absent(new_ref("host", "deployment", "cnpg-cloudnative-pg", "cnpg-system", ""))
                except Exception as exc:
                    errors.append(f"{ref['api']} {ref['kind']}/{ref['name']}: {exc}")
            if milo_proxy:
                milo_proxy.close()
            if host_proxy:
                host_proxy.close()
            if errors:
                raise RuntimeError("cleanup incomplete: " + "; ".join(errors))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f"IPAM prerequisite failed: {error}", file=sys.stderr)
        sys.exit(1)
