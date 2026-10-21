"""Bounded, explicit-context helpers for the isolated Apps native API trial.

This module only speaks to Kubernetes APIs and Flux. It has no storage client.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import http.client
import json
import json
from pathlib import Path
import socket
import ssl
import subprocess
import time


RUN_LABEL = "apps-a1t.datum.net/run-id"


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


class Commands:
    def __init__(self) -> None:
        self.deadline: float | None = None

    def run(self, *argv: str, input_text: str | None = None,
            sensitive: bool = False, timeout: float = 30) -> str:
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            require(remaining > 0, "Apps trial deadline exceeded")
            timeout = min(timeout, remaining)
        result = subprocess.run(argv, input=input_text, text=True,
                                capture_output=True, check=False, timeout=timeout)
        if result.returncode:
            detail = "details withheld" if sensitive else result.stderr.strip()[:900]
            raise RuntimeError(f"{argv[0]} failed: {detail}")
        return result.stdout.strip()


def wait_for(predicate, label: str, seconds: float = 120):
    end = time.monotonic() + seconds
    last = "condition false"
    while time.monotonic() < end:
        try:
            value = predicate()
            if value:
                return value
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            last = str(error)[:500]
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {label}: {last}")


def private_write(path: Path, content: str | bytes) -> None:
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(0o600)


def private_file(path: Path) -> None:
    require(path.is_absolute() and path.is_file(), f"absolute private file required: {path}")
    require(path.stat().st_mode & 0o077 == 0, f"file must have private mode: {path}")


def tree_fingerprint(root: Path) -> tuple[str, int]:
    require(root.is_dir(), f"missing source tree: {root}")
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "source artifact contains a symlink")
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        count += 1
    require(count > 0, "source artifact is empty")
    return digest.hexdigest(), count


def ready(obj: dict, condition: str = "Ready") -> bool:
    generation = obj["metadata"].get("generation", 1)
    status = obj.get("status", {})
    return status.get("observedGeneration", 0) >= generation and any(
        entry.get("type") == condition and entry.get("status") == "True"
        and entry.get("observedGeneration", generation) >= generation
        for entry in status.get("conditions", []))


def certificate_ready(obj: dict) -> bool:
    # cert-manager v1.16 records observedGeneration on each condition, not on
    # CertificateStatus. Requiring Flux's top-level field would always fail.
    generation = obj["metadata"].get("generation", 1)
    return obj.get("kind") == "Certificate" and any(
        entry.get("type") == "Ready" and entry.get("status") == "True"
        and entry.get("observedGeneration", 0) >= generation
        for entry in obj.get("status", {}).get("conditions", []))


class Kube:
    def __init__(self, commands: Commands, config: Path, context: str):
        self.commands, self.config, self.context = commands, config, context

    def k(self, *args: str, sensitive: bool = False,
          input_text: str | None = None, timeout: float = 30) -> str:
        return self.commands.run("kubectl", "--kubeconfig", str(self.config),
                                 "--context", self.context, "--request-timeout=10s",
                                 *args, sensitive=sensitive, input_text=input_text,
                                 timeout=timeout)

    def json(self, *args: str) -> dict:
        return json.loads(self.k(*args, "-o", "json"))

    def optional(self, kind: str, name: str, namespace: str | None = None) -> dict | None:
        scope = ("-n", namespace) if namespace else ()
        raw = self.k(*scope, "get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(raw) if raw else None

    def create(self, manifest: dict, namespace: str | None = None,
               sensitive: bool = False) -> dict:
        scope = ("-n", namespace) if namespace else ()
        return json.loads(self.k(*scope, "create", "-f", "-", "-o", "json",
                                 input_text=json.dumps(manifest), sensitive=sensitive))

    def patch_json(self, private: Path, kind: str, name: str, operations: list[dict],
                   namespace: str | None = None, sensitive: bool = False) -> dict:
        patch_file = private / "cas-patch.json"
        private_write(patch_file, json.dumps(operations))
        scope = ("-n", namespace) if namespace else ()
        return json.loads(self.k(*scope, "patch", kind, name, "--type=json",
                                 "--patch-file", str(patch_file), "-o", "json",
                                 sensitive=sensitive))

    def can_i(self, verb: str, resource: str, expected: bool,
              name: str | None = None) -> None:
        base, separator, subresource = resource.partition("/")
        plural, dot, group = base.partition(".")
        if not dot:
            groups = {"secrets": "", "users": "", "clusterroles": "rbac.authorization.k8s.io"}
            require(plural in groups, "authorization probe needs an explicit API group")
            group = groups[plural]
        attributes = {"verb": verb, "group": group, "resource": plural}
        if separator:
            attributes["subresource"] = subresource
        if name:
            attributes["name"] = name
        review = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                  "spec": {"resourceAttributes": attributes}}
        result = json.loads(self.k("create", "--raw",
                            "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                            "-f", "-", input_text=json.dumps(review)))
        status = result.get("status", {})
        require(isinstance(status.get("allowed"), bool) and
                status["allowed"] == expected and not status.get("evaluationError"),
                f"authorization {verb} {resource}: expected {expected}, "
                f"got {status.get('allowed')!r}")



class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(str(self.path))


class APIProxy:
    def __init__(self, kube: Kube, private: Path, name: str):
        self.kube, self.path = kube, private / f"{name}-api.sock"
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", str(self.kube.config), "--context",
             self.kube.context, "--request-timeout=10s", "proxy",
             f"--unix-socket={self.path}"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        def socket_ready():
            require(self.process.poll() is None, "private API proxy exited")
            return self.path.is_socket()
        wait_for(socket_ready, "private API proxy socket", 15)

    def delete(self, path: str, uid: str, rv: str) -> None:
        body = json.dumps({"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": uid, "resourceVersion": rv}})
        conn = UnixHTTPConnection(self.path)
        try:
            conn.request("DELETE", path, body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            require(response.status in (200, 202),
                    f"UID/RV delete returned HTTP {response.status}")
        finally:
            conn.close()

    def get(self, path: str, retry_seconds: float = 60) -> dict:
        # Milo may rate-limit Project API discovery briefly after a new CRD
        # appears. Retry only 429 on read-only GET; every other response and
        # an oversized body still fails closed.
        deadline = time.monotonic() + retry_seconds
        if self.kube.commands.deadline is not None:
            deadline = min(deadline, self.kube.commands.deadline)
        while True:
            conn = UnixHTTPConnection(self.path)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                raw = response.read(65537)
                status = response.status
            finally:
                conn.close()
            require(len(raw) <= 65536, "private API GET returned oversized body")
            if status == 200:
                return json.loads(raw)
            if status != 429 or time.monotonic() >= deadline:
                raise RuntimeError(f"private API GET returned HTTP {status}")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def close(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


@dataclass
class Owned:
    kube: Kube
    namespace: str | None
    kind: str
    name: str
    path: str
    intent: bool = False
    uid: str | None = None

    def create(self, manifest: dict, run_id: str, *, sensitive: bool = False) -> dict:
        manifest.setdefault("metadata", {}).setdefault("labels", {})[RUN_LABEL] = run_id
        self.intent = True
        obj = self.kube.create(manifest, self.namespace, sensitive=sensitive)
        self.uid = obj["metadata"]["uid"]
        return obj

    def delete(self, proxy: APIProxy, run_id: str, timeout: float = 120) -> None:
        if not self.intent:
            return
        obj = self.kube.optional(self.kind, self.name, self.namespace)
        if obj is None:
            return
        meta = obj["metadata"]
        require(meta["uid"] == self.uid if self.uid else
                meta.get("labels", {}).get(RUN_LABEL) == run_id,
                f"refusing ambiguous {self.kind}/{self.name} cleanup")
        proxy.delete(self.path, meta["uid"], meta["resourceVersion"])
        def absent():
            current = self.kube.optional(self.kind, self.name, self.namespace)
            require(current is None or current["metadata"]["uid"] == meta["uid"],
                    f"refusing replacement {self.kind}/{self.name} after delete")
            return current is None
        wait_for(absent, f"{self.kind}/{self.name} absent after owned delete", timeout)


class HeldLease(Owned):
    """Release only our fixture UID; a controller-created successor is expected."""

    def delete(self, proxy: APIProxy, run_id: str, timeout: float = 120) -> None:
        if not self.intent:
            return
        obj = self.kube.optional(self.kind, self.name, self.namespace)
        if obj is not None:
            meta = obj["metadata"]
            require(self.uid and meta["uid"] == self.uid and
                    obj.get("spec", {}).get("holderIdentity") == run_id,
                    "refusing to release a changed held Lease")
            proxy.delete(self.path, meta["uid"], meta["resourceVersion"])
            def old_uid_gone():
                current = self.kube.optional(self.kind, self.name, self.namespace)
                return current is None or current["metadata"]["uid"] != self.uid
            wait_for(old_uid_gone, "held Lease UID absent", timeout)
        self.intent = False


class Forward:
    def __init__(self, kube: Kube, namespace: str, service: str,
                 local_port: int, remote_port: int):
        self.kube, self.namespace, self.service = kube, namespace, service
        self.local_port, self.remote_port = local_port, remote_port
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", str(self.kube.config), "--context",
             self.kube.context, "--request-timeout=10s", "-n", self.namespace,
             "port-forward", f"service/{self.service}",
             f"{self.local_port}:{self.remote_port}", "--address=127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def connected():
            require(self.process.poll() is None, "private port-forward exited")
            with socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                return True
        wait_for(connected, f"private {self.service} port-forward", 20)

    def verified_tls(self, ca_file: Path) -> None:
        with socket.create_connection(("127.0.0.1", self.local_port), timeout=3) as raw:
            with ssl.create_default_context(cafile=str(ca_file)).wrap_socket(
                    raw, server_hostname="127.0.0.1") as peer:
                require(bool(peer.getpeercert()), "Milo TLS certificate unavailable")

    def close(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


def kubeconfig_text(server: str, ca: bytes, token: str) -> str:
    import yaml
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "scoped",
        "clusters": [{"name": "milo", "cluster": {"server": server,
                      "certificate-authority-data": base64.b64encode(ca).decode()}}],
        "users": [{"name": "scoped", "user": {"token": token}}],
        "contexts": [{"name": "scoped", "context": {"cluster": "milo",
                       "user": "scoped"}}],
    })


def verify_cert(commands: Commands, private: Path, ca: bytes, cert: bytes,
                *, dns: str | None = None, ip: str | None = None) -> None:
    cafile = private / f"ca-{hashlib.sha256(ca).hexdigest()}.pem"
    certfile = private / f"cert-{hashlib.sha256(cert).hexdigest()}.pem"
    private_write(cafile, ca)
    private_write(certfile, cert)
    argv = ["openssl", "verify", "-CAfile", str(cafile)]
    if dns:
        argv += ["-verify_hostname", dns]
    if ip:
        argv += ["-verify_ip", ip]
    argv.append(str(certfile))
    require(commands.run(*argv).endswith(": OK"), "public CA/SAN verification failed")
