"""Offline fail-closed regression for native Apps Flux teardown ordering."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location(
    "apps_native_trial", Path(__file__).resolve().parent / "test-apps-native-authoring.py")
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class Owner:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def delete(self, _proxy, _run_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected stage delete failure")


class FakeHost:
    def __init__(self, lingering=None):
        self.lingering = lingering
        self.context = "isolated-fake"

    def optional(self, kind, name, namespace=None):
        if kind == "kustomization" and name == self.lingering:
            return {"metadata": {"name": name, "namespace": namespace}}
        return None


class FakeProxy:
    def close(self):
        return None


class ForeignDataProxy(FakeProxy):
    def __init__(self, projects):
        self.projects = projects

    def get(self, path):
        if path == trial.PROJECT_API:
            return {"items": [{"metadata": {"name": name, "uid": uid}}
                              for name, uid in self.projects.items()]}
        if path.endswith("/apptemplates"):
            return {"items": [{"metadata": {"name": "foreign", "uid": "foreign-uid",
                                            "resourceVersion": "1"}}]}
        return {"items": []}


class ChangedProjectProxy(FakeProxy):
    def get(self, path):
        if path != trial.PROJECT_API:
            raise AssertionError("resource inventory must not run after Project UID mismatch")
        return {"items": [{"metadata": {"name": name, "uid": "replaced-" + name}}
                          for name in trial.PROJECTS]}


class CleanupGatesTest(unittest.TestCase):
    def test_lease_release_accepts_successor_without_deleting_it(self):
        old = {"metadata": {"uid": "fixture", "resourceVersion": "17"},
               "spec": {"holderIdentity": "run"}}
        successor = {"metadata": {"uid": "controller", "resourceVersion": "18"}}
        with patch.object(trial.Kube, "optional", side_effect=[old, successor]) as get:
            lease = trial.HeldLease(trial.Kube(None, Path("/config"), "isolated"),
                                    "apps-system", "lease", "leader", "/lease",
                                    intent=True, uid="fixture")
            deletes = []
            proxy = SimpleNamespace(delete=lambda *args: deletes.append(args))
            lease.delete(proxy, "run")
            lease.delete(proxy, "run")
        self.assertEqual(deletes, [("/lease", "fixture", "17")])
        self.assertEqual(get.call_count, 2)

    def test_lease_release_refuses_preexisting_successor(self):
        with patch.object(trial.Kube, "optional", return_value={
                "metadata": {"uid": "other", "resourceVersion": "18"},
                "spec": {"holderIdentity": "run"}}):
            lease = trial.HeldLease(trial.Kube(None, Path("/config"), "isolated"),
                                    "apps-system", "lease", "leader", "/lease",
                                    intent=True, uid="fixture")
            with self.assertRaisesRegex(RuntimeError, "changed held Lease"):
                lease.delete(SimpleNamespace(delete=lambda *_: self.fail("deleted successor")), "run")

    def test_root_discovery_is_not_a_write_permission_assertion(self):
        resources = {"resources": [{"name": "apptemplates"},
                                    {"name": "apptemplaterevisions"}]}
        with patch.object(trial, "milo_request", return_value=(200, resources)):
            observation = trial.check_discovery(443, Path("/public-ca"), "fixture")
        self.assertEqual(observation["root_status"], 200)
        self.assertIn("apptemplates", observation["root_resource_names"])

    def test_authorization_before_discovery_preserves_group_and_subresource(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(json.loads(kwargs["input_text"]))
            return '{"status":{"allowed":true}}'
        scoped = trial.Kube(SimpleNamespace(run=run), Path("/private/config"), "scoped")
        scoped.can_i("update", "apptemplates.apps.miloapis.com/status", True)
        self.assertEqual(calls[0]["spec"]["resourceAttributes"], {
            "verb": "update", "group": "apps.miloapis.com",
            "resource": "apptemplates", "subresource": "status"})
        with self.assertRaises(RuntimeError):
            scoped.can_i("get", "apptemplates.apps.miloapis.com", False)

    def test_flux_reconciliation_uses_explicit_host_context(self):
        calls = []
        commands = SimpleNamespace(run=lambda *args, **kwargs: calls.append(args))
        host = SimpleNamespace(commands=commands, config=Path("/private/host-config"),
                               context="kind-isolated-fixture")
        trial.flux_reconcile(host, "kustomization", "fixture", "flux-system", 30)
        trial.flux_cluster(host, "reconcile", "source", "oci", "fixture")
        for argv in calls:
            self.assertEqual(argv[:5], ("flux", "--kubeconfig", "/private/host-config",
                                       "--context", "kind-isolated-fixture"))

    def test_certificate_uses_condition_generation_not_flux_status(self):
        certificate = {"kind": "Certificate", "metadata": {"generation": 2},
                       "status": {"conditions": [{"type": "Ready", "status": "True",
                                                   "observedGeneration": 2}]}}
        self.assertTrue(trial.certificate_ready(certificate))
        self.assertFalse(trial.ready(certificate))
        certificate["status"]["conditions"][0]["observedGeneration"] = 1
        self.assertFalse(trial.certificate_ready(certificate))

    def check(self, refs, lingering=None):
        host = FakeHost(lingering)
        state = {"host": host, "milo": FakeHost(), "run_id": "test-run",
                 "crds_installed": False, "auth_intent": False,
                 "suspend_intent": False}
        with tempfile.TemporaryDirectory() as temporary:
            errors = trial.cleanup(SimpleNamespace(), trial.Commands(), state, refs,
                                   FakeProxy(), FakeProxy(), [], {},
                                   Path(temporary) / "evidence.json")
        self.assertTrue(errors)
        return errors

    def test_failed_admission_stage_retains_host_and_crds(self):
        admission, host, crds = Owner(fail=True), Owner(), Owner()
        self.check({"admission": [admission], "host": [host], "crds": [crds]})
        self.assertEqual((admission.calls, host.calls, crds.calls), (1, 0, 0))

    def test_lingering_admission_stage_retains_host(self):
        admission, host = Owner(), Owner()
        self.check({"admission": [admission], "host": [host]},
                   trial.FLUX_ADMISSION)
        self.assertEqual((admission.calls, host.calls), (1, 0))

    def test_failed_host_stage_retains_crds(self):
        host, crds = Owner(fail=True), Owner()
        self.check({"host": [host], "crds": [crds]})
        self.assertEqual((host.calls, crds.calls), (1, 0))

    def test_failed_crd_stage_retains_installer(self):
        crds, installer = Owner(fail=True), Owner()
        self.check({"crds": [crds], "installer-secret": [installer]})
        self.assertEqual((crds.calls, installer.calls), (1, 0))

    def test_foreign_data_census_prevents_webhook_prune(self):
        projects = {name: "uid-" + name for name in trial.PROJECTS}
        admission, host, crds = Owner(), Owner(), Owner()
        state = {"host": FakeHost(), "milo": FakeHost(), "run_id": "test-run",
                 "crds_installed": True, "projects": projects,
                 "allowed_template_uids": {}, "auth_intent": False,
                 "suspend_intent": False}
        with tempfile.TemporaryDirectory() as temporary:
            errors = trial.cleanup(SimpleNamespace(), trial.Commands(), state,
                                   {"admission": [admission], "host": [host],
                                    "crds": [crds]}, FakeProxy(),
                                   ForeignDataProxy(projects), [], {},
                                   Path(temporary) / "evidence.json")
        self.assertTrue(any("foreign AppTemplate" in error for error in errors))
        self.assertEqual((admission.calls, host.calls, crds.calls), (0, 0, 0))

    def test_changed_project_uid_prevents_webhook_prune(self):
        projects = {name: "original-" + name for name in trial.PROJECTS}
        admission = Owner()
        state = {"host": FakeHost(), "milo": FakeHost(), "run_id": "test-run",
                 "crds_installed": True, "projects": projects,
                 "allowed_template_uids": {}, "auth_intent": False,
                 "suspend_intent": False}
        with tempfile.TemporaryDirectory() as temporary:
            errors = trial.cleanup(SimpleNamespace(), trial.Commands(), state,
                                   {"admission": [admission]}, FakeProxy(),
                                   ChangedProjectProxy(), [], {},
                                   Path(temporary) / "evidence.json")
        self.assertTrue(any("Project set/UID changed" in error for error in errors))
        self.assertEqual(admission.calls, 0)


class CLIIntegrationGatesTest(unittest.TestCase):
    def test_bad_pinned_cli_stops_before_even_read_only_preflight(self):
        argv = ["native-trial", "--apps-source", "/tmp/fixture-apps",
                "--apps-revision", "a" * 40,
                "--datumctl-source", "/tmp/fixture-datumctl",
                "--host-kubeconfig", "/tmp/host", "--host-context", "owned",
                "--milo-kubeconfig", "/tmp/milo"]
        with (mock.patch.object(sys, "argv", argv),
              mock.patch.object(trial, "expected_fixture", return_value={}),
              mock.patch.object(trial, "fixture_contract"),
              mock.patch.object(trial, "source_role_guards"),
              mock.patch.object(trial, "source_contract", return_value={}),
              mock.patch.object(trial, "build_private_cli",
                                side_effect=RuntimeError("injected pin mismatch")) as build,
              mock.patch.object(trial, "preflight") as preflight):
            with self.assertRaisesRegex(RuntimeError, "injected pin mismatch"):
                trial.main()
        build.assert_called_once()
        preflight.assert_not_called()

    def test_cli_failure_never_falls_back_to_direct_delete(self):
        prior = {"template": {"metadata": {
            "name": trial.TEMPLATE_NAME, "uid": "template-uid",
            "resourceVersion": "1"}},
            "revisions": [{"metadata": {"name": "revision-one"}}]}
        args = SimpleNamespace(direct_milo_port=1)
        with (mock.patch.object(trial, "delete_with_cli",
                                side_effect=RuntimeError("injected CLI failure")) as delete,
              mock.patch.object(trial, "milo_request") as request):
            with self.assertRaisesRegex(RuntimeError, "injected CLI failure"):
                trial.delete_history_test(args, {}, {trial.AUTHOR: "fixture"},
                                          Path("/tmp/public-ca"), prior,
                                          lambda _: None, cli=object())
        delete.assert_called_once()
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
