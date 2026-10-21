#!/usr/bin/env python3
"""Offline negative tests for the read-only ARM Kata prerequisite checker."""

import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml


SCRIPT = Path(__file__).with_name("check_native_kata_prerequisite.py")
SPEC = importlib.util.spec_from_file_location("check_native_kata_prerequisite", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class PrerequisiteContractTest(unittest.TestCase):
    def test_checked_in_contract_passes(self):
        lock = CHECK.validate_contract()
        self.assertEqual(lock["arm64"]["handler"], "kata-qemu")

    def test_refuses_default_clh_handler(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            original = CHECK.COMPONENT
            for source in original.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            provider = yaml.safe_load((component / "provider.yaml").read_text())
            provider["spec"]["patches"][0]["patch"] = provider["spec"]["patches"][0]["patch"].replace(
                "runtimeHandler: kata-qemu", "runtimeHandler: kata-clh")
            (component / "provider.yaml").write_text(yaml.safe_dump(provider))
            with self.assertRaisesRegex(ValueError, "exact reviewed QEMU contract"):
                CHECK.validate_contract(component)

    def test_refuses_kind_config_without_kvm_device(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            config = yaml.safe_load((component / "kind-kata-config.yaml").read_text())
            config["nodes"][0]["extraMounts"] = [
                item for item in config["nodes"][0]["extraMounts"]
                if item["hostPath"] != "/dev/kvm"]
            (component / "kind-kata-config.yaml").write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(ValueError, "Colima VM /dev/kvm"):
                CHECK.validate_contract(component)

    def test_refuses_floating_installer_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            runtime = yaml.safe_load((component / "kata-runtime.yaml").read_text())
            runtime["spec"]["values"]["image"]["reference"] = "quay.io/kata-containers/kata-deploy"
            runtime["spec"]["values"]["image"]["tag"] = "latest"
            (component / "kata-runtime.yaml").write_text(yaml.safe_dump(runtime))
            with self.assertRaisesRegex(ValueError, "digest-pinned"):
                CHECK.validate_contract(component)

    def test_refuses_writable_modules_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            config = yaml.safe_load((component / "kind-kata-config.yaml").read_text())
            next(item for item in config["nodes"][0]["extraMounts"]
                 if item["hostPath"] == "/lib/modules")["readOnly"] = False
            (component / "kind-kata-config.yaml").write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(ValueError, "read-only"):
                CHECK.validate_contract(component)

    def test_refuses_duplicate_kvm_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            config = yaml.safe_load((component / "kind-kata-config.yaml").read_text())
            config["nodes"][0]["extraMounts"].append(copy.deepcopy(config["nodes"][0]["extraMounts"][0]))
            (component / "kind-kata-config.yaml").write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(ValueError, "exactly one"):
                CHECK.validate_contract(component)

    def test_refuses_kind_without_provider_guest_dad_sysctl_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            config = yaml.safe_load((component / "kind-kata-config.yaml").read_text())
            config["nodes"][0]["kubeadmConfigPatches"] = []
            (component / "kind-kata-config.yaml").write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(ValueError, "guest DAD sysctls"):
                CHECK.validate_contract(component)

    def test_refuses_instance_pods_without_control_plane_toleration(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            provider = yaml.safe_load((component / "provider.yaml").read_text())
            patch = yaml.safe_load(provider["spec"]["patches"][0]["patch"])
            config = yaml.safe_load(patch["data"]["config.yaml"])
            config["downstreamResourceManagement"]["tolerations"] = []
            patch["data"]["config.yaml"] = yaml.safe_dump(config)
            provider["spec"]["patches"][0]["patch"] = yaml.safe_dump(patch)
            (component / "provider.yaml").write_text(yaml.safe_dump(provider))
            with self.assertRaisesRegex(ValueError, "exact reviewed QEMU contract"):
                CHECK.validate_contract(component)

    def test_vmrule_schema_input_hash_rejects_content_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chart_root = root / "victoria-metrics-k8s-stack"
            crd = chart_root / "charts/victoria-metrics-operator/crd.yaml"
            crd.parent.mkdir(parents=True)
            original = b"apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\n"
            crd.write_bytes(original)
            pin = {
                "crdInputFile": "charts/victoria-metrics-operator/crd.yaml",
                "crdInputFileSha256": hashlib.sha256(original).hexdigest(),
            }
            self.assertEqual(CHECK.read_pinned_vmrule_crd(crd, chart_root, pin), original)
            crd.write_bytes(original + b"# changed schema source\n")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                CHECK.read_pinned_vmrule_crd(crd, chart_root, pin)

    def test_refuses_config_patch_with_extra_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            provider = yaml.safe_load((component / "provider.yaml").read_text())
            patch = yaml.safe_load(provider["spec"]["patches"][0]["patch"])
            config = yaml.safe_load(patch["data"]["config.yaml"])
            config["downstreamResourceManagement"]["nodeSelector"]["other-selector"] = "true"
            patch["data"]["config.yaml"] = yaml.safe_dump(config)
            provider["spec"]["patches"][0]["patch"] = yaml.safe_dump(patch)
            (component / "provider.yaml").write_text(yaml.safe_dump(provider))
            with self.assertRaisesRegex(ValueError, "exact reviewed QEMU contract"):
                CHECK.validate_contract(component)

    def test_refuses_unreviewed_provider_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            provider = yaml.safe_load((component / "provider.yaml").read_text())
            provider["spec"]["patches"].append(copy.deepcopy(provider["spec"]["patches"][1]))
            (component / "provider.yaml").write_text(yaml.safe_dump(provider))
            with self.assertRaisesRegex(ValueError, "exactly the reviewed"):
                CHECK.validate_contract(component)

    def test_refuses_mutable_provider_manager_image(self):
        with tempfile.TemporaryDirectory() as temp:
            component = Path(temp)
            for source in CHECK.COMPONENT.iterdir():
                if source.is_file():
                    (component / source.name).write_bytes(source.read_bytes())
            provider = yaml.safe_load((component / "provider.yaml").read_text())
            patch = yaml.safe_load(provider["spec"]["patches"][1]["patch"])
            patch["spec"]["template"]["spec"]["containers"][0]["image"] = \
                "ghcr.io/datum-labs/kata-provider:latest"
            provider["spec"]["patches"][1]["patch"] = yaml.safe_dump(patch)
            (component / "provider.yaml").write_text(yaml.safe_dump(provider))
            with self.assertRaisesRegex(ValueError, "locked image-index digest"):
                CHECK.validate_contract(component)


if __name__ == "__main__":
    unittest.main()
