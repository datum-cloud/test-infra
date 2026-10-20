#!/usr/bin/env python3
"""Bounded, reversible Catalog operator prerequisite against an isolated Milo.

The operator receives a newly generated token bound to Catalog's upstream
ClusterRole. The existing admin kubeconfig is never copied into its Secret.
"""

import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import secrets
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


def expect_uid(namespace, kind, name, uid):
    actual = json_get(host, "-n", namespace, "get", kind, name)["metadata"]["uid"]
    if actual != uid:
        raise RuntimeError(f"refusing to change replaced {kind}/{name}")


def delete_namespace_if_owned(uid):
    result = host("get", "namespace", "services-system", "-o", "json", "--ignore-not-found")
    if not result.strip():
        return
    if json.loads(result)["metadata"]["uid"] != uid:
        raise RuntimeError("refusing to delete replaced namespace/services-system")
    host("delete", "namespace", "services-system", "--wait=true", "--timeout=90s", timeout=110)


def write_private(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def patch_secret_data(uid, encoded, patch_path):
    write_private(
        patch_path,
        json.dumps([
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "replace", "path": "/data/tokens.csv", "value": encoded},
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


def webhook_ca(getter, name):
    return [w.get("clientConfig", {}).get("caBundle", "")
            for w in json_get(getter, "get", name)["webhooks"]]


def restore_webhook_ca(name, values, patch_path):
    patch = [{"op": "replace", "path": f"/webhooks/{i}/clientConfig/caBundle", "value": value}
             for i, value in enumerate(values)]
    write_private(patch_path, json.dumps(patch))
    root("patch", name, "--type=json", "--patch-file", str(patch_path), sensitive=True)


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
        result = subprocess.run(
            ["kubectl", "--kubeconfig", KUBECONFIG, "--context", CONTEXT,
             "--request-timeout=10s", "-n", namespace, "get", kind, name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
            check=False,
        )
        if result.returncode == 0:
            raise RuntimeError(f"refusing collision with existing {kind}/{name}")

    with tempfile.TemporaryDirectory(prefix="apps-a1t-catalog-", dir="/tmp") as tmp:
        private = Path(tmp)
        token = secrets.token_urlsafe(32)
        new_csv = old_csv.rstrip("\n") + f'\n{token},{OPERATOR_USER},{OPERATOR_USER},"system:authenticated"\n'
        new_b64 = base64.b64encode(new_csv.encode()).decode()
        patch_file = private / "auth-patch.json"
        probe_config = private / "operator-host-kubeconfig"
        saved_ca = {}
        suspended = False
        auth_changed = False
        operator_created = False
        role_created = False
        namespace_created = False
        operator_uid = ""
        role_uid = ""
        namespace_uid = ""
        try:
            expect_uid("flux-system", "kustomization", "milo-core", root_uid)
            flux("suspend", "kustomization", "milo-core", "-n", "flux-system")
            suspended = True
            patch_secret_data(auth_uid, new_b64, patch_file)
            auth_changed = True
            host("-n", "milo-system", "rollout", "restart", "deployment/milo-apiserver")
            host("-n", "milo-system", "rollout", "status", "deployment/milo-apiserver", "--timeout=120s", timeout=140)
            print("Dedicated Catalog token loaded by Milo; root Flux reconciliation paused", flush=True)

            host("apply", "-f", str(ROOT / "components/service-catalog/operator-rbac.yaml"))
            role_created = True
            role_uid = json_get(host, "-n", "milo-system", "get", "kustomization", "service-catalog-operator-rbac")["metadata"]["uid"]
            flux("reconcile", "kustomization", "service-catalog-operator-rbac", "-n", "milo-system", "--timeout=120s")

            ca = json_get(host, "-n", "milo-system", "get", "secret", "milo-apiserver-tls")["data"]["ca.crt"]
            host("create", "namespace", "services-system")
            namespace_created = True
            namespace_uid = json_get(host, "get", "namespace", "services-system")["metadata"]["uid"]
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
            host("apply", "-f", "-", input_text=json.dumps(operator_secret), sensitive=True)

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
                saved_ca[name] = webhook_ca(root, name)

            # Use the authoritative NSO schema, not Catalog's permissive E2E stand-in.
            with urllib.request.urlopen(NSO_CRD_URL, timeout=30) as response:
                nso_crd = response.read()
            if hashlib.sha256(nso_crd).hexdigest() != NSO_CRD_SHA256:
                raise RuntimeError("pinned NSO LocationBinding CRD content changed")
            root("apply", "-f", "-", input_text=nso_crd.decode())

            host("apply", "-f", str(ROOT / "components/service-catalog/operator.yaml"))
            operator_created = True
            operator_uid = json_get(host, "-n", "flux-system", "get", "kustomization", "service-catalog-operator")["metadata"]["uid"]
            flux("reconcile", "kustomization", "service-catalog-operator", "-n", "flux-system", "--timeout=240s", timeout=270)
            cert = json_get(host, "-n", "services-system", "get", "secret", "services-webhook-cert")
            cert_ca = cert["data"]["ca.crt"]
            for name, original in saved_ca.items():
                restore_webhook_ca(name, [cert_ca] * len(original), private / "webhook-patch.json")

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
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            if operator_created:
                cleanup("operator Flux object", lambda: (expect_uid("flux-system", "kustomization", "service-catalog-operator", operator_uid), host("-n", "flux-system", "delete", "kustomization", "service-catalog-operator", "--wait=true", "--timeout=90s", timeout=110)))
            for name, values in saved_ca.items():
                cleanup(f"{name} CA", lambda n=name, v=values: restore_webhook_ca(n, v, private / "webhook-patch.json"))
            if role_created:
                cleanup("operator RBAC Flux object", lambda: (expect_uid("milo-system", "kustomization", "service-catalog-operator-rbac", role_uid), host("-n", "milo-system", "delete", "kustomization", "service-catalog-operator-rbac", "--wait=true", "--timeout=90s", timeout=110)))
            if namespace_created:
                cleanup("operator namespace", lambda: delete_namespace_if_owned(namespace_uid))
            if auth_changed:
                cleanup("Milo auth Secret", lambda: patch_secret_data(auth_uid, old_b64, patch_file))
                cleanup("Milo API token reload", lambda: (host("-n", "milo-system", "rollout", "restart", "deployment/milo-apiserver"), host("-n", "milo-system", "rollout", "status", "deployment/milo-apiserver", "--timeout=120s", timeout=140)))
            if suspended:
                cleanup("Milo Flux reconciliation", lambda: (expect_uid("flux-system", "kustomization", "milo-core", root_uid), flux("resume", "kustomization", "milo-core", "-n", "flux-system"), flux("reconcile", "kustomization", "milo-core", "-n", "flux-system", "--timeout=180s", timeout=210)))
            if errors:
                raise RuntimeError("cleanup incomplete: " + "; ".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Catalog operator prerequisite failed: {error}", file=sys.stderr)
        sys.exit(1)
