"""Failure controls for native quota fixture; not live quota/admission proof."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apps_native_guard_quota as q


def once(predicate, *_):
    value = predicate()
    if not value:
        raise RuntimeError("condition not ready")
    return value


class API:
    def __init__(self):
        self.grant = None
        self.extra_grants, self.claims = [], []
        self.namespace_uid = "namespace-uid"
        self.posts, self.deletes, self.saved = [], [], []
        self.bucket_ready, self.bucket_removed = True, True
        self.remaining_bucket = None
        self.lost_reply = False
        self.post_status = 201
        self.project_present = True
        self.evidence = {}
        self.fixture = q.QuotaFixture(self.request, "apps-a1t-provider", "project-uid", "0123456789abcdef",
                                     self.evidence, self.save, self.ensure_project)

    def save(self):
        self.saved.append(copy.deepcopy(self.evidence))

    def ensure_project(self):
        if not self.project_present:
            raise RuntimeError("Project gone")

    def bucket(self):
        if self.grant is None:
            return self.remaining_bucket
        return {"metadata": {"name": self.fixture.bucket_name, "namespace": q.NAMESPACE,
                             "uid": "bucket-uid", "generation": 1},
                "spec": {"consumerRef": self.fixture.consumer, "resourceType": q.RESOURCE},
                "status": {"observedGeneration": 1, "limit": 8 if self.bucket_ready else 0, "allocated": 0,
                           "available": 8 if self.bucket_ready else 0,
                           "contributingGrantRefs": [{"name": self.grant["metadata"]["name"],
                               "amount": 8, "lastObservedGeneration": 1}]}}

    def request(self, method, path, body=None):
        if method == "POST":
            assert path == self.fixture.collection  # native Project scope, never root grants
            assert self.saved[-1]["quota"]["create_started"] is True
            self.posts.append(copy.deepcopy(body))
            self.grant = copy.deepcopy(body)
            self.grant["metadata"].update(uid="grant-uid", resourceVersion="12", generation=1)
            self.grant["status"] = {"conditions": [{"type": "Active", "status": "True", "observedGeneration": 1}]}
            if self.lost_reply:
                raise ConnectionError("response lost after commit")
            return self.post_status, copy.deepcopy(self.grant)
        if method == "DELETE":
            assert self.saved[-1]["quota"]["uid"] == self.grant["metadata"]["uid"]
            self.deletes.append((path, copy.deepcopy(body)))
            if not self.bucket_removed:
                self.remaining_bucket = self.bucket()
            self.grant = None
            return 200, {}
        assert method == "GET"
        if path == self.fixture.project_path:
            return (200, {"metadata": {"uid": "project-uid"}}) if self.project_present else (404, {})
        if path.endswith("/resourceregistrations/configmaps-per-project"):
            return 200, {"spec": {"resourceType": q.RESOURCE, "consumerType": {
                "apiGroup": "resourcemanager.miloapis.com", "kind": "Project"}},
                "status": {"conditions": [{"type": "Active", "status": "True"}]}}
        if path == self.fixture.namespace_path:
            return 200, {"metadata": {"uid": self.namespace_uid}}
        if path == self.fixture.api + "/resourcegrants":
            return 200, {"kind": "ResourceGrantList", "items": copy.deepcopy(self.extra_grants + ([self.grant] if self.grant else []))}
        if path == self.fixture.api + "/resourceclaims":
            return 200, {"kind": "ResourceClaimList", "items": copy.deepcopy(self.claims)}
        if path == self.fixture.api + "/allowancebuckets":
            bucket = self.bucket()
            return 200, {"kind": "AllowanceBucketList", "items": [copy.deepcopy(bucket)] if bucket else []}
        if path.startswith(self.fixture.collection + "/"):
            return (200, copy.deepcopy(self.grant)) if self.grant else (404, {})
        raise AssertionError(path)


class QuotaControls(unittest.TestCase):
    def setUp(self):
        self.wait = patch.object(q, "wait_for", side_effect=once)
        self.wait.start()
        self.addCleanup(self.wait.stop)
        self.api = API()
        self.f = self.api.fixture

    def test_native_project_grant_and_uid_rv_cleanup(self):
        self.f.prepare()
        self.assertEqual(self.api.posts[0]["spec"]["allowances"][0]["buckets"], [{"amount": 8}])
        self.assertTrue(self.api.evidence["quota"]["active"])
        self.f.cleanup()
        self.assertEqual(self.api.deletes[0][1]["preconditions"], {"uid": "grant-uid", "resourceVersion": "12"})
        self.assertEqual(self.api.evidence["quota"]["cleanup"], "complete")

    def test_active_grant_without_bucket_capacity_is_not_ready(self):
        self.api.bucket_ready = False
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            self.f.prepare()
        self.assertNotIn("active", self.api.evidence["quota"])

    def test_stale_or_missing_active_generation_is_not_ready(self):
        self.f.prepare()
        for generation in (None, 0, 2):
            self.api.grant["status"]["conditions"][0]["observedGeneration"] = generation
            self.assertFalse(self.f.ready())

    def test_stale_or_terminating_bucket_is_not_ready(self):
        self.f.prepare()
        bucket = self.api.bucket()
        bucket["status"]["observedGeneration"] = 0
        with patch.object(self.f, "buckets", return_value=[bucket]):
            self.assertFalse(self.f.ready())
        bucket["status"]["observedGeneration"] = 1
        bucket["metadata"]["deletionTimestamp"] = "2026-10-21T00:00:00Z"
        with patch.object(self.f, "buckets", return_value=[bucket]):
            with self.assertRaisesRegex(RuntimeError, "bucket identity"):
                self.f.ready()

    def test_existing_allowance_or_claim_refuses_create(self):
        self.api.extra_grants = [{"spec": {"consumerRef": self.f.consumer,
            "allowances": [{"resourceType": q.RESOURCE}]}}]
        with self.assertRaisesRegex(RuntimeError, "pre-existing"):
            self.f.prepare()
        self.assertFalse(self.api.posts)

    def test_lost_reply_recovers_exact_intent_uid_before_delete(self):
        self.api.lost_reply = True
        with self.assertRaises(ConnectionError):
            self.f.prepare()
        self.assertIsNone(self.api.evidence["quota"]["uid"])
        self.f.cleanup()
        self.assertEqual(self.api.evidence["quota"]["uid_recovery"], "exact prewritten create intent")
        self.assertEqual(len(self.api.deletes), 1)

    def test_rejected_create_never_adopts_matching_collision(self):
        self.api.post_status = 409
        with self.assertRaisesRegex(RuntimeError, "CREATE failed"):
            self.f.prepare()
        self.f.cleanup()
        self.assertFalse(self.api.deletes)

    def test_known_uid_replacement_is_not_deleted(self):
        self.f.prepare()
        self.api.grant["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "recorded intent or UID"):
            self.f.cleanup()
        self.assertFalse(self.api.deletes)

    def test_namespace_replacement_is_not_deleted(self):
        self.f.prepare()
        self.api.namespace_uid = "replacement"
        with self.assertRaisesRegex(RuntimeError, "namespace replaced"):
            self.f.cleanup()
        self.assertFalse(self.api.deletes)

    def test_name_scoped_grant_can_be_retired_after_project_disappears(self):
        self.f.prepare()
        self.api.project_present = False
        self.api.claims = [{"spec": {"consumerRef": self.f.consumer, "requests": [{"resourceType": q.RESOURCE}]}}]
        self.f.cleanup()
        self.assertEqual(len(self.api.deletes), 1)
        self.assertTrue(self.api.claims)  # never touch another consumer's claims
        self.assertTrue(self.api.evidence["quota"]["project_changed_during_cleanup"])

    def test_remaining_claim_retains_grant(self):
        self.f.prepare()
        self.api.claims = [{"spec": {"consumerRef": self.f.consumer, "requests": [{"resourceType": q.RESOURCE}]}}]
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            self.f.cleanup()
        self.assertFalse(self.api.deletes)

    def test_ambiguous_unknown_uid_payload_is_not_adopted(self):
        self.api.lost_reply = True
        with self.assertRaises(ConnectionError):
            self.f.prepare()
        original = copy.deepcopy(self.api.grant)
        for mutate in (lambda g: g["metadata"].update(ownerReferences=[{"uid": "foreign"}]),
                       lambda g: g["metadata"]["annotations"].update(other="unexpected"),
                       lambda g: g["spec"]["allowances"][0]["buckets"][0].update(amount=80)):
            self.api.grant = copy.deepcopy(original)
            mutate(self.api.grant)
            with self.assertRaisesRegex(RuntimeError, "recorded intent or UID"):
                self.f.cleanup()
        self.assertFalse(self.api.deletes)

    def test_stale_bucket_contribution_prevents_complete_cleanup(self):
        self.f.prepare()
        self.api.bucket_removed = False
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            self.f.cleanup()
        self.assertNotEqual(self.api.evidence["quota"]["cleanup"], "complete")

    def test_intent_write_failure_prevents_post(self):
        self.f.save = lambda: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(OSError):
            self.f.prepare()
        self.assertFalse(self.api.posts)

    def test_malformed_list_cannot_count_as_empty(self):
        with patch.object(self.f, "get", return_value={"kind": "ResourceClaimList"}):
            with self.assertRaisesRegex(RuntimeError, "cannot infer empty"):
                self.f.claims()


if __name__ == "__main__":
    unittest.main()
