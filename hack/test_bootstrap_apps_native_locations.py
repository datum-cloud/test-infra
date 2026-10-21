"""Offline safety and source contracts for real Locations readiness fixture."""

import importlib.util
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HACK = Path(__file__).resolve().parent
sys.path.insert(0, str(HACK))
SPEC = importlib.util.spec_from_file_location(
    "locations_bootstrap", HACK / "bootstrap-apps-native-locations.py")
locations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(locations)


class LocationsBootstrapTest(unittest.TestCase):
    def test_root_reconnect_failure_prevents_both_rbac_creates(self):
        stage = mock.Mock()
        with mock.patch.object(locations, "wait_authenticated_root",
                               side_effect=RuntimeError("root not ready")), \
                mock.patch.object(locations, "row_create") as create:
            with self.assertRaisesRegex(RuntimeError, "root not ready"):
                locations.create_root_rbac_after_reconnect(mock.Mock(), {}, {},
                    Path("/tmp/no-evidence"), seconds=0.01, mark_stage=stage)
        create.assert_not_called()
        stage.assert_not_called()

    def test_source_pin_and_narrow_role_patches(self):
        root = Path(__file__).resolve().parent.parent / "components/locations"
        source = locations.yaml.safe_load((root / "source.yaml").read_text())
        role = locations.yaml.safe_load((root / "readiness-role-patches.yaml").read_text())
        host = locations.yaml.safe_load((root / "host.yaml").read_text())
        self.assertEqual(source["spec"]["ref"], {"commit": locations.LOCATIONS_SHA})
        self.assertEqual(host["spec"]["images"],
                         [{"name": "ghcr.io/milo-os/locations",
                           "digest": locations.LOCATIONS_IMAGE}])
        self.assertNotIn("servinglocations", role[0]["patch"])
        self.assertIn("apps-a1t-locations-operator", role[1]["patch"])

    def test_exact_source_owned_host_and_narrow_role_render_when_source_present(self):
        source = Path(os.environ.get("APPS_LOCATIONS_SOURCE",
                                     "/private/tmp/apps-locations-source-20261021"))
        if not source.is_dir():
            self.skipTest("pinned Locations source clone unavailable in offline runner")
        manifests = locations.offline_contract(
            locations.Commands(), source)
        self.assertEqual(manifests["source"]["spec"]["ref"],
                         {"commit": locations.LOCATIONS_SHA})
        self.assertEqual(manifests["host"]["spec"]["images"],
                         [{"name": "ghcr.io/milo-os/locations",
                           "digest": locations.LOCATIONS_IMAGE}])

    def test_stale_status_cannot_count_as_controller_readiness(self):
        item = {"metadata": {"generation": 2}, "status": {"conditions": [
            {"type": "Ready", "status": "True", "observedGeneration": 1}]}}
        self.assertFalse(locations.condition_ready(item, "Ready"))
        item["status"]["conditions"][0]["observedGeneration"] = 2
        self.assertTrue(locations.condition_ready(item, "Ready"))

    def test_running_image_needs_deployment_owned_ready_pod_and_verified_digest(self):
        image = "ghcr.io/milo-os/locations@" + locations.LOCATIONS_IMAGE
        deployment = {"metadata": {"uid": "dep", "generation": 3},
            "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"observedGeneration": 3, "readyReplicas": 1,
                       "availableReplicas": 1}}
        rs = {"metadata": {"uid": "rs", "ownerReferences": [
            {"kind": "Deployment", "uid": "dep", "controller": True}]}}
        pod = {"metadata": {"uid": "pod", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "rs", "controller": True}]},
            "spec": {"nodeName": "arm-node", "containers": [{"image": image}]},
            "status": {"phase": "Running", "conditions": [
                {"type": "Ready", "status": "True"}], "containerStatuses": [
                {"ready": True, "imageID": "containerd://" +
                 locations.LOCATIONS_ARM64_CONFIG}]}}
        node = {"metadata": {"labels": {"kubernetes.io/arch": "arm64"}}}
        host = mock.Mock()
        host.json.side_effect = [deployment, {"items": [rs]}, {"items": [pod]}, node]
        self.assertEqual(locations.verified_running_manager(host)["image_id"],
                         pod["status"]["containerStatuses"][0]["imageID"])
        pod["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:foreign"
        host.json.side_effect = [deployment, {"items": [rs]}, {"items": [pod]}, node]
        with self.assertRaisesRegex(RuntimeError, "running image ID differs"):
            locations.verified_running_manager(host)
        pod["status"]["containerStatuses"][0]["imageID"] = (
            "containerd://" + locations.LOCATIONS_ARM64_CONFIG)
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
        host.json.side_effect = [deployment, {"items": [rs]}, {"items": [pod]}, node]
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            locations.verified_running_manager(host)

    def test_lost_create_response_preserves_intent_and_does_not_claim_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            evidence = {"run_id": "a" * 24, "attempted": {}}
            locations.atomic_json(path, evidence)
            cluster = mock.Mock()
            cluster.create.side_effect = RuntimeError("lost response")
            with self.assertRaisesRegex(RuntimeError, "lost response"):
                locations.row_create(cluster, locations.ROWS[0],
                    {"apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": "irrelevant"}}, evidence, path)
            self.assertEqual(json.loads(path.read_text())["attempted"], {"source": None})

    def test_location_delete_failure_retains_manager_and_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            state_path = Path(directory) / "apps-a1t-locations-private-state.json"
            evidence = {"run_id": "b" * 24, "host_context": "kind-apps-a1t-ci-test",
                        "milo_port": 43443, "scope_identity": {"same": True},
                        "attempted": {"location": "owned-uid"}}
            state = {"run_id": "b" * 24, "auth_intent": True,
                     "core_suspend_intent": True}
            locations.atomic_json(path, evidence)
            locations.atomic_json(state_path, state)
            root = mock.Mock()
            root.optional.return_value = {"metadata": {
                "uid": "owned-uid", "resourceVersion": "7"}}
            host = mock.Mock()
            with mock.patch.object(locations, "scope_identity", return_value={"same": True}):
                with mock.patch.object(locations, "APIProxy") as proxy_class:
                    proxy_class.return_value.delete.side_effect = RuntimeError("precondition failed")
                    with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                        locations.cleanup(host, root, evidence, state, path, state_path,
                                          Path(directory))
            host.optional.assert_not_called()
            host.patch_json.assert_not_called()
            self.assertEqual(json.loads(path.read_text())["cleanup"], "incomplete")
            self.assertTrue(state_path.exists())

    def test_unknown_uid_requires_intent_and_persists_before_delete(self):
        row = locations.ROWS[5]
        manifest = {"apiVersion": "locations.miloapis.com/v1alpha1",
                    "kind": "LocationClass", "metadata": {"name": locations.CLASS,
                    "labels": {locations.RUN_LABEL: "c" * 24}},
                    "spec": {"controllerName": locations.CONTROLLER}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            evidence = {"run_id": "c" * 24, "attempted": {"class": None},
                        "intents": {"class": locations.intent_digest(manifest)}}
            locations.atomic_json(path, evidence)
            actual = copy.deepcopy(manifest)
            actual["metadata"].update({"uid": "owned", "resourceVersion": "7"})
            cluster = mock.Mock()
            cluster.optional.side_effect = [actual, None]
            proxy = mock.Mock()
            proxy.delete.side_effect = lambda *_: self.assertEqual(
                json.loads(path.read_text())["attempted"]["class"], "owned")
            locations.owned_delete(cluster, proxy, row, evidence, path)
            proxy.delete.assert_called_once()
            evidence["attempted"]["class"] = None
            actual["spec"]["controllerName"] = "foreign"
            cluster.optional.side_effect = [actual]
            proxy.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "unknown-UID"):
                locations.owned_delete(cluster, proxy, row, evidence, path)
            proxy.delete.assert_not_called()

    def test_known_uid_replacement_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            evidence = {"run_id": "d" * 24, "attempted": {"class": "original"}}
            cluster = mock.Mock()
            cluster.optional.return_value = {"metadata": {"uid": "replacement",
                "resourceVersion": "9", "labels": {locations.RUN_LABEL: "d" * 24}}}
            proxy = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, "replaced"):
                locations.owned_delete(cluster, proxy, locations.ROWS[5], evidence, path)
            proxy.delete.assert_not_called()

    def test_cleanup_retains_owned_namespace_and_refuses_foreign_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            private.mkdir()
            path = Path(directory) / "journal.json"
            state_path = private / "state.json"
            evidence = {"run_id": "e" * 24, "host_context": "kind-apps-a1t-ci-test",
                        "milo_port": 43443, "scope_identity": {"same": True},
                        "attempted": {"namespace": "namespace-owned"}}
            state = {"run_id": "e" * 24}
            locations.atomic_json(path, evidence)
            locations.atomic_json(state_path, state)
            host = mock.Mock()
            namespace = {"metadata": {"uid": "namespace-owned", "resourceVersion": "7",
                "labels": {locations.RUN_LABEL: "e" * 24}}}
            host.optional.side_effect = lambda kind, name, namespace=None: (
                namespace_obj if kind == "namespace" else None)
            namespace_obj = namespace
            root = mock.Mock()
            root.optional.return_value = None
            with mock.patch.object(locations, "scope_identity", return_value={"same": True}):
                with mock.patch.object(locations, "APIProxy") as proxy_class:
                    locations.cleanup(host, root, evidence, state, path, state_path, private)
                    proxy_class.return_value.delete.assert_not_called()
            self.assertEqual(evidence["cleanup"], "complete-with-retained-namespace")
            self.assertEqual(evidence["retained_namespace"]["uid"], "namespace-owned")
            # A replacement with the same run label cannot be adopted.
            evidence["cleanup"] = "not-requested"
            namespace_obj = copy.deepcopy(namespace)
            namespace_obj["metadata"]["uid"] = "foreign"
            private.mkdir()
            locations.atomic_json(state_path, state)
            with mock.patch.object(locations, "scope_identity", return_value={"same": True}):
                with mock.patch.object(locations, "APIProxy") as proxy_class:
                    with self.assertRaisesRegex(RuntimeError, "namespace identity changed"):
                        locations.cleanup(host, root, evidence, state, path, state_path, private)
                    proxy_class.assert_not_called()

    def test_resume_refuses_changed_core_generation(self):
        host = mock.Mock()
        host.json.return_value = {"metadata": {"uid": "core-uid", "generation": 9,
                                               "resourceVersion": "rv"},
                                  "spec": {"suspend": True}}
        state = {"core_uid": "core-uid", "core_generation": 3,
                 "suspended_generation": 4, "core_suspend_present": False}
        with self.assertRaisesRegex(RuntimeError, "generation changed"):
            locations.resume_core(host, Path("/tmp"), state)
        host.patch_json.assert_not_called()

    def test_retry_after_auth_cas_restored_but_rollout_failed_restarts_pods(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "apps-a1t-locations-private"
            private.mkdir(mode=0o700)
            evidence_path = Path(directory) / "evidence.json"
            state_path = private / "state.json"
            evidence = {"run_id": "f" * 24, "scope_identity": {"same": True},
                        "host_context": "kind-apps-a1t-ci-test", "milo_port": 43443,
                        "attempted": {}}
            state = {"run_id": "f" * 24, "auth_intent": True,
                     "auth_uid": "auth-uid", "old_b64": "restored",
                     "new_b64": "added"}
            locations.atomic_json(evidence_path, evidence)
            locations.atomic_json(state_path, state)
            host, milo = mock.Mock(), mock.Mock()
            host.optional.return_value = None
            milo.optional.return_value = None
            host.json.return_value = {"metadata": {"uid": "auth-uid"},
                                      "data": {"tokens.csv": "restored"}}
            with (mock.patch.object(locations, "scope_identity",
                                    return_value={"same": True}),
                  mock.patch.object(locations, "APIProxy"),
                  mock.patch.object(locations, "rollout_milo", side_effect=[
                      RuntimeError("restart lost"),
                      {"deployment_uid": "milo-uid", "generation": 8,
                       "pod_uids": ["new-pod"]}]) as rollout):
                with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                    locations.cleanup(host, milo, evidence, state,
                                      evidence_path, state_path, private)
                self.assertTrue(state_path.exists())
                self.assertEqual(evidence["cleanup"], "incomplete")
                locations.cleanup(host, milo, evidence, state,
                                  evidence_path, state_path, private)
            self.assertEqual(rollout.call_count, 2)
            self.assertEqual(state["restored_auth_rollout"]["pod_uids"], ["new-pod"])
            self.assertEqual(evidence["cleanup"], "complete-with-retained-namespace")


if __name__ == "__main__":
    unittest.main()
