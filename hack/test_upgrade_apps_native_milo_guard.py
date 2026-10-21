"""Offline upgrade journal/CAS/failure tests. Never contacts a cluster."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("guard_upgrade", Path(__file__).with_name("upgrade-apps-native-milo-guard.py"))
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def baseline():
    result = {}
    for key, filename in (("source", "source.yaml"), ("core", "core.yaml")):
        obj = yaml.safe_load((upgrade.ROOT / "components/milo-core" / filename).read_text())
        obj["metadata"].update({"uid": key + "-uid", "resourceVersion": "10", "generation": 1})
        obj["status"] = {"observedGeneration": 1, "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}]}
        if key == "source":
            obj["status"]["artifact"] = {"revision": "sha1:" + upgrade.BASE_SHA}
        else:
            obj["status"]["lastAppliedRevision"] = "sha1:" + upgrade.BASE_SHA
        result[key] = obj
    return result


class FakeHost:
    def __init__(self, current, journal):
        self.current, self.journal = current, journal
        self.calls = []
        self.lost_at = None
        self.conflict_at = None
        self.fail_at = None

    def json(self, *_args):
        key = "source" if _args[-2] == "gitrepository" else "core"
        return copy.deepcopy(self.current[key])

    def patch_json(self, _private, kind, _name, operations, **_kwargs):
        step = len(self.calls) + 1
        intent = json.loads(self.journal.read_text())["pending"]
        assert intent is not None and intent["step"] == step, "mutation had no preposted intent"
        key = "source" if kind == "gitrepository" else "core"
        obj = self.current[key]
        self.calls.append(copy.deepcopy(operations))
        if self.conflict_at == step:
            obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        assert operations[0] == {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]}
        if self.fail_at == step or operations[1]["value"] != obj["metadata"]["resourceVersion"]:
            raise RuntimeError("API conflict before mutation")
        for op in operations[2:]:
            field = op["path"].split("/")[-1]
            if op["op"] == "remove":
                del obj["spec"][field]
            else:
                obj["spec"][field] = op["value"]
        obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        if self.lost_at == step:
            raise RuntimeError("success response lost")


class UpgradeTests(unittest.TestCase):
    def fixture(self, directory, explicit_suspend=False):
        current = baseline()
        if explicit_suspend:
            current["core"]["spec"]["suspend"] = False
        state = upgrade.initial(current, {"fixture": "exact"}, "kind-apps-a1t-ci-test")
        path = Path(directory).resolve() / "journal.json"
        upgrade.save(path, state, exclusive=True)
        return FakeHost(current, path), state, path

    def test_cas_order_and_original_suspend_representation(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as directory:
                host, state, path = self.fixture(directory, explicit)
                upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
                self.assertEqual(state["completed_steps"], 4)
                self.assertTrue(upgrade.matches(host.current, state, 4))
                self.assertEqual([ops[-1]["path"] for ops in host.calls], ["/spec/suspend", "/spec/ref", "/spec/images", "/spec/suspend"])
                self.assertEqual("suspend" in host.current["core"]["spec"], explicit)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual([entry["operation"] for entry in state["history"]], list(upgrade.STEPS))

    def test_every_lost_reply_recovers_exact_after_state_without_repeating_write(self):
        for step in range(1, 5):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                host, state, path = self.fixture(directory)
                host.lost_at = step
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
                persisted = json.loads(path.read_text())
                self.assertEqual(persisted["pending"]["step"], step)
                self.assertEqual(persisted["completed_steps"], step - 1)
                host.lost_at = None
                upgrade.advance(host, Path(directory), path, persisted, lambda: {"fixture": "exact"})
                self.assertEqual(len(host.calls), 4)
                self.assertEqual(persisted["completed_steps"], 4)
                self.assertTrue(persisted["history"][step - 1]["recovered"])

    def test_no_reply_before_commit_retains_before_state_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            host, state, path = self.fixture(directory)
            host.fail_at = 1
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
            self.assertTrue(upgrade.matches(host.current, state, 0))
            host.fail_at = None
            host.calls.clear()
            upgrade.advance(host, Path(directory), path, json.loads(path.read_text()), lambda: {"fixture": "exact"})
            self.assertTrue(upgrade.matches(host.current, state, 4))

    def test_status_rv_conflict_retains_intent_no_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            host, state, path = self.fixture(directory)
            host.conflict_at = 1
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
            self.assertEqual(state["completed_steps"], 0)
            self.assertTrue(upgrade.matches(host.current, state, 0))
            host.conflict_at = None
            host.calls.clear()
            upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
            self.assertEqual(host.calls[0][1]["value"], "11")

    def test_replacement_spec_drift_or_wrong_fixture_prevents_write(self):
        changes = (lambda h: h.current["core"]["metadata"].update(uid="replacement"),
                   lambda h: h.current["source"]["spec"].update(url="https://other.example/repo"),
                   lambda h: h.current["core"]["spec"].update(suspend=True))
        for change in changes:
            with tempfile.TemporaryDirectory() as directory:
                host, state, path = self.fixture(directory)
                change(host)
                with self.assertRaisesRegex(RuntimeError, "outside recorded"):
                    upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
                self.assertEqual(host.calls, [])
        with tempfile.TemporaryDirectory() as directory:
            host, state, path = self.fixture(directory)
            with self.assertRaisesRegex(RuntimeError, "binding changed"):
                upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "other"})
            self.assertEqual(host.calls, [])

    def test_lost_reply_then_unrelated_edit_cannot_be_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            host, state, path = self.fixture(directory)
            host.lost_at = 1
            with self.assertRaises(RuntimeError):
                upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
            host.current["core"]["spec"]["postBuild"] = {"substitute": {"unexpected": "value"}}
            with self.assertRaisesRegex(RuntimeError, "outside recorded"):
                upgrade.advance(host, Path(directory), path, json.loads(path.read_text()), lambda: {"fixture": "exact"})
            self.assertEqual(len(host.calls), 1)

    def test_baseline_refuses_wrong_source_image_transform_and_suspension(self):
        for key, field, value in (("source", "ref", {"branch": "main"}), ("core", "images", []),
                                  ("core", "suspend", True), ("core", "postBuild", {"substitute": {"x": "y"}})):
            current = baseline()
            current[key]["spec"][field] = value
            with self.assertRaises(RuntimeError):
                upgrade.baseline(current)

    def test_private_journal_no_replacement_or_public_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "journal.json"
            upgrade.save(path, {"state": "intent"}, exclusive=True)
            with self.assertRaises(RuntimeError):
                upgrade.save(path, {"state": "overwrite"}, exclusive=True)
            path.parent.chmod(0o755)
            with self.assertRaises(RuntimeError):
                upgrade.save(path, {"state": "unsafe"})

    def test_exact_loopback_and_credentials(self):
        doc = {"current-context": "milo-root", "contexts": [{"name": "milo-root", "context": {"cluster": "root", "user": "root"}}],
               "clusters": [{"name": "root", "cluster": {"server": "https://127.0.0.1:18443", "certificate-authority-data": "Y2E="}}],
               "users": [{"name": "root", "user": {"token": "private-sentinel"}}]}
        upgrade.exact_config(doc, "milo-root", True)
        for field, value in (("server", "https://127.0.0.1:443@elsewhere.example"), ("proxy-url", "https://elsewhere.example"), ("insecure-skip-tls-verify", True)):
            changed = copy.deepcopy(doc)
            changed["clusters"][0]["cluster"][field] = value
            with self.assertRaises(RuntimeError):
                upgrade.exact_config(changed, "milo-root", True)
        changed = copy.deepcopy(doc)
        changed["users"][0]["user"]["as"] = "admin"
        with self.assertRaises(RuntimeError):
            upgrade.exact_config(changed, "milo-root", True)

    def test_auth_baseline_no_added_user_or_token_material_in_journal(self):
        doc = {"users": [{"user": {"token": "private-admin-sentinel"}}]}
        def auth(rows):
            return {"data": {"tokens.csv": base64.b64encode(rows.encode()).decode()}}
        rows = 'private-admin-sentinel,admin,admin,system:masters\nprivate-user-sentinel,test-user,test-user,system:authenticated\n'
        upgrade.verify_auth(auth(rows), doc)
        with self.assertRaisesRegex(RuntimeError, "two-user"):
            upgrade.verify_auth(auth(rows + 'third,apps,apps,system:authenticated\n'), doc)
        state = upgrade.initial(baseline(), {"public_ca_hash": "public"}, "kind-apps-a1t-ci-test")
        self.assertNotIn("private-admin-sentinel", json.dumps(state))
        self.assertNotIn("spec", state["objects"]["core"])

    def test_rollout_requires_recorded_deployment_owner_and_arch_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            host, state, path = self.fixture(directory)
            upgrade.advance(host, Path(directory), path, state, lambda: {"fixture": "exact"})
            state["binding"]["apiserver_deployment_uid"] = "deployment-uid"
            host.current["source"]["status"]["artifact"]["revision"] = "sha1:" + upgrade.GUARD_SHA
            host.current["core"]["status"]["lastAppliedRevision"] = "sha1:" + upgrade.GUARD_SHA
            deployment = {"metadata": {"uid": "deployment-uid", "generation": 3}, "spec": {"replicas": 1,
                "selector": {"matchLabels": {"app.kubernetes.io/name": "milo-apiserver"}},
                "template": {"spec": {"containers": [{"name": "milo-apiserver", "image": upgrade.IMAGE + "@" + upgrade.GUARD_DIGEST}]}}},
                "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
            owner = lambda kind, uid: {"apiVersion": "apps/v1", "kind": kind, "uid": uid, "controller": True}
            sets = {"items": [{"metadata": {"uid": "rs-uid", "ownerReferences": [owner("Deployment", "deployment-uid")]}}]}
            image_id = upgrade.IMAGE + "@" + upgrade.GUARD_CHILDREN["arm64"]
            pods = {"items": [{"metadata": {"ownerReferences": [owner("ReplicaSet", "rs-uid")]}, "spec": {"nodeName": "node"},
                               "status": {"containerStatuses": [{"name": "milo-apiserver", "ready": True, "imageID": image_id}]}}]}
            def read(*args):
                if "deployment" in args: return deployment
                if "replicasets" in args: return sets
                if "pods" in args: return pods
                if "nodes" in args: return {"items": [{"metadata": {"name": "node"}, "status": {"nodeInfo": {"architecture": "arm64"}}}]}
                return FakeHost.json(host, *args)
            host.json = read
            self.assertEqual(upgrade.rollout(host, state), [image_id])
            pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = upgrade.IMAGE + "@" + upgrade.GUARD_CHILDREN["amd64"]
            self.assertIsNone(upgrade.rollout(host, state))
            pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = image_id
            pods["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "spoofed-rs"
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                upgrade.rollout(host, state)
            pods["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "rs-uid"
            deployment["metadata"]["uid"] = "replacement"
            with self.assertRaisesRegex(RuntimeError, "Deployment replaced"):
                upgrade.rollout(host, state)

    def test_fixture_census_refuses_project_reuse_active_stages_and_wrong_service(self):
        bootstrap = {"host_context": "kind-apps-a1t-ci-test", "milo_source": upgrade.BASE_SHA,
                     "organization": upgrade.ORG, "projects": list(upgrade.PROJECTS), "run_id": "a" * 24,
                     "cleanup": "not-requested", "uids": {name: name + "-uid" for name in (upgrade.ORG, *upgrade.PROJECTS)}}
        upgrade.verify_bootstrap(bootstrap, bootstrap["host_context"])
        root_config = {"users": [{"user": {"token": "admin-secret"}}],
                       "clusters": [{"cluster": {"certificate-authority-data": "Y2E="}}]}
        service = {"metadata": {"uid": "service-uid"}, "spec": {"type": "ClusterIP",
                   "selector": {"app.kubernetes.io/name": "milo-apiserver"},
                   "ports": [{"name": "https", "port": 6443, "protocol": "TCP", "targetPort": "https"}]}}
        projects = [{"metadata": {"name": name, "uid": bootstrap["uids"][name], "generation": 1, "labels": {upgrade.RUN_LABEL: bootstrap["run_id"]}},
                     "spec": {"ownerRef": {"kind": "Organization", "name": upgrade.ORG}},
                     "status": {"conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}]}} for name in upgrade.PROJECTS]
        organization = {"metadata": {"name": upgrade.ORG, "uid": bootstrap["uids"][upgrade.ORG], "labels": {upgrade.RUN_LABEL: bootstrap["run_id"]}}, "spec": {"type": "Standard"}}
        host_crds, namespaces = [], []
        host, milo = mock.Mock(), mock.Mock()
        def host_read(*args):
            if "crd" in args: return {"items": host_crds}
            if "namespaces" in args: return {"items": namespaces}
            if "service" in args: return service
            if "namespace" in args: return {"metadata": {"uid": "host-uid"}}
            if "deployment" in args: return {"metadata": {"uid": "deployment-uid"}}
            return {"items": []}
        def root_read(*args):
            if "whoami" in args: return {"status": {"userInfo": {"username": "admin", "uid": "admin", "groups": ["system:masters"]}}}
            if "organizations.resourcemanager.miloapis.com" in args: return {"items": [organization]}
            if "projects.resourcemanager.miloapis.com" in args: return {"items": projects}
            return {"items": []}
        csv = "admin-secret,admin,admin,system:masters\nuser-secret,test-user,test-user,system:authenticated\n"
        host.k.side_effect = lambda *args, **kwargs: json.dumps({"data": {"tokens.csv": base64.b64encode(csv.encode()).decode()}} if "milo-apiserver-auth-tokens" in args else {"data": {"ca.crt": "Y2E="}})
        host.json.side_effect, milo.json.side_effect = host_read, root_read
        self.assertEqual(upgrade.fixture(host, milo, bootstrap, root_config)["host_cluster_uid"], "host-uid")
        projects[0]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "inventory or UID"):
            upgrade.fixture(host, milo, bootstrap, root_config)
        projects[0]["metadata"]["uid"] = bootstrap["uids"][upgrade.PROJECTS[0]]
        host_crds.append({"metadata": {"name": "apps.apps.miloapis.com"}})
        with self.assertRaisesRegex(RuntimeError, "stage CRDs"):
            upgrade.fixture(host, milo, bootstrap, root_config)
        host_crds.clear()
        namespaces.append({"metadata": {"name": "locations-system"}})
        with self.assertRaisesRegex(RuntimeError, "namespace already"):
            upgrade.fixture(host, milo, bootstrap, root_config)
        namespaces.clear()
        service["spec"]["ports"][0]["targetPort"] = "foreign"
        with self.assertRaisesRegex(RuntimeError, "forwarding Service"):
            upgrade.fixture(host, milo, bootstrap, root_config)
        host.patch_json.assert_not_called()
        milo.patch_json.assert_not_called()

    def test_other_stage_evidence_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            other = parent / "locations-evidence.json"
            other.write_text('{"cleanup":"not-requested"}')
            with self.assertRaisesRegex(RuntimeError, "other stage evidence"):
                upgrade.stage_files(parent, parent / "bootstrap.json", parent / "upgrade.json")


    def test_failure_reports_safe_phase_even_before_journal_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            sentinel = "private-credential-sentinel"
            result = subprocess.run([sys.executable, str(Path(upgrade.__file__)),
                "--host-kubeconfig", str(parent / sentinel), "--milo-kubeconfig", str(parent / "root"),
                "--bootstrap-evidence", str(parent / "bootstrap"), "--journal", str(parent / "journal"),
                "--host-context", "kind-apps-a1t-ci-test", "--upgrade-port", "19443"],
                text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("private-path-and-option-validation", result.stderr)
            self.assertIn("journal=not created", result.stderr)
            self.assertNotIn(sentinel, result.stderr + result.stdout)
            self.assertFalse((parent / "journal").exists())



if __name__ == "__main__":
    unittest.main()
