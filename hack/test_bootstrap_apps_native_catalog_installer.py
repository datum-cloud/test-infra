"""Offline failure gates for scoped Catalog CRD/webhook installer."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("bootstrap-apps-native-catalog-installer.py")
SPEC = importlib.util.spec_from_file_location("catalog_installer", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class CatalogInstallerTest(unittest.TestCase):
    def test_root_reconnect_failure_prevents_role_create(self):
        stage = mock.Mock()
        with mock.patch.object(installer, "wait_authenticated_root",
                               side_effect=RuntimeError("root not ready")), \
                mock.patch.object(installer, "create_owned") as create:
            with self.assertRaisesRegex(RuntimeError, "root not ready"):
                installer.create_root_role_after_reconnect(mock.Mock(), {}, {},
                    Path("/tmp/no-evidence"), seconds=0.01, mark_stage=stage)
        create.assert_not_called()
        stage.assert_not_called()

    def test_exact_source_crd_and_webhook_role_and_user_binding(self):
        role, binding = installer.exact_role_manifests()
        self.assertEqual(set(role["rules"][1]["resourceNames"]), installer.EXACT_CRDS)
        self.assertEqual(binding["subjects"][0]["name"], installer.USER)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rbac.yaml"
            role["rules"].append({"apiGroups": [""], "resources": ["secrets"],
                                  "verbs": ["get"]})
            path.write_text(installer.yaml.safe_dump_all([role, binding]))
            with mock.patch.object(installer, "ROOT", Path(directory)):
                (Path(directory) / "components/service-catalog").mkdir(parents=True)
                (Path(directory) / "components/service-catalog/installer-rbac.yaml").write_text(
                    path.read_text())
                with self.assertRaisesRegex(RuntimeError, "not exact"):
                    installer.exact_role_manifests()

    def test_partial_setup_retains_installer_if_remote_graph_is_active(self):
        host = mock.Mock()
        host.optional.return_value = {"metadata": {"uid": "active"}}
        milo = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            state_path = Path(directory) / "state.json"
            evidence = {"run_id": "a" * 24, "attempted": {}, "intents": {}}
            state = {"run_id": "a" * 24}
            with mock.patch.object(installer, "APIProxy") as proxy:
                with self.assertRaisesRegex(RuntimeError, "graph .* remains active"):
                    installer.cleanup(host, milo, evidence, state, path, state_path,
                                      Path(directory))
                proxy.assert_not_called()

    def test_unknown_uid_recovery_verifies_exact_secret_bytes_before_delete(self):
        row = installer.ROWS[2]
        manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": installer.SECRET, "namespace": "milo-system",
            "labels": {installer.RUN_LABEL: "b" * 24}}, "type": "Opaque",
            "data": {"kubeconfig": "high-entropy-private-value"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            evidence = {"run_id": "b" * 24, "attempted": {"secret": None},
                        "intents": {"secret": installer.intent_digest(manifest)}}
            installer.atomic_json(path, evidence)
            actual = copy.deepcopy(manifest)
            actual["metadata"].update({"uid": "uid", "resourceVersion": "7"})
            host = mock.Mock()
            host.optional.side_effect = [actual, None]
            proxy = mock.Mock()
            proxy.delete.side_effect = lambda *_: self.assertEqual(
                json.loads(path.read_text())["attempted"]["secret"], "uid")
            installer.delete_owned(host, proxy, row, evidence, path)
            proxy.delete.assert_called_once()
            evidence["attempted"]["secret"] = None
            actual["data"]["kubeconfig"] = "foreign-value"
            host.optional.side_effect = [actual]
            proxy.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "unknown-UID"):
                installer.delete_owned(host, proxy, row, evidence, path)
            proxy.delete.assert_not_called()

    def test_lost_create_response_has_private_intent_without_raw_token(self):
        role, _ = installer.exact_role_manifests()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            evidence = {"run_id": "c" * 24, "attempted": {}, "intents": {}}
            host = mock.Mock()
            host.create.side_effect = RuntimeError("response lost")
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                installer.create_owned(host, installer.ROWS[0], role, evidence, path)
            journal = json.loads(path.read_text())
            self.assertEqual(journal["attempted"], {"role": None})
            self.assertRegex(journal["intents"]["role"], r"^[0-9a-f]{64}$")

    def test_retry_after_auth_cas_restored_but_rollout_failed_restarts_pods(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "apps-a1t-catalog-installer-private"
            private.mkdir(mode=0o700)
            evidence_path = Path(directory) / "evidence.json"
            state_path = private / "state.json"
            evidence = {"run_id": "d" * 24, "attempted": {}}
            state = {"run_id": "d" * 24, "auth_intent": True, "auth_uid": "auth-uid",
                     "old_b64": "restored", "new_b64": "added",
                     "core_suspend_intent": False}
            installer.atomic_json(evidence_path, evidence)
            installer.atomic_json(state_path, state)
            host, milo = mock.Mock(), mock.Mock()
            host.json.return_value = {"metadata": {"uid": "auth-uid"},
                                      "data": {"tokens.csv": "restored"}}
            with (mock.patch.object(installer, "absence_census"),
                  mock.patch.object(installer, "APIProxy"),
                  mock.patch.object(installer, "rollout_milo", side_effect=[
                      RuntimeError("restart lost"),
                      {"deployment_uid": "milo-uid", "generation": 8,
                       "pod_uids": ["new-pod"]}]) as rollout):
                with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                    installer.cleanup(host, milo, evidence, state,
                                      evidence_path, state_path, private)
                self.assertTrue(state_path.exists())
                self.assertEqual(evidence["cleanup"], "incomplete")
                installer.cleanup(host, milo, evidence, state,
                                  evidence_path, state_path, private)
            self.assertEqual(rollout.call_count, 2)
            self.assertEqual(state["restored_auth_rollout"]["pod_uids"], ["new-pod"])
            self.assertEqual(evidence["cleanup"], "complete")

    def test_lost_core_resume_response_recovers_only_exact_original_spec(self):
        original = {"interval": "10m", "sourceRef": {"kind": "GitRepository",
                    "name": "milo"}}
        suspended = {"metadata": {"uid": "core-uid", "generation": 8,
                                  "resourceVersion": "suspended-rv"},
                     "spec": {**original, "suspend": True}}
        restored = {"metadata": {"uid": "core-uid", "generation": 9,
                                 "resourceVersion": "restored-rv"},
                    "spec": copy.deepcopy(original)}
        state = {"core_uid": "core-uid", "core_generation": 7,
                 "core_spec": original, "suspended_generation": 8,
                 "core_suspend_present": False}
        host = mock.Mock()
        host.json.side_effect = [suspended, restored]
        host.patch_json.side_effect = RuntimeError("response lost")
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            installer.resume_core(host, Path("/private"), state)
        installer.resume_core(host, Path("/private"), state)
        self.assertEqual(state["resumed_generation"], 9)
        host.patch_json.assert_called_once()
        changed = copy.deepcopy(restored)
        changed["spec"]["path"] = "./foreign"
        host.json.side_effect = [changed]
        with self.assertRaisesRegex(RuntimeError, "exact original"):
            installer.resume_core(host, Path("/private"), state)


if __name__ == "__main__":
    unittest.main()
