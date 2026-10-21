"""Offline failure gates for the real Milo Project bootstrap helper."""

import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location(
    "project_bootstrap", Path(__file__).resolve().parent / "bootstrap-apps-native-projects.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapContractTest(unittest.TestCase):
    def test_source_owned_shape_and_exact_names(self):
        org = bootstrap.organization_manifest("run")
        self.assertEqual((org["kind"], org["spec"], org["metadata"]["name"]),
                         ("Organization", {"type": "Standard"}, bootstrap.ORG))
        for name in bootstrap.PROJECTS:
            project = bootstrap.project_manifest(name, "run")
            self.assertEqual(project["spec"]["ownerRef"],
                             {"kind": "Organization", "name": bootstrap.ORG})
            self.assertEqual(project["metadata"]["labels"][bootstrap.RUN_LABEL], "run")
        with self.assertRaisesRegex(RuntimeError, "unreviewed Project"):
            bootstrap.project_manifest("other", "run")

    def test_only_verified_loopback_ca_config_and_exclusive_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve() / "private"
            parent.mkdir(mode=0o700)
            output = parent / "root.kubeconfig"
            bootstrap.private_destination(output, must_exist=False)
            config = bootstrap.config_document(18443, b"public-ca", "private-token")
            bootstrap.write_exclusive(output, config)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            bootstrap.exact_config(json.loads(output.read_text()), "milo-root",
                                   "https://127.0.0.1:18443")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                bootstrap.private_destination(output, must_exist=False)
            config["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
            with self.assertRaisesRegex(RuntimeError, "verified CA"):
                bootstrap.exact_config(config, "milo-root", "https://127.0.0.1:18443")
            parent.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "mode-0700"):
                bootstrap.private_destination(parent / "other", must_exist=False)

    def test_project_and_control_plane_uid_generation_must_match(self):
        project = {"metadata": {"uid": "p-uid", "generation": 2,
                                "labels": {bootstrap.RUN_LABEL: "run"}},
                   "spec": {"ownerRef": {"kind": "Organization", "name": bootstrap.ORG}},
                   "status": {"conditions": [{"type": "Ready", "status": "True",
                                              "observedGeneration": 2}]}}
        pcp = {"metadata": {"uid": "pcp-uid", "generation": 1,
                            "labels": {"resourcemanager.miloapis.com/project-uid": "p-uid",
                                       "resourcemanager.miloapis.com/project-name":
                                           bootstrap.PROJECTS[0]}},
               "status": {"conditions": [{"type": "ControlPlaneReady", "status": "True",
                                          "observedGeneration": 1}]}}
        class Kube:
            def optional(self, kind, name):
                return pcp if kind == bootstrap.PCP_RESOURCE else project
        self.assertTrue(bootstrap.project_ready(Kube(), bootstrap.PROJECTS[0],
                                                "p-uid", "run"))
        pcp["metadata"]["labels"]["resourcemanager.miloapis.com/project-uid"] = "other"
        with self.assertRaisesRegex(RuntimeError, "does not match Project UID"):
            bootstrap.project_ready(Kube(), bootstrap.PROJECTS[0], "p-uid", "run")
        pcp["metadata"]["labels"]["resourcemanager.miloapis.com/project-uid"] = "p-uid"
        project["status"]["conditions"][0]["observedGeneration"] = 1
        self.assertFalse(bootstrap.project_ready(Kube(), bootstrap.PROJECTS[0],
                                                 "p-uid", "run"))

    def test_refuse_foreign_cleanup_before_delete_and_use_uid_rv(self):
        obj = {"metadata": {"uid": "original", "resourceVersion": "7",
                            "labels": {bootstrap.RUN_LABEL: "foreign"}},
               "spec": {"ownerRef": {"kind": "Organization", "name": bootstrap.ORG}}}
        class Kube:
            def optional(self, *_):
                return obj
        proxy = SimpleNamespace(delete=mock.Mock())
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            bootstrap.delete_owned(Kube(), proxy, bootstrap.PROJECT_RESOURCE,
                                   bootstrap.PROJECTS[0], "/path", "original",
                                   "owned", 100)
        proxy.delete.assert_not_called()
        obj["metadata"]["labels"][bootstrap.RUN_LABEL] = "owned"
        with mock.patch.object(bootstrap, "wait_true"):
            bootstrap.delete_owned(Kube(), proxy, bootstrap.PROJECT_RESOURCE,
                                   bootstrap.PROJECTS[0], "/path", "original",
                                   "owned", 100)
        proxy.delete.assert_called_once_with("/path", "original", "7")
        obj["spec"]["ownerRef"]["name"] = "foreign-org"
        with self.assertRaisesRegex(RuntimeError, "changed owner reference"):
            bootstrap.verified_owned(Kube(), bootstrap.PROJECT_RESOURCE,
                                     bootstrap.PROJECTS[0], "original", "owned")

    def test_static_auth_changed_or_duplicate_token_fails_closed(self):
        def secret(rows):
            raw = "\n".join(",".join(row) for row in rows) + "\n"
            return json.dumps({"data": {"tokens.csv": base64.b64encode(raw.encode()).decode()}})
        class Host:
            def __init__(self, value):
                self.value = value
            def k(self, *_args, **_kwargs):
                return self.value
        rows = [["token", "admin", "admin", "system:masters"],
                ["token", "test-user", "test-user", "system:authenticated"]]
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            bootstrap.bootstrap_token(Host(secret(rows)))
        rows[1][3] = "system:masters"
        with self.assertRaisesRegex(RuntimeError, "baseline changed"):
            bootstrap.bootstrap_token(Host(secret(rows)))

    def test_partial_cleanup_keeps_organization_when_project_delete_fails(self):
        calls = []
        class Proxy:
            def __init__(self, *_):
                pass
            def start(self):
                pass
            def close(self):
                pass
        def delete(_milo, _proxy, kind, name, *_args):
            calls.append((kind, name))
            if name == bootstrap.PROJECTS[-1]:
                raise RuntimeError("injected Project deletion failure")
        with (mock.patch.object(bootstrap, "APIProxy", Proxy),
              mock.patch.object(bootstrap, "delete_owned", side_effect=delete)):
            errors = bootstrap.remove_owned(SimpleNamespace(), Path("/private/root"),
                                            "run", {bootstrap.ORG: "o", **{
                                                name: name for name in bootstrap.PROJECTS}}, 100)
        self.assertEqual(len(errors), 1)
        self.assertEqual(calls, [(bootstrap.PROJECT_RESOURCE, name)
                                 for name in reversed(bootstrap.PROJECTS)])
        self.assertNotIn((bootstrap.ORG_RESOURCE, bootstrap.ORG), calls)

    def test_lost_create_response_recovers_uid_only_from_owned_object(self):
        object_ = {"metadata": {"uid": "created-without-response",
                                "resourceVersion": "19",
                                "labels": {bootstrap.RUN_LABEL: "run"}},
                   "spec": {"ownerRef": {"kind": "Organization", "name": bootstrap.ORG}}}
        class Kube:
            def optional(self, *_args):
                return object_
        deleted = []
        proxy = SimpleNamespace(delete=lambda *args: deleted.append(args))
        with mock.patch.object(bootstrap, "wait_true"):
            bootstrap.delete_owned(Kube(), proxy, bootstrap.PROJECT_RESOURCE,
                                   bootstrap.PROJECTS[0], "/project", None,
                                   "run", 100)
        self.assertEqual(deleted, [("/project", "created-without-response", "19")])
        object_["metadata"]["labels"][bootstrap.RUN_LABEL] = "someone-else"
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            bootstrap.verified_owned(Kube(), bootstrap.PROJECT_RESOURCE,
                                     bootstrap.PROJECTS[0], None, "run")

    def test_partial_evidence_retries_failed_cleanup_without_losing_unknown_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory).resolve() / "evidence.json"
            evidence = {"run_id": "f" * 24, "attempted": {
                bootstrap.ORG: "org-uid", bootstrap.PROJECTS[0]: None},
                "cleanup": "incomplete"}
            bootstrap.write_exclusive(evidence_file, evidence)
            self.assertEqual(bootstrap.attempted_from_evidence(evidence),
                             {bootstrap.ORG: "org-uid", bootstrap.PROJECTS[0]: None})
            with mock.patch.object(bootstrap, "remove_owned", side_effect=[
                    ["injected transient delete error"], []]) as remove:
                with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                    bootstrap.cleanup_evidenced(SimpleNamespace(), Path("/private/root"),
                                                evidence_file, evidence, 100)
                first = json.loads(evidence_file.read_text())
                self.assertEqual(first["cleanup"], "incomplete")
                self.assertIsNone(first["attempted"][bootstrap.PROJECTS[0]])
                bootstrap.cleanup_evidenced(SimpleNamespace(), Path("/private/root"),
                                            evidence_file, first, 100)
                second = json.loads(evidence_file.read_text())
            self.assertEqual(second["cleanup"], "complete")
            self.assertIsNone(second["attempted"][bootstrap.PROJECTS[0]])
            self.assertEqual(remove.call_count, 2)

    def test_interrupted_create_keeps_preposted_intent_for_cleanup_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory).resolve() / "evidence.json"
            evidence = {"run_id": "f" * 24, "attempted": {}, "uids": {},
                        "cleanup": "not-requested"}
            bootstrap.write_json_atomic(evidence_file, evidence)
            self.assertEqual(bootstrap.attempted_from_evidence(
                json.loads(evidence_file.read_text())), {})
            created = {}
            milo = SimpleNamespace(create=mock.Mock(side_effect=KeyboardInterrupt))
            with self.assertRaises(KeyboardInterrupt):
                bootstrap.create_recorded(
                    milo, bootstrap.organization_manifest("f" * 24), bootstrap.ORG,
                    created, evidence, evidence_file)
            saved = json.loads(evidence_file.read_text())
            self.assertEqual(saved["attempted"], {bootstrap.ORG: None})
            self.assertEqual(saved["cleanup"], "not-requested")
            with mock.patch.object(bootstrap, "remove_owned", return_value=[]) as remove:
                bootstrap.cleanup_evidenced(SimpleNamespace(), Path("/private/root"),
                                            evidence_file, saved, 100)
            remove.assert_called_once_with(mock.ANY, Path("/private/root"),
                                           "f" * 24, {bootstrap.ORG: None}, 100)
            self.assertEqual(json.loads(evidence_file.read_text())["cleanup"],
                             "complete")

    def test_create_uid_is_persisted_before_next_api_write(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory).resolve() / "evidence.json"
            evidence = {"run_id": "f" * 24, "attempted": {}, "uids": {},
                        "cleanup": "not-requested"}
            bootstrap.write_json_atomic(evidence_file, evidence)
            created = {}
            milo = SimpleNamespace(create=mock.Mock(return_value={
                "metadata": {"uid": "org-uid"}}))
            bootstrap.create_recorded(
                milo, bootstrap.organization_manifest("f" * 24), bootstrap.ORG,
                created, evidence, evidence_file)
            saved = json.loads(evidence_file.read_text())
            self.assertEqual(saved["attempted"], {bootstrap.ORG: "org-uid"})
            self.assertEqual(saved["uids"], {bootstrap.ORG: "org-uid"})


if __name__ == "__main__":
    unittest.main()
