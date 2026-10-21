"""Internal pending-only native trial extension; never accepts an installation.

This module cannot be invoked on its own. The original runner retains its
source, TLS, isolated fixture, publication, credentials and cleanup fences.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets
from pathlib import Path

import yaml

from apps_native_profiles import PENDING
from apps_native_support import RUN_LABEL, private_write, require, wait_for

SOURCE = "1bba80e01c02bffe49e1547b8e4d2ed67069c6cc"
REQUESTER = "apps-a1t-pending-requester"
GROUP = "apps-a1t-pending-source-readers"
STAGE = "apps-native-a1t-pending-api"
WC = "apps-internal-pending.apps.miloapis.com"
CRDS = ("apps.apps.miloapis.com", "apprevisions.apps.miloapis.com")
FINALIZER = "apps.miloapis.com/pending-request"
PREFIX = "apps.miloapis.com/request-"
INTENT_LABEL = "apps-a1t.datum.net/pending-intent"
ROLE_NAMES = ("apps-a1t-pending-controller", "apps-a1t-pending-installer", REQUESTER, GROUP)


def digest(value):
    # Fixture inputs are {} and the spec contains strings only; this is the
    # snapshot canonical form, without any float coercion or arbitrary input.
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()


def expected_rules():
    def rule(group, resources, verbs, names=None):
        out = {"apiGroups": [group], "resources": resources, "verbs": verbs}
        if names:
            out["resourceNames"] = names
        return out
    return {
        ROLE_NAMES[0]: [rule("apps.miloapis.com", ["apps"], ["get", "list", "watch", "update"]),
                       rule("apps.miloapis.com", ["apps/status"], ["update"]),
                       rule("authorization.k8s.io", ["subjectaccessreviews"], ["create"])],
        ROLE_NAMES[1]: [rule("apiextensions.k8s.io", ["customresourcedefinitions"],
                            ["get", "patch", "delete"], list(CRDS)),
                       rule("admissionregistration.k8s.io",
                            ["mutatingwebhookconfigurations", "validatingwebhookconfigurations"],
                            ["get", "patch", "delete"], [WC])],
        REQUESTER: [rule("apps.miloapis.com", ["apps"],
                         ["get", "list", "watch", "create", "update", "patch", "delete"]),
                    rule("apps.miloapis.com", ["apps/status"], ["update"]),
                    rule("apps.miloapis.com", ["apprevisions"],
                         ["get", "list", "create", "update", "delete"])],
        GROUP: [rule("apps.miloapis.com", ["apptemplates", "apptemplaterevisions"], ["get"])],
    }


def extend_contract(h, commands, source_path, fixtures, source):
    require(source["profile"] is PENDING, "pending contract requires exact internal profile")
    root = h.ROOT / "components/apps-native-pending"
    extra = {name: h.load_yaml(root / (name + ".yaml")) for name in ("host", "api", "milo-rbac")}
    rendered = h.render(commands, root)
    require(sorted(json.dumps(o, sort_keys=True) for o in rendered) ==
            sorted(json.dumps(o, sort_keys=True) for group in extra.values() for o in group),
            "pending fixture root includes unexpected resources")
    host = copy.deepcopy(fixtures["host"][0])
    host["spec"]["path"] = "./" + PENDING.host_root
    host["spec"]["images"] = [{"name": "ghcr.io/datum-labs/apps", "digest": PENDING.image.split("@")[1]}]
    host["spec"]["dependsOn"].append({"name": STAGE, "namespace": "milo-system"})
    require(extra["host"] == [host], "pending host Flux differs from source-owned exact profile")
    api = copy.deepcopy(fixtures["crds"][0])
    api["metadata"]["name"] = STAGE
    api["spec"]["path"] = "./config/experimental/pending-app-api"
    api["spec"]["healthChecks"] = [{"apiVersion": "apiextensions.k8s.io/v1",
                                     "kind": "CustomResourceDefinition", "name": n} for n in CRDS]
    require(extra["api"] == [api], "pending API Flux differs from scoped source stage")
    rules = expected_rules()
    subjects = dict(zip(ROLE_NAMES, (h.PROJECT, h.INSTALLER, REQUESTER, GROUP)))
    expected = []
    for name in ROLE_NAMES:
        expected.extend([
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
             "metadata": {"name": name}, "rules": rules[name]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
             "metadata": {"name": name}, "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
             "kind": "ClusterRole", "name": name}, "subjects": [{"apiGroup": "rbac.authorization.k8s.io",
             "kind": "Group" if name == GROUP else "User", "name": subjects[name]}]}])
    require(extra["milo-rbac"] == expected, "pending RBAC exceeds exact reviewed fixture privileges")
    objects = h.render(commands, source_path / "config/experimental/pending-app-api")
    crds = [o for o in objects if o["kind"] == "CustomResourceDefinition"]
    hooks = [o for o in objects if o["kind"].endswith("WebhookConfiguration")]
    require(len(objects) == 4 and {o["metadata"]["name"] for o in crds} == set(CRDS) and
            {(o["kind"], o["metadata"]["name"]) for o in hooks} == {
                ("MutatingWebhookConfiguration", WC), ("ValidatingWebhookConfiguration", WC)},
            "pending source must contain exactly two CRDs and two fail-closed WCs")
    paths = {"/mutate-pending-app", "/validate-pending-app", "/deny-app-revisions"}
    observed = []
    for obj in hooks:
        for hook in obj["webhooks"]:
            service = hook["clientConfig"].get("service", {})
            require(hook.get("failurePolicy") == "Fail" and hook.get("sideEffects") == "None" and
                    hook.get("admissionReviewVersions") == ["v1"] and hook.get("timeoutSeconds") == 5 and
                    not any(k in hook for k in ("namespaceSelector", "objectSelector", "matchConditions")) and
                    service == {"namespace": "apps-system", "name": "apps-controller", "port": 443,
                                "path": service.get("path")} and service.get("path") in paths and
                    not hook["clientConfig"].get("caBundle") and "url" not in hook["clientConfig"],
                    "pending source hook is not exact fail-closed service boundary")
            observed.append(service["path"])
            resource = "apprevisions" if service["path"] == "/deny-app-revisions" else "apps"
            expected_rules_ = [{"apiGroups": ["apps.miloapis.com"], "apiVersions": ["v1alpha1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"], "resources": [resource], "scope": "Cluster"}]
            if service["path"] == "/validate-pending-app":
                expected_rules_.append({"apiGroups": ["apps.miloapis.com"], "apiVersions": ["v1alpha1"],
                                        "operations": ["UPDATE"], "resources": ["apps/status"], "scope": "Cluster"})
            require(hook["rules"] == expected_rules_, "pending source hook write coverage changed")
    require(sorted(observed) == sorted(paths), "pending source hook coverage incomplete")
    for crd in crds:
        require(crd["spec"]["scope"] == "Cluster" and crd["spec"]["group"] == "apps.miloapis.com" and
                crd["metadata"]["annotations"]["discovery.miloapis.com/parent-contexts"] == "Project",
                "pending CRDs must be Project-scoped native resources")
    fixtures["host"] = extra["host"]
    fixtures["pending"] = extra
    source["pending"] = {"crds": crds, "admission": hooks}


def preflight(h, state):
    h.absent(state["host"], [("kustomization", STAGE, "milo-system")])
    h.absent(state["milo"], [("customresourcedefinition", n, None) for n in CRDS] +
             [(k, WC, None) for k in ("mutatingwebhookconfiguration", "validatingwebhookconfiguration")] +
             [(k, n, None) for k in ("clusterrole", "clusterrolebinding") for n in ROLE_NAMES])


def install_rbac(h, fixtures, state, refs):
    for obj in fixtures["pending"]["milo-rbac"]:
        if obj["kind"] == "ClusterRoleBinding" and obj["metadata"]["name"] in (REQUESTER, GROUP):
            continue
        h.register_owned(refs, "rbac", state["milo"], obj, None, state["run_id"],
                         "clusterroles" if obj["kind"] == "ClusterRole" else "clusterrolebindings",
                         "/apis/rbac.authorization.k8s.io/v1")


def bind(h, name, fixtures, state, refs):
    obj = next(o for o in fixtures["pending"]["milo-rbac"]
               if o["kind"] == "ClusterRoleBinding" and o["metadata"]["name"] == name)
    h.register_owned(refs, "pending-bindings", state["milo"], obj, None, state["run_id"],
                     "clusterrolebindings", "/apis/rbac.authorization.k8s.io/v1")


def check_crds(h, milo, source):
    out = {}
    for obj in source["pending"]["crds"]:
        name = obj["metadata"]["name"]
        live = milo.json("get", "customresourcedefinition", name)
        require(any(c.get("type") == "Established" and c.get("status") == "True"
                    for c in live.get("status", {}).get("conditions", [])), "pending CRD is not Established")
        for field in ("scope", "group", "names", "versions"):
            if field == "versions":
                # API defaults extra fields; compare every supplied schema and subresource exactly.
                require(len(live["spec"][field]) == len(obj["spec"][field]), "pending CRD versions changed")
                for expected, actual in zip(obj["spec"][field], live["spec"][field]):
                    require(all(actual.get(k) == v for k, v in expected.items()), "pending CRD schema changed")
            else:
                require(live["spec"][field] == obj["spec"][field], "pending CRD identity changed")
        require(live["metadata"].get("annotations", {}).get("discovery.miloapis.com/parent-contexts") == "Project",
                "pending CRD Project routing changed")
        out[name] = digest(obj["spec"])
    return out


def install_api(h, fixtures, source, state, refs, oci_digest):
    manifest = copy.deepcopy(fixtures["pending"]["api"][0])
    manifest["spec"]["commonMetadata"] = {"labels": {RUN_LABEL: state["run_id"]}}
    # Mark intent before any remote Flux apply. An incomplete census retains the
    # stage and capability chain; a partial apply is never treated as empty.
    state["pending_api_intent"] = True
    h.register_flux_owned(refs, "pending-api", state["host"], manifest, state["run_id"],
                          source["pending"]["crds"] + source["pending"]["admission"],
                          oci_digest, state["pending_evidence_file"])
    h.flux_reconcile(state["host"], "kustomization", STAGE, "milo-system", 300)
    h.exact_flux(state["host"], STAGE, "milo-system", oci_digest)
    return wait_for(lambda: check_crds(h, state["milo"], source), "exact pending CRDs Established", 120)


def enable_hooks(h, source, state, refs, app_ca, oci_digest):
    host = state["host"]
    ref = refs["pending-api"][0]
    ref.bind_webhook_ca(state["private"], app_ca)
    ca = base64.b64encode(app_ca).decode()
    h.flux_reconcile(host, "kustomization", STAGE, "milo-system", 300)
    h.exact_flux(host, STAGE, "milo-system", oci_digest)
    for obj in source["pending"]["admission"]:
        live = state["milo"].json("get", obj["kind"], WC)
        require(live["metadata"].get("labels", {}).get(RUN_LABEL) == state["run_id"] and
                len(live["webhooks"]) == len(obj["webhooks"]), "pending WC ownership/shape changed")
        for wanted, actual in zip(obj["webhooks"], live["webhooks"]):
            wanted = copy.deepcopy(wanted)
            wanted["clientConfig"]["caBundle"] = ca
            require(all(actual.get(k) == v for k, v in wanted.items()) and
                    not any(actual.get(k) for k in ("namespaceSelector", "objectSelector", "matchConditions")),
                    "pending hooks do not match source and verified CA")


def app_object(name, project_uid, template, revision):
    return {"apiVersion": "apps.miloapis.com/v1alpha1", "kind": "App", "metadata": {"name": name},
            "spec": {"templateRef": {"projectRevision": {"projectUID": project_uid,
                "templateName": template["metadata"]["name"], "templateUID": template["metadata"]["uid"],
                "revisionName": revision["metadata"]["name"], "revisionUID": revision["metadata"]["uid"],
                "digest": revision["spec"]["digest"]}}, "inputsJSON": "{}", "inputsDigest": digest({}),
                "uninstallPolicy": "Delete"}}


def annotations(spec, project_uid):
    return {PREFIX + "protocol": "pending-only-v1", PREFIX + "project-uid": project_uid,
            PREFIX + "actor-uid": REQUESTER, PREFIX + "spec-digest": digest(spec), PREFIX + "generation": "1"}


def match_intent(obj, intent, run_id):
    meta = obj.get("metadata", {})
    require(obj.get("apiVersion") == "apps.miloapis.com/v1alpha1" and obj.get("kind") == "App" and
            set(obj) <= {"apiVersion", "kind", "metadata", "spec", "status"} and meta.get("name") == intent["name"] and meta.get("uid") and meta.get("resourceVersion") and
            (not intent.get("uid") or meta["uid"] == intent["uid"]) and
            not meta.get("namespace") and not meta.get("ownerReferences") and
            meta.get("labels") == {RUN_LABEL: run_id, INTENT_LABEL: intent["nonce"]} and
            obj.get("spec") == intent["spec"] and
            meta.get("annotations") == annotations(intent["spec"], intent["project_uid"]) and
            set(meta.get("finalizers", [])) <= {FINALIZER} and
            (meta.get("deletionTimestamp") or meta.get("finalizers") == [FINALIZER]) and
            obj.get("status", {}) in ({}, {"projectUID": intent["project_uid"], "admittedGeneration": 1, "phase": "Pending"}),
            "unknown or changed pending App; retaining entire capability chain")
    return meta["uid"]


def census(h, proxy, state):
    actual = {o["metadata"]["name"]: o["metadata"]["uid"] for o in proxy.get(h.PROJECT_API)["items"]}
    require(actual == state["projects"], "Project set/UID changed during pending drain")
    found = []
    for project, uid in state["projects"].items():
        require(not h.resource_empty(proxy, project, "apprevisions"),
                "installation history present; retaining pending hooks/controller/credentials")
        for obj in h.resource_empty(proxy, project, "apps"):
            candidates = [i for i in state["pending_intents"] if i["project"] == project and
                          i["project_uid"] == uid and i["name"] == obj["metadata"]["name"] and
                          (i.get("uid") == obj["metadata"]["uid"] or not i.get("uid"))]
            require(len(candidates) == 1, "unknown pending App UID; retaining capability chain")
            intent = candidates[0]
            match_intent(obj, intent, state["run_id"])
            found.append((project, obj))
    return found


def persist_intents(state, evidence, evidence_file):
    evidence["pending_intents"] = state["pending_intents"]
    # No inputs from users: this runner creates only public {} fixture inputs.
    # Atomic private journal precedes POST/DELETE, including recovered reply UIDs.
    temporary = evidence_file.with_name(evidence_file.name + ".pending-write")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(evidence, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, evidence_file)
        directory = os.open(evidence_file.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def drain(h, state, refs, host_proxy, milo_proxy):
    # Revocation precedes the last census. Native read/list remains available.
    h.delete_group(refs, "pending-bindings", milo_proxy, state["run_id"])
    h.delete_group(refs, "lease", host_proxy, state["run_id"])
    found = census(h, milo_proxy, state)
    for project, obj in found:
        meta = obj["metadata"]
        for intent in state["pending_intents"]:
            if intent["project"] == project and intent["name"] == meta["name"] and not intent.get("uid"):
                intent["uid"] = meta["uid"]
        if "pending_evidence_file" in state:
            persist_intents(state, state["pending_evidence"], state["pending_evidence_file"])
        milo_proxy.delete(h.project_path(project, "apps", meta["name"]), meta["uid"], meta["resourceVersion"])
    wait_for(lambda: not census(h, milo_proxy, state), "known pending requests cancelled with hooks active", 120)


def prune_api(h, state, refs, host_proxy, milo_proxy):
    require(not census(h, milo_proxy, state), "pending data appeared after drain; retaining API stage")
    h.stage_prune_gate(refs, "pending-api", state["host"], host_proxy, state["run_id"], STAGE, "milo-system")
    h.absent(state["milo"], [("customresourcedefinition", n, None) for n in CRDS] +
             [(k, WC, None) for k in ("mutatingwebhookconfiguration", "validatingwebhookconfiguration")])


def exercise(h, args, state, refs, fixtures, tokens, ca, record, evidence, evidence_file, host_proxy):
    port, token = args.direct_milo_port, tokens[REQUESTER]
    def req(method, path, body=None, content_type="application/json"):
        return h.milo_request(port, ca, token, method, path, body, content_type=content_type)
    def get(project, name):
        return h.must_http(port, ca, token, "GET", h.project_path(project, "apps", name), 200)
    def save():
        persist_intents(state, evidence, evidence_file)
    def create(project, body):
        nonce = secrets.token_hex(16)
        body = copy.deepcopy(body)
        body["metadata"]["labels"] = {RUN_LABEL: state["run_id"], INTENT_LABEL: nonce}
        intent = {"project": project, "project_uid": state["projects"][project],
                  "name": body["metadata"]["name"], "spec": body["spec"], "nonce": nonce}
        state["pending_intents"].append(intent)
        save()  # durable noncredential intent BEFORE POST, including lost response
        status, app = req("POST", h.project_path(project, "apps"), body)
        require(status == 201, "pending request creation failed")
        intent["uid"] = match_intent(app, intent, state["run_id"])
        save()
        return wait_for(lambda: (v if v.get("status", {}).get("phase") == "Pending" else False)
                        if (v := get(project, body["metadata"]["name"])) else False,
                        "pending controller status", 120)
    sources = {}
    for project in h.PROJECTS[1:]:
        name = "apps-a1t-pending-source"
        body = h.template_object(name, "Pending-only source")
        h.create_fixture_template(args, state, tokens, ca, project, body, evidence, evidence_file)
        template = h.wait_template(port, ca, tokens[h.AUTHOR], project, name, 1)
        revision = h.must_http(port, ca, tokens[h.AUTHOR], "GET", h.project_path(project,
                               "apptemplaterevisions", template["status"]["latestRevision"]), 200)
        sources[project] = (template, revision)
    # Requester has CREATE but has no template GET until the group binding is
    # added. Manager has GET throughout; this detects using service authority.
    bind(h, REQUESTER, fixtures, state, refs)
    project = h.PROJECTS[1]
    body = app_object("apps-a1t-pending-proof", state["projects"][project], *sources[project])
    require(req("POST", h.project_path(project, "apps"), body)[0] == 403,
            "pending App source SAR did not deny effective actor without source GET")
    require(req("GET", h.project_path(project, "apptemplates", "apps-a1t-pending-source"))[0] == 403,
            "requester unexpectedly has source GET before group grant")
    bind(h, GROUP, fixtures, state, refs)
    wait_for(lambda: req("GET", h.project_path(project, "apptemplates", "apps-a1t-pending-source"))[0] == 200,
             "group-only template read authorization", 60)
    record("pending source authorization denied ungranted actor; group-only GET grant became effective")
    for key, value in ((PREFIX + "actor-uid", "forged"), ("apps.miloapis.com/project-uid", state["projects"][project])):
        attack = copy.deepcopy(body)
        attack["metadata"]["annotations"] = {key: value}
        require(req("POST", h.project_path(project, "apps"), attack)[0] == 403, "forged App provenance accepted")
    foreign = copy.deepcopy(body)
    foreign["spec"]["templateRef"]["projectRevision"]["projectUID"] = state["projects"][h.PROJECTS[2]]
    require(req("POST", h.project_path(project, "apps"), foreign)[0] == 403, "cross-Project source pin accepted")
    unavailable_project_probe(h, args, state, refs, fixtures, tokens, ca, body, evidence, evidence_file)
    record("unavailable fresh root Project authorization denied pending CREATE; exact discovery role restored")
    apps = {}
    for project in h.PROJECTS[1:]:
        apps[project] = create(project, app_object("apps-a1t-pending-proof", state["projects"][project], *sources[project]))
    require(apps[h.PROJECTS[1]]["metadata"]["uid"] != apps[h.PROJECTS[2]]["metadata"]["uid"], "Project App storage not isolated")
    record("two real Project App paths stamped exact effective UID, Project UID, spec digest and pending finalizer")
    project = h.PROJECTS[1]
    path = h.project_path(project, "apps", "apps-a1t-pending-proof")
    original = get(project, "apps-a1t-pending-proof")
    listed = h.must_http(port, ca, token, "GET", h.project_path(project, "apps"), 200)
    require({o["metadata"]["uid"] for o in listed["items"]} == {original["metadata"]["uid"]},
            "requester List is not isolated to exact Project App")
    watch_path = h.project_path(project, "apps") + "?watch=true&timeoutSeconds=1&resourceVersion=" + listed["metadata"]["resourceVersion"]
    require(req("GET", watch_path)[0] == 200, "requester native App watch unavailable")
    for mutate in (lambda o: o["spec"].update(uninstallPolicy="Retain"),
                   lambda o: o["metadata"].update(finalizers=[]),
                   lambda o: o["metadata"]["annotations"].update({PREFIX + "actor-uid": "forged"})):
        attack = copy.deepcopy(original)
        mutate(attack)
        require(req("PUT", path, attack)[0] == 403, "pending PUT mutation accepted")
    attack = copy.deepcopy(original)
    attack["status"]["phase"] = "Accepted"
    require(req("PUT", path + "/status", attack)[0] == 403, "forged Accepted status accepted")
    patch = {"metadata": {"uid": original["metadata"]["uid"], "resourceVersion": original["metadata"]["resourceVersion"]},
             "spec": {"uninstallPolicy": "Retain"}}
    require(req("PATCH", path, patch, "application/merge-patch+json")[0] == 403, "pending merge edit accepted")
    apply = {"apiVersion": original["apiVersion"], "kind": "App", "metadata": patch["metadata"] | {"name": original["metadata"]["name"]},
             "spec": copy.deepcopy(original["spec"])}
    apply["spec"]["uninstallPolicy"] = "Retain"
    require(req("PATCH", path + "?fieldManager=pending-trial&force=true", apply, "application/apply-patch+yaml")[0] == 403,
            "pending server-side apply edit accepted")
    revision = {"apiVersion": original["apiVersion"], "kind": "AppRevision", "metadata": {"name": "apps-a1t-forged-history"},
                "spec": {"projectUID": state["projects"][project], "appName": original["metadata"]["name"],
                    "appUID": original["metadata"]["uid"], "admittedGeneration": 1, "sequence": 1,
                    "effectiveActorUID": REQUESTER, "acceptedAt": "2026-10-21T00:00:00Z", "source": "Create",
                    "desiredSpec": original["spec"], "specDigest": original["metadata"]["annotations"][PREFIX + "spec-digest"]}}
    require(req("POST", h.project_path(project, "apprevisions"), revision)[0] == 403, "installation history forgery accepted")
    require(get(project, "apps-a1t-pending-proof") == original, "denied writes changed pending App")
    record("pending spec/provenance/finalizer PUT, merge/SSA edits, Accepted status and AppRevision forgery denied")
    before = h.pod_snapshot(state["host"], args.expected_architecture, PENDING)
    held = hold_controller(h, state, refs, evidence, evidence_file)
    state["host"].k("rollout", "restart", "deployment/apps-controller", "-n", "apps-system")
    state["host"].k("rollout", "status", "deployment/apps-controller", "-n", "apps-system", "--timeout=180s", timeout=190)
    after = wait_for(lambda: h.pod_snapshot(state["host"], args.expected_architecture, PENDING),
                     "both replacement pending controller Pods ready", 60)
    require(before["deployment_uid"] == after["deployment_uid"] and
            {p["uid"] for p in before["pods"]}.isdisjoint({p["uid"] for p in after["pods"]}),
            "pending controller restart did not replace both Pods")
    wait_for(lambda: get(project, "apps-a1t-pending-proof") == original, "pending request unchanged after restart", 120)
    held_now = state["host"].json("get", "lease", held.name, "-n", "apps-system")
    require(held_now["metadata"]["uid"] == held.uid and held_now["spec"]["holderIdentity"] == state["run_id"],
            "controller Lease is not held after restart")
    old = original["metadata"]
    opts = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": old["uid"], "resourceVersion": old["resourceVersion"]}}
    require(req("DELETE", path, opts)[0] in (200, 202), "pending cancellation DELETE failed")
    terminating = get(project, "apps-a1t-pending-proof")
    require(terminating["metadata"]["uid"] == old["uid"] and terminating["metadata"].get("deletionTimestamp") and
            terminating["metadata"].get("finalizers") == [FINALIZER] and terminating["spec"] == original["spec"] and
            terminating["metadata"]["annotations"] == original["metadata"]["annotations"],
            "DELETE did not retain exact pending request while reconciliation was held")
    evidence["pending_held_delete"] = {"app_uid": old["uid"], "deletion_timestamp": terminating["metadata"]["deletionTimestamp"],
                                        "lease_uid": held.uid, "finalizers": terminating["metadata"]["finalizers"]}
    persist_intents(state, evidence, evidence_file)
    record("DELETE retained exact terminating App UID and pending finalizer with both restarted replicas held from leadership")
    held.delete(host_proxy, state["run_id"])
    wait_for(lambda: req("GET", path)[0] == 404, "pending request cancelled after leader release", 120)
    replacement = create(project, body)
    require(replacement["metadata"]["uid"] != old["uid"] and req("DELETE", path, opts)[0] == 409,
            "stale cancellation deleted replacement UID")
    require(req("PUT", path, original)[0] == 409, "stale pending UID/RV update not rejected")
    require(get(project, "apps-a1t-pending-proof")["metadata"]["uid"] == replacement["metadata"]["uid"],
            "replacement disappeared after stale cancellation")
    record("pending-only restart, cancellation, same-name fresh UID and stale UID/RV delete denial")
    evidence["pending_limitations"] = ["No installation acceptance or native resources; pending-only protocol is irreversible",
        "Static RBAC effective actor/group exercised; arbitrary UserInfo extras fidelity is source/unit evidence only",
        "No production OAuth/OpenFGA revocation bound, Project teardown or unavailable-provider isolation claim"]


def unavailable_project_probe(h, args, state, refs, fixtures, tokens, ca, body, evidence, evidence_file):
    """Revoke only fixture discovery GET, preserving list/watch and source grants.

    No new impersonation or controller exception: the normal trusted root
    client's fresh Project GET must fail. Restore the exact role with UID/RV CAS.
    """
    milo = state["milo"]
    expected = next(o for o in fixtures["milo-rbac"] if o["kind"] == "ClusterRole" and
                    o["metadata"]["name"] == h.DISCOVERY)["rules"]
    ref = next(r for r in refs["rbac"] if r.kind == "clusterrole" and r.name == h.DISCOVERY)
    live = milo.json("get", "clusterrole", h.DISCOVERY)
    require(live["metadata"]["uid"] == ref.uid and live["rules"] == expected and
            live["metadata"].get("labels", {}).get(RUN_LABEL) == state["run_id"],
            "discovery role changed before unavailable-Project probe")
    reduced = copy.deepcopy(expected)
    for rule in reduced:
        rule["verbs"] = [verb for verb in rule["verbs"] if verb != "get"]
    def change(current, before, after):
        require(current["metadata"]["uid"] == ref.uid and current["rules"] == before,
                "discovery role changed concurrently; refusing overwrite")
        milo.patch_json(state["private"], "clusterrole", h.DISCOVERY, [
            {"op": "test", "path": "/metadata/uid", "value": ref.uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/rules", "value": before},
            {"op": "replace", "path": "/rules", "value": after}])
    evidence["discovery_get_probe"] = {"role_uid": ref.uid, "original_rules": expected, "reduced_rules": reduced, "restored": False}
    persist_intents(state, evidence, evidence_file)
    try:
        change(live, expected, reduced)
        project = h.PROJECTS[1]
        wait_for(lambda: h.milo_request(args.direct_milo_port, ca, tokens[h.DISCOVERY], "GET",
                 h.PROJECT_API + "/" + project)[0] == 403, "discovery GET denied", 60)
        status, reply = h.milo_request(args.direct_milo_port, ca, tokens[REQUESTER], "POST",
                                      h.project_path(project, "apps"), body)
        require(status == 403 and "Project identity unavailable" in reply.get("message", ""),
                "pending CREATE was not denied by fresh trusted Project resolution")
    finally:
        current = milo.json("get", "clusterrole", h.DISCOVERY)
        require(current["metadata"]["uid"] == ref.uid and current["rules"] in (expected, reduced),
                "discovery role restore is ambiguous; retaining capability chain")
        if current["rules"] == reduced:
            change(current, reduced, expected)
        wait_for(lambda: h.milo_request(args.direct_milo_port, ca, tokens[h.DISCOVERY], "GET",
                 h.PROJECT_API + "/" + h.PROJECTS[1])[0] == 200, "discovery GET restored", 60)
        evidence["discovery_get_probe"]["restored"] = True
        persist_intents(state, evidence, evidence_file)


def verify_discovery_restore(state):
    probe = state.get("pending_evidence", {}).get("discovery_get_probe")
    if probe is None:
        return
    require(probe.get("restored") is True, "discovery authorization probe restore is incomplete")
    live = state["milo"].json("get", "clusterrole", "apps-a1t-native-discovery")
    require(live["metadata"]["uid"] == probe["role_uid"] and live["rules"] == probe["original_rules"],
            "discovery authorization changed after restore; retaining capability chain")


def hold_controller(h, state, refs, evidence, evidence_file):
    host = state["host"]
    namespace = host.json("get", "namespace", "apps-system")
    namespace_ref = next(r for r in refs["namespace"] if r.kind == "namespace" and r.name == "apps-system")
    require(namespace["metadata"]["uid"] == namespace_ref.uid and
            namespace["metadata"].get("labels", {}).get(RUN_LABEL) == state["run_id"],
            "leader hold requires exact run-owned namespace")
    name = "apps-authoring.apps.miloapis.com"
    live = host.json("get", "lease", name, "-n", "apps-system")
    meta = live["metadata"]
    require(meta.get("uid") and meta.get("resourceVersion") and not meta.get("deletionTimestamp") and
            not meta.get("ownerReferences") and live["spec"].get("holderIdentity") and
            live["spec"]["holderIdentity"] != state["run_id"], "existing controller Lease is ambiguous")
    held = h.HeldLease(host, "apps-system", "lease", name,
                       "/apis/coordination.k8s.io/v1/namespaces/apps-system/leases/" + name,
                       intent=True, uid=meta["uid"])
    refs.setdefault("lease", []).append(held)
    evidence["pending_leader_hold"] = {"lease_uid": meta["uid"], "prior_resource_version": meta["resourceVersion"],
                                       "holder_identity": state["run_id"]}
    persist_intents(state, evidence, evidence_file)
    # Native API CAS only. Both Pods are restarted immediately afterwards, so
    # no old leader process survives to race the held DELETE observation.
    next_spec = copy.deepcopy(live["spec"])
    next_spec.update(holderIdentity=state["run_id"], leaseDurationSeconds=1800,
                     renewTime=h.datetime.now(h.timezone.utc).isoformat())
    host.patch_json(state["private"], "lease", name, [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/spec", "value": live["spec"]},
        {"op": "replace", "path": "/spec", "value": next_spec}], "apps-system")
    return held
