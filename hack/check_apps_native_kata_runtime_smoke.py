#!/usr/bin/env python3
"""Validate the local Kata runtime component and the exact pinned chart render."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

import yaml

from run_apps_native_kata_runtime_smoke import COMPONENT, PreflightError, load_lock, require, verify_source_manifest_contract


def checked_command(argv: list[str], timeout: int = 90) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)
    require(result.returncode == 0,
            f"{Path(argv[0]).name} failed; captured output is suppressed for safety")
    return result.stdout


def extract_chart_layer(layout: Path, expected_digest: str) -> Path:
    index_path = layout / "index.json"
    require(index_path.is_file(), "chart OCI layout lacks index.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = [entry for entry in index.get("manifests", []) if entry.get("digest") == expected_digest]
    require(len(entries) == 1, "chart layout must contain exactly the locked chart manifest")
    algorithm, digest = expected_digest.split(":", 1)
    require(algorithm == "sha256", "chart manifest must use SHA-256")
    manifest_path = layout / "blobs" / algorithm / digest
    raw = manifest_path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == digest, "chart manifest bytes do not match the locked digest")
    manifest = json.loads(raw)
    annotations = manifest.get("annotations", {})
    require(annotations.get("org.opencontainers.image.title") == "kata-deploy" and
            annotations.get("org.opencontainers.image.version") == "4.1.0",
            "chart manifest title/version does not match the pinned chart")
    layers = [layer for layer in manifest.get("layers", [])
              if layer.get("mediaType") == "application/vnd.cncf.helm.chart.content.v1.tar+gzip"]
    require(len(layers) == 1, "chart manifest must contain exactly one Helm chart content layer")
    layer = layers[0]
    digest = layer.get("digest", "")
    require(digest.startswith("sha256:"), "chart layer lacks a SHA-256 digest")
    layer_path = layout / "blobs" / "sha256" / digest.split(":", 1)[1]
    layer_bytes = layer_path.read_bytes()
    require(len(layer_bytes) == layer.get("size") and
            hashlib.sha256(layer_bytes).hexdigest() == digest.split(":", 1)[1],
            "chart content layer size or digest differs from its descriptor")
    return layer_path


def safe_extract(layer: Path, destination: Path) -> None:
    with tarfile.open(layer, "r:gz") as archive:
        for member in archive.getmembers():
            rel = Path(member.name)
            require(not rel.is_absolute() and ".." not in rel.parts,
                    "chart layer contains an unsafe archive path")
            require(member.isfile() or member.isdir(), "chart layer contains unsupported archive entry")
            target = destination / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                require(stream is not None, "chart layer file could not be read")
                target.write_bytes(stream.read())


def validate_component() -> None:
    verify_source_manifest_contract()
    output = checked_command(["kustomize", "build", str(COMPONENT)])
    docs = [item for item in yaml.safe_load_all(output) if isinstance(item, dict)]
    require(len(docs) == 2 and
            {(item.get("kind"), item.get("metadata", {}).get("name")) for item in docs} == {
                ("OCIRepository", "apps-native-kata-runtime-smoke-chart"),
                ("HelmRelease", "apps-native-kata-runtime-smoke")},
            "runtime component must render exactly its local chart source and HelmRelease")
    release = next(item for item in docs if item["kind"] == "HelmRelease")
    require("kubeConfig" not in release.get("spec", {}), "rendered release must remain member-local")
    validate_flux_crs(output, docs)


def canonical_sha256(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_flux_crs(rendered: str, docs: list[dict]) -> int:
    lock_path = COMPONENT / "schemas/flux-schema-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    flux_source = yaml.safe_load((COMPONENT.parent / "flux/kustomization.yaml").read_text(encoding="utf-8"))
    require(lock.get("repository") == "https://github.com/fluxcd/flux2" and
            lock.get("tag") == "v2.7.5" and
            lock.get("commit") == "9ecc22ab10ca3940c694c7f4c79f6168e879195e" and
            flux_source.get("resources") == ["github.com/fluxcd/flux2/manifests/install?ref=v2.7.5"],
            "Flux schema snapshots are not pinned to the repository's source revision")
    require(len(lock.get("crds", {})) == 2, "exactly two pinned Flux CRD schemas are required")
    expected_crds = {
        "ocirepositories.source.toolkit.fluxcd.io",
        "helmreleases.helm.toolkit.fluxcd.io",
    }
    schema_root = COMPONENT / "schemas"
    schema_objects = {}
    crds = {}
    for name, pin in lock["crds"].items():
        crd = yaml.safe_load((schema_root / pin["file"]).read_text(encoding="utf-8"))
        require(name in expected_crds and crd.get("kind") == "CustomResourceDefinition" and
                crd.get("metadata", {}).get("name") == name and
                canonical_sha256(crd) == pin["canonicalSha256"],
                f"pinned Flux CRD source differs from lock: {name}")
        versions = [version for version in crd.get("spec", {}).get("versions", [])
                    if version.get("name") == pin["version"] and version.get("served") is True]
        require(len(versions) == 1 and
                crd["spec"].get("group") == pin["group"] and
                crd["spec"].get("names", {}).get("kind") == pin["kind"],
                f"pinned Flux CRD does not serve the locked {pin['kind']} version")
        schema = versions[0].get("schema", {}).get("openAPIV3Schema")
        require(isinstance(schema, dict) and canonical_sha256(schema) == pin["schemaSha256"],
                f"pinned Flux {pin['kind']} schema differs from lock")
        key = (pin["group"], pin["kind"], pin["version"])
        require(key not in schema_objects, "Flux schema lock contains a duplicate served GVK")
        schema_objects[key] = schema
        crds[name] = crd
    require(set(lock["crds"]) == expected_crds,
            "Flux schema snapshots must cover only OCIRepository and HelmRelease")
    rendered_by_gvk = {}
    for item in docs:
        api = item.get("apiVersion", "")
        group, separator, version = api.partition("/")
        require(bool(separator), "Flux resource must use a grouped API version")
        key = (group, item.get("kind"), version)
        require(key in schema_objects, f"rendered resource has no pinned source schema: {key}")
        rendered_by_gvk[key] = rendered_by_gvk.get(key, 0) + 1
    require(rendered_by_gvk == {
        ("source.toolkit.fluxcd.io", "OCIRepository", "v1"): 1,
        ("helm.toolkit.fluxcd.io", "HelmRelease", "v2"): 1,
    }, "rendered Flux component must strictly validate exactly its two expected resources")
    with tempfile.TemporaryDirectory(prefix="apps-kata-flux-schema-") as temp:
        root = Path(temp)
        manifest = root / "component.yaml"
        manifest.write_text(rendered, encoding="utf-8")
        for (group, kind, version), schema in schema_objects.items():
            directory = root / group
            directory.mkdir(exist_ok=True)
            (directory / f"{kind.lower()}_{version}.json").write_text(
                json.dumps(schema, sort_keys=True), encoding="utf-8")
        location = f"file://{root}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json"
        result = checked_command(["kubeconform", "-strict", "-summary", "-kubernetes-version", "1.33.0",
                                 "-schema-location", location, str(manifest)], timeout=90).strip()
        counts = parse_kubeconform_summary(result, 2)
        require(counts == {"valid": 2, "invalid": 0, "errors": 0, "skipped": 0},
                f"Flux CRDs were not strictly validated without skips: {result}")
        print(f"strict Flux source schemas: Valid 2; Invalid 0; Errors 0; Skipped 0 (Flux {lock['tag']} {lock['commit']})")
    return 2


def validate_smoke_registry() -> None:
    lock = load_lock()
    for image_field, manifest_field, config_field in (
            ("kataInstallerImage", "kataInstallerArm64Manifest", "kataInstallerArm64Config"),
            ("kataKubectlImage", "kataKubectlArm64Manifest", "kataKubectlArm64Config")):
        image = lock[image_field]
        index_digest = image.rsplit("@", 1)[1]
        require(checked_command(["crane", "digest", image]).strip() == index_digest,
                f"public {image_field} index differs from its immutable lock")
        child_digest = checked_command(["crane", "digest", "--platform", "linux/arm64", image]).strip()
        require(child_digest == lock[manifest_field],
                f"public {image_field} ARM64 manifest differs from its immutable lock")
        child = json.loads(checked_command(["crane", "manifest", "--platform", "linux/arm64", image]))
        require(child.get("config", {}).get("digest") == lock[config_field],
                f"public {image_field} ARM64 config differs from its immutable lock")
        config = json.loads(checked_command(["crane", "config", "--platform", "linux/arm64", image]))
        require(config.get("architecture") == "arm64" and config.get("os") == "linux",
                f"public {image_field} ARM64 config reports a different platform")
    raw = checked_command(["crane", "manifest", lock["smokeImage"]]).strip()
    expected_index = lock["smokeImage"].rsplit("@", 1)[1].split(":", 1)[1]
    require(hashlib.sha256(raw.encode("utf-8")).hexdigest() == expected_index,
            "public smoke image index bytes differ from the immutable lock")
    index = json.loads(raw)
    children = [item for item in index.get("manifests", [])
                if item.get("digest") == lock["smokeImageArm64Manifest"]]
    require(len(children) == 1 and children[0].get("platform") == {
        "architecture": "arm64", "os": "linux", "variant": "v8"},
        "locked smoke image child is absent or not linux/arm64/v8")
    child = checked_command(["crane", "digest", "--platform", "linux/arm64", lock["smokeImage"]]).strip()
    require(child == lock["smokeImageArm64Manifest"], "registry ARM64 child digest differs from the lock")
    child_manifest = json.loads(checked_command(["crane", "manifest", f"{lock['smokeImage'].split('@', 1)[0]}@{child}"]))
    require(child_manifest.get("config", {}).get("digest") == lock["smokeImageArm64Config"],
            "registry ARM64 child config digest differs from the lock")
    config = json.loads(checked_command(["crane", "config", "--platform", "linux/arm64", lock["smokeImage"]]))
    require(config.get("architecture") == "arm64" and config.get("os") == "linux" and
            config.get("variant") == "v8", "registry smoke image config is not ARM64/v8")


def validate_chart(layout: Path) -> tuple[int, int]:
    lock = load_lock()
    layer = extract_chart_layer(layout, lock["kataChart"].rsplit("@", 1)[1])
    with tempfile.TemporaryDirectory(prefix="apps-kata-runtime-chart-") as tmp:
        root = Path(tmp)
        extracted = root / "chart-layer"
        extracted.mkdir()
        safe_extract(layer, extracted)
        chart_files = list(extracted.glob("*/Chart.yaml"))
        require(len(chart_files) == 1, "pinned layer must contain exactly one Helm chart")
        chart_dir = chart_files[0].parent
        chart_meta = yaml.safe_load(chart_files[0].read_text(encoding="utf-8"))
        require(chart_meta.get("name") == "kata-deploy" and
                chart_meta.get("version") == lock["kataChartVersion"],
                "chart metadata differs from the lock")
        release = yaml.safe_load((COMPONENT / "kata-runtime.yaml").read_text(encoding="utf-8"))
        values_file = root / "values.yaml"
        values_file.write_text(yaml.safe_dump(release["spec"]["values"], sort_keys=False), encoding="utf-8")
        rendered = checked_command(["helm", "template", "kata-deploy", str(chart_dir),
                                    "--namespace", "kube-system", "--values", str(values_file)], timeout=120)
        manifest_file = root / "rendered.yaml"
        manifest_file.write_text(rendered, encoding="utf-8")
        validation = checked_command(["kubeconform", "-strict", "-summary",
                                      "-kubernetes-version", "1.33.0", str(manifest_file)], timeout=120)
        counts = parse_kubeconform_summary(validation, None)
        docs = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
        require(bool(docs), "pinned chart rendered no Kubernetes resources")
        images = set()
        for item in docs:
            podspec = item.get("spec", {}).get("template", {}).get("spec", {})
            for field in ("containers", "initContainers"):
                for container in podspec.get(field, []) or []:
                    if container.get("image"):
                        images.add(container["image"])
        expected_images = {lock["kataInstallerImage"], lock["kataKubectlImage"]}
        require(images == expected_images, "chart emitted an image outside the exact immutable lock")
        classes = [item for item in docs if item.get("kind") == "RuntimeClass" and
                   item.get("metadata", {}).get("name") == lock["runtimeClass"]]
        require(len(classes) == 1 and classes[0].get("handler") == lock["runtimeHandler"],
                "chart must render exactly one named kata-qemu RuntimeClass")
        daemonsets = [item for item in docs if item.get("kind") == "DaemonSet" and
                      item.get("metadata", {}).get("name") == "kata-deploy"]
        require(len(daemonsets) == 1, "chart must render exactly one kata-deploy DaemonSet")
        podspec = daemonsets[0]["spec"]["template"]["spec"]
        require(podspec.get("nodeSelector") == {"node-role.kubernetes.io/control-plane": ""},
                "chart DaemonSet must target the single member control-plane node")
        return len(docs), counts["valid"]


def parse_kubeconform_summary(output: str, expected_valid: int | None) -> dict[str, int]:
    values = {}
    for key in ("valid", "invalid", "errors", "skipped"):
        match = re.search(rf"(?i)(?:^|[\s,\-]){key.capitalize()}\s*:\s*(\d+)", output)
        require(match is not None, f"kubeconform summary omitted {key} count")
        values[key] = int(match.group(1))
    require(values["invalid"] == 0 and values["errors"] == 0 and values["skipped"] == 0,
            f"kubeconform reported invalid, error or skipped resources: {values}")
    if expected_valid is not None:
        require(values["valid"] == expected_valid,
                f"kubeconform validated {values['valid']} objects; expected {expected_valid}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart-layout", type=Path,
                        help="read-only OCI layout fetched from the exact kataChart digest in image-lock.json")
    parser.add_argument("--verify-smoke-image-registry", action="store_true",
                        help="read the public Kata and BusyBox indexes, ARM64 manifests and configs to recheck their locks")
    parser.add_argument("--component-only", action="store_true",
                        help="render and strictly validate both member-local Flux resources without fetching chart artifacts")
    args = parser.parse_args()
    try:
        required_tools = ("kustomize", "kubeconform") if args.component_only else ("kustomize", "helm", "kubeconform")
        for name in required_tools:
            require(shutil.which(name) is not None, f"required validator missing: {name}")
        validate_component()
        if args.component_only:
            return 0
        require(args.chart_layout is not None, "--chart-layout is required for full pinned chart validation")
        if args.verify_smoke_image_registry:
            require(shutil.which("crane") is not None, "crane is required to recheck the public smoke image lock")
            validate_smoke_registry()
        objects, checks = validate_chart(args.chart_layout)
        print(f"member-local component: strict Flux schemas Valid 2; Invalid 0; Errors 0; Skipped 0; "
              f"pinned chart Valid {objects}; Invalid 0; Errors 0; Skipped 0")
        print("all runtime and smoke images, ARM64 children/configs and kata-qemu handler matched the immutable lock")
        return 0
    except (OSError, ValueError, KeyError, PreflightError, subprocess.TimeoutExpired) as exc:
        print(f"runtime manifest validation failed: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
