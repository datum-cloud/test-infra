"""Bounded, explicit-context helpers for the isolated Apps native API trial.

This module only speaks to Kubernetes APIs and Flux. It has no storage client.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time

import yaml


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
        require(private.is_dir() and not private.is_symlink() and
                private.stat().st_mode & 0o077 == 0,
                "private API proxy journal directory required")
        # macOS rejects AF_UNIX paths longer than 103 bytes. Journals may be
        # deeply nested, so only the ephemeral socket moves to a fresh 0700
        # directory; the kubeconfig and recovery journal remain in `private`.
        socket_dir = Path(tempfile.mkdtemp(prefix="apps-a1t-api-", dir="/tmp"))
        self.kube, self.path, self.socket_dir = kube, socket_dir / "api.sock", socket_dir
        if len(os.fsencode(self.path)) >= 100:
            socket_dir.rmdir()
            raise RuntimeError("private API proxy socket path exceeds platform bound")
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        try:
            self.process = subprocess.Popen(
                ["kubectl", "--kubeconfig", str(self.kube.config), "--context",
                 self.kube.context, "--request-timeout=10s", "proxy",
                 f"--unix-socket={self.path}"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            def socket_ready():
                require(self.process.poll() is None, "private API proxy exited")
                return self.path.is_socket()
            wait_for(socket_ready, "private API proxy socket", 15)
        except Exception:
            self.close()
            raise

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
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.process = None
        self.path.unlink(missing_ok=True)
        try:
            self.socket_dir.rmdir()
        except FileNotFoundError:
            pass


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

def write_flux_journal(path: Path, state: dict) -> None:
    """Atomic private journal; failure prevents the associated API mutation."""
    require(path.is_absolute() and path.parent.is_dir() and not path.is_symlink(),
            "absolute non-symlink Flux journal required")
    fd, pending = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(pending).unlink(missing_ok=True)


def flux_inventory(rendered: list[dict]) -> frozenset[tuple[str, str]]:
    """Flux v2.7.5 ResourceRef: namespace_name_group_kind plus API version.

    Inputs must be the final source-owned render, including targetNamespace and
    name transformations. No live inventory is ever used to expand this set.
    """
    require(isinstance(rendered, list) and 0 < len(rendered) <= 256,
            "bounded nonempty source render required for Flux prune")
    entries = set()
    ids = set()
    for obj in rendered:
        api, kind, meta = obj.get("apiVersion", ""), obj.get("kind", ""), obj.get("metadata", {})
        group, version = api.split("/", 1) if "/" in api else ("", api)
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        require(all(isinstance(v, str) and "_" not in v for v in (group, version, kind, namespace, name)) and
                bool(version and kind and name) and not meta.get("generateName"),
                "source render lacks an exact Flux inventory identity")
        identity = f"{namespace}_{name}_{group}_{kind}"
        require(identity not in ids, "duplicate or version-aliased source inventory identity")
        ids.add(identity)
        entries.add((identity, version))
    return frozenset(entries)


def _flux_spec(spec: dict) -> dict:
    """Only the two defaults in the pinned Flux v2.7.5 v1 CRD schema."""
    require(isinstance(spec, dict), "Flux spec must be an object")
    out = copy.deepcopy(spec)
    out.setdefault("force", False)
    for ref in out.get("postBuild", {}).get("substituteFrom", []):
        ref.setdefault("optional", False)
    return out


class FluxOwned(Owned):
    """A narrowly typed Kustomization owner with bounded prune authority.

    Spec checks are in addition to UID/RV DELETE, never a replacement. The
    optional journal callback must atomically persist the supplied noncredential
    intent before returning; a callback error prevents the next API mutation.
    It receives public CA patches and reference names, never kubeconfig contents.
    """
    def __init__(self, kube: Kube, namespace: str, name: str,
                 rendered: list[dict], revision: str, journal=None):
        super().__init__(kube, namespace, "kustomization", name,
                         f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{namespace}/kustomizations/{name}")
        require(isinstance(revision, str) and bool(revision), "exact source artifact revision required")
        self.allowed = flux_inventory(rendered)
        self._rendered = copy.deepcopy(rendered)
        self.revision, self.journal = revision, journal
        self._initial = None
        self._run_id = None
        self._ca = None
        self._ca_committed = False
        self._delete_intent = False

    def _capture(self, manifest: dict, run_id: str) -> dict:
        obj = copy.deepcopy(manifest)
        meta = obj.setdefault("metadata", {})
        meta.setdefault("labels", {})[RUN_LABEL] = run_id
        require(obj.get("apiVersion") == "kustomize.toolkit.fluxcd.io/v1" and
                obj.get("kind") == "Kustomization" and
                meta.get("name") == self.name and meta.get("namespace") == self.namespace and
                set(obj) == {"apiVersion", "kind", "metadata", "spec"} and
                set(meta) <= {"name", "namespace", "labels", "annotations"} and
                obj["spec"].get("prune") is True and isinstance(run_id, str) and bool(run_id),
                "exact independent source-owned pruning Kustomization required")
        require(len(json.dumps(obj)) <= 1024 * 1024, "Flux intent exceeds bounded size")
        obj["spec"] = _flux_spec(obj["spec"])
        return obj

    def recovery_state(self) -> dict:
        return copy.deepcopy({"version": 1, "run_id": self._run_id, "initial": self._initial,
                              "inventory": [list(x) for x in sorted(self.allowed)], "revision": self.revision,
                              "uid": self.uid, "intent": self.intent, "delete_intent": self._delete_intent,
                              "ca_bundle": self._ca, "ca_committed": self._ca_committed})

    def _record(self):
        if self.journal is not None:
            self.journal(self.recovery_state())

    def recover(self, manifest: dict, run_id: str, saved: dict) -> None:
        """Restore only an intent bound to a newly verified original source render."""
        require(not self.intent and self._initial is None, "Flux owner is already initialized")
        initial = self._capture(manifest, run_id)
        require(set(saved) == {"version", "run_id", "initial", "inventory", "revision", "uid", "intent",
                               "delete_intent", "ca_bundle", "ca_committed"} and
                saved["version"] == 1 and saved["run_id"] == run_id and saved["initial"] == initial and
                saved["inventory"] == [list(x) for x in sorted(self.allowed)] and saved["revision"] == self.revision and
                saved["intent"] is True and type(saved["delete_intent"]) is bool and type(saved["ca_committed"]) is bool and
                (saved["uid"] is None or isinstance(saved["uid"], str) and bool(saved["uid"])) and
                (not saved["delete_intent"] or saved["uid"]),
                "Flux recovery journal differs from source-owned intent")
        self._initial, self._run_id = initial, run_id
        self._ca = saved["ca_bundle"]
        if self._ca is not None:
            self._ca_spec(self._ca)  # validates exact supported transition
        require(self._ca is not None or saved["ca_committed"] is False, "invalid Flux CA recovery phase")
        self._ca_committed = saved["ca_committed"]
        self.uid, self.intent, self._delete_intent = saved["uid"], True, saved["delete_intent"]

    def create(self, manifest: dict, run_id: str, *, sensitive: bool = False) -> dict:
        require(not self.intent and self._initial is None, "Flux create intent already exists")
        self._initial, self._run_id = self._capture(manifest, run_id), run_id
        self.intent = True
        self._record()
        obj = self.kube.create(copy.deepcopy(self._initial), self.namespace, sensitive=sensitive)
        self.uid = self._validate(obj, run_id)
        self._record()
        return obj

    def _ca_spec(self, bundle: str) -> dict:
        require(isinstance(bundle, str) and 0 < len(bundle) <= 131072,
                "bounded public CA bundle required")
        try:
            decoded = base64.b64decode(bundle, validate=True)
        except (ValueError, TypeError):
            raise RuntimeError("public CA bundle encoding invalid") from None
        require(decoded.startswith(b"-----BEGIN CERTIFICATE-----") and
                b"PRIVATE KEY" not in decoded and len(decoded) <= 65536,
                "only a bounded public certificate bundle may be bound")
        require(not self._initial["spec"].get("patches"), "CA transition requires an initially unpatched API stage")
        hooks = [o for o in self._rendered if o.get("apiVersion") == "admissionregistration.k8s.io/v1" and
                 o.get("kind") in ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration")]
        require(bool(hooks), "CA transition requires source-rendered webhook configurations")
        patches = []
        for obj in hooks:
            require(obj.get("webhooks") and all(isinstance(h.get("clientConfig", {}).get("service"), dict) and
                    "url" not in h["clientConfig"] and not h["clientConfig"].get("caBundle") for h in obj["webhooks"]),
                    "CA transition requires initially unbound source Service webhooks")
            steps = [{"op": "add", "path": f"/webhooks/{i}/clientConfig/caBundle", "value": bundle}
                     for i in range(len(obj["webhooks"]))]
            patches.append({"target": {"kind": obj["kind"], "name": obj["metadata"]["name"]},
                            "patch": yaml.safe_dump(steps, sort_keys=False)})
        result = copy.deepcopy(self._initial["spec"])
        result["patches"] = patches
        return result

    def _validate(self, obj: dict, run_id: str) -> str:
        require(self.intent and self._initial is not None and self._run_id == run_id,
                "missing exact Flux create intent")
        meta = obj.get("metadata", {})
        expected = self._initial["metadata"]
        require(obj.get("apiVersion") == self._initial["apiVersion"] and obj.get("kind") == "Kustomization" and
                set(obj) <= {"apiVersion", "kind", "metadata", "spec", "status"} and
                meta.get("name") == self.name and meta.get("namespace") == self.namespace and
                isinstance(meta.get("uid"), str) and bool(meta["uid"]) and bool(meta.get("resourceVersion")) and
                (self.uid is None or meta["uid"] == self.uid) and not meta.get("ownerReferences") and
                set(meta) <= {"name", "namespace", "uid", "resourceVersion", "generation", "creationTimestamp",
                              "managedFields", "labels", "annotations", "finalizers", "ownerReferences",
                              "deletionTimestamp", "deletionGracePeriodSeconds"} and
                (not meta.get("deletionTimestamp") or self._delete_intent and self.uid == meta["uid"]) and
                meta.get("labels", {}) == expected.get("labels", {}) and
                meta.get("finalizers", []) in ([], ["finalizers.fluxcd.io"]),
                "Flux identity/lifecycle metadata differs from exact intent")
        annotations = copy.deepcopy(meta.get("annotations", {}))
        marker = "reconcile.fluxcd.io/requestedAt"
        if marker in annotations and marker not in expected.get("annotations", {}):
            value = annotations.pop(marker)
            require(isinstance(value, str) and len(value) <= 128, "Flux reconcile annotation invalid")
        require(annotations == expected.get("annotations", {}), "Flux annotations differ from exact intent")
        allowed_specs = [] if self._ca_committed else [self._initial["spec"]]
        if self._ca is not None:
            allowed_specs.append(self._ca_spec(self._ca))
        require(_flux_spec(obj.get("spec")) in allowed_specs, "Flux source/path/target/spec drift blocks prune")
        status = obj.get("status", {})
        require(isinstance(status, dict), "Flux status unavailable")
        for field in ("lastAppliedRevision", "lastAttemptedRevision"):
            require(field not in status or status[field] == self.revision,
                    "Flux observed a different source artifact revision")
        inventory = status.get("inventory", {"entries": []})
        require(isinstance(inventory, dict) and set(inventory) == {"entries"} and
                isinstance(inventory["entries"], list) and len(inventory["entries"]) <= len(self.allowed),
                "Flux inventory is malformed or exceeds source-owned render")
        found = set()
        for entry in inventory["entries"]:
            require(isinstance(entry, dict) and set(entry) == {"id", "v"} and
                    isinstance(entry["id"], str) and isinstance(entry["v"], str), "Flux inventory entry malformed")
            identity = (entry["id"], entry["v"])
            require(identity in self.allowed and identity not in found,
                    "foreign, aliased or duplicate Flux inventory blocks prune")
            found.add(identity)
        return meta["uid"]

    def bind_webhook_ca(self, private: Path, ca: bytes) -> dict:
        """Only supported spec transition; lost replies accept exact before/after.

        Caller must verify the serving certificate chain/SAN before this method.
        No arbitrary patches, references or inventory may enter via this API.
        """
        require(self.intent and not self._delete_intent, "active Flux stage required for CA binding")
        bundle = base64.b64encode(ca).decode()
        target = self._ca_spec(bundle)
        require(self._ca in (None, bundle), "different CA transition already recorded")
        current = self.kube.optional(self.kind, self.name, self.namespace)
        require(current is not None, "Flux CA stage is absent")
        self.uid = self._validate(current, self._run_id)
        self._ca = bundle
        self._record()  # exact before/after plus recovered UID precedes PATCH
        if _flux_spec(current["spec"]) != target:
            current = self.kube.patch_json(private, self.kind, self.name, [
                {"op": "test", "path": "/metadata/uid", "value": self.uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec", "value": current["spec"]},
                {"op": "add", "path": "/spec/patches", "value": target["patches"]}], self.namespace)
        self._validate(current, self._run_id)
        require(_flux_spec(current["spec"]) == target, "Flux CA response did not contain exact target")
        self._ca_committed = True
        self._record()
        return current

    def restore_webhook_ca(self, private: Path) -> dict:
        """Remove only the public-CA patch this exact stage added.

        A lost PATCH reply leaves a journal that accepts precisely the old or
        target spec on retry; no other patch can be adopted as cleanup.
        """
        require(self.intent and not self._delete_intent and self._ca is not None,
                "recorded active Flux CA transition required for restore")
        current = self.kube.optional(self.kind, self.name, self.namespace)
        require(current is not None, "Flux CA stage disappeared before restore")
        self.uid = self._validate(current, self._run_id)
        self._ca_committed = False
        self._record()  # exact before/after accepted before CAS mutation
        target = self._initial["spec"]
        if _flux_spec(current["spec"]) != target:
            current = self.kube.patch_json(private, self.kind, self.name, [
                {"op": "test", "path": "/metadata/uid", "value": self.uid},
                {"op": "test", "path": "/metadata/resourceVersion",
                 "value": current["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec", "value": current["spec"]},
                {"op": "remove", "path": "/spec/patches"}], self.namespace)
        self._validate(current, self._run_id)
        require(_flux_spec(current["spec"]) == target,
                "Flux CA restore response did not contain exact original spec")
        self._ca = None
        self._record()
        return current

    def delete(self, proxy: APIProxy, run_id: str, timeout: float = 120) -> None:
        if not self.intent:
            return
        obj = self.kube.optional(self.kind, self.name, self.namespace)
        if obj is None:
            return
        self.uid = self._validate(obj, run_id)
        self._delete_intent = True
        self._record()  # persists recovered UID and delete intent before DELETE
        meta = obj["metadata"]
        proxy.delete(self.path, self.uid, meta["resourceVersion"])
        def absent():
            current = self.kube.optional(self.kind, self.name, self.namespace)
            if current is None:
                return True
            self._validate(current, run_id)
            return False
        wait_for(absent, f"validated Flux Kustomization/{self.name} absent", timeout)


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
