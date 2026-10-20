#!/usr/bin/env python3
"""Bounded, reversible Catalog operator prerequisite against an isolated Milo.

The operator receives a newly generated token bound to Catalog's upstream
ClusterRole. The existing admin kubeconfig is never copied into its Secret.
"""

import base64
import csv
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
CONTEXT = os.environ.get("CATALOG_HOST_CONTEXT", "")
KUBECONFIG = os.environ.get("KUBECONFIG", "")
MILO_KUBECONFIG = os.environ.get("CATALOG_MILO_KUBECONFIG", "")
HTTPS_PORT = os.environ.get("CATALOG_MILO_HTTPS_PORT", "")
OPERATOR_USER = "apps-a1t-services-operator"
NSO_SHA = "4c4c48ecb5e7f0f4d1950e6441ebe0c02afcfb96"
NSO_CRD_SHA256 = "73fc1945ec88f651ee6ace8518a4593792c005af0d52dbc8da3e22d1f173f875"
NSO_CRD_URL = (
    "https://raw.githubusercontent.com/datum-cloud/network-services-operator/"
    f"{NSO_SHA}/config/crd/bases/networking.datumapis.com_locationbindings.yaml"
)


def command(*args, input_text=None, sensitive=False, timeout=30, env=None):
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        check=False,
    )
    if result.returncode:
        # Secret-bearing commands never echo the request body or server output.
        detail = " (details withheld)" if sensitive else f": {result.stderr.strip()}"
        raise RuntimeError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed{detail}")
    return result.stdout


def host(*args, **kwargs):
    return command(
        "kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT,
        "--request-timeout=10s", *args, **kwargs
    )


def root(*args, **kwargs):
    return command(
        "kubectl", "--kubeconfig", MILO_KUBECONFIG, "--context", "milo-root",
        "--request-timeout=10s", *args, **kwargs
    )


def flux(*args, timeout=300):
    return command("flux", *args, timeout=timeout, env={**os.environ, "KUBECONFIG": KUBECONFIG})


def json_get(getter, *args):
    return json.loads(getter(*args, "-o", "json"))


def get_optional(namespace, kind, name):
    # --ignore-not-found still fails on transport, authentication, and RBAC errors.
    result = host("-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(result) if result.strip() else None


def root_get_optional(kind, name):
    # A denied or unavailable Milo API must fail preflight, not look absent.
    result = root("get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(result) if result.strip() else None


def mark_created(manifest, run_id, state, sensitive=False):
    manifest.setdefault("metadata", {}).setdefault("labels", {})["apps-a1t.datum.net/run-id"] = run_id
    state["intent"] = True
    response = json.loads(host("create", "-f", "-", "-o", "json",
                               input_text=json.dumps(manifest), sensitive=sensitive))
    state["uid"] = response["metadata"]["uid"]


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


class ApiProxy:
    def __init__(self, private):
        self.socket_path = str(private / "host-api.sock")
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT,
             "proxy", f"--unix-socket={self.socket_path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if Path(self.socket_path).is_socket():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("private API proxy did not start")

    def delete(self, path, uid, resource_version):
        body = json.dumps({"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
            "uid": uid, "resourceVersion": resource_version}})
        connection = UnixHTTPConnection(self.socket_path)
        try:
            connection.request("DELETE", path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            if response.status not in (200, 202):
                raise RuntimeError(f"UID/resourceVersion-preconditioned DELETE returned HTTP {response.status}")
        finally:
            connection.close()

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


def delete_owned(proxy, run_id, state):
    if not state["intent"]:
        return
    obj = get_optional(state["namespace"], state["kind"], state["name"])
    if obj is None:
        return
    metadata = obj["metadata"]
    if state["uid"]:
        # Flux may reconcile away our run label, but a returned create UID is
        # stronger ownership evidence. Ambiguous creates have no such UID.
        if metadata["uid"] != state["uid"]:
            raise RuntimeError(f"refusing to delete replaced {state['kind']}/{state['name']}")
    elif metadata.get("labels", {}).get("apps-a1t.datum.net/run-id") != run_id:
        raise RuntimeError(f"ownership unknown for {state['kind']}/{state['name']}")
    proxy.delete(state["path"], metadata["uid"], metadata["resourceVersion"])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = get_optional(state["namespace"], state["kind"], state["name"])
        if current is None or current["metadata"]["uid"] != metadata["uid"]:
            return
        time.sleep(1)
    raise RuntimeError(f"timed out deleting owned {state['kind']}/{state['name']}")


def write_private(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def patch_secret_data(uid, expected, replacement, patch_path):
    write_private(
        patch_path,
        json.dumps([
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/data/tokens.csv", "value": expected},
            {"op": "replace", "path": "/data/tokens.csv", "value": replacement},
        ]),
    )
    host(
        "-n", "milo-system", "patch", "secret", "milo-apiserver-auth-tokens",
        "--type=json", "--patch-file", str(patch_path), sensitive=True
    )


def check_can_i(probe_config, verb, resource, expected):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(probe_config), "--request-timeout=10s",
         "auth", "can-i", verb, resource],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        check=False,
    )
    answer = result.stdout.strip().splitlines()[-1:]
    if answer not in (["yes"], ["no"]):
        raise RuntimeError(f"Catalog operator RBAC review was inconclusive for {verb} {resource}")
    actual = answer == ["yes"]
    if actual != expected:
        raise RuntimeError(f"Catalog operator RBAC {verb} {resource}: expected {expected}, got {actual}")
    print(f"RBAC {'allow' if expected else 'deny'}: {verb} {resource}", flush=True)


def patch_json(getter, name, operations, patch_path, sensitive=False):
    write_private(patch_path, json.dumps(operations))
    return json.loads(getter("patch", name, "--type=json", "--patch-file", str(patch_path),
                             "-o", "json", sensitive=sensitive))


def set_root_suspend(uid, expected_generation, value, patch_path, restore_remove=False):
    current = json_get(host, "-n", "flux-system", "get", "kustomization", "milo-core")
    metadata = current["metadata"]
    if metadata["uid"] != uid or metadata["generation"] != expected_generation:
        raise RuntimeError("milo-core changed during scoped trial")
    if current["spec"].get("suspend", False) == value:
        raise RuntimeError("milo-core suspend state changed unexpectedly")
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/metadata/generation", "value": expected_generation},
    ]
    if value:
        operations.append({"op": "add", "path": "/spec/suspend", "value": True})
    else:
        operations.extend([
            {"op": "test", "path": "/spec/suspend", "value": True},
            {"op": "remove" if restore_remove else "replace", "path": "/spec/suspend",
             **({} if restore_remove else {"value": False})},
        ])
    write_private(patch_path, json.dumps(operations))
    return json.loads(host("-n", "flux-system", "patch", "kustomization", "milo-core",
                           "--type=json", "--patch-file", str(patch_path), "-o", "json"))


def webhook_snapshot(name):
    obj = json_get(root, "get", name)
    return {
        "uid": obj["metadata"]["uid"],
        "rv": obj["metadata"]["resourceVersion"],
        "generation": obj["metadata"]["generation"],
        "names": [w["name"] for w in obj["webhooks"]],
        "ca": [w["clientConfig"].get("caBundle") for w in obj["webhooks"]],
        "desired": None,
        "applied_generation": None,
        "applied_rv": None,
    }


def set_webhook_ca(name, snapshot, desired, patch_path):
    current = json_get(root, "get", name)
    metadata = current["metadata"]
    names = [w["name"] for w in current["webhooks"]]
    current_ca = [w["clientConfig"].get("caBundle") for w in current["webhooks"]]
    if metadata["uid"] != snapshot["uid"] or names != snapshot["names"]:
        raise RuntimeError(f"refusing to change replaced {name}")
    if desired == snapshot["ca"]:
        if snapshot["desired"] is None or current_ca != snapshot["desired"]:
            raise RuntimeError(f"webhook CA changed outside scoped trial: {name}")
        if snapshot["applied_generation"] is None:
            raise RuntimeError(f"webhook CA injection outcome unknown: {name}")
        expected_generation = snapshot["applied_generation"]
        expected_rv = snapshot["applied_rv"]
    else:
        if (current_ca != snapshot["ca"] or metadata["generation"] != snapshot["generation"]
                or metadata["resourceVersion"] != snapshot["rv"]):
            raise RuntimeError(f"webhook config changed before CA injection: {name}")
        snapshot["desired"] = desired
        expected_generation = snapshot["generation"]
        expected_rv = snapshot["rv"]
    if metadata["generation"] != expected_generation or metadata["resourceVersion"] != expected_rv:
        raise RuntimeError(f"webhook config generation changed: {name}")
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": snapshot["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/metadata/generation", "value": expected_generation},
    ]
    for i, (webhook_name, old_ca, new_ca) in enumerate(zip(names, current_ca, desired)):
        operations.extend([
            {"op": "test", "path": f"/webhooks/{i}/name", "value": webhook_name},
            {"op": "test", "path": f"/webhooks/{i}/clientConfig",
             "value": current["webhooks"][i]["clientConfig"]},
        ])
        operations.append(
            {"op": "remove" if new_ca is None else ("add" if old_ca is None else "replace"),
             "path": f"/webhooks/{i}/clientConfig/caBundle",
             **({} if new_ca is None else {"value": new_ca})})
    result = patch_json(root, name, operations, patch_path, sensitive=True)
    if desired != snapshot["ca"]:
        snapshot["applied_generation"] = result["metadata"]["generation"]
        snapshot["applied_rv"] = result["metadata"]["resourceVersion"]


def restore_webhook_ca(name, snapshot, patch_path):
    if snapshot["desired"] is None:
        return
    current = json_get(root, "get", name)
    if current["metadata"]["uid"] != snapshot["uid"]:
        raise RuntimeError(f"webhook config was replaced: {name}")
    current_ca = [w["clientConfig"].get("caBundle") for w in current["webhooks"]]
    if current_ca == snapshot["ca"] and current["metadata"]["generation"] == snapshot["generation"]:
        return  # An uncertain injection request never applied.
    set_webhook_ca(name, snapshot, snapshot["ca"], patch_path)


def main():
    if not (KUBECONFIG.startswith("/tmp/") and Path(KUBECONFIG).is_file()):
        raise RuntimeError("an explicit private /tmp KUBECONFIG is required")
    if not (MILO_KUBECONFIG.startswith("/tmp/") and Path(MILO_KUBECONFIG).is_file()):
        raise RuntimeError("an explicit private /tmp CATALOG_MILO_KUBECONFIG is required")
    if not CONTEXT.startswith("kind-apps-a1t-"):
        raise RuntimeError("CATALOG_HOST_CONTEXT must name a dedicated apps-a1t kind cluster")
    if not HTTPS_PORT.isdecimal() or not 1 <= int(HTTPS_PORT) <= 65535:
        raise RuntimeError("CATALOG_MILO_HTTPS_PORT must be a valid port")
    if command("kubectl", "--kubeconfig", KUBECONFIG, "config", "current-context").strip() != CONTEXT:
        raise RuntimeError("private kubeconfig current context differs from CATALOG_HOST_CONTEXT")
    if command("kubectl", "--kubeconfig", MILO_KUBECONFIG, "config", "current-context").strip() != "milo-root":
        raise RuntimeError("private Milo kubeconfig current context must be milo-root")
    milo_config = json.loads(command("kubectl", "--kubeconfig", MILO_KUBECONFIG,
                                     "config", "view", "--minify", "-o", "json"))
    if milo_config["clusters"][0]["cluster"]["server"] != f"https://127.0.0.1:{HTTPS_PORT}":
        raise RuntimeError("private Milo kubeconfig server differs from CATALOG_MILO_HTTPS_PORT")

    root_ks = json_get(host, "-n", "flux-system", "get", "kustomization", "milo-core")
    root_uid = root_ks["metadata"]["uid"]
    if root_ks["spec"].get("suspend", False):
        raise RuntimeError("milo-core was already suspended")
    auth = json_get(host, "-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
    auth_uid = auth["metadata"]["uid"]
    old_b64 = auth["data"]["tokens.csv"]
    old_csv = base64.b64decode(old_b64).decode("utf-8")
    rows = list(csv.reader(old_csv.splitlines()))
    identities = {(row[1], row[2], row[3]) for row in rows if len(row) == 4}
    if len(rows) != 2 or len(identities) != 2 or any(not row[0] for row in rows) or identities != {
        ("admin", "admin", "system:masters"),
        ("test-user", "test-user", "system:authenticated"),
    }:
        raise RuntimeError("Milo auth Secret is not the expected two-user test baseline")
    for namespace, kind, name in [
        ("flux-system", "kustomization", "service-catalog-operator"),
        ("milo-system", "kustomization", "service-catalog-operator-rbac"),
        ("default", "namespace", "services-system"),
    ]:
        if get_optional(namespace, kind, name) is not None:
            raise RuntimeError(f"refusing collision with existing {kind}/{name}")
    # Flux's remote apply could otherwise adopt these existing cluster-scoped
    # objects, and pruning our short-lived Kustomization would then delete them.
    # The operator overlay's only host cluster-scoped object is the namespace
    # checked above; its remaining resources are namespaced there.
    for kind in ("clusterrole", "clusterrolebinding"):
        name = "services-controller-manager"
        if root_get_optional(kind, name) is not None:
            raise RuntimeError(f"refusing collision with existing Milo {kind}/{name}")

    with tempfile.TemporaryDirectory(prefix="apps-a1t-catalog-", dir="/tmp") as tmp:
        private = Path(tmp)
        run_id = secrets.token_hex(16)
        proxy = ApiProxy(private)
        token = secrets.token_urlsafe(32)
        new_csv = old_csv.rstrip("\n") + f'\n{token},{OPERATOR_USER},{OPERATOR_USER},"system:authenticated"\n'
        new_b64 = base64.b64encode(new_csv.encode()).decode()
        patch_file = private / "auth-patch.json"
        probe_config = private / "operator-host-kubeconfig"
        saved_ca = {}
        suspend_intent = False
        suspended_generation = None
        auth_intent = False
        objects = {
            "operator": {"namespace": "flux-system", "kind": "kustomization",
                         "name": "service-catalog-operator", "uid": None, "intent": False,
                         "path": "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/service-catalog-operator"},
            "role": {"namespace": "milo-system", "kind": "kustomization",
                     "name": "service-catalog-operator-rbac", "uid": None, "intent": False,
                     "path": "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/milo-system/kustomizations/service-catalog-operator-rbac"},
            "namespace": {"namespace": "default", "kind": "namespace", "name": "services-system",
                          "uid": None, "intent": False, "path": "/api/v1/namespaces/services-system"},
            "secret": {"namespace": "services-system", "kind": "secret", "name": "milo-kubeconfig",
                       "uid": None, "intent": False,
                       "path": "/api/v1/namespaces/services-system/secrets/milo-kubeconfig"},
        }
        try:
            proxy.start()
            suspend_intent = True
            suspended = set_root_suspend(root_uid, root_ks["metadata"]["generation"], True, patch_file)
            suspended_generation = suspended["metadata"]["generation"]
            auth_intent = True
            patch_secret_data(auth_uid, old_b64, new_b64, patch_file)
            host("-n", "milo-system", "rollout", "restart", "deployment/milo-apiserver")
            host("-n", "milo-system", "rollout", "status", "deployment/milo-apiserver", "--timeout=120s", timeout=140)
            print("Dedicated Catalog token loaded by Milo; root Flux reconciliation paused", flush=True)

            role_manifest = json.loads(host("create", "--dry-run=client", "-f",
                                           str(ROOT / "components/service-catalog/operator-rbac.yaml"),
                                           "-o", "json"))
            mark_created(role_manifest, run_id, objects["role"])
            flux("reconcile", "kustomization", "service-catalog-operator-rbac", "-n", "milo-system", "--timeout=120s")

            ca = json_get(host, "-n", "milo-system", "get", "secret", "milo-apiserver-tls")["data"]["ca.crt"]
            mark_created({"apiVersion": "v1", "kind": "Namespace",
                          "metadata": {"name": "services-system"}}, run_id, objects["namespace"])
            operator_kubeconfig = {
                "apiVersion": "v1", "kind": "Config", "current-context": "milo",
                "clusters": [{"name": "milo", "cluster": {
                    "server": "https://milo-apiserver.milo-system.svc.cluster.local:6443",
                    "certificate-authority-data": ca,
                }}],
                "users": [{"name": OPERATOR_USER, "user": {"token": token}}],
                "contexts": [{"name": "milo", "context": {"cluster": "milo", "user": OPERATOR_USER}}],
            }
            operator_secret = {
                "apiVersion": "v1", "kind": "Secret", "metadata": {
                    "name": "milo-kubeconfig", "namespace": "services-system"},
                "type": "Opaque", "data": {"kubeconfig": base64.b64encode(
                    json.dumps(operator_kubeconfig).encode()).decode()},
            }
            mark_created(operator_secret, run_id, objects["secret"], sensitive=True)

            probe = dict(operator_kubeconfig)
            probe["clusters"] = [{"name": "milo", "cluster": {
                "server": f"https://127.0.0.1:{HTTPS_PORT}",
                "insecure-skip-tls-verify": True}}]
            write_private(probe_config, json.dumps(probe))
            for verb, resource in [
                ("get", "secrets"),
                ("create", "clusterrolebindings.rbac.authorization.k8s.io"),
                ("impersonate", "users"),
            ]:
                check_can_i(probe_config, verb, resource, False)
            for verb, resource in [
                ("list", "projects.resourcemanager.miloapis.com"),
                ("list", "services.services.miloapis.com"),
                ("update", "services.services.miloapis.com/status"),
            ]:
                check_can_i(probe_config, verb, resource, True)

            for name in ["validatingwebhookconfiguration/services-validating",
                         "mutatingwebhookconfiguration/services-mutating"]:
                saved_ca[name] = webhook_snapshot(name)

            # Use the authoritative NSO schema, not Catalog's permissive E2E stand-in.
            with urllib.request.urlopen(NSO_CRD_URL, timeout=30) as response:
                nso_crd = response.read()
            if hashlib.sha256(nso_crd).hexdigest() != NSO_CRD_SHA256:
                raise RuntimeError("pinned NSO LocationBinding CRD content changed")
            root("apply", "-f", "-", input_text=nso_crd.decode())

            operator_manifest = json.loads(host("create", "--dry-run=client", "-f",
                                               str(ROOT / "components/service-catalog/operator.yaml"),
                                               "-o", "json"))
            mark_created(operator_manifest, run_id, objects["operator"])
            flux("reconcile", "kustomization", "service-catalog-operator", "-n", "flux-system", "--timeout=240s", timeout=270)
            cert = json_get(host, "-n", "services-system", "get", "secret", "services-webhook-cert")
            cert_ca = cert["data"]["ca.crt"]
            for name, snapshot in saved_ca.items():
                set_webhook_ca(name, snapshot, [cert_ca] * len(snapshot["ca"]), private / "webhook-patch.json")

            deadline = time.monotonic() + 120
            pending = {"apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b"}
            while pending and time.monotonic() < deadline:
                logs = host("-n", "services-system", "logs", "deployment/services-controller-manager", "--tail=1000")
                for project in tuple(pending):
                    if any("Successfully registered and engaged new cluster" in line and project in line
                           for line in logs.splitlines()):
                        print(f"Catalog operator engaged {project}", flush=True)
                        pending.remove(project)
                if pending:
                    time.sleep(2)
            if pending:
                raise RuntimeError(f"Catalog operator did not engage: {', '.join(sorted(pending))}")
            print("Catalog operator prerequisite passed with scoped token and real Project engagement", flush=True)
        finally:
            errors = []
            def cleanup(label, action):
                try:
                    action()
                    print(f"restored {label}", flush=True)
                    return True
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
                    return False
            cleanup("operator Flux object", lambda: delete_owned(proxy, run_id, objects["operator"]))
            for name, snapshot in saved_ca.items():
                cleanup(f"{name} CA", lambda n=name, s=snapshot: restore_webhook_ca(n, s, private / "webhook-patch.json"))
            cleanup("operator RBAC Flux object", lambda: delete_owned(proxy, run_id, objects["role"]))
            cleanup("operator Secret", lambda: delete_owned(proxy, run_id, objects["secret"]))
            cleanup("operator namespace", lambda: delete_owned(proxy, run_id, objects["namespace"]))

            auth_restored = True
            if auth_intent:
                def restore_auth():
                    current = json_get(host, "-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
                    if current["metadata"]["uid"] != auth_uid:
                        raise RuntimeError("Milo auth Secret was replaced")
                    data = current["data"]["tokens.csv"]
                    if data == old_b64:
                        return  # An uncertain patch request never applied.
                    if data != new_b64:
                        raise RuntimeError("Milo auth Secret changed outside scoped trial")
                    patch_secret_data(auth_uid, new_b64, old_b64, patch_file)
                auth_restored = cleanup("Milo auth Secret", restore_auth)
                if auth_restored:
                    cleanup("Milo API token reload", lambda: (host("-n", "milo-system", "rollout", "restart", "deployment/milo-apiserver"), host("-n", "milo-system", "rollout", "status", "deployment/milo-apiserver", "--timeout=120s", timeout=140)))

            if suspend_intent and auth_restored:
                def resume_root():
                    current = json_get(host, "-n", "flux-system", "get", "kustomization", "milo-core")
                    if current["metadata"]["uid"] != root_uid:
                        raise RuntimeError("milo-core was replaced")
                    if not current["spec"].get("suspend", False):
                        if current["metadata"]["generation"] != root_ks["metadata"]["generation"]:
                            raise RuntimeError("milo-core suspend outcome unknown")
                        return
                    if suspended_generation is None:
                        raise RuntimeError("milo-core suspend outcome unknown")
                    set_root_suspend(root_uid, suspended_generation, False, patch_file,
                                     restore_remove="suspend" not in root_ks["spec"])
                    flux("reconcile", "kustomization", "milo-core", "-n", "flux-system", "--timeout=180s", timeout=210)
                cleanup("Milo Flux reconciliation", resume_root)
            proxy.close()
            if errors:
                raise RuntimeError("cleanup incomplete: " + "; ".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Catalog operator prerequisite failed: {error}", file=sys.stderr)
        sys.exit(1)
