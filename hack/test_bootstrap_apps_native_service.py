"""Offline failure checks for the real public-API Catalog prerequisite."""

import copy
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HACK = Path(__file__).resolve().parent
sys.path.insert(0, str(HACK))
spec = importlib.util.spec_from_file_location(
    "native_service_fixture", HACK / "bootstrap-apps-native-service.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
PINNED_CATALOG = Path("/tmp/apps-service-catalog-source-20261021")


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.catalog = Path(cls.temporary.name)
        samples = cls.catalog / "config/samples"
        samples.mkdir(parents=True)
        (samples / "services_v1alpha1_service.yaml").write_text(
            "apiVersion: services.miloapis.com/v1alpha1\nkind: Service\n"
            "metadata:\n  name: compute-miloapis-com\nspec:\n"
            "  serviceName: compute.miloapis.com\n  phase: Published\n"
            "  owner:\n    producerProjectRef:\n      name: compute-platform\n")
        (samples / "services_v1alpha1_serviceavailability.yaml").write_text(
            "apiVersion: services.miloapis.com/v1alpha1\nkind: ServiceAvailability\n"
            "metadata:\n  name: compute-miloapis-com--us-central1-a\nspec:\n"
            "  serviceRef:\n    name: compute-miloapis-com\n"
            "  locationRef:\n    name: us-central1-a\n")
        entitlement = cls.catalog / "test/e2e-milo/service-provisioning"
        entitlement.mkdir(parents=True)
        (entitlement / "04-create-entitlement.yaml").write_text(
            "apiVersion: services.miloapis.com/v1alpha1\nkind: ServiceEntitlement\n"
            "metadata:\n  name: e2e-provisioning-networking\nspec:\n"
            "  serviceRef:\n    name: e2e-provisioning-networking\n")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.manifests = fixture.manifests(self.catalog, "a" * 24)

    def test_exact_upstream_contract_and_project_routes(self):
        if PINNED_CATALOG.is_dir():
            fixture.source_contract(fixture.Commands(), PINNED_CATALOG)
        self.assertEqual(self.manifests["service"]["spec"]["owner"],
                         {"producerProjectRef": {"name": fixture.PROVIDER}})
        self.assertEqual(self.manifests["configuration"]["spec"]["locations"],
                         {"supportedClasses": ["datum-managed"]})
        self.assertEqual(fixture.SERVICE_CONSUMER, "sc-7bbe8ad350fb1b5a")
        self.assertIn("/projects/apps-a1t-consumer-a/control-plane/",
                      fixture.PATHS["entitlement"])
        self.assertNotIn("apps-a1t-consumer-b", fixture.PATHS["entitlement"])

    def test_normal_python_invocation_does_not_dirty_signed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("bootstrap-apps-native-service.py",
                         "apps_native_fixture_scope.py", "apps_native_support.py"):
                shutil.copy2(HACK / name, root / name)
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            result = subprocess.run([sys.executable, str(root /
                "bootstrap-apps-native-service.py"), "--help"],
                env=environment, capture_output=True, text=True,
                check=False, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr[:200])
            self.assertFalse(list(root.glob("**/__pycache__/**")))

    def test_native_finalizers_are_source_exact(self):
        for key, value in (("service", "services.miloapis.com/service"),
                           ("configuration", "services.miloapis.com/serviceconfiguration-protection"),
                           ("entitlement", "services.miloapis.com/service-entitlement")):
            observed = copy.deepcopy(self.manifests[key])
            observed["metadata"].update(uid="uid-1", finalizers=[value])
            self.assertEqual(fixture.exact_created(key, observed,
                                                    self.manifests[key], "uid-1"), "uid-1")
            observed["metadata"]["finalizers"] = ["foreign/finalizer"]
            with self.assertRaises(RuntimeError):
                fixture.exact_created(key, observed, self.manifests[key], "uid-1")

    def test_location_binding_condition_has_no_fabricated_generation(self):
        binding = {"metadata": {"generation": 5},
                   "status": {"conditions": [{"type": "Available", "status": "True"}]}}
        self.assertTrue(fixture.binding_available(binding))
        self.assertFalse(fixture.condition(binding, "Available"))
        binding["status"]["conditions"][0]["status"] = "False"
        self.assertFalse(fixture.binding_available(binding))

    def test_future_condition_generation_cannot_ack_current_object(self):
        obj = {"metadata": {"generation": 4},
               "status": {"conditions": [{"type": "Ready", "status": "True",
                                          "observedGeneration": 5}]}}
        self.assertFalse(fixture.condition(obj, "Ready"))
        obj["status"]["conditions"][0]["observedGeneration"] = 4
        self.assertTrue(fixture.condition(obj, "Ready"))

    def test_empty_or_incomplete_webhook_registration_refused(self):
        ca = b"public-ca"
        digest = fixture.hashlib.sha256(ca).hexdigest()
        name = "services-mutating"
        obj = {"metadata": {"uid": "hook-uid"}, "webhooks": [
            {"name": hook, "clientConfig": {"caBundle":
             fixture.base64.b64encode(ca).decode()}}
            for hook in sorted(fixture.WEBHOOK_NAMES[name])]}
        self.assertTrue(fixture.webhook_matches(obj, name, "hook-uid", digest))
        obj["webhooks"] = []
        self.assertFalse(fixture.webhook_matches(obj, name, "hook-uid", digest))
        obj["webhooks"] = [{"name": "mservice.kb.io", "clientConfig": {"caBundle":
                           fixture.base64.b64encode(ca).decode()}}]
        self.assertFalse(fixture.webhook_matches(obj, name, "hook-uid", digest))

    def test_intent_write_failure_prevents_create(self):
        with mock.patch.object(fixture, "save", side_effect=OSError("disk full")), \
             mock.patch.object(fixture, "request") as send:
            with self.assertRaises(OSError):
                fixture.create_one(object(), "service", self.manifests["service"],
                                   {"attempted": {}, "intents": {}}, Path("/tmp/journal"))
            send.assert_not_called()

    def test_lost_create_uid_recovered_only_for_exact_intent(self):
        manifest = self.manifests["service"]
        evidence = {"attempted": {"service": None}, "intents": {
            "service": fixture.hashlib.sha256(fixture.json.dumps(
                manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}}
        observed = copy.deepcopy(manifest)
        observed["metadata"].update(uid="owned-uid", resourceVersion="7")
        with mock.patch.object(fixture, "get_optional", return_value=observed):
            self.assertEqual(fixture.owned_for_delete(None, "service", manifest,
                                                      evidence)["metadata"]["uid"], "owned-uid")
            observed["metadata"]["annotations"] = {"foreign": "change"}
            with self.assertRaises(RuntimeError):
                fixture.owned_for_delete(None, "service", manifest, evidence)
            observed["metadata"].pop("annotations")
            evidence["intents"]["service"] = "wrong"
            with self.assertRaises(RuntimeError):
                fixture.owned_for_delete(None, "service", manifest, evidence)

    def test_known_uid_replacement_refuses_delete(self):
        manifest = self.manifests["service"]
        observed = copy.deepcopy(manifest)
        observed["metadata"].update(uid="replacement", resourceVersion="8")
        evidence = {"attempted": {"service": "original"}, "intents": {
            "service": fixture.hashlib.sha256(fixture.json.dumps(
                manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}}
        with mock.patch.object(fixture, "get_optional", return_value=observed):
            with self.assertRaises(RuntimeError):
                fixture.owned_for_delete(None, "service", manifest, evidence)

    def test_known_uid_disappearance_without_delete_intent_is_not_success(self):
        evidence = {"attempted": {"service": "owned"}, "intents": {},
                    "delete_intent": {}, "removed": {}}
        with mock.patch.object(fixture, "owned_for_delete", return_value=None), \
             mock.patch.object(fixture, "save"):
            with self.assertRaises(RuntimeError):
                fixture.delete_one(None, fixture.Commands(), "service",
                                   self.manifests["service"], evidence,
                                   Path("/tmp/journal"))
            evidence["delete_intent"]["service"] = True
            fixture.delete_one(None, fixture.Commands(), "service",
                               self.manifests["service"], evidence,
                               Path("/tmp/journal"))
            self.assertEqual(evidence["removed"]["service"], "owned")

    def test_unknown_uid_and_absent_object_remains_ambiguous(self):
        evidence = {"attempted": {"service": None}, "intents": {},
                    "delete_intent": {}, "removed": {}}
        with mock.patch.object(fixture, "owned_for_delete", return_value=None), \
             mock.patch.object(fixture, "save"):
            with self.assertRaises(RuntimeError):
                fixture.delete_one(None, fixture.Commands(), "service",
                                   self.manifests["service"], evidence,
                                   Path("/tmp/journal"))

    def test_lost_entitlement_create_recovers_uid_before_foreign_census(self):
        evidence = {"complete": False, "cleanup": "not-requested",
                    "attempted": {"entitlement": None}, "children": {}}
        recovered = {"metadata": {"uid": "entitlement-uid"}}
        seen = []
        def census(_proxy, journal):
            seen.append(journal["attempted"]["entitlement"])
        with mock.patch.object(fixture, "save"), \
             mock.patch.object(fixture, "control_absent"), \
             mock.patch.object(fixture, "owned_for_delete", return_value=recovered), \
             mock.patch.object(fixture, "no_foreign_compute_dependents",
                               side_effect=census), \
             mock.patch.object(fixture, "child_census"), \
             mock.patch.object(fixture, "checked_children_gone"), \
             mock.patch.object(fixture, "delete_one"):
            fixture.cleanup(None, fixture.Commands(), self.manifests,
                            evidence, Path("/tmp/journal"), {})
        self.assertEqual(seen, ["entitlement-uid"])
        self.assertEqual(evidence["cleanup"], "complete")

    def test_native_child_finalizer_barrier_blocks_parent_delete(self):
        evidence = {"complete": False, "cleanup": "not-requested",
                    "attempted": {"entitlement": "uid-1"}, "children": {}}
        with mock.patch.object(fixture, "save"), \
             mock.patch.object(fixture, "control_absent"), \
             mock.patch.object(fixture, "no_foreign_compute_dependents"), \
             mock.patch.object(fixture, "child_census"), \
             mock.patch.object(fixture, "delete_one") as delete, \
             mock.patch.object(fixture, "checked_children_gone",
                               side_effect=RuntimeError("native child still live")):
            with self.assertRaises(RuntimeError):
                fixture.cleanup(None, fixture.Commands(), self.manifests,
                                evidence, Path("/tmp/journal"), {})
        self.assertEqual([call.args[2] for call in delete.call_args_list],
                         ["entitlement"])
        self.assertEqual(evidence["cleanup"], "incomplete")

    def test_foreign_child_census_prevents_any_delete(self):
        evidence = {"complete": True, "cleanup": "not-requested",
                    "attempted": {"entitlement": "uid-1"}, "children": {}}
        with mock.patch.object(fixture, "save"), \
             mock.patch.object(fixture, "control_absent"), \
             mock.patch.object(fixture, "no_foreign_compute_dependents"), \
             mock.patch.object(fixture, "child_census",
                               side_effect=RuntimeError("foreign child")), \
             mock.patch.object(fixture, "delete_one") as delete:
            with self.assertRaises(RuntimeError):
                fixture.cleanup(None, fixture.Commands(), self.manifests,
                                evidence, Path("/tmp/journal"), {})
            delete.assert_not_called()

    def test_control_403_is_not_absence(self):
        with mock.patch.object(fixture, "request", return_value=(403, {"kind": "Status"})):
            with self.assertRaises(RuntimeError):
                fixture.get_optional(mock.Mock(kube=mock.Mock(
                    commands=mock.Mock(deadline=None))),
                    fixture.CONTROL_PATHS["entitlement"])

    def test_child_recovery_rejects_foreign_projection(self):
        source = {"spec": {"topology": {"topology.datum.net/city-code": "DFW"}}}
        binding = {"apiVersion": "networking.datumapis.com/v1alpha",
                   "kind": "LocationBinding",
                   "metadata": {"name": "dfw", "uid": "uid-1",
                                "labels": {"app.kubernetes.io/managed-by": "services.miloapis.com",
                                           "networking.datumapis.com/location": "dfw",
                                           "networking.datumapis.com/class": "datum-managed"}},
                   "spec": {"locationRef": {"name": "dfw"},
                            "locationClassName": "datum-managed",
                            "topology": copy.deepcopy(source["spec"]["topology"])}}
        self.assertEqual(fixture.child_source_shape("binding", binding, source), "uid-1")
        binding["kind"] = "ConfigMap"
        with self.assertRaises(RuntimeError):
            fixture.child_source_shape("binding", binding, source)
        binding["kind"] = "LocationBinding"
        binding["spec"]["topology"]["topology.datum.net/city-code"] = "MIA"
        with self.assertRaises(RuntimeError):
            fixture.child_source_shape("binding", binding, source)

    def test_generated_consumer_extra_spec_is_not_adopted(self):
        consumer = {"apiVersion": "services.miloapis.com/v1alpha1",
                    "kind": "ServiceConsumer",
                    "metadata": {"name": fixture.SERVICE_CONSUMER, "uid": "uid-1"},
                    "spec": {"serviceRef": {"name": fixture.SERVICE_NAME},
                             "consumerProjectRef": {"name": fixture.CONSUMER}}}
        self.assertEqual(fixture.child_source_shape("consumer", consumer, {}), "uid-1")
        consumer["spec"]["requestMessage"] = "unreviewed"
        with self.assertRaises(RuntimeError):
            fixture.child_source_shape("consumer", consumer, {})


if __name__ == "__main__":
    unittest.main()
