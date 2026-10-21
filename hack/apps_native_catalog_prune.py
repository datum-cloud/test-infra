"""Shared nonmutating census before pruning the task-owned Catalog API graph."""

from __future__ import annotations

import json
from pathlib import Path

from apps_native_support import Kube, RUN_LABEL, private_file, require


WEBHOOKS = (("mutatingwebhookconfiguration", "services-mutating"),
            ("validatingwebhookconfiguration", "services-validating"))


def root_inventory(milo: Kube) -> tuple[set[str], set[str]]:
    crds = {item["metadata"]["name"] for item in milo.json("get", "crd")["items"]
            if item["metadata"]["name"].endswith((
                ".services.miloapis.com", ".locations.miloapis.com",
                ".billing.miloapis.com"))}
    webhooks = {name for kind, name in WEBHOOKS
                if milo.optional(kind, name) is not None}
    return crds, webhooks


def assert_no_catalog_dependents(host: Kube, milo: Kube, scope_identity: dict,
                                 locations_evidence_file: Path | None) -> None:
    # A partially applied Flux stage can already hold data. This census
    # therefore applies regardless of a setup journal's complete flag.
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
                locations.get("scope_identity") == scope_identity and
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
                not census["items"],
                "retained Locations namespace workload census is unavailable or nonempty")
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
