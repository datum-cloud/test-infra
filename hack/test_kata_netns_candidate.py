import json
from pathlib import Path
import unittest

import yaml

from render_kata_netns_candidate import require_exactly_one_target


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "components/apps-native-kata-netns"


def load_profile():
    kustomization = yaml.safe_load((PROFILE / "kustomization.yaml").read_text())
    resources = kustomization.get("resources", [])
    if resources != ["provider-netns.yaml"]:
        raise AssertionError("candidate profile must render only its provider Kustomization")
    docs = list(yaml.safe_load_all((PROFILE / "provider-netns.yaml").read_text()))
    if len(docs) != 1:
        raise AssertionError("candidate provider file must contain exactly one object")
    return docs[0]


class KataNetnsCandidateTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT / "components/apps-native-kata/image-lock.json").read_text())
        self.network_lock = json.loads((PROFILE / "network-contract-lock.json").read_text())
        self.profile = load_profile()
        self.spec = self.profile["spec"]

    def patches(self):
        return self.spec.get("patches", [])

    def find_patch(self, group, version, kind, name):
        for patch in self.patches():
            target = patch.get("target", {})
            if (target.get("group", "") == group and target.get("version") == version
                    and target.get("kind") == kind and target.get("name") == name):
                return patch
        self.fail(f"missing reviewed patch for {group}/{version} {kind}/{name}")

    def test_candidate_replaces_existing_provider_flux_object(self):
        self.assertEqual(self.profile["apiVersion"], "kustomize.toolkit.fluxcd.io/v1")
        self.assertEqual(self.profile["kind"], "Kustomization")
        self.assertEqual(self.profile["metadata"], {"name": "apps-a1t-kata-provider", "namespace": "flux-system"})
        self.assertEqual(self.spec["sourceRef"], {"kind": "OCIRepository", "name": "apps-a1t-kata-provider"})
        self.assertEqual(self.spec["path"], "./overlays/cell")
        self.assertEqual(self.spec["targetNamespace"], "kata-provider-system")
        self.assertEqual(self.spec["kubeConfig"]["secretRef"], {
            "name": "apps-a1t-dfw-member-kubeconfig", "key": "kubeconfig"
        })
        self.assertEqual(self.spec["dependsOn"], [{
            "name": "compute-a1t-dfw-cell", "namespace": "flux-system"
        }])

    def test_provider_opts_into_vpc_and_stays_on_kata_qemu_nodes(self):
        patch = self.find_patch("", "v1", "ConfigMap", "kata-provider-config")
        doc = yaml.safe_load(patch["patch"])
        config = yaml.safe_load(doc["data"]["config.yaml"])
        downstream = config["downstreamResourceManagement"]
        self.assertEqual(downstream["runtimeHandler"], "kata-qemu")
        self.assertEqual(downstream["nodeSelector"], {"katacontainers.io/kata-runtime": "true"})
        self.assertEqual(downstream["enableVPCNetworking"], True)
        self.assertEqual(downstream["tolerations"], [{
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        }])

    def test_manager_image_is_locked_and_runtimeclass_patch_is_not_misplaced(self):
        manager_patch = self.find_patch("apps", "v1", "Deployment", "kata-provider")
        manager_doc = yaml.safe_load(manager_patch["patch"])
        manager_image = manager_doc["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(manager_image, self.lock["kataProvider"]["managerIndex"])
        self.assertIn("@sha256:", manager_image)
        self.assertEqual(len(self.patches()), 2)
        self.assertFalse(any(
            patch.get("target", {}).get("kind") == "RuntimeClass"
            for patch in self.patches()
        ), "Compute RuntimeClass is not in the member-scoped provider source path")

    def test_patch_target_selection_rejects_missing_and_duplicate_objects(self):
        target = {"group": "compute.datumapis.com", "version": "v1alpha",
                  "kind": "RuntimeClass", "name": "general-purpose"}
        runtime_class = {"apiVersion": "compute.datumapis.com/v1alpha",
                         "kind": "RuntimeClass", "metadata": {"name": "general-purpose"}}
        with self.assertRaisesRegex(ValueError, "matched 0"):
            require_exactly_one_target([], target)
        with self.assertRaisesRegex(ValueError, "matched 2"):
            require_exactly_one_target([runtime_class, runtime_class], target)
        self.assertIs(require_exactly_one_target([runtime_class], target), runtime_class)

    def test_only_one_runtime_profile_is_rendered_by_default(self):
        top = yaml.safe_load((ROOT / "cluster/kustomization.yaml").read_text())
        rendered_refs = json.dumps(top)
        self.assertNotIn("apps-native-kata-netns", rendered_refs)

    def test_profile_does_not_forge_network_or_workload_state(self):
        encoded = (PROFILE / "provider-netns.yaml").read_text()
        for forbidden in ("NetworkInterface", "NetworkInterfaceClaim", "NetworkService", "HTTPRoute", "status:"):
            self.assertNotIn(forbidden, encoded)

    def test_source_and_runtime_artifact_pins_are_immutable(self):
        self.assertEqual(self.network_lock["kata"]["version"], "4.1.0")
        self.assertEqual(self.network_lock["kata"]["sourceRevision"], "ddcb1ad8d23cbb4323f86c209f132b89592902df")
        self.assertEqual(self.network_lock["kata"]["arm64GoShimAsset"], "kata-go-static-4.1.0-arm64.tar.zst")
        self.assertEqual(self.network_lock["kata"]["arm64StaticAsset"], "kata-static-4.1.0-arm64.tar.zst")
        self.assertEqual(self.network_lock["kata"]["deployChartVersion"], "4.1.0")
        self.assertEqual(self.network_lock["compute"]["sourceRevision"], "88a7101c2b91f7195c2bb5433c50f54c384a90ce")
        self.assertEqual(self.network_lock["kataProvider"]["sourceRevision"], self.lock["kataProvider"]["sourceRevision"])
        self.assertEqual(self.network_lock["kataProvider"]["imageIndex"], self.lock["kataProvider"]["managerIndex"].split("@")[1])
        self.assertEqual(self.network_lock["networkServicesOperator"]["sourceRevision"], "79245f00d6220c128df3d024475ff7b1566ce305")
        self.assertEqual(self.network_lock["galactic"]["moduleVersion"], "v0.5.8-0.20261016143526-45d3c3514bec")
        self.assertEqual(self.network_lock["galactic"]["sourceRevision"], "45d3c3514bec321333c3d88cba0bdc9f6c0f6594")
        for source in ("compute", "networkServicesOperator"):
            self.assertRegex(self.network_lock[source]["imageIndex"], r"^sha256:[0-9a-f]{64}$")
        self.assertIn("@sha256:", self.network_lock["networkServicesOperator"]["bundle"])
        self.assertRegex(self.network_lock["galactic"]["cniImageIndex"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(self.network_lock["galactic"]["cniArm64"], r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
