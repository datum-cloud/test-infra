import importlib.util
from pathlib import Path
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "components/apps-native-kata-runtimeclass-project"
PROFILE = PROFILE_DIR / "runtimeclass-project.yaml"
RENDERER_PATH = ROOT / "hack/render_runtimeclass_project_candidate.py"
spec = importlib.util.spec_from_file_location("runtimeclass_project_renderer", RENDERER_PATH)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class RuntimeClassProjectCandidateTests(unittest.TestCase):
    def setUp(self):
        self.profile = yaml.safe_load(PROFILE.read_text())

    def test_candidate_is_suspended_until_endpoint_uid_and_credential_exist(self):
        renderer.validate_candidate_profile(self.profile)
        self.assertTrue(self.profile["spec"]["suspend"])
        annotations = self.profile["metadata"]["annotations"]
        self.assertEqual(annotations["test-infra.datumapis.com/candidate-project-name"],
                         "apps-a1t-consumer-a")
        self.assertEqual(annotations["test-infra.datumapis.com/candidate-project-endpoint"],
                         "BOOTSTRAP_REQUIRED")
        self.assertEqual(annotations["test-infra.datumapis.com/candidate-project-uid"],
                         "BOOTSTRAP_REQUIRED")
        self.assertEqual(self.profile["spec"]["kubeConfig"]["secretRef"]["name"],
                         "apps-a1t-runtimeclass-project-kubeconfig")
        self.assertFalse((PROFILE_DIR / "runtimeclass-project-kubeconfig.yaml").exists(),
                         "candidate must not check in a credential")

    def test_patch_is_exactly_one_field_and_never_sets_status(self):
        renderer.validate_candidate_profile(self.profile)
        patch = yaml.safe_load(self.profile["spec"]["patches"][0]["patch"])
        self.assertEqual(patch["spec"], {"networkAttachment": "Netns"})
        self.assertNotIn("status", patch)

    def test_source_compare_preserves_every_other_field(self):
        source = {
            "apiVersion": "compute.datumapis.com/v1alpha",
            "kind": "RuntimeClass",
            "metadata": {"name": "general-purpose", "labels": {"provider": "kata"}},
            "spec": {"networkAttachment": "HypervisorDeclared", "controllerName": "kata"},
        }
        candidate = {
            "apiVersion": "compute.datumapis.com/v1alpha",
            "kind": "RuntimeClass",
            "metadata": {"name": "general-purpose", "labels": {
                "provider": "kata",
                "kustomize.toolkit.fluxcd.io/name": "apps-a1t-kata-runtimeclass-project",
                "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
            }},
            "spec": {"networkAttachment": "Netns", "controllerName": "kata"},
        }
        renderer.assert_source_change_is_only_attachment(source, candidate)
        candidate["spec"]["controllerName"] = "other"
        with self.assertRaisesRegex(ValueError, "outside networkAttachment"):
            renderer.assert_source_change_is_only_attachment(source, candidate)

    def test_missing_or_duplicate_source_runtimeclass_is_rejected(self):
        source = {"apiVersion": "compute.datumapis.com/v1alpha", "kind": "RuntimeClass",
                  "metadata": {"name": "general-purpose"}, "spec": {}}
        with self.assertRaisesRegex(ValueError, "got 0"):
            renderer.select_source_runtime_class([])
        with self.assertRaisesRegex(ValueError, "got 2"):
            renderer.select_source_runtime_class([source, source])
        self.assertIs(renderer.select_source_runtime_class([source]), source)
        result = {**source, "spec": {"networkAttachment": "Netns"}}
        with self.assertRaisesRegex(ValueError, "identity differs"):
            renderer.assert_source_change_is_only_attachment(source,
                                                             {**result, "metadata": {"name": "other"}})

    def test_profile_is_not_a_default_cluster_root(self):
        root = yaml.safe_load((ROOT / "cluster/kustomization.yaml").read_text())
        serialized = str(root)
        self.assertNotIn("apps-native-kata-runtimeclass-project", serialized)

    def test_source_path_is_the_provider_component_not_a_copied_class(self):
        renderer.validate_candidate_profile(self.profile)
        self.assertEqual(self.profile["spec"]["path"], "./components/runtime_classes")
        self.assertEqual(self.profile["spec"]["sourceRef"],
                         {"kind": "OCIRepository", "name": "apps-a1t-kata-provider"})

    def test_activating_or_retargeting_candidate_fails_contract(self):
        profile = yaml.safe_load(PROFILE.read_text())
        profile["spec"]["suspend"] = False
        with self.assertRaisesRegex(ValueError, "must be suspended"):
            renderer.validate_candidate_profile(profile)

        profile = yaml.safe_load(PROFILE.read_text())
        profile["spec"]["patches"][0]["target"]["name"] = "other-class"
        with self.assertRaisesRegex(ValueError, "exact provider class"):
            renderer.validate_candidate_profile(profile)

    def test_candidate_rejects_fabricated_status_and_wrong_flux_identity(self):
        source = {"apiVersion": "compute.datumapis.com/v1alpha",
                  "kind": "RuntimeClass",
                  "metadata": {"name": "general-purpose"},
                  "spec": {"networkAttachment": "HypervisorDeclared"}}
        candidate = {"apiVersion": "compute.datumapis.com/v1alpha",
                     "kind": "RuntimeClass",
                     "metadata": {"name": "general-purpose", "labels": {
                         "kustomize.toolkit.fluxcd.io/name": "wrong-kustomization",
                         "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
                     }},
                     "spec": {"networkAttachment": "Netns"}}
        with self.assertRaisesRegex(ValueError, "Flux build metadata labels"):
            renderer.assert_source_change_is_only_attachment(source, candidate)

        candidate["metadata"]["labels"]["kustomize.toolkit.fluxcd.io/name"] = \
            "apps-a1t-kata-runtimeclass-project"
        candidate["status"] = {"conditions": [{"type": "Available", "status": "True"}]}
        with self.assertRaisesRegex(ValueError, "must not fabricate status"):
            renderer.assert_source_change_is_only_attachment(source, candidate)

    def test_schema_lock_rejects_wrong_compute_revision_and_crd_schema(self):
        lock = renderer.load_schema_lock()
        self.assertEqual(lock["compute"]["sourceRevision"],
                         "88a7101c2b91f7195c2bb5433c50f54c384a90ce")
        crd = {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "runtimeclasses.compute.datumapis.com"},
            "spec": {"group": "compute.datumapis.com",
                     "names": {"kind": "RuntimeClass"},
                     "versions": [{"name": "v1alpha", "served": True,
                                   "schema": {"openAPIV3Schema": {"type": "object"}}}]},
        }
        schema = renderer.schema_for_crd(
            crd, group="compute.datumapis.com", kind="RuntimeClass",
            version="v1alpha", crd_name=lock["compute"]["crd"])
        self.assertEqual(schema, {"type": "object"})
        with self.assertRaisesRegex(ValueError, "not the locked RuntimeClass CRD"):
            renderer.schema_for_crd(
                {**crd, "metadata": {"name": "runtimeclasses.other.example"}},
                group="compute.datumapis.com", kind="RuntimeClass",
                version="v1alpha", crd_name=lock["compute"]["crd"])
        expected = renderer.canonical_sha256(schema)
        renderer.require_schema_hash(schema, expected, "Compute RuntimeClass")
        with self.assertRaisesRegex(ValueError, "canonical SHA256 differs"):
            renderer.require_schema_hash(schema, "0" * 64, "Compute RuntimeClass")
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            flux_source = Path(temp) / "kustomization.yaml"
            flux_source.write_text("resources:\n- github.com/fluxcd/flux2/manifests/install?ref=latest\n")
            with self.assertRaisesRegex(ValueError, "schema source must remain pinned"):
                renderer.validate_flux_source_pin(flux_source)

    def test_source_checkout_must_be_exact_clean_revision(self):
        lock = renderer.load_schema_lock()
        with mock.patch.object(renderer, "run", return_value="deadbeef\n"):
            with self.assertRaisesRegex(ValueError, "HEAD differs"):
                renderer.verify_compute_source(Path("/tmp/fake-compute"), lock)

        responses = iter([lock["compute"]["sourceRevision"] + "\n", " M file\n"])
        with mock.patch.object(renderer, "run", side_effect=lambda *_args, **_kwargs: next(responses)):
            with self.assertRaisesRegex(ValueError, "must be clean"):
                renderer.verify_compute_source(Path("/tmp/fake-compute"), lock)


if __name__ == "__main__":
    unittest.main()
