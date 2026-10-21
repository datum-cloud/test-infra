"""Read-only identity checks for task-owned host and Milo API fixtures.

No credential value is returned or written. The private Project bootstrap journal
binds subsequent fixture cleanup to the original API and Project identities.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re

import yaml

from apps_native_support import Kube, RUN_LABEL, private_file, require, wait_for


ORG = "apps-a1t-ci-org"
PROJECTS = ("apps-a1t-provider", "apps-a1t-consumer-a", "apps-a1t-consumer-b")


def _selected_cluster(config: dict, context: str, *, host: bool, port: int) -> dict:
    require(config.get("current-context") == context and
            len(config.get("contexts", [])) == 1 and
            len(config.get("clusters", [])) == 1 and
            len(config.get("users", [])) == 1,
            "private kubeconfig must have one selected context, cluster and user")
    selected = config["contexts"][0]
    cluster = config["clusters"][0]
    user = config["users"][0]
    require(selected.get("name") == context and
            selected.get("context", {}).get("cluster") == cluster.get("name") and
            selected.get("context", {}).get("user") == user.get("name"),
            "private kubeconfig selected context differs from its cluster/user")
    settings = cluster.get("cluster", {})
    server = settings.get("server", "")
    match = re.fullmatch(r"https://127\.0\.0\.1:([0-9]{1,5})", server)
    require(match is not None and 1 <= int(match.group(1)) <= 65535 and
            (host or int(match.group(1)) == port) and
            not settings.get("insecure-skip-tls-verify", False) and
            not settings.get("proxy-url") and
            bool(settings.get("certificate-authority-data")),
            "private kubeconfig must target exact CA-verified loopback API")
    try:
        ca = base64.b64decode(settings["certificate-authority-data"], validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("private kubeconfig CA is malformed") from error
    require(b"BEGIN CERTIFICATE" in ca, "private kubeconfig CA is not PEM")
    return {"server": server, "ca_sha256": hashlib.sha256(ca).hexdigest()}


def scope_identity(host: Kube, milo: Kube, host_context: str, port: int,
                   *, projects_removed: bool = False) -> dict:
    require(re.fullmatch(r"kind-apps-a1t-ci-[a-z0-9-]+", host_context) is not None,
            "explicit task-owned Kind context required")
    host_cluster = _selected_cluster(host.json("config", "view", "--raw"),
                                     host_context, host=True, port=port)
    milo_cluster = _selected_cluster(milo.json("config", "view", "--raw"),
                                     "milo-root", host=False, port=port)
    require(host.k("get", "--raw", "/readyz") == "ok" and
            milo.k("get", "--raw", "/readyz") == "ok",
            "host or Milo API is not ready")
    host_cluster["kube_system_uid"] = host.json("get", "namespace", "kube-system")["metadata"]["uid"]
    journal_path = milo.config.parent / "apps-a1t-project-bootstrap-evidence.json"
    private_file(journal_path)
    bootstrap = json.loads(journal_path.read_text())
    uids = bootstrap.get("uids", {})
    require(bootstrap.get("host_context") == host_context and
            bootstrap.get("organization") == ORG and
            bootstrap.get("projects") == list(PROJECTS) and
            bootstrap.get("cleanup") == ("complete" if projects_removed else "not-requested") and
            set(uids) == {ORG, *PROJECTS} and
            all(isinstance(uid, str) and bool(uid) for uid in uids.values()),
            "Project bootstrap journal is not the retained owned fixture")
    run_id = bootstrap.get("run_id")
    require(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{24}", run_id) is not None,
            "Project bootstrap run identity is malformed")
    for name in (ORG, *PROJECTS):
        kind = ("organizations" if name == ORG else "projects") + ".resourcemanager.miloapis.com"
        if projects_removed:
            require(milo.optional(kind, name) is None,
                    f"Project bootstrap cleanup is incomplete: {name}")
            continue
        obj = milo.json("get", kind, name)
        metadata = obj.get("metadata", {})
        require(metadata.get("uid") == uids[name] and
                metadata.get("labels", {}).get(RUN_LABEL) == run_id and
                not metadata.get("deletionTimestamp"),
                f"retained Project bootstrap identity changed: {name}")
        if name != ORG:
            require(obj.get("spec", {}).get("ownerRef") == {
                "kind": "Organization", "name": ORG},
                f"retained Project ownership changed: {name}")
    return {"host": host_cluster, "milo": milo_cluster,
            "bootstrap_run_id": run_id, "bootstrap_uids": uids}


def require_same_identity(expected: dict, actual: dict) -> None:
    require(expected == actual,
            "live host/Milo API or Project bootstrap identity differs from fixture journal")


def catalog_installer_intent_digest(obj: dict) -> str:
    """Hash the complete credential/RBAC create intent without exposing contents."""
    meta = obj.get("metadata", {})
    require(not meta.get("ownerReferences") and not meta.get("finalizers") and
            not meta.get("deletionTimestamp"),
            "Catalog installer object has unexpected lifecycle metadata")
    projection = {"apiVersion": obj.get("apiVersion"), "kind": obj.get("kind"),
                  "name": meta.get("name"), "namespace": meta.get("namespace"),
                  "labels": meta.get("labels", {}),
                  "annotations": meta.get("annotations", {}),
                  "rules": obj.get("rules"), "roleRef": obj.get("roleRef"),
                  "subjects": obj.get("subjects"), "type": obj.get("type"),
                  "data": obj.get("data")}
    return hashlib.sha256(json.dumps(projection, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def rollout_static_auth(host: Kube) -> dict:
    """Restart Milo and prove Ready pods belong to the new Deployment revision."""
    before = host.json("-n", "milo-system", "get", "deployment", "milo-apiserver")
    before_meta = before["metadata"]
    selector = before.get("spec", {}).get("selector", {})
    labels = selector.get("matchLabels", {})
    require(isinstance(labels, dict) and bool(labels) and
            not selector.get("matchExpressions"),
            "Milo apiserver selector differs from reviewed Deployment shape")
    label_arg = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    before_restart = before["spec"]["template"].get("metadata", {}).get(
        "annotations", {}).get("kubectl.kubernetes.io/restartedAt")
    host.k("-n", "milo-system", "rollout", "restart", "deployment/milo-apiserver")
    host.k("-n", "milo-system", "rollout", "status", "deployment/milo-apiserver",
           "--timeout=120s", timeout=140)

    def observed() -> dict | bool:
        deployment = host.json("-n", "milo-system", "get", "deployment",
                               "milo-apiserver")
        meta = deployment["metadata"]
        status = deployment.get("status", {})
        expected = deployment["spec"].get("replicas", 1)
        require(type(expected) is int and expected > 0 and
                expected == before["spec"].get("replicas", 1),
                "Milo apiserver replica count changed or is empty")
        restarted = deployment["spec"]["template"].get("metadata", {}).get(
            "annotations", {}).get("kubectl.kubernetes.io/restartedAt")
        require(meta.get("uid") == before_meta.get("uid") and
                meta.get("generation", 0) > before_meta.get("generation", 0) and
                deployment["spec"]["selector"] == selector and
                deployment["spec"]["template"]["spec"] ==
                    before["spec"]["template"]["spec"] and
                restarted and restarted != before_restart,
                "Milo apiserver restart did not advance the owned Deployment")
        if not (status.get("observedGeneration", 0) >= meta["generation"] and
                status.get("readyReplicas", 0) == expected and
                status.get("updatedReplicas", 0) == expected and
                status.get("availableReplicas", 0) == expected):
            return False
        replicasets = host.json("-n", "milo-system", "get", "replicasets", "-l",
                                label_arg).get("items", [])
        current_rs = {rs["metadata"]["uid"] for rs in replicasets
                      if any(ref.get("kind") == "Deployment" and
                             ref.get("uid") == meta["uid"] and
                             ref.get("controller") is True
                             for ref in rs["metadata"].get("ownerReferences", [])) and
                      rs.get("spec", {}).get("template", {}).get("metadata", {}).get(
                          "annotations", {}).get("kubectl.kubernetes.io/restartedAt") ==
                      restarted}
        pods = host.json("-n", "milo-system", "get", "pods", "-l",
                         label_arg).get("items", [])
        active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        # Terminating old pods may still authenticate the removed static token.
        # Wait for their actual disappearance before declaring revocation.
        if len(pods) != expected or len(active) != expected or not current_rs:
            return False
        if not all(any(ref.get("kind") == "ReplicaSet" and
                       ref.get("uid") in current_rs and ref.get("controller") is True
                       for ref in pod["metadata"].get("ownerReferences", [])) and
                   pod.get("status", {}).get("phase") == "Running" and
                   any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in pod.get("status", {}).get("conditions", []))
                   for pod in active):
            return False
        return {"deployment_uid": meta["uid"], "generation": meta["generation"],
                "pod_uids": sorted(pod["metadata"]["uid"] for pod in active)}

    return wait_for(observed, "Milo apiserver new static-auth pods Ready", 120)


def verified_flux_remote_secret(host: Kube, identity: dict,
                                secret: dict | None = None,
                                expected_token: str | None = None) -> None:
    if secret is None:
        secret = host.json("-n", "milo-system", "get", "secret",
                           "apps-a1t-catalog-installer-kubeconfig")
    require(secret.get("type") in (None, "Opaque") and
            set(secret.get("data", {})) == {"kubeconfig"},
            "Flux remote-apply Secret has unexpected data")
    try:
        raw = base64.b64decode(secret["data"]["kubeconfig"], validate=True)
        config = yaml.safe_load(raw)
    except (KeyError, ValueError, TypeError, yaml.YAMLError) as error:
        raise RuntimeError("Flux remote-apply kubeconfig is malformed") from error
    require(isinstance(config, dict) and
            len(config.get("clusters", [])) == 1 and
            len(config.get("contexts", [])) == 1 and
            len(config.get("users", [])) == 1,
            "Flux remote-apply kubeconfig must have one cluster/context/user")
    cluster = config["clusters"][0]
    context = config["contexts"][0]["context"]
    settings = cluster["cluster"]
    user = config["users"][0]
    require(context.get("cluster") == cluster.get("name") and
            context.get("user") == user.get("name") and
            config.get("current-context") == config["contexts"][0]["name"] and
            user.get("name") == "apps-a1t-catalog-installer" and
            set(user.get("user", {})) == {"token"} and
            isinstance(user["user"]["token"], str) and
            bool(user["user"]["token"]) and
            (expected_token is None or user["user"]["token"] == expected_token) and
            settings.get("server") ==
                "https://milo-apiserver.milo-system.svc.cluster.local:6443" and
            not settings.get("insecure-skip-tls-verify", False) and
            not settings.get("proxy-url") and
            bool(settings.get("certificate-authority-data")),
            "Flux remote-apply kubeconfig does not target verified Milo service")
    try:
        ca = base64.b64decode(settings["certificate-authority-data"], validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("Flux remote-apply CA is malformed") from error
    require(hashlib.sha256(ca).hexdigest() == identity["milo"]["ca_sha256"],
            "Flux remote-apply CA differs from verified Milo root API")


def active_catalog_installer(host: Kube, milo: Kube, identity: dict,
                             path: Path, *, auth_allowed: set[str] | None = None
                             ) -> tuple[dict, dict]:
    require(not path.is_symlink(), "Catalog installer evidence symlink refused")
    private_file(path)
    evidence = json.loads(path.read_text())
    state_path = path.parent / "apps-a1t-catalog-installer-private/state.json"
    private_file(state_path)
    state = json.loads(state_path.read_text())
    run_id = evidence.get("run_id")
    require(evidence.get("complete") is True and
            evidence.get("cleanup") == "not-requested" and
            evidence.get("milo_source") ==
                "200de48a305732720377cb83308d57c84fd5f688" and
            run_id == state.get("run_id") and
            isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{24}", run_id) is not None and
            evidence.get("scope_identity") == identity and
            set(evidence.get("attempted", {})) == {"role", "binding", "secret"} and
            set(evidence.get("intents", {})) == {"role", "binding", "secret"} and
            all(re.fullmatch(r"[0-9a-f]{64}", value or "") is not None
                for value in evidence["intents"].values()),
            "Catalog scoped installer is not active for this host/Milo/Project identity")
    observed: dict[str, dict] = {}
    for kind, name, namespace, key in (
        ("clusterrole", "apps-a1t-catalog-installer", None, "role"),
        ("clusterrolebinding", "apps-a1t-catalog-installer", None, "binding"),
        ("secret", "apps-a1t-catalog-installer-kubeconfig", "milo-system", "secret"),
    ):
        cluster = host if key == "secret" else milo
        obj = cluster.optional(kind, name, namespace)
        require(obj is not None and
                obj["metadata"].get("uid") == evidence["attempted"][key] and
                obj["metadata"].get("labels", {}).get(RUN_LABEL) == run_id and
                catalog_installer_intent_digest(obj) == evidence["intents"][key],
                f"Catalog scoped installer {key} changed")
        observed[key] = obj
    auth = host.json("-n", "milo-system", "get", "secret", "milo-apiserver-auth-tokens")
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    require(auth["metadata"]["uid"] == state["auth_uid"] and
            auth["data"]["tokens.csv"] in (
                auth_allowed if auth_allowed is not None else {state["new_b64"]}) and
            core["metadata"]["uid"] == state["core_uid"] and
            core["metadata"]["generation"] == state["suspended_generation"] and
            core["spec"].get("suspend") is True,
            "Catalog installer static auth or Milo Flux suspension changed")
    try:
        rows = list(csv.reader(io.StringIO(base64.b64decode(
            state["new_b64"], validate=True).decode())))
    except (ValueError, UnicodeError, TypeError) as error:
        raise RuntimeError("Catalog installer private auth state malformed") from error
    installer_rows = [row for row in rows if len(row) == 4 and
                      row[1:] == ["apps-a1t-catalog-installer",
                                  "apps-a1t-catalog-installer", "system:authenticated"]]
    require(len(installer_rows) == 1 and bool(installer_rows[0][0]),
            "Catalog installer credential absent from private auth state")
    verified_flux_remote_secret(host, identity, observed["secret"], installer_rows[0][0])
    return evidence, state
