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
    def test_live_harness_revision_requires_clean_tracked_source(self):
        revision = "a" * 40
        calls = []
        def run(*argv):
            calls.append(argv)
            return revision if argv[-2:] == ("rev-parse", "HEAD") else ""
        self.assertEqual(trial.harness_revision(SimpleNamespace(run=run)), revision)
        self.assertEqual(calls[0][-3:], ("status", "--porcelain", "--untracked-files=all"))
        with self.assertRaisesRegex(RuntimeError, "dirty"):
            trial.harness_revision(SimpleNamespace(run=lambda *_: " M hack/script.py"))

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
                "--image-evidence", "/tmp/fixture-image.json",
                "--host-kubeconfig", "/tmp/host", "--host-context", "owned",
                "--milo-kubeconfig", "/tmp/milo"]
        with (mock.patch.object(sys, "argv", argv),
              mock.patch.object(trial, "expected_fixture", return_value={}),
              mock.patch.object(trial, "fixture_contract"),
              mock.patch.object(trial, "source_role_guards"),
              mock.patch.object(trial, "source_contract", return_value={}),
              mock.patch.object(trial, "image_contract"),
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


class MiloRolloutObservationTest(unittest.TestCase):
    def test_transient_root_disconnect_retries_exact_schema_read(self):
        expected = {"apptemplates.apps.miloapis.com": "exact-schema-hash"}
        with patch.object(trial, "check_crds", side_effect=[
                RuntimeError("port-forward reconnecting"), expected]) as check, \
                patch.object(trial.time, "sleep", return_value=None):
            self.assertEqual(trial.wait_exact_crds(None, {}, seconds=1), expected)
        self.assertEqual(check.call_count, 2)

    def test_persistent_wrong_schema_cannot_pass_retry(self):
        with patch.object(trial, "check_crds", side_effect=RuntimeError(
                "native CRD schema differs")), \
                patch.object(trial.time, "sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "schema differs"):
                trial.wait_exact_crds(None, {}, seconds=0.01)


class PrivateAPIReadRetryTest(unittest.TestCase):
    def proxy(self):
        return trial.APIProxy(SimpleNamespace(commands=SimpleNamespace(deadline=None)),
                              Path('/tmp'), 'offline-test')

    def connection(self, status, body):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = status
        response.read.return_value = body
        return connection

    def test_transient_429_retries_read_only_get(self):
        first = self.connection(429, b'{}')
        second = self.connection(200, b'{"items":[]}')
        with patch('apps_native_support.UnixHTTPConnection',
                   side_effect=[first, second]) as create, \
                patch('apps_native_support.time.sleep', return_value=None):
            self.assertEqual(self.proxy().get('/projects', retry_seconds=1),
                             {'items': []})
        self.assertEqual(create.call_count, 2)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_persistent_429_and_oversized_response_fail_closed(self):
        with patch('apps_native_support.UnixHTTPConnection',
                   return_value=self.connection(429, b'{}')) as create:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
                self.proxy().get('/projects', retry_seconds=0)
        create.assert_called_once()
        with patch('apps_native_support.UnixHTTPConnection',
                   return_value=self.connection(200, b'x' * 65537)):
            with self.assertRaisesRegex(RuntimeError, 'oversized body'):
                self.proxy().get('/projects', retry_seconds=60)


class RuntimeSourcePinTest(unittest.TestCase):
    def test_new_runtime_source_cannot_pass_with_an_older_published_image(self):
        revision = "b" * 40
        commands = SimpleNamespace(run=mock.Mock(side_effect=[
            revision, "", "", "internal/templatecontroller/controller.go"]))
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(trial, "render") as render:
                with self.assertRaisesRegex(RuntimeError, "runtime differs from published image"):
                    trial.source_contract(commands, Path(directory).resolve(), revision)
                render.assert_not_called()

    def test_test_only_go_source_is_allowed_when_not_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "internal" / "example"
            package.mkdir(parents=True)
            (package / "helper.go").write_text("package example\n")
            (package / "close_test.go").write_text("package example\n")
            self.assertEqual(
                trial.runtime_source_violations(
                    source, ["internal/example/close_test.go"]), [])

    def test_production_go_change_remains_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "pkg" / "example"
            package.mkdir(parents=True)
            (package / "helper.go").write_text("package example\n")
            for changed in ("pkg/example/helper.go", "Dockerfile", ".dockerignore",
                            "go.mod", "go.sum"):
                with self.subTest(changed=changed):
                    self.assertEqual(
                        trial.runtime_source_violations(source, [changed]), [changed])

    def test_test_file_matched_by_production_embed_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "internal" / "example"
            (package / "testdata").mkdir(parents=True)
            (package / "embed.go").write_text(
                "package example\n//go:embed testdata\nvar files embed.FS\n")
            test_file = package / "testdata" / "fixture_test.go"
            test_file.write_text("test bytes\n")
            self.assertEqual(
                trial.runtime_source_violations(
                    source, ["internal/example/testdata/fixture_test.go"]),
                ["internal/example/testdata/fixture_test.go"])

    def test_ambiguous_production_embed_syntax_fails_closed_for_package(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "pkg" / "example"
            package.mkdir(parents=True)
            (package / "embed.go").write_text(
                'package example\n//go:embed "assets\\x2f*"\nvar files embed.FS\n')
            self.assertEqual(
                trial.runtime_source_violations(
                    source, ["pkg/example/new_test.go"]),
                ["pkg/example/new_test.go"])


class PlatformPinTest(unittest.TestCase):
    def test_exact_multiarch_evidence_rejects_changed_amd64_digest(self):
        def platform(architecture):
            manifest, config = trial.PLATFORM_DIGESTS[architecture]
            return {"platform": {"os": "linux", "architecture": architecture},
                    "manifest": manifest, "config": config,
                    "revision": trial.APPS_BINARY_SHA,
                    "source": "https://github.com/datum-labs/apps",
                    "user": "65532:65532", "entrypoint": ["/apps-controller"]}
        evidence = {"image": trial.APPS_IMAGE,
                    "platforms": [platform("amd64"), platform("arm64")]}
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "image.json"
            file.write_text(json.dumps(evidence))
            trial.image_contract(file)
            evidence["platforms"][0]["config"] = "sha256:" + "0" * 64
            file.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(RuntimeError, "amd64 manifest/config"):
                trial.image_contract(file)

    def test_node_architecture_and_running_image_match_selected_platform(self):
        node = {"status": {"nodeInfo": {"architecture": "amd64",
                                           "operatingSystem": "linux"},
                           "images": [{"names": [trial.APPS_IMAGE]}]}}
        self.assertEqual(trial.node_images([node], "amd64"), {trial.APPS_IMAGE})
        with self.assertRaisesRegex(RuntimeError, "Linux/arm64"):
            trial.node_images([node], "arm64")

        class Host:
            def __init__(self, image_id):
                self.image_id = image_id

            def json(self, *args):
                if args[1] == "deployment":
                    return {"metadata": {"uid": "deployment-uid"},
                            "spec": {"replicas": 2},
                            "status": {"availableReplicas": 2}}
                return {"items": [{"metadata": {"uid": f"pod-{i}"},
                                   "status": {"phase": "Running", "conditions": [
                                       {"type": "Ready", "status": "True"}],
                                       "containerStatuses": [{"ready": True,
                                                              "imageID": self.image_id}]}}
                                  for i in (1, 2)]}

        trial.pod_snapshot(Host("docker://" + trial.AMD_CONFIG), "amd64")
        with self.assertRaisesRegex(RuntimeError, "amd64 manifest/config"):
            trial.pod_snapshot(Host("docker://" + trial.ARM_CONFIG), "amd64")


if __name__ == "__main__":
    unittest.main()
