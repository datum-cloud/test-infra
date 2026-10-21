#!/usr/bin/env python3
"""Bounded real Milo Service -> entitlement -> LocationBinding prerequisite.

Only public Kubernetes APIs are used. The host-held root config is never mounted
in a Pod; Catalog's separately scoped controller supplies every status and
projected object. This is test-overlay routing evidence, not customer IAM.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import quote

import yaml

# Importing the two local helpers must not create untracked .pyc files before
# signed_revision checks the exact checkout. Also protects a later cleanup run
# that invokes the script without a PYTHONDONTWRITEBYTECODE environment.
sys.dont_write_bytecode = True

from apps_native_fixture_scope import (PROJECTS, require_same_identity,
                                       scope_identity)
from apps_native_support import (APIProxy, Commands, Kube, RUN_LABEL,
                                 UnixHTTPConnection, private_file, ready, require)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_SHA = "5f2b663b6c5e039c4c7b666ef3e959803851b675"
OPERATOR_MIN_SHA = "247c24738334b14d65cfcc904b193d967485e589"
SERVICE_NAME = "compute-miloapis-com"
SERVICE_CANONICAL = "compute.miloapis.com"
CONFIG_NAME = "compute-miloapis-com-a1t-v1"
AVAILABILITY_NAME = SERVICE_NAME + "--dfw"
LOCATION = "dfw"
LOCATION_CLASS = "datum-managed"
PROVIDER = "apps-a1t-provider"
CONSUMER = "apps-a1t-consumer-a"
CONTROL = "apps-a1t-consumer-b"
SERVICES_API = "/apis/services.miloapis.com/v1alpha1"
LOCATIONS_API = "/apis/locations.miloapis.com/v1alpha1"
NETWORKING_API = "/apis/networking.datumapis.com/v1alpha"
PROJECTS_API = "/apis/resourcemanager.miloapis.com/v1alpha1/projects"
SERVICE_CONSUMER = "sc-" + hashlib.sha256(
    (SERVICE_CANONICAL + "/" + CONSUMER).encode()).hexdigest()[:16]
PATHS = {
    "service": SERVICES_API + "/services/" + SERVICE_NAME,
    "configuration": SERVICES_API + "/serviceconfigurations/" + CONFIG_NAME,
    "availability": SERVICES_API + "/serviceavailabilities/" + AVAILABILITY_NAME,
    "entitlement": "",  # filled from the consumer Project prefix below
}


def project_prefix(name: str) -> str:
    require(name in PROJECTS, "unreviewed Project path")
    return PROJECTS_API + "/" + quote(name, safe="") + "/control-plane"


PATHS["entitlement"] = project_prefix(CONSUMER) + SERVICES_API + "/serviceentitlements/" + SERVICE_NAME
CHILDREN = {
    "consumer": project_prefix(PROVIDER) + SERVICES_API + "/serviceconsumers/" + SERVICE_CONSUMER,
    "binding": project_prefix(CONSUMER) + NETWORKING_API + "/locationbindings/" + LOCATION,
    "project_location": project_prefix(CONSUMER) + LOCATIONS_API + "/locations/" + LOCATION,
    "mirror": project_prefix(CONSUMER) + SERVICES_API + "/serviceavailabilities/" + AVAILABILITY_NAME,
}
CONTROL_PATHS = {
    "entitlement": project_prefix(CONTROL) + SERVICES_API + "/serviceentitlements/" + SERVICE_NAME,
    "binding": project_prefix(CONTROL) + NETWORKING_API + "/locationbindings/" + LOCATION,
    "project_location": project_prefix(CONTROL) + LOCATIONS_API + "/locations/" + LOCATION,
    "mirror": project_prefix(CONTROL) + SERVICES_API + "/serviceavailabilities/" + AVAILABILITY_NAME,
}
FINALIZERS = {
    "service": {"services.miloapis.com/service"},
    "configuration": {"services.miloapis.com/serviceconfiguration-protection"},
    "availability": set(),
    "entitlement": {"services.miloapis.com/service-entitlement"},
}
WEBHOOK_NAMES = {
    "services-mutating": {"mservice.kb.io", "mserviceactivationrequest.kb.io"},
    "services-validating": {"vservice.kb.io", "vserviceactivationrequest.kb.io",
                            "vserviceavailability.kb.io", "vserviceconfiguration.kb.io",
                            "vserviceconsumer.kb.io", "vserviceentitlement.kb.io"},
}


def _operator_module():
    source = ROOT / "hack" / "bootstrap-apps-native-catalog-operator.py"
    spec = importlib.util.spec_from_file_location("apps_catalog_operator_fixture", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signed_revision(commands: Commands) -> str:
    require(ROOT.is_dir() and not ROOT.is_symlink(), "isolated source required")
    revision = commands.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) and
            not commands.run("git", "-C", str(ROOT), "status", "--porcelain",
                             "--untracked-files=all"), "clean exact source required")
    commands.run("git", "-C", str(ROOT), "verify-commit", "HEAD", sensitive=True)
    commands.run("git", "-C", str(ROOT), "merge-base", "--is-ancestor",
                 OPERATOR_MIN_SHA, revision)
    return revision


def source_contract(commands: Commands, catalog: Path) -> None:
    require(catalog.is_dir() and not catalog.is_symlink(), "clean Catalog checkout required")
    require(commands.run("git", "-C", str(catalog), "rev-parse", "HEAD") == CATALOG_SHA and
            not commands.run("git", "-C", str(catalog), "status", "--porcelain",
                             "--untracked-files=all"), "Catalog source differs from exact commit")
    service = yaml.safe_load((catalog / "config/samples/services_v1alpha1_service.yaml").read_text())
    availability = yaml.safe_load((catalog / "config/samples/services_v1alpha1_serviceavailability.yaml").read_text())
    require(service["kind"] == "Service" and service["metadata"]["name"] == SERVICE_NAME and
            service["spec"]["serviceName"] == SERVICE_CANONICAL and
            service["spec"]["phase"] == "Published" and
            availability["kind"] == "ServiceAvailability" and
            availability["spec"]["serviceRef"]["name"] == SERVICE_NAME,
            "pinned Catalog Service or Availability sample changed")
    code = (catalog / "internal/controller/locationbinding_controller.go").read_text()
    require("sc.Spec.Locations.SupportedClasses" in code and
            "ConditionTypeAvailable" in code and
            'Version: "v1alpha"' in code,
            "pinned Catalog LocationBinding projection contract changed")
    crd = yaml.safe_load((catalog / "config/base/crd/bases/services.miloapis.com_serviceconfigurations.yaml").read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    locations = schema["properties"]["spec"]["properties"]["locations"]["properties"]
    require("supportedClasses" in locations and
            locations["supportedClasses"].get("type") == "array" and
            locations["supportedClasses"].get("items", {}).get("type") == "string",
            "pinned Catalog ServiceConfiguration Location class schema changed")
    webhook_docs = list(yaml.safe_load_all((catalog /
        "config/base/webhook/manifests.yaml").read_text()))
    require(len(webhook_docs) == 2 and
            {doc.get("kind"): {item.get("name") for item in doc.get("webhooks", [])}
             for doc in webhook_docs} == {
                "MutatingWebhookConfiguration": WEBHOOK_NAMES["services-mutating"],
                "ValidatingWebhookConfiguration": WEBHOOK_NAMES["services-validating"]},
            "pinned Catalog webhook names changed")


def manifests(catalog: Path, run_id: str) -> dict[str, dict]:
    source_service = yaml.safe_load((catalog / "config/samples/services_v1alpha1_service.yaml").read_text())
    source_availability = yaml.safe_load((catalog / "config/samples/services_v1alpha1_serviceavailability.yaml").read_text())
    source_entitlement = yaml.safe_load((catalog / "test/e2e-milo/service-provisioning/04-create-entitlement.yaml").read_text())
    service = {"apiVersion": SERVICES_API.split("/apis/")[1], "kind": "Service",
               "metadata": {"name": SERVICE_NAME, "labels": {RUN_LABEL: run_id}},
               "spec": {**{key: value for key, value in source_service["spec"].items()
                            if key != "owner"},
                        "owner": {"producerProjectRef": {"name": PROVIDER}}}}
    configuration = {"apiVersion": "services.miloapis.com/v1alpha1",
                     "kind": "ServiceConfiguration",
                     "metadata": {"name": CONFIG_NAME, "labels": {RUN_LABEL: run_id}},
                     "spec": {"serviceRef": {"name": SERVICE_NAME},
                              "phase": "Published",
                              "locations": {"supportedClasses": [LOCATION_CLASS]}}}
    availability = {"apiVersion": "services.miloapis.com/v1alpha1",
                    "kind": "ServiceAvailability",
                    "metadata": {"name": AVAILABILITY_NAME,
                                 "labels": {RUN_LABEL: run_id}},
                    "spec": {"serviceRef": source_availability["spec"]["serviceRef"],
                             "locationRef": {"name": LOCATION}}}
    entitlement = {"apiVersion": source_entitlement["apiVersion"],
                   "kind": source_entitlement["kind"],
                   "metadata": {"name": SERVICE_NAME,
                                "labels": {RUN_LABEL: run_id}},
                   "spec": {"serviceRef": {"name": SERVICE_NAME}}}
    require(service["spec"]["serviceName"] == SERVICE_CANONICAL and
            source_entitlement["spec"].get("serviceRef", {}).get("name"),
            "source Service or Entitlement contract changed")
    return {"service": service, "configuration": configuration,
            "availability": availability, "entitlement": entitlement}


def request(proxy: APIProxy, method: str, path: str,
            body: dict | None = None) -> tuple[int, dict | None]:
    require(method in ("GET", "POST") and path.startswith("/apis/") and
            len(path) < 1024, "unreviewed Milo API request")
    conn = UnixHTTPConnection(proxy.path)
    try:
        payload = json.dumps(body, separators=(",", ":")) if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        response = conn.getresponse()
        raw = response.read(131073)
        require(len(raw) <= 131072, "Milo API response exceeds bounded size")
        return response.status, json.loads(raw) if raw else None
    finally:
        conn.close()


def get_optional(proxy: APIProxy, path: str) -> dict | None:
    end = time.monotonic() + 15
    if proxy.kube.commands.deadline is not None:
        end = min(end, proxy.kube.commands.deadline)
    while True:
        status, obj = request(proxy, "GET", path)
        if status == 404:
            return None
        if status == 200:
            require(isinstance(obj, dict), "Milo API read returned nonobject JSON")
            return obj
        require(status == 429 and time.monotonic() < end,
                "Milo API read returned unexpected or persistent status")
        time.sleep(1)


def condition(obj: dict, kind: str) -> bool:
    meta = obj.get("metadata", {})
    generation = meta.get("generation", 0)
    return isinstance(generation, int) and generation > 0 and any(
        item.get("type") == kind and item.get("status") == "True" and
        item.get("observedGeneration") == generation
        for item in obj.get("status", {}).get("conditions", []))


def binding_available(obj: dict) -> bool:
    # This source deliberately omits observedGeneration from its unstructured
    # LocationBinding condition. Do not invent a matching generation.
    return any(c.get("type") == "Available" and c.get("status") == "True"
               for c in obj.get("status", {}).get("conditions", []))


def exact_created(key: str, obj: dict, expected: dict, uid: str | None,
                  *, deleting: bool = False) -> str:
    meta = obj.get("metadata", {})
    want = expected["metadata"]
    finalizers = set(meta.get("finalizers") or [])
    require(key in PATHS, "unsupported Catalog fixture identity")
    require(obj.get("apiVersion") == expected["apiVersion"] and
            obj.get("kind") == expected["kind"] and
            meta.get("name") == want["name"] and
            meta.get("labels") == want["labels"] and
            not meta.get("ownerReferences") and
            finalizers <= FINALIZERS[key] and
            (deleting or not meta.get("deletionTimestamp")) and
            obj.get("spec") == expected["spec"] and
            isinstance(meta.get("uid"), str) and bool(meta["uid"]) and
            (uid is None or uid == meta["uid"]),
            "source-owned Catalog fixture object or UID changed")
    return meta["uid"]


def save(path: Path, evidence: dict) -> None:
    _operator_module().atomic_json(path, evidence)


def wait_until(commands: Commands, seconds: float, label: str, predicate):
    end = time.monotonic() + seconds
    if commands.deadline is not None:
        end = min(end, commands.deadline)
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise RuntimeError("timed out waiting for " + label)


def webhook_matches(hook: dict, name: str, uid: str, ca_sha256: str) -> bool:
    entries = hook.get("webhooks", [])
    return (name in WEBHOOK_NAMES and
            hook.get("metadata", {}).get("uid") == uid and
            isinstance(entries, list) and len(entries) == len(WEBHOOK_NAMES[name]) and
            {entry.get("name") for entry in entries} == WEBHOOK_NAMES[name] and
            all(hashlib.sha256(base64.b64decode(
                entry.get("clientConfig", {}).get("caBundle", ""),
                validate=True)).hexdigest() == ca_sha256 for entry in entries))


def operator_live(commands: Commands, host: Kube, milo: Kube,
                  identity: dict, path: Path, catalog: Path) -> dict:
    private_file(path)
    previous = json.loads(path.read_text())
    revision = previous.get("source_revision")
    require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) and
            previous.get("complete") is True and
            previous.get("cleanup") == "not-requested" and
            previous.get("scope_identity") == identity and
            previous.get("catalog_source") == CATALOG_SHA and
            previous.get("namespace_reuse", {}).get("uid") and
            previous.get("manager", {}).get("image_id") ==
                "ghcr.io/milo-os/service-catalog@" + previous.get("image_index", ""),
            "Catalog operator prior success, source or scope changed")
    commands.run("git", "-C", str(ROOT), "verify-commit", revision, sensitive=True)
    commands.run("git", "-C", str(ROOT), "merge-base", "--is-ancestor",
                 OPERATOR_MIN_SHA, revision)
    manager = host.json("-n", "services-system", "get", "deployment",
                        "services-controller-manager")
    manager_status = manager.get("status", {})
    require(manager["metadata"].get("uid") == previous["manager"]["deployment_uid"] and
            manager_status.get("observedGeneration") == manager["metadata"].get("generation") and
            manager_status.get("readyReplicas") == manager["spec"].get("replicas") == 1 and
            manager["spec"]["template"]["spec"]["containers"][0]["image"] ==
                "ghcr.io/milo-os/service-catalog@" + previous["image_index"],
            "Catalog manager no longer matches exact pinned Ready Deployment")
    pods = host.json("-n", "services-system", "get", "pod", "-l",
                     "control-plane=services").get("items", [])
    require(len(pods) == 1 and
            pods[0]["metadata"].get("uid") == previous["manager"]["pod_uid"] and
            any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in pods[0].get("status", {}).get("conditions", [])) and
            pods[0]["status"]["containerStatuses"][0].get("imageID") ==
                previous["manager"]["image_id"],
            "Catalog manager Pod UID/readiness/image changed")
    for key, name in (("ca_stage", "apps-a1t-services-ca"),
                      ("host_stage", "apps-a1t-services-host")):
        stage = host.json("-n", "flux-system", "get", "kustomization", name)
        old = previous["flux_stages"][key]
        require(stage["metadata"].get("uid") == old["uid"] and ready(stage) and
                stage.get("status", {}).get("lastAppliedRevision") == old["revision"],
                "Catalog operator retained Flux stage changed")
    rendered = list(yaml.safe_load_all(commands.run(
        "kustomize", "build", str(catalog / "config/base/webhook"))))
    ca_bundle = previous.get("api_flux_state", {}).get("ca_bundle")
    require(isinstance(ca_bundle, str) and bool(ca_bundle) and
            hashlib.sha256(base64.b64decode(ca_bundle, validate=True)).hexdigest() ==
                previous["public_ca_sha256"],
            "Catalog operator public webhook CA journal changed")
    observed = _operator_module().baseline_webhooks(
        milo, rendered, previous["webhook_uids"], ca_bundle)
    for kind, name in (("mutatingwebhookconfiguration", "services-mutating"),
                       ("validatingwebhookconfiguration", "services-validating")):
        require(webhook_matches(milo.json("get", kind, name), name,
                                observed[name], previous["public_ca_sha256"]),
                "Catalog webhook names, count, UID or CA changed")
    return {"run_id": previous["run_id"], "source_revision": revision,
            "manager_uid": previous["manager"]["deployment_uid"],
            "webhook_uids": previous["webhook_uids"]}


def observed_location(proxy: APIProxy, expected_uid: str) -> dict:
    loc = get_optional(proxy, LOCATIONS_API + "/locations/" + LOCATION)
    require(loc is not None and loc.get("metadata", {}).get("uid") == expected_uid and
            loc.get("spec", {}).get("locationClassRef", {}).get("name") ==
                LOCATION_CLASS and condition(loc, "Ready") and
            loc.get("spec", {}).get("topology", {}).get(
                "topology.datum.net/city-code") == "DFW",
            "source-produced DFW Location UID, class, topology or Ready changed")
    return loc


def list_items(proxy: APIProxy, path: str, kind: str) -> list[dict]:
    obj = get_optional(proxy, path)
    require(obj is not None and obj.get("kind") == kind and
            isinstance(obj.get("items"), list), "project API list unavailable")
    return obj["items"]


def absence(proxy: APIProxy) -> None:
    for path, kind in ((SERVICES_API + "/serviceavailabilities", "ServiceAvailabilityList"),
                       (project_prefix(PROVIDER) + SERVICES_API + "/serviceconsumers",
                        "ServiceConsumerList"),
                       (project_prefix(CONSUMER) + SERVICES_API + "/serviceentitlements",
                        "ServiceEntitlementList"),
                       (project_prefix(CONSUMER) + NETWORKING_API + "/locationbindings",
                        "LocationBindingList"),
                       (project_prefix(CONSUMER) + LOCATIONS_API + "/locations",
                        "LocationList"),
                       (project_prefix(CONSUMER) + SERVICES_API + "/serviceavailabilities",
                        "ServiceAvailabilityList")):
        list_items(proxy, path, kind)
    for path in (*PATHS.values(), *CHILDREN.values(), *CONTROL_PATHS.values()):
        require(get_optional(proxy, path) is None,
                "Catalog fixture object collision before create")
    services = list_items(proxy, SERVICES_API + "/services", "ServiceList")
    require(not any(s.get("spec", {}).get("serviceName") == SERVICE_CANONICAL
                    for s in services), "canonical Compute Service already exists")
    configurations = list_items(proxy, SERVICES_API + "/serviceconfigurations",
                                "ServiceConfigurationList")
    require(not any(x.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME
                    for x in configurations), "Compute ServiceConfiguration already exists")
    require(not any(x.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME
                    for x in list_items(proxy, SERVICES_API + "/serviceavailabilities",
                                        "ServiceAvailabilityList")),
            "Compute ServiceAvailability already exists")
    require(not any(x.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME
                    for x in list_items(proxy, project_prefix(PROVIDER) + SERVICES_API +
                                        "/serviceconsumers", "ServiceConsumerList")),
            "provider already contains Compute ServiceConsumer")
    for project in (CONSUMER, CONTROL):
        items = list_items(proxy, project_prefix(project) + SERVICES_API +
                           "/serviceentitlements", "ServiceEntitlementList")
        require(not any(x.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME
                        for x in items), "Project already has Compute entitlement")


def create_one(proxy: APIProxy, key: str, manifest: dict,
               evidence: dict, path: Path) -> dict:
    require(key in PATHS and key not in evidence["attempted"],
            "duplicate Catalog fixture create intent")
    collection = PATHS[key].rsplit("/", 1)[0]
    evidence["attempted"][key] = None
    evidence["intents"][key] = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    save(path, evidence)
    status, obj = request(proxy, "POST", collection, manifest)
    require(status == 201 and isinstance(obj, dict),
            "Catalog fixture public API CREATE did not return HTTP 201")
    evidence["attempted"][key] = exact_created(key, obj, manifest, None)
    save(path, evidence)
    return obj


def wait_owned(proxy: APIProxy, commands: Commands, key: str,
               expected: dict, evidence: dict, *, seconds: float = 90,
               statuses: tuple[str, ...] = ()) -> dict:
    def observed():
        obj = get_optional(proxy, PATHS[key])
        if obj is None:
            return False
        exact_created(key, obj, expected, evidence["attempted"][key])
        if statuses and not all(condition(obj, name) for name in statuses):
            return False
        return obj
    return wait_until(commands, seconds, key + " controller status", observed)


def children_ready(proxy: APIProxy, commands: Commands,
                   evidence: dict, source_location: dict) -> dict:
    def observed():
        found = {key: get_optional(proxy, path) for key, path in CHILDREN.items()}
        if not all(found.values()):
            return False
        consumer, binding = found["consumer"], found["binding"]
        mirror, location = found["mirror"], found["project_location"]
        for key, item in found.items():
            child_source_shape(key, item, source_location)
        require(consumer.get("apiVersion") == "services.miloapis.com/v1alpha1" and
                consumer.get("kind") == "ServiceConsumer" and
                consumer.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME and
                consumer.get("spec", {}).get("consumerProjectRef", {}).get("name") == CONSUMER and
                consumer.get("metadata", {}).get("name") == SERVICE_CONSUMER and
                not consumer.get("metadata", {}).get("ownerReferences"),
                "provider ServiceConsumer identity differs from source controller contract")
        require(binding.get("apiVersion") == "networking.datumapis.com/v1alpha" and
                binding.get("kind") == "LocationBinding" and
                binding.get("metadata", {}).get("name") == LOCATION and
                binding.get("spec", {}).get("locationRef", {}).get("name") == LOCATION and
                binding.get("spec", {}).get("locationClassName") == LOCATION_CLASS and
                binding.get("spec", {}).get("topology") ==
                    source_location["spec"]["topology"],
                "project LocationBinding differs from genuine DFW source")
        require(mirror.get("apiVersion") == "services.miloapis.com/v1alpha1" and
                mirror.get("kind") == "ServiceAvailability" and
                mirror.get("metadata", {}).get("name") == AVAILABILITY_NAME and
                mirror.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME and
                mirror.get("spec", {}).get("locationRef", {}).get("name") == LOCATION and
                location.get("apiVersion") == "locations.miloapis.com/v1alpha1" and
                location.get("kind") == "Location" and
                location.get("metadata", {}).get("name") == LOCATION and
                location.get("spec", {}).get("locationClassRef", {}).get("name") ==
                    LOCATION_CLASS and
                location.get("spec", {}).get("topology") ==
                    source_location["spec"]["topology"],
                "consumer Location or ServiceAvailability projection differs")
        if (consumer.get("status", {}).get("phase") != "Active" or
                not condition(consumer, "Ready") or
                not binding_available(binding) or
                not condition(mirror, "Available") or
                not any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in location.get("status", {}).get("conditions", []))):
            return False
        require(binding.get("status", {}).get("conditions") and
                all(x.get("metadata", {}).get("uid") and
                    not x["metadata"].get("deletionTimestamp") for x in found.values()),
                "Catalog projected object UID/lifecycle missing")
        return found
    return wait_until(commands, 180, "provider consumer and location projection", observed)


def child_source_shape(key: str, obj: dict, source_location: dict) -> str:
    meta = obj.get("metadata", {})
    require(key in CHILDREN and meta.get("name") == CHILDREN[key].rsplit("/", 1)[1] and
            isinstance(meta.get("uid"), str) and bool(meta["uid"]) and
            not meta.get("ownerReferences") and not meta.get("annotations") and
            not meta.get("finalizers") and
            not meta.get("generateName"),
            "native Catalog child has unknown identity or metadata")
    expected_gvk = {"consumer": ("services.miloapis.com/v1alpha1", "ServiceConsumer"),
                    "binding": ("networking.datumapis.com/v1alpha", "LocationBinding"),
                    "project_location": ("locations.miloapis.com/v1alpha1", "Location"),
                    "mirror": ("services.miloapis.com/v1alpha1", "ServiceAvailability")}
    require((obj.get("apiVersion"), obj.get("kind")) == expected_gvk[key],
            "native Catalog child GVK changed")
    spec, labels = obj.get("spec", {}), meta.get("labels", {})
    if key == "consumer":
        require(spec == {"serviceRef": {"name": SERVICE_NAME},
                         "consumerProjectRef": {"name": CONSUMER}} and
                labels in (None, {}),
                "native provider ServiceConsumer was replaced or changed")
    else:
        expected_spec = ({"locationRef": {"name": LOCATION},
                          "locationClassName": LOCATION_CLASS,
                          "displayName": source_location.get("spec", {}).get("displayName"),
                          "topology": source_location["spec"]["topology"]}
                         if key == "binding" else
                         ({"locationClassRef": {"name": LOCATION_CLASS},
                           "topology": source_location["spec"]["topology"],
                           **({"coordinates": source_location["spec"]["coordinates"]}
                              if "coordinates" in source_location["spec"] else {})}
                          if key == "project_location" else
                          {"serviceRef": {"name": SERVICE_NAME},
                           "locationRef": {"name": LOCATION}}))
        if expected_spec.get("displayName") is None:
            expected_spec.pop("displayName", None)
        expected_labels = ({"app.kubernetes.io/managed-by": "services.miloapis.com"}
                           if key == "mirror" else
                           {"app.kubernetes.io/managed-by": "services.miloapis.com",
                            "networking.datumapis.com/location": LOCATION,
                            "networking.datumapis.com/class": LOCATION_CLASS})
        require(spec == expected_spec and labels == expected_labels,
                "native Catalog location projection differs from source")
    return meta["uid"]


def control_absent(proxy: APIProxy) -> None:
    # A named 404 alone could be an unavailable Project route. Every list must
    # answer 200 before a negative cross-Project claim is recorded.
    for resource, kind in (("serviceentitlements", "ServiceEntitlementList"),
                           ("locationbindings", "LocationBindingList"),
                           ("locations", "LocationList"),
                           ("serviceavailabilities", "ServiceAvailabilityList")):
        group = NETWORKING_API if resource == "locationbindings" else (
            LOCATIONS_API if resource == "locations" else SERVICES_API)
        items = list_items(proxy, project_prefix(CONTROL) + group + "/" + resource, kind)
        if resource in ("serviceentitlements", "serviceavailabilities"):
            require(not any(x.get("spec", {}).get("serviceRef", {}).get("name") ==
                            SERVICE_NAME for x in items),
                    "unentitled control Project has Compute service access")
    require(all(get_optional(proxy, path) is None for path in CONTROL_PATHS.values()),
            "unentitled control Project contains a Catalog projection")


def no_foreign_compute_dependents(proxy: APIProxy, evidence: dict) -> None:
    consumer_uid = evidence.get("children", {}).get("consumer")
    for obj in list_items(proxy, project_prefix(PROVIDER) + SERVICES_API +
                          "/serviceconsumers", "ServiceConsumerList"):
        if obj.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME:
            require(obj.get("metadata", {}).get("name") == SERVICE_CONSUMER and
                    (consumer_uid is None or obj.get("metadata", {}).get("uid") == consumer_uid),
                    "foreign provider Compute ServiceConsumer prevents cleanup")
    for project in (CONSUMER, CONTROL):
        for obj in list_items(proxy, project_prefix(project) + SERVICES_API +
                              "/serviceentitlements", "ServiceEntitlementList"):
            if obj.get("spec", {}).get("serviceRef", {}).get("name") == SERVICE_NAME:
                require(project == CONSUMER and
                        obj.get("metadata", {}).get("name") == SERVICE_NAME and
                        obj.get("metadata", {}).get("uid") ==
                            evidence.get("attempted", {}).get("entitlement"),
                        "foreign Compute entitlement prevents cleanup")


def location_dependency(path: Path, identity: dict, proxy: APIProxy) -> tuple[dict, dict]:
    private_file(path)
    prior = json.loads(path.read_text())
    require(prior.get("scope_identity") == identity and
            prior.get("cleanup") == "not-requested" and
            prior.get("setup_stage") == "ready" and
            re.fullmatch(r"[0-9a-f]{24}", str(prior.get("run_id"))) and
            re.fullmatch(r"[0-9a-f-]{36}", str(prior.get("attempted", {}).get("location"))),
            "retained Locations source/run/UID journal differs")
    loc = observed_location(proxy, prior["attempted"]["location"])
    require(loc.get("metadata", {}).get("labels", {}).get(RUN_LABEL) == prior["run_id"],
            "retained Location run label changed")
    return prior, loc


def owned_for_delete(proxy: APIProxy, key: str, manifest: dict,
                     evidence: dict) -> dict | None:
    observed = get_optional(proxy, PATHS[key])
    if observed is None:
        return None
    uid = evidence.get("attempted", {}).get(key)
    # A CREATE may have succeeded without a response. Recovery is limited to
    # the exact prior intent and the observed run-labelled object.
    require(evidence.get("intents", {}).get(key) == hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "Catalog cleanup create intent differs")
    exact_created(key, observed, manifest, uid, deleting=True)
    require(not observed.get("metadata", {}).get("annotations") and
            not observed.get("metadata", {}).get("generateName"),
            "Catalog cleanup object has unreviewed metadata")
    return observed


def delete_one(proxy: APIProxy, commands: Commands, key: str,
               manifest: dict, evidence: dict, journal: Path) -> None:
    if key not in evidence.get("attempted", {}):
        return
    observed = owned_for_delete(proxy, key, manifest, evidence)
    if observed is None:
        # A lost CREATE reply followed by a 404 does not establish whether the
        # server may still commit the request. Only our known UID plus a
        # persisted DELETE intent can safely settle an absent object.
        require(evidence["attempted"][key] is not None and
                evidence.get("delete_intent", {}).get(key) is True,
                "Catalog object absent without a known UID and our delete intent")
        evidence.setdefault("removed", {})[key] = evidence["attempted"][key]
        save(journal, evidence)
        return
    uid = observed["metadata"]["uid"]
    if evidence["attempted"][key] is None:
        evidence["attempted"][key] = uid
        save(journal, evidence)
    require(not observed["metadata"].get("deletionTimestamp") or
            evidence.get("delete_intent", {}).get(key) is True,
            "owned Catalog object is terminating without our delete intent")
    if not evidence.setdefault("delete_intent", {}).get(key):
        evidence["delete_intent"][key] = True
        save(journal, evidence)
    if not observed["metadata"].get("deletionTimestamp"):
        rv = observed["metadata"].get("resourceVersion")
        require(isinstance(rv, str) and bool(rv), "owned Catalog object lacks resourceVersion")
        proxy.delete(PATHS[key], uid, rv)

    def gone():
        current = get_optional(proxy, PATHS[key])
        if current is None:
            return True
        exact_created(key, current, manifest, uid, deleting=True)
        return False
    wait_until(commands, 120, key + " native finalizer cleanup", gone)
    evidence["removed"][key] = uid
    save(journal, evidence)


def checked_children_gone(proxy: APIProxy, commands: Commands,
                          evidence: dict, source_location: dict,
                          journal: Path) -> None:
    expected = evidence.get("children", {})
    def gone():
        for key, path in CHILDREN.items():
            current = get_optional(proxy, path)
            if current is None:
                continue
            uid = child_source_shape(key, current, source_location)
            require(expected.get(key) in (None, uid),
                    "native Catalog child was replaced during cleanup")
            if expected.get(key) is None:
                expected[key] = uid
                save(journal, evidence)
            return False
        return True
    wait_until(commands, 150, "native ServiceConsumer and location projection cleanup", gone)


def child_census(proxy: APIProxy, evidence: dict, source_location: dict,
                 journal: Path) -> None:
    known = evidence.setdefault("children", {})
    for key, path in CHILDREN.items():
        current = get_optional(proxy, path)
        if current is None:
            continue
        uid = child_source_shape(key, current, source_location)
        require(known.get(key) in (None, uid),
                "native Catalog child UID changed before cleanup")
        if known.get(key) is None:
            known[key] = uid
            save(journal, evidence)


def cleanup(proxy: APIProxy, commands: Commands, manifests_by_key: dict,
            evidence: dict, journal: Path, source_location: dict) -> None:
    require(evidence.get("complete") in (True, False) and
            evidence.get("cleanup") in ("not-requested", "incomplete"),
            "Catalog fixture journal is not an active cleanup target")
    control_absent(proxy)
    if ("entitlement" in evidence.get("attempted", {}) and
            evidence["attempted"]["entitlement"] is None):
        recovered = owned_for_delete(proxy, "entitlement",
                                     manifests_by_key["entitlement"], evidence)
        require(recovered is not None,
                "unknown-UID entitlement CREATE outcome remains ambiguous")
        evidence["attempted"]["entitlement"] = recovered["metadata"]["uid"]
        save(journal, evidence)
    no_foreign_compute_dependents(proxy, evidence)
    child_census(proxy, evidence, source_location, journal)
    evidence["cleanup"] = "incomplete"
    save(journal, evidence)
    # An entitlement finalizer must finish provider deprovisioning before any
    # source Service or root Availability is removed. Never force finalizers.
    delete_one(proxy, commands, "entitlement", manifests_by_key["entitlement"],
               evidence, journal)
    if "entitlement" in evidence["attempted"]:
        checked_children_gone(proxy, commands, evidence, source_location, journal)
    else:
        require(all(get_optional(proxy, path) is None for path in CHILDREN.values()),
                "unexpected native Catalog child before source cleanup")
    for key in ("availability", "configuration", "service"):
        delete_one(proxy, commands, key, manifests_by_key[key], evidence, journal)
    control_absent(proxy)
    evidence["cleanup"] = "complete"
    save(journal, evidence)


def run(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    commands = Commands()
    commands.deadline = time.monotonic() + args.deadline_seconds
    host = Kube(commands, args.host_kubeconfig, args.host_context)
    milo = Kube(commands, args.milo_kubeconfig, "milo-root")
    require(args.evidence_file.is_absolute() and args.evidence_file.parent.is_dir() and
            args.evidence_file.parent.stat().st_mode & 0o077 == 0,
            "private absolute Catalog fixture journal directory required")
    require(args.execute or not args.cleanup, "cleanup requires --execute")
    for file in (args.host_kubeconfig, args.milo_kubeconfig):
        private_file(file)
    revision = signed_revision(commands)
    source_contract(commands, args.catalog_source)
    identity = scope_identity(host, milo, args.host_context, args.milo_port)
    operator = operator_live(commands, host, milo, identity,
                             args.catalog_operator_evidence_file,
                             args.catalog_source)
    with contextlib.closing(APIProxy(milo, args.evidence_file.parent,
                                     "apps-native-service")) as proxy:
        proxy.start()
        locations, location = location_dependency(args.locations_evidence_file,
                                                  identity, proxy)
        if args.cleanup:
            private_file(args.evidence_file)
            evidence = json.loads(args.evidence_file.read_text())
            require_same_identity(evidence.get("scope_identity"), identity)
            require(evidence.get("source_revision") == revision and
                    evidence.get("catalog_source") == CATALOG_SHA and
                    evidence.get("operator_run_id") == operator["run_id"] and
                    evidence.get("locations_run_id") == locations["run_id"],
                    "Catalog fixture cleanup revision or dependency changed")
            fixture = manifests(args.catalog_source, evidence["run_id"])
            commands.deadline = time.monotonic() + min(args.deadline_seconds, 300)
            cleanup_started = time.monotonic()
            cleanup(proxy, commands, fixture, evidence, args.evidence_file,
                    location)
            evidence["cleanup_seconds"] = round(time.monotonic() - cleanup_started, 3)
            save(args.evidence_file, evidence)
            return evidence
        require(not args.evidence_file.exists(), "Catalog fixture journal already exists")
        absence(proxy)
        control_absent(proxy)
        if not args.execute:
            return {"preflight": "passed", "source_revision": revision,
                    "operator_run_id": operator["run_id"],
                    "locations_run_id": locations["run_id"]}
        run_id = secrets.token_hex(12)
        fixture = manifests(args.catalog_source, run_id)
        evidence = {"version": 1, "run_id": run_id,
                    "source_revision": revision, "catalog_source": CATALOG_SHA,
                    "scope_identity": identity, "operator_run_id": operator["run_id"],
                    "locations_run_id": locations["run_id"],
                    "location_uid": location["metadata"]["uid"],
                    "attempted": {}, "intents": {}, "delete_intent": {},
                    "removed": {}, "children": {}, "complete": False,
                    "cleanup": "not-requested", "stage": "before-create"}
        save(args.evidence_file, evidence)
        try:
            create_one(proxy, "service", fixture["service"], evidence, args.evidence_file)
            evidence["stage"] = "service-controller"
            save(args.evidence_file, evidence)
            wait_owned(proxy, commands, "service", fixture["service"], evidence,
                       statuses=("Ready", "Published"))
            for key, statuses in (("configuration", ("Ready",)),
                                  ("availability", ("Available",))):
                evidence["stage"] = key + "-create"
                save(args.evidence_file, evidence)
                create_one(proxy, key, fixture[key], evidence, args.evidence_file)
                wait_owned(proxy, commands, key, fixture[key], evidence,
                           statuses=statuses)
            evidence["stage"] = "entitlement-create"
            save(args.evidence_file, evidence)
            create_one(proxy, "entitlement", fixture["entitlement"], evidence,
                       args.evidence_file)
            entitlement = wait_owned(proxy, commands, "entitlement",
                                     fixture["entitlement"], evidence,
                                     statuses=("Ready",))
            require(entitlement.get("status", {}).get("phase") == "Active",
                    "native entitlement did not become Active")
            found = children_ready(proxy, commands, evidence, location)
            evidence["children"] = {key: item["metadata"]["uid"]
                                    for key, item in found.items()}
            save(args.evidence_file, evidence)
            control_absent(proxy)
            evidence["stage"] = "ready"
            evidence["complete"] = True
            evidence["setup_seconds"] = round(time.monotonic() - started, 3)
            save(args.evidence_file, evidence)
        except Exception as error:
            # Never put exception bodies or public API response payloads in the
            # journal; they can reflect credentials from unrelated admission.
            evidence["failure_class"] = type(error).__name__
            save(args.evidence_file, evidence)
            try:
                commands.deadline = time.monotonic() + 300
                cleanup_started = time.monotonic()
                cleanup(proxy, commands, fixture, evidence, args.evidence_file,
                        location)
                evidence["cleanup_seconds"] = round(time.monotonic() - cleanup_started, 3)
                save(args.evidence_file, evidence)
            except Exception as cleanup_error:
                evidence["cleanup_failure_class"] = type(cleanup_error).__name__
                save(args.evidence_file, evidence)
            evidence["setup_seconds"] = round(time.monotonic() - started, 3)
            save(args.evidence_file, evidence)
            raise RuntimeError("Catalog fixture setup failed at " + evidence["stage"] +
                               "; cleanup=" + evidence["cleanup"]) from None
        return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-port", required=True, type=int)
    parser.add_argument("--catalog-operator-evidence-file", required=True, type=Path)
    parser.add_argument("--locations-evidence-file", required=True, type=Path)
    parser.add_argument("--catalog-source", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    require(1 <= args.milo_port <= 65535 and 120 <= args.deadline_seconds <= 1800,
            "bounded Milo port and deadline required")
    return args


if __name__ == "__main__":
    try:
        result = run(parse_args())
        print(json.dumps({key: result[key] for key in (
            "preflight", "source_revision", "operator_run_id", "locations_run_id",
            "run_id", "complete", "cleanup", "stage") if key in result},
            sort_keys=True))
    except Exception as exc:
        print("Catalog fixture failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
