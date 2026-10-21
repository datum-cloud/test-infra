#!/usr/bin/env python3
"""Switch the owned Catalog API Flux stage to its exact published OCI source."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

from apps_native_support import (
    APIProxy, Commands, FluxOwned, Kube, RUN_LABEL, _flux_spec, private_file,
    ready, require, wait_for, write_flux_journal,
)
from apps_native_fixture_scope import (
    active_catalog_installer, require_same_identity, scope_identity,
)
from apps_native_catalog_oci import (
    CATALOG_SHA, OCI_DIGEST, ROOT, SOURCE_NAME, transition_contract,
)
from apps_native_catalog_prune import assert_no_catalog_dependents, root_inventory


GRAPH_KEY = "milo-system/service-catalog-api-crds"
GIT_KEY = "flux-system/service-catalog"
OCI_PATH = (
    "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/"
    f"ocirepositories/{SOURCE_NAME}"
)
SOURCE_CRDS = {
    "locationclasses.locations.miloapis.com", "locations.locations.miloapis.com",
    "servinglocations.locations.miloapis.com",
    "serviceactivationrequests.services.miloapis.com",
    "serviceagentconfigurations.services.miloapis.com",
    "serviceagents.services.miloapis.com",
    "serviceavailabilities.services.miloapis.com",
    "serviceconfigurations.services.miloapis.com",
    "serviceconsumers.services.miloapis.com",
    "serviceentitlements.services.miloapis.com",
    "services.services.miloapis.com",
}
LOCATIONS_SHA = "507ac2cbd48c0228f09baea64199882eca91e865"


def private_json(path: Path) -> dict:
    require(not path.is_symlink(), "private journal symlink refused")
    private_file(path)
    result = json.loads(path.read_text())
    require(isinstance(result, dict), "private journal must be an object")
    return result


def active_installer_with_locations(host: Kube, milo: Kube, identity: dict,
                                    installer_path: Path, locations_path: Path,
                                    *, cleanup: bool) -> tuple[dict, dict]:
    """Bind the later Locations auth extension to the original Catalog installer."""
    private = locations_path.parent / "apps-a1t-locations-private"
    require(locations_path.is_absolute() and installer_path.is_absolute() and
            locations_path.parent == installer_path.parent and
            not locations_path.parent.is_symlink() and
            locations_path.parent.stat().st_mode & 0o077 == 0,
            "Locations and installer journals must share the private fixture directory")
    locations = private_json(locations_path)
    require(locations.get("source") == LOCATIONS_SHA and
            locations.get("scope_identity") == identity and
            re.fullmatch(r"[0-9a-f]{24}", locations.get("run_id", "")) is not None and
            re.fullmatch(r"[0-9a-f]{24}", locations.get("installer_run_id", "")) is not None,
            "Locations auth journal does not bind the retained fixture")
    if cleanup:
        require(locations.get("cleanup") == "complete-with-retained-namespace" and
                isinstance(locations.get("retained_namespace"), dict) and
                locations["retained_namespace"].get("name") == "locations-system" and
                bool(locations["retained_namespace"].get("uid")),
                "Locations cleanup must precede Catalog OCI source prune")
        installer, _ = active_catalog_installer(host, milo, identity, installer_path)
        require(locations["installer_run_id"] == installer["run_id"],
                "Locations cleanup journal differs from Catalog installer run")
        return installer, locations
    require(locations.get("cleanup") == "not-requested" and
            locations.get("setup_stage") == "ready" and
            private.is_dir() and not private.is_symlink() and
            private.stat().st_mode & 0o077 == 0,
            "active Locations auth required before Catalog OCI transition")
    state = private_json(private / "state.json")
    require(locations.get("run_id") == state.get("run_id") and
            state.get("auth_intent") is True and
            isinstance(state.get("old_b64"), str) and
            isinstance(state.get("new_b64"), str) and
            state.get("auth_uid"),
            "Locations auth state does not bind the retained fixture")
    installer, installer_state = active_catalog_installer(
        host, milo, identity, installer_path, auth_allowed={state["new_b64"]})
    require(state["auth_uid"] == installer_state["auth_uid"] and
            state["old_b64"] == installer_state["new_b64"] and
            locations.get("installer_run_id") == installer["run_id"],
            "Locations auth does not extend exact Catalog installer state")
    return installer, locations


def clean_testinfra_source(commands: Commands) -> str:
    revision = commands.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None and
            not commands.run("git", "-C", str(ROOT), "status", "--porcelain",
                             "--untracked-files=all"),
            "signed clean test-infra source required before live transition")
    commands.run("git", "-C", str(ROOT), "verify-commit", "HEAD", sensitive=True)
    return revision


def journal(path: Path, evidence: dict) -> None:
    write_flux_journal(path, evidence)
    private_file(path)


def source_intent(source: dict, *, allow_terminating: bool = False) -> str:
    meta = source.get("metadata", {})
    deletion = meta.get("deletionTimestamp")
    require(not meta.get("ownerReferences") and
            (not deletion or allow_terminating and isinstance(deletion, str) and
             len(deletion) <= 64) and
            meta.get("finalizers", []) in ([], ["finalizers.fluxcd.io"]),
            "Catalog OCI source has unexpected lifecycle metadata")
    annotations = dict(meta.get("annotations", {}))
    requested = annotations.pop("reconcile.fluxcd.io/requestedAt", None)
    require(requested is None or isinstance(requested, str) and len(requested) <= 128,
            "Catalog OCI reconcile annotation is malformed")
    spec = copy.deepcopy(source.get("spec", {}))
    # Pinned Flux v2.7.5 CRDs default only these two source fields.
    spec.setdefault("timeout", "60s")
    spec.setdefault("provider", "generic")
    data = {"apiVersion": source.get("apiVersion"), "kind": source.get("kind"),
            "name": meta.get("name"), "namespace": meta.get("namespace"),
            "labels": meta.get("labels", {}), "annotations": annotations,
            "spec": spec}
    return hashlib.sha256(json.dumps(data, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_oci_source(host: Kube, contract: dict, evidence: dict,
                        *, ready_required: bool,
                        allow_terminating: bool = False) -> dict:
    current = host.optional("ocirepository", SOURCE_NAME, "flux-system")
    require(current is not None, "task-owned Catalog OCI source is absent")
    expected = copy.deepcopy(contract["source"])
    expected["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    require(not allow_terminating or evidence.get("phase") == "source-delete-intent",
            "terminating Catalog OCI source requires journaled delete phase")
    require(source_intent(current, allow_terminating=allow_terminating) ==
            evidence["oci_intent"] == source_intent(expected) and
            current["metadata"].get("uid") == evidence.get("oci_uid") and
            isinstance(evidence.get("oci_uid"), str) and bool(evidence["oci_uid"]),
            "Catalog OCI source UID or exact digest intent changed")
    observed_revision = current.get("status", {}).get("artifact", {}).get("revision")
    require(observed_revision in (None, OCI_DIGEST),
            "Catalog OCI source reported a foreign artifact revision")
    if ready_required:
        require(ready(current) and observed_revision == OCI_DIGEST,
                "Catalog OCI source has not fetched the exact current digest")
    return current


def original_state(host: Kube, contract: dict, graph: dict) -> tuple[dict, dict]:
    require(graph.get("catalog_source") == CATALOG_SHA and graph.get("complete") is True and
            graph.get("cleanup") == "not-requested" and
            re.fullmatch(r"[0-9a-f]{24}", graph.get("run_id", "")) is not None,
            "retained Catalog graph journal is not active")
    git = original_git(host, graph)
    saved = graph.get("flux_stages", {}).get(GRAPH_KEY)
    require(graph.get("attempted", {}).get(GRAPH_KEY) ==
            (saved or {}).get("uid"), "original Catalog API stage journal UID differs")
    original_owner = FluxOwned(host, "milo-system", "service-catalog-api-crds",
                               contract["rendered"], "sha1:" + CATALOG_SHA)
    original_owner.recover(contract["original"], graph["run_id"], saved)
    stage = host.json("-n", "milo-system", "get", "kustomization",
                      "service-catalog-api-crds")
    original_owner._validate(stage, graph["run_id"])
    require(ready(stage) and stage.get("status", {}).get("lastAppliedRevision") ==
            "sha1:" + CATALOG_SHA and
            len(stage.get("status", {}).get("inventory", {}).get("entries", [])) == 13,
            "original Catalog API stage is not Ready with exact inventory")
    return git, stage


def original_git(host: Kube, graph: dict) -> dict:
    git = host.json("-n", "flux-system", "get", "gitrepository", "service-catalog")
    source_meta = git.get("metadata", {})
    source_annotations = dict(source_meta.get("annotations", {}))
    requested = source_annotations.pop("reconcile.fluxcd.io/requestedAt", None)
    require(requested is None or isinstance(requested, str) and len(requested) <= 128,
            "original Git source reconcile marker is malformed")
    require(source_meta.get("uid") == graph.get("attempted", {}).get(GIT_KEY) and
            source_meta.get("labels") == {RUN_LABEL: graph["run_id"]} and
            not source_annotations and
            not source_meta.get("ownerReferences") and
            not source_meta.get("deletionTimestamp") and
            git.get("spec") == {"interval": "10m", "timeout": "60s",
                "url": "https://github.com/milo-os/service-catalog.git",
                "ref": {"commit": CATALOG_SHA}} and
            git.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + CATALOG_SHA,
            "original Catalog Git source UID, spec or last artifact changed")
    return git


def target_flux_state(host: Kube, contract: dict, graph: dict,
                      evidence: dict, path: Path | None = None) -> tuple[FluxOwned, dict]:
    def record(saved: dict) -> None:
        require(path is not None, "Catalog OCI Flux recovery journal path is absent")
        evidence["target_flux_state"] = saved
        journal(path, evidence)
    owner = FluxOwned(host, "milo-system", "service-catalog-api-crds",
                      contract["rendered"], OCI_DIGEST, record if path else None)
    labeled = copy.deepcopy(contract["target"])
    labeled["metadata"].setdefault("labels", {})[RUN_LABEL] = graph["run_id"]
    labeled["spec"] = _flux_spec(labeled["spec"])
    expected = {"version": 1, "run_id": graph["run_id"], "initial": labeled,
             "inventory": [list(entry) for entry in sorted(owner.allowed)],
             "revision": OCI_DIGEST, "uid": evidence["stage_uid"],
             "intent": True, "delete_intent": False,
             "ca_bundle": None, "ca_committed": False}
    saved = evidence.get("target_flux_state", expected)
    normalized = copy.deepcopy(saved)
    require(isinstance(normalized, dict) and
            (not normalized.get("delete_intent") or evidence.get("phase") in
             ("api-delete-intent", "api-pruned", "source-delete-intent", "cleaned")),
            "Catalog API Flux delete intent has an invalid phase")
    normalized["delete_intent"] = False
    require(normalized == expected,
            "Catalog API Flux recovery journal differs from exact OCI source")
    owner.recover(contract["target"], graph["run_id"], saved)
    return owner, saved


def active_transition(host: Kube, identity: dict, contract: dict,
                      graph: dict, evidence: dict) -> dict:
    require(evidence.get("version") == 1 and
            evidence.get("run_id") and re.fullmatch(r"[0-9a-f]{24}", evidence["run_id"]) and
            evidence.get("graph_run_id") == graph["run_id"] and
            evidence.get("scope_identity") == identity and
            evidence.get("catalog_source") == CATALOG_SHA and
            evidence.get("oci_digest") == OCI_DIGEST and
            evidence.get("render_sha256") == contract["render_sha256"] and
            evidence.get("original_git_uid") == graph["attempted"][GIT_KEY] and
            evidence.get("stage_uid") == graph["attempted"][GRAPH_KEY] and
            evidence.get("oci_intent") == source_intent({
                **contract["source"], "metadata": {
                    **contract["source"]["metadata"],
                    "labels": {RUN_LABEL: evidence["run_id"]}}}),
            "Catalog OCI transition journal differs from source, graph or fixture")
    validate_oci_source(host, contract, evidence, ready_required=True)
    owner, _ = target_flux_state(host, contract, graph, evidence)
    stage = host.json("-n", "milo-system", "get", "kustomization",
                      "service-catalog-api-crds")
    owner._validate(stage, graph["run_id"])
    require(stage["metadata"].get("uid") == evidence["stage_uid"] and
            ready(stage) and stage.get("status", {}).get("lastAppliedRevision") ==
                OCI_DIGEST and
            len(stage.get("status", {}).get("inventory", {}).get("entries", [])) == 13,
            "existing Catalog API stage has not applied exact OCI inventory")
    return stage


def setup(host: Kube, contract: dict, graph: dict, identity: dict,
          evidence: dict, path: Path) -> None:
    git = original_git(host, graph)
    require(git["metadata"]["uid"] == evidence["original_git_uid"],
            "retained Catalog Git provenance changed")
    if evidence.get("phase") == "complete":
        active_transition(host, identity, contract, graph, evidence)
        return
    desired = copy.deepcopy(contract["source"])
    desired["metadata"].setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    if evidence.get("oci_uid") is None:
        current = host.optional("ocirepository", SOURCE_NAME, "flux-system")
        if current is None:
            created = host.create(desired, "flux-system")
            current = created
        require(source_intent(current) == evidence["oci_intent"],
                "uncertain OCI create response does not match exact intent")
        evidence["oci_uid"] = current["metadata"]["uid"]
        journal(path, evidence)
    wait_for(lambda: ready(validate_oci_source(host, contract, evidence,
                                                ready_required=False)) and
             validate_oci_source(host, contract, evidence,
                                 ready_required=True), "exact Catalog OCI source Ready", 180)
    evidence["phase"] = "source-ready"
    journal(path, evidence)
    original, target = contract["original"], contract["target"]
    current = host.json("-n", "milo-system", "get", "kustomization",
                        "service-catalog-api-crds")
    require(current["metadata"]["uid"] == evidence["stage_uid"],
            "Catalog API Kustomization was replaced during transition")
    source_spec, target_spec = _flux_spec(original["spec"]), _flux_spec(target["spec"])
    require(_flux_spec(current["spec"]) in (source_spec, target_spec),
            "Catalog API stage has a foreign spec; refusing source switch")
    owner, saved = target_flux_state(host, contract, graph, evidence, path)
    evidence["target_flux_state"] = saved
    evidence["patch_intent"] = True
    evidence["phase"] = "api-patch-intent"
    journal(path, evidence)
    if _flux_spec(current["spec"]) == source_spec:
        with tempfile.TemporaryDirectory(prefix="apps-catalog-oci-patch-",
                                         dir=path.parent) as temporary:
            try:
                current = host.patch_json(Path(temporary), "kustomization",
                    "service-catalog-api-crds", [
                        {"op": "test", "path": "/metadata/uid",
                         "value": evidence["stage_uid"]},
                        {"op": "test", "path": "/metadata/resourceVersion",
                         "value": current["metadata"]["resourceVersion"]},
                        {"op": "test", "path": "/spec", "value": current["spec"]},
                        {"op": "replace", "path": "/spec", "value": target_spec}],
                    "milo-system")
            except Exception:
                current = host.json("-n", "milo-system", "get", "kustomization",
                                    "service-catalog-api-crds")
                require(current["metadata"].get("uid") == evidence["stage_uid"] and
                        _flux_spec(current.get("spec", {})) == target_spec,
                        "Catalog API patch outcome uncertain; private journal retained")
    require(current["metadata"]["uid"] == evidence["stage_uid"] and
            _flux_spec(current["spec"]) == target_spec,
            "Catalog API patch did not preserve exact owned stage")
    # Force a reconciliation of the same UID; the pinned source is already Ready.
    host.commands.run("flux", "reconcile", "kustomization",
                      "service-catalog-api-crds", "-n", "milo-system",
                      "--kubeconfig", str(host.config), "--context", host.context,
                      "--timeout=180s", timeout=195)
    wait_for(lambda: active_transition(host, identity, contract, graph, evidence),
             "exact Catalog OCI API inventory Ready", 180)
    evidence["phase"] = "complete"
    journal(path, evidence)


def cleanup(host: Kube, milo: Kube, contract: dict, graph: dict,
            identity: dict, evidence: dict, path: Path,
            locations_evidence_file: Path | None) -> None:
    require(evidence.get("phase") in ("complete", "api-delete-intent",
                                     "api-pruned", "source-delete-intent",
                                     "cleaned") and
            graph.get("cleanup") == "not-requested",
            "Catalog OCI cleanup needs a completed active graph transition")
    require_same_identity(evidence["scope_identity"], identity)
    for name, namespace in (("apps-a1t-locationbinding-crd", "milo-system"),
                            ("service-catalog-operator", "flux-system"),
                            ("service-catalog-operator-rbac", "milo-system")):
        require(host.optional("kustomization", name, namespace) is None,
                f"dependent Flux Kustomization {namespace}/{name} remains")
    assert_no_catalog_dependents(host, milo, identity, locations_evidence_file)
    if evidence["phase"] not in ("source-delete-intent", "cleaned"):
        validate_oci_source(host, contract, evidence,
                            ready_required=evidence["phase"] == "complete")
    owner, saved = target_flux_state(host, contract, graph, evidence, path)
    require(evidence.get("target_flux_state") == saved,
            "Catalog OCI stage cleanup contract differs from transition journal")
    if evidence["phase"] in ("complete", "api-delete-intent"):
        evidence["phase"] = "api-delete-intent"
        journal(path, evidence)
        current = host.optional("kustomization", "service-catalog-api-crds",
                                "milo-system")
        require(current is not None or saved["delete_intent"] is True,
                "Catalog API stage is absent without a journaled UID prune")
        proxy = APIProxy(host, path.parent, "catalog-oci-cleanup")
        try:
            proxy.start()
            owner.delete(proxy, graph["run_id"])
        finally:
            proxy.close()
        require(host.optional("kustomization", "service-catalog-api-crds",
                              "milo-system") is None,
                "Catalog API Kustomization remains after OCI prune")
        evidence["phase"] = "api-pruned"
        journal(path, evidence)
    require(host.optional("kustomization", "service-catalog-api-crds",
                          "milo-system") is None,
            "Catalog API Kustomization was recreated after prune")
    expected_remaining = set(graph["crd_names"]) - SOURCE_CRDS
    wait_for(lambda: root_inventory(milo) == (expected_remaining, set()),
             "Catalog API CRDs and webhooks absent after OCI prune", 120)
    evidence["phase"] = "source-delete-intent"
    journal(path, evidence)
    current = host.optional("ocirepository", SOURCE_NAME, "flux-system")
    if current is not None:
        current = validate_oci_source(host, contract, evidence,
                                      ready_required=False, allow_terminating=True)
        if not current["metadata"].get("deletionTimestamp"):
            proxy = APIProxy(host, path.parent, "catalog-oci-source-cleanup")
            try:
                proxy.start()
                proxy.delete(OCI_PATH, evidence["oci_uid"],
                             current["metadata"]["resourceVersion"])
            finally:
                proxy.close()
    def absent() -> bool:
        current = host.optional("ocirepository", SOURCE_NAME, "flux-system")
        if current is None:
            return True
        validate_oci_source(host, contract, evidence, ready_required=False,
                            allow_terminating=True)
        return False
    wait_for(absent, "task-owned Catalog OCI source absent", 120)
    evidence["phase"] = "cleaned"
    evidence["cleanup"] = "complete"
    journal(path, evidence)


def run(args: argparse.Namespace) -> None:
    require(not (args.execute and args.cleanup) and
            120 <= args.deadline_seconds <= 900 and
            1 <= args.milo_port <= 65535 and
            args.evidence_file.is_absolute() and
            args.evidence_file.parent.is_dir() and
            args.evidence_file.parent.stat().st_mode & 0o077 == 0 and
            not args.evidence_file.is_symlink(),
            "private transition path, mode or bounded deadline invalid")
    for file in (args.host_kubeconfig, args.milo_kubeconfig):
        require(not file.is_symlink(), "private kubeconfig symlink refused")
        private_file(file)
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    revision = clean_testinfra_source(commands)
    contract = transition_contract(commands, args.catalog_source,
                                   args.oci_manifest, args.oci_layer)
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    project = private_json(milo.config.parent / "apps-a1t-project-bootstrap-evidence.json")
    identity = scope_identity(host, milo, args.host_context, args.milo_port,
                              projects_removed=args.cleanup and
                              project.get("cleanup") == "complete")
    installer, locations = active_installer_with_locations(
        host, milo, identity, args.installer_evidence_file,
        args.locations_evidence_file, cleanup=args.cleanup)
    graph = private_json(args.catalog_api_evidence_file)
    require(graph.get("scope_identity") == identity and
            graph.get("installer_run_id") == installer["run_id"],
            "Catalog graph and scoped installer do not bind this fixture")
    if args.cleanup or args.resume:
        evidence = private_json(args.evidence_file)
        require(evidence.get("scope_identity") == identity and
                evidence.get("testinfra_revision") == revision and
                evidence.get("graph_run_id") == graph["run_id"] and
                evidence.get("locations_run_id") == locations["run_id"] and
                evidence.get("catalog_source") == CATALOG_SHA and
                evidence.get("oci_digest") == OCI_DIGEST,
                "transition recovery journal differs from current fixture")
        require(host.json("-n", "flux-system", "get", "gitrepository",
                          "service-catalog")["metadata"]["uid"] ==
                evidence.get("original_git_uid") ==
                graph["attempted"][GIT_KEY],
                "original Git provenance UID changed")
        if args.cleanup:
            cleanup(host, milo, contract, graph, identity, evidence,
                    args.evidence_file, args.locations_evidence_file)
            print("Catalog OCI stage pruned and source removed; retained Git/Billing graph remains")
            return
        require(args.execute and evidence.get("phase") in (
            "source-create-intent", "source-ready", "api-patch-intent", "complete"),
            "transition resume needs incomplete setup intent")
        setup(host, contract, graph, identity, evidence, args.evidence_file)
        print("Catalog API stage reused its UID/inventory on exact OCI source")
        return
    require(not args.evidence_file.exists(), "refusing existing Catalog OCI transition journal")
    git, stage = original_state(host, contract, graph)
    require(host.optional("ocirepository", SOURCE_NAME, "flux-system") is None,
            "refusing to adopt an existing Catalog OCI source")
    print("read-only Catalog OCI transition preflight passed: exact published tree and 13-object render")
    if not args.execute:
        return
    transition_run = secrets.token_hex(12)
    desired = copy.deepcopy(contract["source"])
    desired["metadata"].setdefault("labels", {})[RUN_LABEL] = transition_run
    evidence = {"version": 1, "run_id": transition_run,
                "testinfra_revision": revision,
                "graph_run_id": graph["run_id"],
                "locations_run_id": locations["run_id"],
                "scope_identity": identity,
                "catalog_source": CATALOG_SHA, "oci_digest": OCI_DIGEST,
                "render_sha256": contract["render_sha256"],
                "original_git_uid": git["metadata"]["uid"],
                "stage_uid": stage["metadata"]["uid"],
                "oci_intent": source_intent(desired), "oci_uid": None,
                "patch_intent": False, "phase": "source-create-intent",
                "cleanup": "not-requested"}
    journal(args.evidence_file, evidence)
    setup(host, contract, graph, identity, evidence, args.evidence_file)
    print("Catalog API stage reused its UID/inventory on exact OCI source")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("catalog-source", "oci-manifest", "oci-layer",
                 "host-kubeconfig", "milo-kubeconfig", "catalog-api-evidence-file",
                 "installer-evidence-file", "evidence-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--locations-evidence-file", type=Path, required=True)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-port", type=int, required=True)
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except Exception:
        # Kubernetes command failures can contain reflected object data. The
        # private journal identifies the failed phase without echoing a token.
        print("Catalog OCI transition stopped; inspect its private phase journal",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
