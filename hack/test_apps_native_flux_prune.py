"""Source-bound Flux prune regression; no Kubernetes or storage calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apps_native_support import FluxOwned, RUN_LABEL, flux_inventory, write_flux_journal

REVISION = "sha256:" + "1" * 64
CA = b"-----BEGIN CERTIFICATE-----\npublic-fixture\n-----END CERTIFICATE-----\n"
RENDER = [
    {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "apps", "namespace": "apps-system"}},
    {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingWebhookConfiguration",
     "metadata": {"name": "apps-test"}, "webhooks": [{"name": "validate.apps.test", "clientConfig": {
         "service": {"name": "apps", "namespace": "apps-system", "path": "/validate", "port": 443}}}]},
]
MANIFEST = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
            "metadata": {"name": "stage", "namespace": "milo-system"}, "spec": {
                "interval": "10m", "prune": True, "sourceRef": {"kind": "OCIRepository", "name": "source", "namespace": "flux-system"},
                "path": "./config/api", "kubeConfig": {"secretRef": {"name": "scoped", "key": "kubeconfig"}}}}


class NativeAPI:
    def __init__(self):
        self.obj = None
        self.lost_create = False
        self.lost_patch = False
        self.patch_calls = []
        self.delete_calls = []
        self.before_delete = None
    def create(self, manifest, namespace, **kwargs):
        self.obj = copy.deepcopy(manifest)
        self.obj["metadata"].update(uid="uid", resourceVersion="1", generation=1, creationTimestamp="2026-10-21T00:00:00Z")
        if self.lost_create:
            raise RuntimeError("response lost")
        return copy.deepcopy(self.obj)
    def optional(self, *args):
        return copy.deepcopy(self.obj)
    def patch_json(self, _private, _kind, _name, operations, _namespace):
        assert operations[:3] == [{"op": "test", "path": "/metadata/uid", "value": self.obj["metadata"]["uid"]},
                                  {"op": "test", "path": "/metadata/resourceVersion", "value": self.obj["metadata"]["resourceVersion"]},
                                  {"op": "test", "path": "/spec", "value": self.obj["spec"]}]
        self.patch_calls.append(copy.deepcopy(operations))
        self.obj["spec"]["patches"] = copy.deepcopy(operations[3]["value"])
        self.obj["metadata"]["resourceVersion"] = "2"
        if self.lost_patch:
            raise RuntimeError("patch response lost")
        return copy.deepcopy(self.obj)
    def delete(self, path, uid, rv):
        if self.before_delete:
            self.before_delete()
        assert uid == self.obj["metadata"]["uid"] and rv == self.obj["metadata"]["resourceVersion"]
        self.delete_calls.append((path, uid, rv))
        self.obj = None


class FluxPruneTests(unittest.TestCase):
    def owner(self, *, lost=False, journal=None, manifest=None):
        api = NativeAPI()
        api.lost_create = lost
        owner = FluxOwned(api, "milo-system", "stage", RENDER, REVISION, journal)
        if lost:
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                owner.create(manifest or MANIFEST, "run")
        else:
            owner.create(manifest or MANIFEST, "run")
        return owner, api

    def test_inventory_mapping_includes_core_group_namespace_kind_and_version(self):
        self.assertEqual(flux_inventory(RENDER), frozenset({("apps-system_apps__Service", "v1"),
            ("_apps-test_admissionregistration.k8s.io_ValidatingWebhookConfiguration", "v1")}))
        for render in ([], RENDER + RENDER, [dict(RENDER[0], apiVersion="v2"), RENDER[0]]):
            with self.assertRaises(RuntimeError):
                flux_inventory(render)

    def test_known_and_lost_reply_uids_allow_partial_exact_inventory(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                records = []
                owner, api = self.owner(lost=lost, journal=lambda state: records.append(state))
                api.obj["metadata"]["finalizers"] = ["finalizers.fluxcd.io"]
                api.obj["metadata"]["annotations"] = {"reconcile.fluxcd.io/requestedAt": "operator-request"}
                api.obj["status"] = {"lastAppliedRevision": REVISION,
                                      "inventory": {"entries": [{"id": "apps-system_apps__Service", "v": "v1"}]}}
                api.before_delete = lambda: self.assertEqual((records[-1]["uid"], records[-1]["delete_intent"]), ("uid", True))
                owner.delete(api, "run")
                self.assertEqual(api.delete_calls[0][1:], ("uid", "1"))

    def test_current_spec_drift_blocks_known_and_unknown_uid_before_delete(self):
        mutations = [lambda o: o["spec"].update(path="./foreign"),
                     lambda o: o["spec"]["sourceRef"].update(name="other"),
                     lambda o: o["spec"]["kubeConfig"]["secretRef"].update(name="admin"),
                     lambda o: o["spec"].update(patches=[{"patch": "foreign"}]),
                     lambda o: o["spec"].update(targetNamespace="foreign"),
                     lambda o: o["spec"].update(force=True),
                     lambda o: o["spec"].update(deletionPolicy="Orphan"),
                     lambda o: o["spec"].update(postBuild={"substitute": {"foreign": "value"}})]
        for lost in (False, True):
            for mutate in mutations:
                with self.subTest(lost=lost, mutation=mutate):
                    owner, api = self.owner(lost=lost)
                    mutate(api.obj)
                    with self.assertRaises(RuntimeError):
                        owner.delete(api, "run")
                    self.assertFalse(api.delete_calls)

    def test_foreign_duplicate_version_and_malformed_inventory_denied(self):
        inventories = [{"entries": [{"id": "foreign_namespace__Namespace", "v": "v1"}]},
                       {"entries": [{"id": "apps-system_apps__Service", "v": "v2"}]},
                       {"entries": [{"id": "apps-system_apps__Service", "v": "v1"}] * 2},
                       {"entries": [{"id": "apps-system_apps__Service", "v": "v1", "unknown": True}]},
                       None, {}, {"entries": "unavailable"}]
        for lost in (False, True):
            for inventory in inventories:
                with self.subTest(lost=lost, inventory=inventory):
                    owner, api = self.owner(lost=lost)
                    api.obj["status"] = {"inventory": inventory}
                    with self.assertRaises(RuntimeError):
                        owner.delete(api, "run")
                    self.assertFalse(api.delete_calls)

    def test_other_artifact_revision_blocks_prune(self):
        for field in ("lastAttemptedRevision", "lastAppliedRevision"):
            owner, api = self.owner()
            api.obj["status"] = {field: "sha256:foreign"}
            with self.assertRaises(RuntimeError):
                owner.delete(api, "run")
            self.assertFalse(api.delete_calls)

    def test_lost_response_requires_full_intent_not_run_label(self):
        mutations = [lambda o: o["metadata"].update(ownerReferences=[{"uid": "foreign"}]),
                     lambda o: o["metadata"].update(finalizers=["foreign/finalizer"]),
                     lambda o: o["metadata"].update(deletionTimestamp="now"),
                     lambda o: o["metadata"]["labels"].update(other="foreign"),
                     lambda o: o["metadata"].update(annotations={"other": "foreign"}),
                     lambda o: o.update(kind="Other"),
                     lambda o: o["metadata"].update(name="other")]
        for mutate in mutations:
            owner, api = self.owner(lost=True)
            mutate(api.obj)
            with self.assertRaises(RuntimeError):
                owner.delete(api, "run")
            self.assertFalse(api.delete_calls)

    def test_only_pinned_native_defaults_are_normalized(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["spec"]["postBuild"] = {"substituteFrom": [{"kind": "ConfigMap", "name": "public"}]}
        owner, api = self.owner(manifest=manifest)
        del api.obj["spec"]["force"]
        del api.obj["spec"]["postBuild"]["substituteFrom"][0]["optional"]
        owner.delete(api, "run")
        self.assertEqual(len(api.delete_calls), 1)
        owner, api = self.owner(manifest=manifest)
        api.obj["spec"]["postBuild"]["substituteFrom"][0]["optional"] = True
        with self.assertRaises(RuntimeError):
            owner.delete(api, "run")

    def test_failed_journal_prevents_create_patch_and_delete(self):
        with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
            self.owner(journal=lambda _: (_ for _ in ()).throw(RuntimeError("journal unavailable")))
        for operation in ("patch", "delete"):
            owner, api = self.owner()
            owner.journal = lambda _: (_ for _ in ()).throw(RuntimeError("journal unavailable"))
            with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
                owner.bind_webhook_ca(Path("/unused"), CA) if operation == "patch" else owner.delete(api, "run")
            self.assertFalse(api.patch_calls or api.delete_calls)

    def test_ca_transition_lost_reply_recovered_without_second_patch(self):
        records = []
        owner, api = self.owner(journal=lambda state: records.append(state))
        api.lost_patch = True
        with self.assertRaisesRegex(RuntimeError, "patch response lost"):
            owner.bind_webhook_ca(Path("/unused"), CA)
        saved = copy.deepcopy(records[-1])
        self.assertTrue(saved["ca_bundle"])
        self.assertFalse(saved["ca_committed"])
        restored = FluxOwned(api, "milo-system", "stage", RENDER, REVISION, records.append)
        restored.recover(MANIFEST, "run", saved)
        api.lost_patch = False
        restored.bind_webhook_ca(Path("/unused"), CA)
        self.assertEqual(len(api.patch_calls), 1)
        self.assertTrue(records[-1]["ca_committed"])
        restored.delete(api, "run")
        self.assertEqual(len(api.delete_calls), 1)

    def test_ca_pending_accepts_only_exact_before_or_after(self):
        for commit in (False, True):
            owner, api = self.owner()
            api.lost_patch = True
            with self.assertRaises(RuntimeError):
                owner.bind_webhook_ca(Path("/unused"), CA)
            if not commit:
                api.obj["spec"] = copy.deepcopy(owner._initial["spec"])
            owner.delete(api, "run")
            self.assertEqual(len(api.delete_calls), 1)
        owner, api = self.owner()
        owner.bind_webhook_ca(Path("/unused"), CA)
        api.obj["spec"] = copy.deepcopy(owner._initial["spec"])
        with self.assertRaises(RuntimeError):
            owner.delete(api, "run")  # confirmed binding cannot silently roll back

    def test_ca_recovery_rejects_foreign_patches_and_different_bundle(self):
        owner, api = self.owner()
        owner.bind_webhook_ca(Path("/unused"), CA)
        with self.assertRaises(RuntimeError):
            owner.bind_webhook_ca(Path("/unused"), CA + b"different")
        api.obj["spec"]["patches"][0]["target"]["name"] = "foreign"
        with self.assertRaises(RuntimeError):
            owner.delete(api, "run")
        self.assertFalse(api.delete_calls)

    def test_recovery_is_bound_to_source_render_and_initial_spec(self):
        owner, api = self.owner(lost=True)
        saved = owner.recovery_state()
        for field, value in (("initial", {}), ("inventory", []), ("revision", "foreign"), ("run_id", "foreign")):
            altered = copy.deepcopy(saved)
            altered[field] = value
            restored = FluxOwned(api, "milo-system", "stage", RENDER, REVISION)
            with self.assertRaises(RuntimeError):
                restored.recover(MANIFEST, "run", altered)

    def test_delete_retry_can_observe_only_own_terminating_uid(self):
        owner, api = self.owner()
        original_delete = api.delete
        def lost_delete(*args):
            api.obj["metadata"]["deletionTimestamp"] = "2026-10-21T01:00:00Z"
            api.obj["metadata"]["resourceVersion"] = "8"
            raise RuntimeError("delete response lost")
        api.delete = lost_delete
        with self.assertRaises(RuntimeError):
            owner.delete(api, "run")
        restored = FluxOwned(api, "milo-system", "stage", RENDER, REVISION)
        restored.recover(MANIFEST, "run", owner.recovery_state())
        api.delete = original_delete
        restored.delete(api, "run")
        self.assertEqual(api.delete_calls[0][1:], ("uid", "8"))
        owner, api = self.owner()
        api.obj["metadata"]["uid"] = "replacement"
        with self.assertRaises(RuntimeError):
            owner.delete(api, "run")

    def test_file_journal_private_and_precedes_recovered_delete(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "flux.json"
            owner, api = self.owner(lost=True, journal=lambda state: write_flux_journal(path, state))
            api.before_delete = lambda: self.assertEqual(json.loads(path.read_text())["uid"], "uid")
            owner.delete(api, "run")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(json.loads(path.read_text())["delete_intent"])

    def test_authoring_stage_registration_uses_typed_owner_and_surviving_journal(self):
        spec = importlib.util.spec_from_file_location("flux_harness", Path(__file__).with_name("test-apps-native-authoring.py"))
        h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(h)
        api, refs = NativeAPI(), {}
        with tempfile.TemporaryDirectory() as d:
            h.register_flux_owned(refs, "api", api, MANIFEST, "run", RENDER, REVISION, Path(d) / "evidence.json")
            self.assertIsInstance(refs["api"][0], FluxOwned)
            self.assertTrue((Path(d) / "evidence-flux-api.json").is_file())
            with self.assertRaises(RuntimeError):
                h.register_owned({}, "unsafe", api, MANIFEST, "milo-system", "run", "kustomizations", "/unused")


if __name__ == "__main__":
    unittest.main()
