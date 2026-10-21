#!/usr/bin/env python3
"""Render the pinned Kata provider with the isolated Netns candidate patches.

This is an offline source/artifact check. It does not install Flux resources or
change a cluster. The Compute RuntimeClass publisher is rendered separately to
make its distinct source path explicit; this profile does not currently own a
scoped KubeConfig for publishing that resource.
"""

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "components/apps-native-kata-netns/provider-netns.yaml"
BASE_CHECK_PATH = ROOT / "hack/check_native_kata_prerequisite.py"


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def matches_target(obj: dict, target: dict) -> bool:
    api_version = obj.get("apiVersion", "")
    group, sep, version = api_version.partition("/")
    if not sep:
        version, group = group, ""
    metadata = obj.get("metadata", {})
    return (
        (not target.get("group") or target["group"] == group)
        and (not target.get("version") or target["version"] == version)
        and (not target.get("kind") or target["kind"] == obj.get("kind"))
        and (not target.get("name") or target["name"] == metadata.get("name"))
    )


def require_exactly_one_target(objects: list[dict], target: dict) -> dict:
    matches = [obj for obj in objects if matches_target(obj, target)]
    if len(matches) != 1:
        raise ValueError(
            f"patch target must match exactly one upstream object; matched {len(matches)}"
        )
    return matches[0]


def _run(argv: list[str], timeout: int = 90) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)
    require(result.returncode == 0,
            f"command failed ({argv[0]}): {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def _module():
    spec = importlib.util.spec_from_file_location("kata_contract", BASE_CHECK_PATH)
    require(spec is not None and spec.loader is not None, "cannot import pinned artifact checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_with_schemas(rendered: str, vmrule_crd: Path) -> None:
    module = _module()
    module.validate_kubernetes_schema(rendered, vmrule_crd)


def render_candidate(provider_layout: Path, vmrule_crd: Path) -> tuple[int, int]:
    module = _module()
    lock = module.validate_contract()
    kata_pin = lock["kataProvider"]
    layer = module._layout_layer(
        provider_layout,
        kata_pin["bundle"].split("@", 1)[1],
        "application/vnd.cncf.flux.content.v1.tar+gzip",
        {
            "org.opencontainers.image.revision":
                f"{kata_pin['sourceVersion']}@sha1:{kata_pin['sourceRevision']}",
            "org.opencontainers.image.source": "https://github.com/datum-labs/kata-provider",
        },
    )
    profile = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    patches = profile["spec"].get("patches", [])
    require(len(patches) == 2,
            "member-scoped provider overlay must contain only its ConfigMap and Deployment patches")

    with tempfile.TemporaryDirectory(prefix="apps-kata-netns-provider-") as temp:
        root = Path(temp)
        bundle = root / "bundle"
        bundle.mkdir()
        module._extract_tar_safely(layer, bundle)
        provider_path = bundle / "overlays/cell"
        require(provider_path.is_dir(), "pinned provider bundle lacks overlays/cell")

        base_render = _run(["kustomize", "build", str(provider_path)])
        base_objects = [obj for obj in yaml.safe_load_all(base_render) if isinstance(obj, dict)]
        targets = [entry.get("target", {}) for entry in patches]
        for target in targets:
            require_exactly_one_target(base_objects, target)

        # The RuntimeClass is not part of the provider manager path. Confirm the
        # exact source-owned publisher path separately; a different scoped Flux
        # object is needed before the Netns attachment can be enabled there.
        runtime_source = bundle / "components/runtime_classes"
        require(runtime_source.is_dir(), "pinned provider lacks components/runtime_classes")
        runtime_render = _run(["kustomize", "build", str(runtime_source)])
        runtime_objects = [obj for obj in yaml.safe_load_all(runtime_render) if isinstance(obj, dict)]
        runtime = require_exactly_one_target(runtime_objects, {
            "group": "compute.datumapis.com", "version": "v1alpha",
            "kind": "RuntimeClass", "name": "general-purpose",
        })
        require(runtime.get("spec", {}).get("networkAttachment") == "HypervisorDeclared",
                "source RuntimeClass changed; re-review the attachment contract")

        rendered = _run(["flux", "build", "kustomization", profile["metadata"]["name"],
                         "--path", str(provider_path), "--kustomization-file", str(PROFILE),
                         "--dry-run"])
        objects = [obj for obj in yaml.safe_load_all(rendered) if isinstance(obj, dict)]
        require(len(objects) == 12, f"expected 12 provider resources after patches; rendered {len(objects)}")
        _validate_with_schemas(rendered, vmrule_crd)

        config_obj = require_exactly_one_target(objects, {
            "version": "v1", "kind": "ConfigMap", "name": "kata-provider-config",
        })
        config = yaml.safe_load(config_obj["data"]["config.yaml"])
        downstream = config["downstreamResourceManagement"]
        require(downstream.get("enableVPCNetworking") is True,
                "Netns provider patch must opt into the actual interface-injection path")
        require(downstream.get("runtimeHandler") == "kata-qemu",
                "Netns candidate must keep the QEMU runtime handler")
        deployment = require_exactly_one_target(objects, {
            "group": "apps", "version": "v1", "kind": "Deployment", "name": "kata-provider",
        })
        managers = [container for container in deployment["spec"]["template"]["spec"]["containers"]
                    if container.get("name") == "manager"]
        require(len(managers) == 1 and managers[0].get("image") == kata_pin["managerIndex"],
                "rendered member manager image differs from the locked digest")

    print(f"strict schema validation: {len(objects)}/12 pinned provider objects passed")
    print("separate Compute RuntimeClass source path: 1 object; published attachment remains HypervisorDeclared")
    return len(objects), len(runtime_objects)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-layout", required=True, type=Path)
    parser.add_argument("--vmrule-crd", required=True, type=Path)
    args = parser.parse_args()
    count, runtime_count = render_candidate(args.provider_layout, args.vmrule_crd)
    print(f"Rendered {count} member-provider objects and checked {runtime_count} separate RuntimeClass object.")
    print("No cluster API, Flux reconciliation, or workload traffic was exercised.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.TimeoutExpired, yaml.YAMLError) as error:
        print(f"Netns candidate render failed: {error}", file=sys.stderr)
        sys.exit(1)
