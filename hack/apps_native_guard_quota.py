"""Eight-unit native Project allowance for the isolated ownership guard probe.

The caller supplies its already verified API transport and durable private
evidence writer. Only the quota controller writes status. This fixture is not
install authority: Milo consumerRef is name-scoped, without a Project UID.
"""
import copy
import hashlib
from urllib.parse import quote

from apps_native_support import require, wait_for

GROUP = "quota.miloapis.com/v1alpha1"
RESOURCE = "core.miloapis.com/configmaps"
NAMESPACE = "milo-system"
LABEL = "apps-guard-probe.datum.net/run-id"
ANNOTATION = "apps-guard-probe.datum.net/project-uid"
AMOUNT = 8  # ordinary control plus six concurrent PATCH seeds, with one spare


class QuotaFixture:
    def __init__(self, request, project, project_uid, run_id, evidence, save, ensure_project):
        self.request, self.save, self.ensure_project = request, save, ensure_project
        self.project, self.project_uid, self.run_id = project, project_uid, run_id
        self.evidence = evidence
        self.project_path = "/apis/resourcemanager.miloapis.com/v1alpha1/projects/" + quote(project, safe="")
        self.prefix = self.project_path + "/control-plane"
        self.api = self.prefix + "/apis/" + GROUP
        self.collection = self.api + "/namespaces/" + NAMESPACE + "/resourcegrants"
        self.namespace_path = self.prefix + "/api/v1/namespaces/" + NAMESPACE
        self.consumer = {"apiGroup": "resourcemanager.miloapis.com", "kind": "Project", "name": project}
        # Native quota API controller naming at pinned Milo #870; no storage access.
        self.bucket_name = "bucket-" + hashlib.sha256((RESOURCE + "Project" + project).encode()).hexdigest()

    def get(self, path, *, optional=False):
        code, value = self.request("GET", path)
        if optional and code == 404:
            return None
        require(code == 200 and isinstance(value, dict), "quota API GET failed")
        return value

    def listing(self, resource, kind):
        value = self.get(self.api + "/" + resource)
        require(value.get("kind") == kind and isinstance(value.get("items"), list),
                "quota API list unavailable; cannot infer empty state")
        return value["items"]

    def relevant(self, obj, field):
        spec = obj.get("spec", {})
        return spec.get("consumerRef") == self.consumer and any(
            v.get("resourceType") == RESOURCE for v in spec.get(field, []))

    def grants(self):
        return [g for g in self.listing("resourcegrants", "ResourceGrantList") if self.relevant(g, "allowances")]

    def claims(self):
        return [c for c in self.listing("resourceclaims", "ResourceClaimList") if self.relevant(c, "requests")]

    def buckets(self):
        return [b for b in self.listing("allowancebuckets", "AllowanceBucketList")
                if b.get("spec") == {"consumerRef": self.consumer, "resourceType": RESOURCE}]

    def exact(self, obj, *, terminating=False):
        state = self.evidence["quota"]
        meta = obj.get("metadata", {})
        require(obj.get("apiVersion") == GROUP and obj.get("kind") == "ResourceGrant" and
                meta.get("name") == state["name"] and meta.get("namespace") == NAMESPACE and
                meta.get("labels") == {LABEL: self.run_id} and
                meta.get("annotations") == {ANNOTATION: self.project_uid} and
                not meta.get("ownerReferences") and not meta.get("finalizers") and
                (terminating or not meta.get("deletionTimestamp")) and
                not meta.get("generateName") and obj.get("spec") == state["spec"] and
                meta.get("uid") and meta.get("resourceVersion") and
                (state.get("uid") is None or meta["uid"] == state["uid"]),
                "quota grant differs from exact recorded intent or UID")
        return meta

    def prepare(self):
        require("quota" not in self.evidence, "quota fixture already has an intent")
        self.ensure_project()
        registration = self.get("/apis/" + GROUP + "/resourceregistrations/configmaps-per-project")
        require(registration.get("spec", {}).get("resourceType") == RESOURCE and
                registration["spec"].get("consumerType") == {"apiGroup": "resourcemanager.miloapis.com", "kind": "Project"} and
                any(c.get("type") == "Active" and c.get("status") == "True"
                    for c in registration.get("status", {}).get("conditions", [])),
                "source-owned ConfigMap quota registration is not active")
        namespace = self.get(self.namespace_path)
        require(namespace.get("metadata", {}).get("uid") and
                not namespace["metadata"].get("deletionTimestamp"), "Project quota namespace unavailable")
        require(not self.grants() and not self.claims(), "pre-existing Project ConfigMap quota state; refusing adoption")
        spec = {"consumerRef": self.consumer,
                "allowances": [{"resourceType": RESOURCE, "buckets": [{"amount": AMOUNT}]}]}
        state = {"name": "apps-guard-quota-" + self.run_id, "spec": copy.deepcopy(spec),
                 "namespace_uid": namespace["metadata"]["uid"], "uid": None,
                 "create_started": False, "cleanup": "not-requested",
                 "consumer_binding": "native name-scoped consumerRef; no install authority"}
        self.evidence["quota"] = state
        self.save()
        manifest = {"apiVersion": GROUP, "kind": "ResourceGrant",
                    "metadata": {"name": state["name"], "namespace": NAMESPACE,
                                 "labels": {LABEL: self.run_id}, "annotations": {ANNOTATION: self.project_uid}},
                    "spec": spec}
        self.ensure_project()
        state["create_started"] = True
        self.save()  # fsynced by caller before POST; an unknown reply is recoverable
        code, reply = self.request("POST", self.collection, manifest)
        if 400 <= code < 500:
            state["create_started"] = False
            state["rejected_status"] = code
            self.save()
        require(code == 201 and isinstance(reply, dict), "native quota grant CREATE failed")
        state["uid"] = self.exact(reply)["uid"]
        self.save()
        wait_for(self.ready, "native Project ConfigMap allowance reconciled", 90)
        state["active"] = True
        self.save()

    def ready(self):
        self.ensure_project()
        state = self.evidence["quota"]
        grant = self.get(self.collection + "/" + state["name"])
        meta = self.exact(grant)
        generation = meta.get("generation")
        require(isinstance(generation, int) and generation > 0, "quota grant generation absent")
        grants = self.grants()
        require(len(grants) == 1 and grants[0]["metadata"]["uid"] == state["uid"], "Project quota grants changed")
        if not any(c.get("type") == "Active" and c.get("status") == "True" and
                   c.get("observedGeneration") == generation
                   for c in grant.get("status", {}).get("conditions", [])):
            return False
        buckets = self.buckets()
        require(len(buckets) <= 1, "ambiguous Project quota buckets")
        if not buckets:
            return False
        meta = buckets[0].get("metadata", {})
        require(meta.get("name") == self.bucket_name and meta.get("namespace") == NAMESPACE and
                meta.get("uid") and not meta.get("deletionTimestamp"), "Project quota bucket identity changed")
        status = buckets[0].get("status", {})
        return (isinstance(meta.get("generation"), int) and meta["generation"] > 0 and
                status.get("observedGeneration") == meta["generation"] and
                status.get("limit") == AMOUNT and status.get("allocated") == 0 and
                status.get("available") == AMOUNT and status.get("contributingGrantRefs") == [
                    {"name": state["name"], "amount": AMOUNT, "lastObservedGeneration": generation}])

    def cleanup(self):
        state = self.evidence.get("quota")
        if state is None:
            return
        if not state["create_started"]:
            state["cleanup"] = "no-create-or-rejected"
            self.save()
            return
        # Do not require the original Project to remain Ready/live. The grant
        # itself, its original namespace and the trusted API transport are the
        # deletion boundary. Removing our name-scoped grant limits inheritance.
        namespace = self.get(self.namespace_path)
        require(namespace.get("metadata", {}).get("uid") == state["namespace_uid"],
                "Project quota namespace replaced; retain grant recovery evidence")
        path = self.collection + "/" + state["name"]
        grant = self.get(path, optional=True)
        if grant is not None:
            meta = self.exact(grant, terminating=True)
            if state.get("uid") is None:
                state["uid"] = meta["uid"]
                state["uid_recovery"] = "exact prewritten create intent"
                self.save()
            project = self.get(self.project_path, optional=True)
            if project is not None and project.get("metadata", {}).get("uid") == self.project_uid:
                wait_for(lambda: not self.claims(), "Project ConfigMap quota claims released", 60)
            else:
                # A name-scoped allowance must not be stranded for a replacement
                # Project merely because the replacement now has claims. Delete
                # our exact original grant only; never modify those claims.
                state["project_changed_during_cleanup"] = True
                self.save()
            # The waiting period can change RV. Re-read, verify, then condition
            # this single deletion on both UID and the latest resourceVersion.
            grant = self.get(path, optional=True)
            if grant is not None:
                meta = self.exact(grant, terminating=True)
                code, _ = self.request("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
                    "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}})
                require(code in (200, 202, 404), "quota UID/RV DELETE failed")
        def retired():
            current = self.get(path, optional=True)
            if current is not None:
                self.exact(current, terminating=True)
                return False
            return all(not any(r.get("name") == state["name"] for r in b.get("status", {}).get("contributingGrantRefs", []))
                       for b in self.buckets())
        wait_for(retired, "owned quota grant and bucket contribution absent", 60)
        state["cleanup"] = "complete"
        self.save()
