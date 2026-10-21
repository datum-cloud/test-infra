"""Exact upstream Catalog OCI publication contract for the task-owned fixture."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile

import yaml

from apps_native_support import (
    Commands, FluxOwned, Kube, RUN_LABEL, _flux_spec, flux_inventory, ready, require,
)


ROOT = Path(__file__).resolve().parent.parent
CATALOG_SHA = "5f2b663b6c5e039c4c7b666ef3e959803851b675"
OCI_DIGEST = "sha256:d34680228b91a5a5d46fedb40700f805b9b2ce97f13232e178cffa2db2cae431"
LAYER_DIGEST = "sha256:45e7a0d1afb367bc2e33ba08b2e01dac96887d46ba74f90ec3139a0ebc42d6af"
SOURCE_NAME = "apps-a1t-catalog-bundle"
SOURCE_FILE = ROOT / "components/service-catalog/source-oci.yaml"
ORIGINAL_STAGE_FILE = ROOT / "components/service-catalog/api-crds.yaml"
PUBLISH_CHANGED = "base/manager/kustomization.yaml"
SOURCE_CHANGED_SHA = "61135fefee473ea1391fe7f97eb79e0390a9e7bbe2d2e97d1f2f99c67dfb66dd"
BUNDLE_CHANGED_SHA = "f9e7fe959a9f0da0c2f686dbc25ee23f8965fd70c4b5d56001f3cbc294fb2e20"
RENDER_SHA = "a861cfd2310f7bed584ef358abcd82083243615efd704656114603455d3c1f41"


def exact_source(commands: Commands, source: Path) -> Path:
    require(source.is_absolute() and source.is_dir() and not source.is_symlink(),
            "absolute clean Catalog source directory required")
    require(commands.run("git", "-C", str(source), "rev-parse", "HEAD") == CATALOG_SHA and
            not commands.run("git", "-C", str(source), "status", "--porcelain",
                             "--untracked-files=all"),
            "Catalog source is not exact clean commit 5f2b663")
    return source / "config"


def exact_manifest(path: Path) -> dict:
    require(path.is_absolute() and path.is_file() and not path.is_symlink() and
            path.stat().st_size <= 4096, "bounded OCI manifest file required")
    raw = path.read_bytes()
    require("sha256:" + hashlib.sha256(raw).hexdigest() == OCI_DIGEST,
            "Catalog OCI manifest digest differs from published artifact")
    manifest = json.loads(raw)
    require(manifest.get("schemaVersion") == 2 and
            manifest.get("mediaType") == "application/vnd.oci.image.manifest.v1+json" and
            len(manifest.get("layers", [])) == 1 and
            manifest["layers"][0] == {
                "mediaType": "application/vnd.cncf.flux.content.v1.tar+gzip",
                "size": 45410, "digest": LAYER_DIGEST} and
            manifest.get("config", {}).get("mediaType") ==
                "application/vnd.cncf.flux.config.v1+json" and
            manifest.get("annotations", {}).get("org.opencontainers.image.revision") ==
                "v0.0.0-5f2b663@sha1:" + CATALOG_SHA and
            manifest["annotations"].get("org.opencontainers.image.source") ==
                "https://github.com/milo-os/service-catalog",
            "published OCI manifest does not identify the reviewed source and layer")
    return manifest


def _files_from_layer(path: Path) -> dict[str, bytes]:
    require(path.is_absolute() and path.is_file() and not path.is_symlink() and
            path.stat().st_size == 45410 and
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == LAYER_DIGEST,
            "Catalog OCI layer differs from published compressed digest")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(path.read_bytes()), mode="r:gz") as archive:
        members = archive.getmembers()
        require(len(members) <= 160 and all(
            (item.isfile() or item.isdir()) and not Path(item.name).is_absolute() and
            ".." not in Path(item.name).parts and chr(92) not in item.name
            for item in members), "Catalog OCI layer has unsupported entries")
        for item in members:
            if item.isfile():
                name = item.name.removeprefix("./")
                require(name not in files and name and len(name) <= 256 and
                        item.size <= 1024 * 1024, "Catalog OCI file path or size is unsafe")
                files[name] = archive.extractfile(item).read()
    require(len(files) == 89, "Catalog OCI bundle file count changed")
    return files


def verify_tree(source_config: Path, files: dict[str, bytes]) -> None:
    originals = {p.relative_to(source_config).as_posix(): p.read_bytes()
                 for p in source_config.rglob("*") if p.is_file()}
    require(len(originals) == 89 and set(originals) == set(files),
            "Catalog OCI publication has a different source file set")
    differences = [name for name in files if files[name] != originals[name]]
    require(differences == [PUBLISH_CHANGED] and
            hashlib.sha256(originals[PUBLISH_CHANGED]).hexdigest() == SOURCE_CHANGED_SHA and
            hashlib.sha256(files[PUBLISH_CHANGED]).hexdigest() == BUNDLE_CHANGED_SHA,
            "Catalog publication changed bytes beyond the reviewed image rewrite")


def _render(commands: Commands, manifest: dict, path: Path) -> list[dict]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as target:
        yaml.safe_dump(manifest, target)
        target.flush()
        output = commands.run(
            "flux", "build", "kustomization", manifest["metadata"]["name"],
            "-n", manifest["metadata"]["namespace"], "--path", str(path),
            "--kustomization-file", target.name, "--dry-run", timeout=120)
    result = [obj for obj in yaml.safe_load_all(output) if obj]
    require(len(result) == 13, "Catalog API render count changed")
    return result


def transition_contract(commands: Commands, source: Path, manifest_path: Path,
                        layer_path: Path) -> dict:
    source_config = exact_source(commands, source)
    exact_manifest(manifest_path)
    files = _files_from_layer(layer_path)
    verify_tree(source_config, files)
    oci_source = yaml.safe_load(SOURCE_FILE.read_text())
    original = yaml.safe_load(ORIGINAL_STAGE_FILE.read_text())
    require(oci_source.get("apiVersion") == "source.toolkit.fluxcd.io/v1" and
            oci_source.get("kind") == "OCIRepository" and
            oci_source.get("metadata") == {"name": SOURCE_NAME, "namespace": "flux-system"} and
            oci_source.get("spec") == {"interval": "10m",
                "url": "oci://ghcr.io/milo-os/service-catalog-kustomize",
                "ref": {"digest": OCI_DIGEST}},
            "task-owned OCI source manifest changed")
    target = copy.deepcopy(original)
    require(original["spec"].get("sourceRef") == {"kind": "GitRepository",
            "name": "service-catalog", "namespace": "flux-system"} and
            original["spec"].get("path") ==
                "./config/overlays/e2e-milo/milo-resources",
            "original Catalog API stage is not the reviewed Git contract")
    target["spec"]["sourceRef"] = {"kind": "OCIRepository", "name": SOURCE_NAME,
                                    "namespace": "flux-system"}
    target["spec"]["path"] = "./overlays/e2e-milo/milo-resources"
    with tempfile.TemporaryDirectory(prefix="apps-catalog-oci-render-") as temporary:
        bundle = Path(temporary) / "config"
        for name, data in files.items():
            file = bundle / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        before = _render(commands, original,
                         source_config / "overlays/e2e-milo/milo-resources")
        after = _render(commands, target,
                        bundle / "overlays/e2e-milo/milo-resources")
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
    require(before == after and digest == RENDER_SHA and
            flux_inventory(before) == flux_inventory(after),
            "OCI Catalog API render differs from source-owned Git inventory")
    return {"source": oci_source, "original": original, "target": target,
            "rendered": after, "render_sha256": digest}


def verified_live_transition(host: Kube, identity: dict, contract: dict,
                             graph: dict, evidence: dict) -> dict:
    """Accept only the same original graph UID with an exact OCI source switch."""
    git_uid = graph.get("attempted", {}).get("flux-system/service-catalog")
    stage_uid = graph.get("attempted", {}).get("milo-system/service-catalog-api-crds")
    run_id = evidence.get("run_id")
    require(evidence.get("version") == 1 and
            isinstance(run_id, str) and len(run_id) == 24 and
            all(char in "0123456789abcdef" for char in run_id) and
            evidence.get("phase") == "complete" and
            evidence.get("cleanup") == "not-requested" and
            evidence.get("scope_identity") == identity and
            evidence.get("graph_run_id") == graph.get("run_id") and
            evidence.get("catalog_source") == CATALOG_SHA and
            evidence.get("oci_digest") == OCI_DIGEST and
            evidence.get("render_sha256") == contract["render_sha256"] and
            evidence.get("original_git_uid") == git_uid and
            evidence.get("stage_uid") == stage_uid and
            isinstance(evidence.get("oci_uid"), str) and bool(evidence["oci_uid"]) and
            evidence.get("patch_intent") is True,
            "Catalog OCI transition journal is not a completed predecessor")
    git = host.json("-n", "flux-system", "get", "gitrepository", "service-catalog")
    require(git["metadata"].get("uid") == git_uid and
            git.get("spec") == {"interval": "10m", "timeout": "60s",
                "url": "https://github.com/milo-os/service-catalog.git",
                "ref": {"commit": CATALOG_SHA}} and
            git.get("status", {}).get("artifact", {}).get("revision") ==
                "sha1:" + CATALOG_SHA,
            "original Catalog Git provenance UID, spec or artifact changed")
    source = host.json("-n", "flux-system", "get", "ocirepository", SOURCE_NAME)
    source_spec = copy.deepcopy(contract["source"]["spec"])
    source_spec.update({"provider": "generic", "timeout": "60s"})
    require(source["metadata"].get("uid") == evidence["oci_uid"] and
            source["metadata"].get("labels") == {RUN_LABEL: run_id} and
            not source["metadata"].get("ownerReferences") and
            not source["metadata"].get("deletionTimestamp") and
            source.get("spec") == source_spec and ready(source) and
            source.get("status", {}).get("artifact", {}).get("revision") == OCI_DIGEST,
            "Catalog OCI source UID/spec/current digest changed")
    owner = FluxOwned(host, "milo-system", "service-catalog-api-crds",
                      contract["rendered"], OCI_DIGEST)
    labeled = copy.deepcopy(contract["target"])
    labeled["metadata"].setdefault("labels", {})[RUN_LABEL] = graph["run_id"]
    labeled["spec"] = _flux_spec(labeled["spec"])
    expected = {"version": 1, "run_id": graph["run_id"], "initial": labeled,
                "inventory": [list(entry) for entry in sorted(owner.allowed)],
                "revision": OCI_DIGEST, "uid": stage_uid, "intent": True,
                "delete_intent": False, "ca_bundle": None, "ca_committed": False}
    require(evidence.get("target_flux_state") == expected,
            "Catalog OCI transition target Flux intent differs from source")
    owner.recover(contract["target"], graph["run_id"], expected)
    stage = host.json("-n", "milo-system", "get", "kustomization",
                      "service-catalog-api-crds")
    owner._validate(stage, graph["run_id"])
    require(stage["metadata"].get("uid") == stage_uid and ready(stage) and
            stage.get("status", {}).get("lastAppliedRevision") == OCI_DIGEST and
            len(stage.get("status", {}).get("inventory", {}).get("entries", [])) == 13,
            "Catalog API Kustomization has not applied exact OCI inventory")
    return stage
