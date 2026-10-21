#!/usr/bin/env python3
"""Reconcile pinned Catalog, Billing and Locations API schemas into test Milo.

This stage uses only public Kubernetes APIs and keeps a private UID journal for
bounded cleanup. It installs no operator, entitlement, or customer workload.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import tarfile
import time

import yaml

from apps_native_support import APIProxy, Commands, Kube, RUN_LABEL, private_file, ready, require, wait_for
from apps_native_operator_flux import create_stage, delete_stage
from apps_native_fixture_scope import active_catalog_installer, scope_identity, require_same_identity


ROOT = Path(__file__).resolve().parent.parent
CATALOG_SHA = "5f2b663b6c5e039c4c7b666ef3e959803851b675"
BILLING_DIGEST = "sha256:9e772461787d0b0f2561ed348109a6d80e171d04eeea9fee36a4e5a67d37f20c"
MILO_SHA = "200de48a305732720377cb83308d57c84fd5f688"
MILO_IMAGE = "sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c"
COMPONENT = ROOT / "components/service-catalog"
GRAPH = (
    ("source.yaml", "gitrepository", "service-catalog", "flux-system",
     "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/service-catalog"),
    ("billing-source.yaml", "ocirepository", "catalog-billing", "flux-system",
     "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/ocirepositories/catalog-billing"),
    ("api-crds.yaml", "kustomization", "service-catalog-api-crds", "milo-system",
     "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/milo-system/kustomizations/service-catalog-api-crds"),
    ("billing-crds.yaml", "kustomization", "service-catalog-billing-crds", "milo-system",
     "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/milo-system/kustomizations/service-catalog-billing-crds"),
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
BILLING_CRDS = {
    "billingaccountbindings.billing.miloapis.com",
    "billingaccounts.billing.miloapis.com",
    "billingarrangements.billing.miloapis.com",
    "billingentitlements.billing.miloapis.com", "invoices.billing.miloapis.com",
    "meterdefinitions.billing.miloapis.com",
    "monitoredresourcetypes.billing.miloapis.com", "offers.billing.miloapis.com",
    "paymentmethodclasses.billing.miloapis.com",
    "paymentmethods.billing.miloapis.com", "servicepricings.billing.miloapis.com",
}
WEBHOOKS = (("mutatingwebhookconfiguration", "services-mutating"),
            ("validatingwebhookconfiguration", "services-validating"))


# Verified from immutable BILLING_DIGEST manifest's single content layer:
# compressed sha256:71bf45b57750df1e214e7689a10a8e98adabce7373ab432a18aab88fd45f9cc6.
# This is the exact decompressed tar, not an arbitrary re-export of its files.
BILLING_TAR_SHA256 = "c5aba6d4f649280af327b49bf128a59cca64e008c9d8fc6c9c6783d1890dd44f"


def source_contracts(commands: Commands, source: Path, billing_tar: Path) -> dict:
    require(source.is_absolute() and source.is_dir() and not source.is_symlink() and
            commands.run("git", "-C", str(source), "rev-parse", "HEAD") == CATALOG_SHA and
            not commands.run("git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"),
            "exact clean Catalog source required for execute and cleanup")
    require(billing_tar.is_absolute() and billing_tar.is_file() and not billing_tar.is_symlink() and
            billing_tar.stat().st_size == 394240,
            "exact bounded billing source tar required")
    raw = billing_tar.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == BILLING_TAR_SHA256,
            "billing tar is not verified immutable OCI content")
    manifests = manifest_set()
    def render(row, local_path):
        result = commands.run("flux", "build", "kustomization", row[2], "-n", row[3],
            "--path", str(local_path), "--kustomization-file", str(COMPONENT / row[0]), "--dry-run")
        return [obj for obj in yaml.safe_load_all(result) if obj]
    api = render(GRAPH[2], source / "config/overlays/e2e-milo/milo-resources")
    with tempfile.TemporaryDirectory(prefix="apps-billing-source-") as directory:
        # Native tar parser only; never execute archive paths or follow links.
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            require(len(members) <= 500 and all(
                not Path(m.name).is_absolute() and ".." not in Path(m.name).parts and
                (m.isdir() or m.isfile()) for m in members), "billing archive has unsupported entries")
            archive.extractall(directory, members=members, filter="data")
        billing = render(GRAPH[3], Path(directory) / "base/crd/bases")
    require({o["metadata"]["name"] for o in api if o["kind"] == "CustomResourceDefinition"} == SOURCE_CRDS and
            {(o["kind"].lower(), o["metadata"]["name"]) for o in api if o["kind"] != "CustomResourceDefinition"} == set(WEBHOOKS) and
            len(api) == len(SOURCE_CRDS) + len(WEBHOOKS), "Catalog render exceeds reviewed API inventory")
    require(len(billing) == len(BILLING_CRDS) and all(o["kind"] == "CustomResourceDefinition" for o in billing) and
            {o["metadata"]["name"] for o in billing} == BILLING_CRDS, "billing render exceeds reviewed CRD inventory")
    return {journal_key(row): {"manifest": manifest, "rendered": rendered, "revision": revision}
            for row, manifest, rendered, revision in (
                (GRAPH[2], manifests[2], api, "sha1:" + CATALOG_SHA),
                (GRAPH[3], manifests[3], billing, BILLING_DIGEST))}


def atomic_json(path: Path, document: dict) -> None:
    fd, pending = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        private_file(path)
    finally:
        Path(pending).unlink(missing_ok=True)


def manifest_set() -> list[dict]:
    result = [yaml.safe_load((COMPONENT / filename).read_text()) for filename, *_ in GRAPH]
    require(all(isinstance(obj, dict) for obj in result), "Catalog graph is malformed")
    source, billing, api, billing_api = result
    for obj, row in zip(result, GRAPH):
        require(obj.get("apiVersion") == ("source.toolkit.fluxcd.io/v1" if row[1] != "kustomization" else "kustomize.toolkit.fluxcd.io/v1") and
                obj.get("kind", "").lower() == ("OCIRepository" if row[1] == "ocirepository" else "GitRepository" if row[1] == "gitrepository" else "Kustomization").lower() and
                obj.get("metadata", {}).get("name") == row[2] and
                obj.get("metadata", {}).get("namespace") == row[3],
                "Catalog graph object identity differs from reviewed source")
    require(source["spec"]["url"] == "https://github.com/milo-os/service-catalog.git" and
            source["spec"]["ref"] == {"commit": CATALOG_SHA} and
            set(source["spec"]) == {"interval", "url", "ref"},
            "Catalog source is not pinned to reviewed commit")
    require(billing["spec"]["url"] == "oci://ghcr.io/milo-os/billing-kustomize" and
            billing["spec"]["ref"] == {"digest": BILLING_DIGEST} and
            set(billing["spec"]) == {"interval", "url", "ref"},
            "Billing source is not pinned to reviewed digest")
    require(api["spec"]["path"] == "./config/overlays/e2e-milo/milo-resources" and
            billing_api["spec"]["path"] == "./base/crd/bases" and
            api["spec"].get("sourceRef") == {"kind": "GitRepository", "name": "service-catalog", "namespace": "flux-system"} and
            billing_api["spec"].get("sourceRef") == {"kind": "OCIRepository", "name": "catalog-billing", "namespace": "flux-system"} and
            all(set(obj["spec"]) == {"interval", "retryInterval", "timeout", "prune", "dependsOn", "sourceRef", "path", "kubeConfig", "healthChecks"} and
                obj["spec"].get("dependsOn") == [{"name": "milo-core", "namespace": "flux-system"}] and
                obj["spec"].get("prune") is True and
                obj["spec"].get("kubeConfig", {}).get("secretRef") == {
                    "name": "apps-a1t-catalog-installer-kubeconfig", "key": "kubeconfig"}
                for obj in (api, billing_api)),
            "remote Catalog graph must apply source-owned schemas to Milo")
    return result


def baseline_guard(host: Kube) -> None:
    source = host.json("-n", "flux-system", "get", "gitrepository", "milo")
    core = host.json("-n", "flux-system", "get", "kustomization", "milo-core")
    require(source["spec"].get("ref") == {"commit": MILO_SHA} and
            source.get("status", {}).get("artifact", {}).get("revision") == "sha1:" + MILO_SHA and
            core["spec"].get("images") == [{"name": "ghcr.io/milo-os/milo",
                                             "digest": MILO_IMAGE}] and
            ready(source) and core["spec"].get("suspend") is True,
            "host Milo source/Flux/image differs from reviewed baseline")


def installer_guard(host: Kube, milo: Kube, identity: dict, path: Path) -> str:
    evidence, _ = active_catalog_installer(host, milo, identity, path)
    return evidence["run_id"]


def intent_digest(manifest: dict) -> str:
    meta = manifest.get("metadata", {})
    require(not meta.get("ownerReferences") and not meta.get("finalizers") and
            not meta.get("deletionTimestamp"), "Catalog create intent has lifecycle metadata")
    projection = {"apiVersion": manifest.get("apiVersion"), "kind": manifest.get("kind"),
                  "name": meta.get("name"), "namespace": meta.get("namespace"),
                  "labels": meta.get("labels", {}), "annotations": meta.get("annotations", {}),
                  "spec": manifest.get("spec", {})}
    return hashlib.sha256(json.dumps(projection, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def root_inventory(milo: Kube) -> tuple[set[str], set[str]]:
    crds = {item["metadata"]["name"] for item in milo.json("get", "crd")["items"]
            if item["metadata"]["name"].endswith((
                ".services.miloapis.com", ".locations.miloapis.com",
                ".billing.miloapis.com"))}
    webhooks = {name for kind, name in WEBHOOKS
                if milo.optional(kind, name) is not None}
    return crds, webhooks


def preflight(host: Kube, milo: Kube) -> None:
    for _, kind, name, namespace, _ in GRAPH:
        require(host.optional(kind, name, namespace) is None,
                f"refusing existing Flux {kind}/{namespace}/{name}")
    crds, webhooks = root_inventory(milo)
    require(not crds and not webhooks,
            "refusing to adopt existing Catalog, Billing or Locations APIs")


def journal_key(row: tuple[str, str, str, str, str]) -> str:
    return row[3] + "/" + row[2]


def create_recorded(host: Kube, row: tuple[str, str, str, str, str],
                    manifest: dict, evidence: dict, path: Path, flux_contracts=None) -> None:
    key = journal_key(row)
    if row[1] == "kustomization":
        return create_stage(host, key, manifest, flux_contracts, evidence, path, atomic_json)
    manifest.setdefault("metadata", {}).setdefault("labels", {})[RUN_LABEL] = evidence["run_id"]
    evidence.setdefault("intents", {})[key] = intent_digest(manifest)
    evidence["attempted"][key] = None
    atomic_json(path, evidence)
    result = host.create(manifest, row[3])
    uid = result.get("metadata", {}).get("uid")
    require(isinstance(uid, str) and bool(uid), f"{key} create response lacks UID")
    evidence["attempted"][key] = uid
    atomic_json(path, evidence)


def observed_ready(host: Kube, row: tuple[str, str, str, str, str],
                   evidence: dict) -> bool:
    obj = host.optional(row[1], row[2], row[3])
    key = journal_key(row)
    require(obj is not None and obj["metadata"]["uid"] == evidence["attempted"][key],
            f"owned {key} was replaced")
    return ready(obj)


def root_ready(milo: Kube) -> dict:
    crds, webhooks = root_inventory(milo)
    require(webhooks == {name for _, name in WEBHOOKS},
            "Catalog webhook registration set differs from source")
    require(crds == SOURCE_CRDS | BILLING_CRDS,
            "Catalog, Billing and Locations CRD names differ from 22 pinned sources")
    for name in crds:
        obj = milo.json("get", "crd", name)
        require(any(c.get("type") == "Established" and c.get("status") == "True"
                    for c in obj.get("status", {}).get("conditions", [])),
                f"CRD/{name} is not Established")
    return {"crd_names": sorted(crds), "webhook_names": sorted(webhooks)}


def delete_recorded(host: Kube, proxy: APIProxy, row: tuple[str, str, str, str, str],
                    evidence: dict, path: Path, flux_contracts=None) -> None:
    key = journal_key(row)
    if row[1] == "kustomization":
        return delete_stage(host, proxy, key, flux_contracts, evidence, path, atomic_json)
    if key not in evidence["attempted"]:
        return
    obj = host.optional(row[1], row[2], row[3])
    if obj is None:
        return
    meta = obj["metadata"]
    expected = evidence["attempted"][key]
    if expected:
        require(meta["uid"] == expected, f"refusing replaced {key}")
    else:
        require(meta.get("labels", {}).get(RUN_LABEL) == evidence["run_id"] and
                intent_digest(obj) == evidence.get("intents", {}).get(key),
                f"refusing foreign or changed unknown-UID {key}")
        evidence["attempted"][key] = meta["uid"]
        atomic_json(path, evidence)
    proxy.delete(row[4], meta["uid"], meta["resourceVersion"])
    def absent() -> bool:
        current = host.optional(row[1], row[2], row[3])
        require(current is None or current["metadata"]["uid"] == meta["uid"],
                f"refusing replacement {key} during cleanup")
        return current is None
    wait_for(absent, f"{key} deleted", 120)


def cleanup(host: Kube, milo: Kube, evidence: dict, path: Path,
            locations_evidence_file: Path | None = None, flux_contracts=None) -> None:
    attempted = evidence.get("attempted", {})
    require(set(attempted).issubset({journal_key(row) for row in GRAPH}) and
            all(uid is None or isinstance(uid, str) and bool(uid)
                for uid in attempted.values()), "invalid Catalog cleanup journal")
    # These checks apply even after a partially applied Flux graph. A partial
    # source-owned CRD install can already hold data, and pruning it deletes
    # that data. Retain the graph and journal if any census is ambiguous.
    require(host.optional("namespace", "services-system") is None,
            "Catalog operator namespace services-system still exists")
    locations_namespace = host.optional("namespace", "locations-system")
    if locations_namespace is not None:
        require(locations_evidence_file is not None and
                not locations_evidence_file.is_symlink(),
                "retained Locations namespace requires its private ownership journal")
        private_file(locations_evidence_file)
        locations = json.loads(locations_evidence_file.read_text())
        require(locations.get("cleanup") == "complete-with-retained-namespace" and
                locations.get("scope_identity") == evidence["scope_identity"] and
                locations.get("retained_namespace") == {
                    "name": "locations-system",
                    "uid": locations_namespace["metadata"].get("uid")} and
                locations_namespace["metadata"].get("labels", {}).get(RUN_LABEL) ==
                    locations.get("run_id") and
                not locations_namespace["metadata"].get("deletionTimestamp"),
                "retained Locations namespace identity or cleanup differs")
        census = host.json("-n", "locations-system", "get",
            "deployments,statefulsets,daemonsets,replicasets,replicationcontrollers,pods,jobs,cronjobs")
        require(isinstance(census, dict) and isinstance(census.get("items"), list) and
                not census["items"], "retained Locations namespace workload census is unavailable or nonempty")
    require(milo.json("get", "projects.resourcemanager.miloapis.com")["items"] == [],
            "remove owned Projects before pruning their Catalog APIs")
    crds, _ = root_inventory(milo)
    for crd_name in sorted(crds):
        require(milo.json("get", crd_name, "-A")["items"] == [],
                f"Catalog API {crd_name} still contains data")
    for kind, name in WEBHOOKS:
        obj = milo.optional(kind, name)
        if obj is not None:
            require(all(not hook.get("clientConfig", {}).get("caBundle")
                        for hook in obj.get("webhooks", [])),
                    f"{kind}/{name} still has an operator CA bundle")
    proxy = APIProxy(host, path.parent, "catalog-crd-cleanup")
    errors: list[str] = []
    try:
        proxy.start()
        # Remote Kustomizations must be gone before checking API absence;
        # otherwise live Flux could recreate schemas after this check.
        for row in reversed(GRAPH[2:]):
            try:
                delete_recorded(host, proxy, row, evidence, path, flux_contracts)
            except Exception:
                errors.append(f"{journal_key(row)}: delete failed")
        if not errors:
            try:
                wait_for(lambda: root_inventory(milo) == (set(), set()),
                         "remote Catalog/Locations/Billing APIs pruned", 120)
            except Exception:
                errors.append("remote API absence verification failed")
        if not errors:
            for row in reversed(GRAPH[:2]):
                try:
                    delete_recorded(host, proxy, row, evidence, path, flux_contracts)
                except Exception:
                    errors.append(f"{journal_key(row)}: delete failed")
    finally:
        proxy.close()
    evidence["cleanup"] = "complete" if not errors else "incomplete"
    evidence["cleanup_errors"] = errors
    atomic_json(path, evidence)
    require(not errors, "Catalog graph cleanup incomplete: " + "; ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-source", required=True, type=Path)
    parser.add_argument("--billing-artifact", required=True, type=Path)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--installer-evidence-file", required=True, type=Path)
    parser.add_argument("--locations-evidence-file", type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    require(not (args.execute and args.cleanup) and 1 <= args.milo_port <= 65535 and
            120 <= args.deadline_seconds <= 900, "invalid mode, port or deadline")
    require(args.evidence_file.is_absolute() and args.evidence_file.parent.is_dir() and
            args.evidence_file.parent.stat().st_mode & 0o077 == 0 and
            not args.evidence_file.is_symlink(), "private evidence path required")
    for config in (args.host_kubeconfig, args.milo_kubeconfig):
        require(not config.is_symlink(), "kubeconfig symlink refused")
        private_file(config)
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    flux_contracts = source_contracts(commands, args.catalog_source, args.billing_artifact)
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    project_journal = json.loads((milo.config.parent /
        "apps-a1t-project-bootstrap-evidence.json").read_text())
    identity = scope_identity(host, milo, args.host_context, args.milo_port,
                              projects_removed=args.cleanup and
                              project_journal.get("cleanup") == "complete")
    baseline_guard(host)
    installer_run_id = installer_guard(host, milo, identity,
                                       args.installer_evidence_file)
    if args.cleanup:
        private_file(args.evidence_file)
        evidence = json.loads(args.evidence_file.read_text())
        require(evidence.get("host_context") == args.host_context and
                evidence.get("milo_source") == MILO_SHA and
                evidence.get("installer_run_id") == installer_run_id and
                re.fullmatch(r"[0-9a-f]{24}", evidence.get("run_id", "")),
                "Catalog cleanup evidence differs from task-owned scope")
        require_same_identity(evidence["scope_identity"], identity)
        cleanup(host, milo, evidence, args.evidence_file,
                args.locations_evidence_file, flux_contracts)
        print("Catalog API graph cleanup complete", flush=True)
        return
    manifests = manifest_set()
    preflight(host, milo)
    print("Catalog API graph preflight passed: exact sources and no API collisions", flush=True)
    if not args.execute:
        return
    require(not args.evidence_file.exists(), "refusing to overwrite Catalog evidence")
    evidence = {"host_context": args.host_context, "milo_source": MILO_SHA,
                "scope_identity": identity,
                "installer_run_id": installer_run_id,
                "catalog_source": CATALOG_SHA, "billing_digest": BILLING_DIGEST,
                "run_id": secrets.token_hex(12), "attempted": {}, "intents": {},
                "cleanup": "not-requested"}
    atomic_json(args.evidence_file, evidence)
    try:
        for row, manifest in zip(GRAPH, manifests):
            create_recorded(host, row, manifest, evidence, args.evidence_file, flux_contracts)
        for row in GRAPH:
            wait_for(lambda item=row: observed_ready(host, item, evidence),
                     f"{journal_key(row)} Ready", 240)
        source = host.json("-n", "flux-system", "get", "gitrepository", "service-catalog")
        require(source.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + CATALOG_SHA, "Flux fetched unexpected Catalog revision")
        evidence.update(root_ready(milo))
        evidence["complete"] = True
        atomic_json(args.evidence_file, evidence)
        print("Catalog API graph passed: exact Flux revisions, 22 established CRDs and two webhooks", flush=True)
    except Exception:
        commands.deadline = time.monotonic() + 300
        try:
            cleanup(host, milo, evidence, args.evidence_file,
                    args.locations_evidence_file, flux_contracts)
        except Exception:
            print("Catalog API graph cleanup incomplete; private journal retained", file=sys.stderr)
        raise RuntimeError("Catalog API graph setup failed; inspect sanitized evidence") from None


if __name__ == "__main__":
    main()
