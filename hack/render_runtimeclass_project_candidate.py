#!/usr/bin/env python3
"""Render the offline, suspended fixture Project RuntimeClass candidate.

This verifies the provider's source-owned Kustomize Component and the exact
Flux patch against the pinned OCI bundle. It does not create credentials,
connect to Milo, or prove Project admission, placement, or network traffic.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "components/apps-native-kata-runtimeclass-project"
PROFILE = PROFILE_DIR / "runtimeclass-project.yaml"
LOCK = ROOT / "components/apps-native-kata/image-lock.json"
PROVIDER_CHECK = ROOT / "hack/check_native_kata_prerequisite.py"
SCHEMA_LOCK = PROFILE_DIR / "schema-lock.json"


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def run(argv: list[str], timeout: int = 90) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)
    require(result.returncode == 0,
            f"command failed ({argv[0]}): {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def load_provider_checker():
    spec = importlib.util.spec_from_file_location("pinned_provider_checker", PROVIDER_CHECK)
    require(spec is not None and spec.loader is not None, "cannot import pinned provider checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_candidate_profile(profile: dict) -> None:
    require(profile.get("apiVersion") == "kustomize.toolkit.fluxcd.io/v1" and
            profile.get("kind") == "Kustomization", "candidate must be a Flux Kustomization")
    metadata = profile.get("metadata", {})
    require(metadata.get("name") == "apps-a1t-kata-runtimeclass-project" and
            metadata.get("namespace") == "flux-system", "unexpected candidate identity")
    annotations = metadata.get("annotations", {})
    require(annotations.get("test-infra.datumapis.com/candidate-project-name") ==
            "apps-a1t-consumer-a" and
            annotations.get("test-infra.datumapis.com/candidate-project-endpoint") ==
            "BOOTSTRAP_REQUIRED" and
            annotations.get("test-infra.datumapis.com/candidate-project-uid") ==
            "BOOTSTRAP_REQUIRED", "candidate must remain unbound to an unverified Project")
    require(annotations.get("test-infra.datumapis.com/candidate-state") ==
            "suspended-no-credential", "candidate must declare its non-runnable state")
    spec = profile.get("spec", {})
    require(spec.get("suspend") is True, "candidate must be suspended")
    require(spec.get("sourceRef") == {"kind": "OCIRepository", "name": "apps-a1t-kata-provider"},
            "candidate must use the pinned provider bundle source")
    require(spec.get("path") == "./components/runtime_classes",
            "candidate must wrap the provider's source-owned RuntimeClass Component")
    require(spec.get("kubeConfig", {}).get("secretRef") == {
        "name": "apps-a1t-runtimeclass-project-kubeconfig", "key": "kubeconfig"},
        "candidate must reference the dedicated Project credential Secret")
    require(spec.get("dependsOn") == [{
        "name": "compute-a1t-management", "namespace": "flux-system"}],
        "candidate must wait for the Compute management API prerequisites")
    patches = spec.get("patches", [])
    require(len(patches) == 1, "candidate must contain exactly one RuntimeClass patch")
    patch = patches[0]
    require(patch.get("target") == {
        "group": "compute.datumapis.com", "version": "v1alpha",
        "kind": "RuntimeClass", "name": "general-purpose"},
        "candidate target must identify the exact provider class")
    doc = yaml.safe_load(patch.get("patch", ""))
    require(doc == {
        "apiVersion": "compute.datumapis.com/v1alpha",
        "kind": "RuntimeClass",
        "metadata": {"name": "general-purpose"},
        "spec": {"networkAttachment": "Netns"},
    }, "candidate may change only the reviewed networkAttachment field")
    require("status" not in doc, "candidate must not fabricate RuntimeClass status")


def assert_source_change_is_only_attachment(source: dict, result: dict) -> None:
    require(source.get("apiVersion") == result.get("apiVersion") and
            source.get("kind") == result.get("kind") and
            source.get("metadata", {}).get("name") == result.get("metadata", {}).get("name"),
            "rendered RuntimeClass identity differs from provider source")
    source_copy = yaml.safe_load(yaml.safe_dump(source))
    result_copy = yaml.safe_load(yaml.safe_dump(result))
    require("status" not in result_copy, "rendered provider source must not fabricate status")
    labels = result_copy.get("metadata", {}).get("labels", {})
    flux_labels = {
        "kustomize.toolkit.fluxcd.io/name": "apps-a1t-kata-runtimeclass-project",
        "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
    }
    require(all(labels.get(key) == value for key, value in flux_labels.items()),
            "Flux build metadata labels are missing or differ from the candidate identity")
    for key in flux_labels:
        labels.pop(key)
    if labels:
        result_copy["metadata"]["labels"] = labels
    else:
        result_copy["metadata"].pop("labels", None)
    result_spec = result_copy.get("spec", {})
    source_spec = source_copy.get("spec", {})
    require(result_spec.pop("networkAttachment", None) == "Netns",
            "rendered class must select Netns")
    source_spec.pop("networkAttachment", None)
    require(result_copy == source_copy,
            "rendered class differs from provider source outside networkAttachment")


def select_source_runtime_class(objects: list[dict]) -> dict:
    matches = [obj for obj in objects
               if obj.get("apiVersion") == "compute.datumapis.com/v1alpha" and
               obj.get("kind") == "RuntimeClass" and
               obj.get("metadata", {}).get("name") == "general-purpose"]
    require(len(matches) == 1,
            f"provider Component must render exactly one general-purpose RuntimeClass, got {len(matches)}")
    return matches[0]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False).encode("utf-8"))


def load_schema_lock() -> dict:
    lock = json.loads(SCHEMA_LOCK.read_text(encoding="utf-8"))
    require(lock.get("compute", {}).get("sourceRevision") ==
            "88a7101c2b91f7195c2bb5433c50f54c384a90ce",
            "Compute schema source revision is not the reviewed pin")
    require(lock.get("compute", {}).get("crd") ==
            "runtimeclasses.compute.datumapis.com" and
            lock.get("compute", {}).get("crdFile") ==
            "config/base/crd/bases/compute.datumapis.com_runtimeclasses.yaml" and
            len(lock.get("compute", {}).get("crdFileSha256", "")) == 64,
            "Compute RuntimeClass CRD source lock is incomplete")
    require(lock.get("flux", {}).get("version") == "v2.7.5" and
            lock.get("flux", {}).get("tagCommit") ==
            "9ecc22ab10ca3940c694c7f4c79f6168e879195e" and
            lock.get("flux", {}).get("crd") ==
            "kustomizations.kustomize.toolkit.fluxcd.io" and
            len(lock.get("flux", {}).get("crdCanonicalSha256", "")) == 64,
            "Flux Kustomization CRD source lock is incomplete")
    return lock


def verify_compute_source(source_dir: Path, lock: dict) -> Path:
    source_dir = source_dir.resolve()
    head = run(["git", "-C", str(source_dir), "rev-parse", "HEAD"]).strip()
    dirty = run(["git", "-C", str(source_dir), "status", "--porcelain",
                 "--untracked-files=all"]).strip()
    require(head == lock["compute"]["sourceRevision"],
            "Compute source HEAD differs from the reviewed schema revision")
    require(not dirty, "Compute source checkout must be clean, including untracked files")
    crd_path = source_dir / lock["compute"]["crdFile"]
    raw = crd_path.read_bytes()
    require(sha256(raw) == lock["compute"]["crdFileSha256"],
            "Compute RuntimeClass CRD source file SHA256 differs from the schema lock")
    return crd_path


def require_schema_hash(schema: dict, expected: str, kind: str) -> None:
    require(canonical_sha256(schema) == expected,
            f"{kind} schema canonical SHA256 differs from lock")


def validate_flux_source_pin(path: Path | None = None) -> None:
    source = path or (ROOT / "components/flux/kustomization.yaml")
    flux_config = yaml.safe_load(source.read_text(encoding="utf-8"))
    require(flux_config.get("resources") == [
        "github.com/fluxcd/flux2/manifests/install?ref=v2.7.5"],
        "repository Flux schema source must remain pinned to flux2 v2.7.5")


def schema_for_crd(crd: dict, *, group: str, kind: str, version: str,
                   crd_name: str) -> dict:
    require(crd.get("apiVersion") == "apiextensions.k8s.io/v1" and
            crd.get("kind") == "CustomResourceDefinition" and
            crd.get("metadata", {}).get("name") == crd_name and
            crd.get("spec", {}).get("group") == group and
            crd.get("spec", {}).get("names", {}).get("kind") == kind,
            f"schema input is not the locked {kind} CRD")
    versions = [item for item in crd["spec"].get("versions", [])
                if item.get("name") == version and item.get("served") is True]
    require(len(versions) == 1, f"locked CRD does not serve {version} exactly once")
    schema = versions[0].get("schema", {}).get("openAPIV3Schema")
    require(isinstance(schema, dict) and schema.get("type") == "object",
            f"locked {kind} CRD has no structural object schema")
    return schema


def kubeconform_one(manifest: str, schema: dict, *, group: str, kind: str,
                    version: str, expected_crd_hash: str) -> str:
    with tempfile.TemporaryDirectory(prefix="apps-runtimeclass-schema-") as temp:
        root = Path(temp)
        manifest_path = root / "candidate.yaml"
        manifest_path.write_text(manifest, encoding="utf-8")
        schema_dir = root / group
        schema_dir.mkdir()
        schema_path = schema_dir / f"{kind.lower()}_{version}.json"
        schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
        require_schema_hash(schema, expected_crd_hash, kind)
        location = f"file://{root}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json"
        summary = run(["kubeconform", "-strict", "-summary", "-kubernetes-version",
                       "1.33.0", "-schema-location", location, str(manifest_path)],
                      timeout=90).strip()
        require("Valid: 1" in summary and "Invalid: 0" in summary and
                "Errors: 0" in summary and "Skipped: 0" in summary,
                f"strict schema validation did not validate exactly one {kind}: {summary}")
        return summary


def render(provider_layout: Path, compute_source: Path,
           output_path: Path | None = None) -> tuple[int, dict, dict[str, str]]:
    checker = load_provider_checker()
    lock = checker.validate_contract()
    schema_lock = load_schema_lock()
    compute_crd_path = verify_compute_source(compute_source, schema_lock)
    compute_crd_raw = compute_crd_path.read_bytes()
    compute_crd = yaml.safe_load(compute_crd_raw)
    require(compute_crd.get("metadata", {}).get("name") == schema_lock["compute"]["crd"],
            "Compute source CRD name differs from schema lock")
    compute_schema = schema_for_crd(
        compute_crd, group="compute.datumapis.com", kind="RuntimeClass",
        version="v1alpha", crd_name=schema_lock["compute"]["crd"])
    require_schema_hash(compute_schema, schema_lock["compute"]["schemaCanonicalSha256"],
                        "Compute RuntimeClass")
    pin = lock["kataProvider"]
    layer = checker._layout_layer(
        provider_layout,
        pin["bundle"].split("@", 1)[1],
        "application/vnd.cncf.flux.content.v1.tar+gzip",
        {
            "org.opencontainers.image.revision":
                f"{pin['sourceVersion']}@sha1:{pin['sourceRevision']}",
            "org.opencontainers.image.source": "https://github.com/datum-labs/kata-provider",
        },
    )
    profile_raw = PROFILE.read_text(encoding="utf-8")
    profile = yaml.safe_load(profile_raw)
    validate_candidate_profile(profile)

    # Use the repository's Flux v2.7.5 installer source as the authoritative
    # schema source; verify the source reference before extracting its CRD.
    validate_flux_source_pin()
    flux_render = run(["kustomize", "build", str(ROOT / "components/flux")])
    flux_crds = [obj for obj in yaml.safe_load_all(flux_render)
                 if isinstance(obj, dict) and obj.get("kind") == "CustomResourceDefinition" and
                 obj.get("metadata", {}).get("name") == schema_lock["flux"]["crd"]]
    require(len(flux_crds) == 1, "pinned Flux install must render one Kustomization CRD")
    flux_crd = flux_crds[0]
    require(canonical_sha256(flux_crd) == schema_lock["flux"]["crdCanonicalSha256"],
            "Flux Kustomization CRD differs from pinned schema lock")
    flux_schema = schema_for_crd(
        flux_crd, group="kustomize.toolkit.fluxcd.io", kind="Kustomization",
        version="v1", crd_name=schema_lock["flux"]["crd"])
    require_schema_hash(flux_schema, schema_lock["flux"]["schemaCanonicalSha256"],
                        "Flux Kustomization")

    with tempfile.TemporaryDirectory(prefix="apps-runtimeclass-project-") as temp:
        root = Path(temp)
        bundle = root / "bundle"
        bundle.mkdir()
        checker._extract_tar_safely(layer, bundle)
        component = bundle / "components/runtime_classes"
        require(component.is_dir(), "pinned provider bundle lacks the RuntimeClass Component")
        source_render = run(["kustomize", "build", str(component)])
        source_objects = [obj for obj in yaml.safe_load_all(source_render) if isinstance(obj, dict)]
        require(len(source_objects) == 1, "provider Component must render exactly one object")
        source = select_source_runtime_class(source_objects)
        require(source.get("spec", {}).get("networkAttachment") == "HypervisorDeclared",
                "source catalog attachment changed; candidate needs renewed review")

        rendered = run([
            "flux", "build", "kustomization", profile["metadata"]["name"],
            "--path", str(component), "--kustomization-file", str(PROFILE), "--dry-run",
        ])
        objects = [obj for obj in yaml.safe_load_all(rendered) if isinstance(obj, dict)]
        require(len(objects) == 1, f"candidate must render exactly one RuntimeClass, got {len(objects)}")
        assert_source_change_is_only_attachment(source, objects[0])
        runtime_validation = kubeconform_one(
            yaml.safe_dump(objects[0], sort_keys=False), compute_schema,
            group="compute.datumapis.com", kind="RuntimeClass", version="v1alpha",
            expected_crd_hash=schema_lock["compute"]["schemaCanonicalSha256"])
        flux_validation = kubeconform_one(
            profile_raw, flux_schema, group="kustomize.toolkit.fluxcd.io",
            kind="Kustomization", version="v1",
            expected_crd_hash=schema_lock["flux"]["schemaCanonicalSha256"])
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        return len(objects), objects[0], {
            "runtimeClass": runtime_validation,
            "fluxKustomization": flux_validation,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-layout", required=True, type=Path)
    parser.add_argument("--compute-source", required=True, type=Path)
    parser.add_argument("--output", type=Path,
                        help="optional path to save the validated rendered RuntimeClass")
    args = parser.parse_args()
    count, runtime_class, validations = render(args.provider_layout, args.compute_source, args.output)
    print(f"pinned provider Component: rendered exactly {count} RuntimeClass")
    print(f"fixture Project candidate: {runtime_class['metadata']['name']} networkAttachment=Netns")
    print(f"strict RuntimeClass CRD validation: {validations['runtimeClass']}")
    print(f"strict Flux Kustomization CRD validation: {validations['fluxKustomization']}")
    if args.output:
        print(f"validated rendered object written to {args.output}")
    print("candidate remains suspended; Project endpoint/UID bootstrap pins and credential are absent")
    print("no API calls, admission, placement, network attachment, or traffic were exercised")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.TimeoutExpired, yaml.YAMLError) as error:
        print(f"RuntimeClass Project candidate render failed: {error}", file=sys.stderr)
        sys.exit(1)
