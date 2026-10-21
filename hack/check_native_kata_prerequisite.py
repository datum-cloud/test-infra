#!/usr/bin/env python3
"""Read-only contract check for the ARM Kata Compute prerequisite.

This script reads repository files and optional local source/artifact checkouts.
When source renders are requested, it uses Kustomize, Helm template, and
kubeconform's read-only Kubernetes schema downloads. It never calls kubectl,
docker, kind creation, Helm install, mutating registry commands, or VM probes.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/apps-native-kata"
PIN = re.compile(r"^sha256:[0-9a-f]{64}$")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"invalid YAML object: {path.name}")
    return value


def validate_contract(component: Path = COMPONENT) -> dict:
    lock = json.loads((component / "image-lock.json").read_text(encoding="utf-8"))
    provider = read_yaml(component / "provider.yaml")
    provider_source = read_yaml(component / "provider-source.yaml")
    chart_source = read_yaml(component / "kata-chart-source.yaml")
    runtime = read_yaml(component / "kata-runtime.yaml")
    kind_config = read_yaml(component / "kind-kata-config.yaml")

    compute = lock["compute"]
    kata = lock["kataProvider"]
    deploy = lock["kataDeploy"]
    arm = lock["arm64"]
    require(compute["sourceRevision"] == "88a7101c2b91f7195c2bb5433c50f54c384a90ce",
            "Compute source revision is not the reviewed prerequisite")
    require(compute["moduleVersion"] == "v0.12.10",
            "Compute module version does not match the Kata provider API dependency")
    require(lock.get("vmRuleSchema") == {
        "sourceChart": "victoria-metrics-k8s-stack", "sourceChartVersion": "0.58.3",
        "crd": "vmrules.operator.victoriametrics.com", "apiVersion": "v1beta1",
        "crdInputFile": "charts/victoria-metrics-operator/crd.yaml",
        "crdInputFileSha256": "9a623ea333298d2e31d86ee48f97f080bdc58645892b1a4f4e643c112c6422e9",
    }, "VMRule validation source must match the checked-in VictoriaMetrics chart pin")
    observability = read_yaml(ROOT / "components/observability/victoria-metrics-hr.yaml")
    vm_chart = observability["spec"]["chart"]["spec"]
    require(vm_chart.get("chart") == lock["vmRuleSchema"]["sourceChart"] and
            vm_chart.get("version") == lock["vmRuleSchema"]["sourceChartVersion"],
            "VMRule schema version differs from the checked-in observability chart")
    require(kata["sourceRevision"] == "9578d304201428f6d7370460b4a6e9933ef1b25f",
            "Kata provider source revision is not the reviewed ARM build")

    for field in ("bundle", "managerIndex"):
        require("@" in kata[field] and PIN.fullmatch(kata[field].rsplit("@", 1)[1]) is not None,
                f"Kata provider {field} must use an immutable OCI digest")
    for field in ("chart", "imageIndex", "kubectlImageIndex"):
        require("@" in deploy[field] and PIN.fullmatch(deploy[field].rsplit("@", 1)[1]) is not None,
                f"Kata Deploy {field} must use an immutable OCI digest")
    for field in ("managerArm64",):
        require(PIN.fullmatch(kata[field]) is not None,
                f"Kata provider {field} must be an immutable sha256 child")
    for field in ("imageArm64", "kubectlImageArm64"):
        require(PIN.fullmatch(deploy[field]) is not None,
                f"Kata Deploy {field} must be an immutable sha256 child")

    require(provider_source["kind"] == "OCIRepository", "provider source must be an OCIRepository")
    require(provider_source["spec"]["url"] == "oci://ghcr.io/datum-labs/kata-provider-kustomize",
            "unexpected provider bundle source")
    require(provider_source["spec"]["ref"]["digest"] == kata["bundle"].split("@", 1)[1],
            "provider Flux source digest differs from image lock")
    pspec = provider["spec"]
    require(pspec["sourceRef"] == {"kind": "OCIRepository", "name": "apps-a1t-kata-provider"},
            "provider Kustomization must use the pinned source-owned bundle")
    require(pspec["path"] == "./overlays/cell", "provider must use upstream cell overlay")
    require(pspec["kubeConfig"]["secretRef"] == {
        "name": "apps-a1t-dfw-member-kubeconfig", "key": "kubeconfig"},
        "provider must target the dedicated member kubeconfig")
    patches = pspec.get("patches", [])
    require(len(patches) == 2, "provider must contain exactly the reviewed ConfigMap and Deployment patches")
    config_patch = next((entry for entry in patches
                         if entry.get("target", {}).get("kind") == "ConfigMap"), None)
    image_patch = next((entry for entry in patches
                        if entry.get("target", {}).get("kind") == "Deployment"), None)
    require(config_patch is not None and config_patch["target"] == {
        "version": "v1", "kind": "ConfigMap", "name": "kata-provider-config"},
        "provider ConfigMap patch target must match the reviewed upstream bundle")
    config_patch_doc = yaml.safe_load(config_patch["patch"])
    config_text = config_patch_doc.get("data", {}).get("config.yaml")
    require(isinstance(config_text, str), "provider patch must carry a complete config.yaml value")
    provider_config = yaml.safe_load(config_text)
    require(provider_config == {
        "apiVersion": "apiserver.config.datumapis.com/v1alpha1",
        "kind": "KataProvider",
        "metricsServer": {"bindAddress": ":8080"},
        "webhookServer": {},
        "downstreamResourceManagement": {
            "runtimeHandler": "kata-qemu",
            "nodeSelector": {"katacontainers.io/kata-runtime": "true"},
            "tolerations": [{
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "Exists",
                "effect": "NoSchedule",
            }],
            "enableVPCNetworking": False,
        },
    }, "provider runtime configuration differs from the exact reviewed QEMU contract")
    require(image_patch is not None and image_patch["target"] == {
        "group": "apps", "version": "v1", "kind": "Deployment", "name": "kata-provider"},
        "provider Deployment image patch target must match the upstream manager")
    image_patch_doc = yaml.safe_load(image_patch["patch"])
    require(image_patch_doc == {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "kata-provider"},
        "spec": {"template": {"spec": {"containers": [{
            "name": "manager", "image": kata["managerIndex"],
        }]}}},
    }, "provider manager must use the exact locked image-index digest")
    require(any(check.get("kind") == "RuntimeClass" and
                check.get("name") == arm["runtimeClass"]
                for check in pspec.get("healthChecks", [])),
            "provider Kustomization must wait for the real kata-qemu RuntimeClass")

    require(chart_source["spec"]["url"] ==
            "oci://quay.io/kata-containers/kata-deploy-charts/kata-deploy",
            "unexpected Kata chart source")
    require(chart_source["spec"]["ref"]["digest"] == deploy["chart"].split("@", 1)[1],
            "Kata chart source digest differs from image lock")
    require(chart_source["spec"]["layerSelector"] == {
        "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
        "operation": "copy"}, "OCI source must select the exact Helm chart layer")

    rspec = runtime["spec"]
    require(rspec["chartRef"] == {
        "kind": "OCIRepository", "name": "apps-a1t-kata-deploy-chart",
        "namespace": "flux-system"}, "Kata runtime HelmRelease must use its pinned chart source")
    require(rspec["kubeConfig"]["secretRef"] == pspec["kubeConfig"]["secretRef"],
            "runtime and provider must target the same explicit member cluster")
    values = rspec["values"]
    require(values["image"]["reference"] == deploy["imageIndex"] and
            values["image"]["tag"] == "", "Kata installer image must be digest-pinned")
    require(values["kubectlImage"]["reference"] == deploy["kubectlImageIndex"] and
            values["kubectlImage"]["tag"] == "", "Kata chart helper image must be digest-pinned")
    require(values["image"]["reference"].endswith("@" + deploy["imageIndex"].rsplit("@", 1)[1]) and
            values["kubectlImage"]["reference"].endswith("@" + deploy["kubectlImageIndex"].rsplit("@", 1)[1]),
            "Kata chart images must use the locked immutable digest")
    require(values["shims"] == {"disableAll": True, "qemu": {"enabled": True}},
            "only the QEMU shim should be installed")
    require(values["defaultShim"] == {"amd64": "qemu", "arm64": "qemu"},
            "Kata handler must match the provider's QEMU runtimeHandler")
    require(values["runtimeClasses"]["enabled"] is True and
            values["runtimeClasses"]["createDefault"] is False,
            "kata-deploy must create named handler classes without an unrelated default")
    require(values.get("enableVPCNetworking", False) is False,
            "this runtime prerequisite must not imply VPC networking support")

    nodes = kind_config.get("nodes", [])
    require(len(nodes) == 1, "Kata fixture must have one dedicated Kind node")
    kubelet_patches = nodes[0].get("kubeadmConfigPatches", [])
    require(len(kubelet_patches) == 1 and yaml.safe_load(kubelet_patches[0]) == {
        "kind": "KubeletConfiguration",
        "allowedUnsafeSysctls": [
            "net.ipv6.conf.all.accept_dad",
            "net.ipv6.conf.default.accept_dad",
        ],
    }, "Kind kubelet must allow exactly the provider's guest DAD sysctls")
    mounts = nodes[0].get("extraMounts", [])
    device_mounts = [item for item in mounts
                     if item.get("hostPath") == "/dev/kvm" and item.get("containerPath") == "/dev/kvm"]
    require(len(device_mounts) == 1,
            "Kind node must receive exactly one Colima VM /dev/kvm device mount")
    module_mounts = [item for item in mounts
                     if item.get("hostPath") == "/lib/modules" and item.get("containerPath") == "/lib/modules"]
    require(len(module_mounts) == 1 and module_mounts[0].get("readOnly") is True,
            "Kind node must receive the Colima VM kernel modules exactly once and read-only")
    return lock


def _run(argv: list[str], timeout: int = 30) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)
    require(result.returncode == 0,
            f"command failed ({argv[0]}): {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def read_pinned_vmrule_crd(path: Path, chart_root: Path, pin: dict) -> bytes:
    """Read the exact schema input file from the pinned chart content."""
    expected_path = chart_root / pin["crdInputFile"]
    require(path.resolve() == expected_path.resolve(),
            "VMRule CRD input path differs from the pinned chart file")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == pin["crdInputFileSha256"],
            "VMRule CRD input SHA256 differs from the pinned chart content")
    return data


def validate_kubernetes_schema(rendered: str, vmrule_crd: Path | None = None) -> None:
    """Run strict built-in Kubernetes schema validation for rendered objects."""
    with tempfile.TemporaryDirectory(prefix="apps-kata-schema-") as temp:
        root = Path(temp)
        manifest = root / "rendered.yaml"
        manifest.write_text(rendered, encoding="utf-8")
        command = ["kubeconform", "-strict", "-summary", "-kubernetes-version", "1.33.0"]
        if vmrule_crd is not None:
            lock = json.loads((COMPONENT / "image-lock.json").read_text(encoding="utf-8"))
            chart_roots = []
            for parent in vmrule_crd.parents:
                chart_file = parent / "Chart.yaml"
                if chart_file.is_file():
                    meta = read_yaml(chart_file)
                    if (meta.get("name") == lock["vmRuleSchema"]["sourceChart"] and
                            meta.get("version") == lock["vmRuleSchema"]["sourceChartVersion"]):
                        chart_roots.append(parent)
            require(len(chart_roots) == 1,
                    "VMRule schema must come from the checked-in VictoriaMetrics chart version")
            crd_data = read_pinned_vmrule_crd(vmrule_crd, chart_roots[0], lock["vmRuleSchema"])
            matches = [item for item in yaml.safe_load_all(crd_data.decode("utf-8"))
                       if isinstance(item, dict) and
                       item.get("metadata", {}).get("name") == lock["vmRuleSchema"]["crd"]]
            require(len(matches) == 1, "schema input must contain exactly the pinned VMRule CRD")
            crd = matches[0]
            version_name = lock["vmRuleSchema"]["apiVersion"]
            require(crd.get("spec", {}).get("group") == "operator.victoriametrics.com" and
                    crd.get("spec", {}).get("names", {}).get("kind") == "VMRule",
                    "schema input is not the pinned VMRule group/kind")
            versions = [version for version in crd["spec"].get("versions", [])
                        if version.get("name") == version_name and version.get("served") is True]
            require(len(versions) == 1, "pinned VMRule CRD must serve v1beta1")
            schema = versions[0].get("schema", {}).get("openAPIV3Schema")
            require(isinstance(schema, dict), "pinned VMRule CRD lacks its structural OpenAPI schema")
            schema_dir = root / "operator.victoriametrics.com"
            schema_dir.mkdir()
            (schema_dir / "vmrule_v1beta1.json").write_text(json.dumps(schema), encoding="utf-8")
            command.extend(["-schema-location", "default",
                            "-schema-location",
                            f"file://{root}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json"])
        command.append(str(manifest))
        summary = _run(command, timeout=90).strip()
        if summary:
            print(summary)


def _layout_layer(layout: Path, expected_manifest: str, media_type: str,
                  annotations: dict[str, str]) -> Path:
    """Verify a local OCI layout against the locked manifest and layer digests."""
    index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    descriptor = next((entry for entry in index.get("manifests", [])
                       if entry.get("digest") == expected_manifest), None)
    require(descriptor is not None, f"OCI layout does not contain locked manifest {expected_manifest}")
    manifest_digest = expected_manifest.split(":", 1)[1]
    manifest_bytes = (layout / "blobs/sha256" / manifest_digest).read_bytes()
    require(hashlib.sha256(manifest_bytes).hexdigest() == manifest_digest,
            "OCI manifest bytes do not match the locked digest")
    manifest = json.loads(manifest_bytes)
    actual_annotations = manifest.get("annotations", {})
    for name, expected in annotations.items():
        require(actual_annotations.get(name) == expected,
                f"OCI manifest annotation {name} does not match the source lock")
    layers = [entry for entry in manifest.get("layers", [])
              if entry.get("mediaType") == media_type]
    require(len(layers) == 1, f"OCI manifest must contain exactly one {media_type} layer")
    layer_digest = layers[0]["digest"].split(":", 1)[1]
    layer = layout / "blobs/sha256" / layer_digest
    require(hashlib.sha256(layer.read_bytes()).hexdigest() == layer_digest,
            "OCI layer bytes do not match their content digest")
    return layer


def _extract_tar_safely(layer: Path, destination: Path) -> None:
    with tarfile.open(layer, "r:gz") as archive:
        for member in archive.getmembers():
            name = Path(member.name)
            require(not name.is_absolute() and ".." not in name.parts,
                    "OCI artifact contains an unsafe archive path")
            require(member.isdir() or member.isfile(),
                    "OCI artifact contains a non-file archive entry")
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            require(stream is not None, "OCI artifact file could not be read")
            target.write_bytes(stream.read())


def validate_provider_render(layout: Path, vmrule_crd: Path, lock: dict) -> None:
    """Build the exact locked OCI bundle overlay with the same patches as Flux."""
    provider = lock["kataProvider"]
    layer = _layout_layer(
        layout, provider["bundle"].split("@", 1)[1],
        "application/vnd.cncf.flux.content.v1.tar+gzip",
        {"org.opencontainers.image.revision":
         f"{provider['sourceVersion']}@sha1:{provider['sourceRevision']}",
         "org.opencontainers.image.source": "https://github.com/datum-labs/kata-provider"},
    )
    with tempfile.TemporaryDirectory(prefix="apps-kata-provider-render-") as temp:
        root = Path(temp)
        tempdir = root / "render"
        bundle = root / "bundle"
        tempdir.mkdir()
        bundle.mkdir()
        _extract_tar_safely(layer, bundle)
        overlay = bundle / "overlays/cell"
        require(overlay.is_dir(), "locked Kata provider bundle lacks overlays/cell")
        # Kustomize applies the two same patches declared in Flux to the exact
        # content extracted from the digest-pinned OCI bundle.
        source = COMPONENT / "provider.yaml"
        flux = read_yaml(source)["spec"]
        kustomization = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": [os.path.relpath(overlay, tempdir)],
            "patches": flux["patches"],
        }
        (tempdir / "kustomization.yaml").write_text(yaml.safe_dump(kustomization), encoding="utf-8")
        rendered = _run(["kustomize", "build", "--load-restrictor=LoadRestrictionsNone", str(tempdir)])
    docs = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    validate_kubernetes_schema(rendered, vmrule_crd)
    deployments = [item for item in docs if item.get("kind") == "Deployment" and
                   item.get("metadata", {}).get("name") == "kata-provider"]
    configmaps = [item for item in docs if item.get("kind") == "ConfigMap" and
                  item.get("metadata", {}).get("name") == "kata-provider-config"]
    require(len(deployments) == 1 and len(configmaps) == 1,
            "pinned provider render must contain one manager Deployment and config ConfigMap")
    deployment = deployments[0]
    podspec = deployment["spec"]["template"]["spec"]
    managers = [container for container in podspec.get("containers", []) if container.get("name") == "manager"]
    require(len(managers) == 1 and managers[0].get("image") == lock["kataProvider"]["managerIndex"],
            "rendered provider manager image is not the locked immutable digest")
    require(podspec.get("nodeSelector") == {"node-role.kubernetes.io/control-plane": ""},
            "rendered provider manager must schedule on the dedicated Kind control-plane node")
    require(podspec.get("tolerations") == [{
        "key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule",
    }], "rendered provider manager must tolerate the dedicated control-plane taint")
    config = yaml.safe_load(configmaps[0]["data"]["config.yaml"])
    require(config["downstreamResourceManagement"] == {
        "runtimeHandler": "kata-qemu",
        "nodeSelector": {"katacontainers.io/kata-runtime": "true"},
        "tolerations": [{
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        }],
        "enableVPCNetworking": False,
    }, "rendered provider config does not match the exact QEMU/no-VPC contract")


def validate_chart_render(layout: Path, lock: dict) -> None:
    """Render the chart layer extracted from the exact pinned OCI manifest."""
    chart_pin = lock["kataDeploy"]
    layer = _layout_layer(
        layout, chart_pin["chart"].split("@", 1)[1],
        "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
        {"org.opencontainers.image.title": "kata-deploy",
         "org.opencontainers.image.version": chart_pin["chartVersion"]},
    )
    values = read_yaml(COMPONENT / "kata-runtime.yaml")["spec"]["values"]
    with tempfile.TemporaryDirectory(prefix="apps-kata-chart-render-") as temp:
        root = Path(temp)
        chart_root = root / "chart"
        chart_root.mkdir()
        _extract_tar_safely(layer, chart_root)
        chart_files = list(chart_root.glob("*/Chart.yaml"))
        require(len(chart_files) == 1, "locked chart layer must contain exactly one Chart.yaml")
        chart = chart_files[0].parent
        chart_meta = read_yaml(chart_files[0])
        require(chart_meta.get("name") == "kata-deploy" and
                chart_meta.get("version") == chart_pin["chartVersion"],
                "chart contents differ from the locked name and version")
        values_path = root / "values.yaml"
        values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
        rendered = _run(["helm", "template", "kata-deploy", str(chart),
                         "--namespace", "kube-system", "--values", str(values_path)])
    docs = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    validate_kubernetes_schema(rendered)
    emitted = set()
    for item in docs:
        spec = item.get("spec", {})
        podspec = spec.get("template", {}).get("spec", {})
        for field in ("containers", "initContainers"):
            for container in podspec.get(field, []) or []:
                if container.get("image"):
                    emitted.add(container["image"])
    expected = {lock["kataDeploy"]["imageIndex"], lock["kataDeploy"]["kubectlImageIndex"]}
    require(emitted == expected,
            f"rendered Kata chart images differ from the two locked digests: {sorted(emitted)}")
    runtime_classes = [item for item in docs if item.get("kind") == "RuntimeClass" and
                       item.get("metadata", {}).get("name") == lock["arm64"]["runtimeClass"]]
    require(len(runtime_classes) == 1 and runtime_classes[0].get("handler") == "kata-qemu",
            "rendered Kata chart must create exactly the kata-qemu RuntimeClass handler")
    daemonsets = [item for item in docs if item.get("kind") == "DaemonSet" and
                  item.get("metadata", {}).get("name") == "kata-deploy"]
    require(len(daemonsets) == 1,
            "rendered Kata chart must install the node runtime as exactly one DaemonSet")
    node_selector = daemonsets[0]["spec"]["template"]["spec"].get("nodeSelector", {})
    require(node_selector == {"node-role.kubernetes.io/control-plane": ""},
            "rendered kata-deploy DaemonSet must target the single Kind control-plane node")


def verify_source(path: Path, expected: str, module: str | None = None) -> None:
    require(path.is_dir(), f"source checkout does not exist: {path}")
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                            text=True, capture_output=True, check=False, timeout=10)
    require(result.returncode == 0, f"cannot read source revision at {path}")
    require(result.stdout.strip() == expected,
            f"source revision mismatch at {path}: expected {expected}")
    status = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                            text=True, capture_output=True, check=False, timeout=10)
    require(status.returncode == 0 and not status.stdout.strip(),
            f"source checkout must be clean: {path}")
    if module:
        go_mod = (path / "go.mod").read_text(encoding="utf-8")
        require(re.search(rf"(?m)^\s*go\.datum\.net/compute\s+{re.escape(module)}\s*$", go_mod),
                f"source checkout does not require Compute {module}: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-source", type=Path,
                        help="optional clean Kata provider checkout to compare with the pin")
    parser.add_argument("--compute-source", type=Path,
                        help="optional clean Compute checkout to compare with the pin")
    parser.add_argument("--provider-layout", type=Path,
                        help="optional OCI layout made from the pinned provider bundle digest")
    parser.add_argument("--kata-chart-layout", type=Path,
                        help="optional OCI layout made from the pinned Kata chart digest")
    parser.add_argument("--vmrule-crd", type=Path,
                        help="CRD YAML from the pinned victoria-metrics-k8s-stack chart")
    args = parser.parse_args()
    require(bool(args.provider_source) == bool(args.compute_source),
            "pass both --provider-source and --compute-source, or neither")
    lock = validate_contract()
    if args.provider_layout:
        require(shutil.which("kustomize") and shutil.which("kubeconform"),
                "provider render check requires kustomize and kubeconform")
        require(args.vmrule_crd is not None,
                "provider render check requires --vmrule-crd from the pinned observability chart")
    require(not args.vmrule_crd or args.provider_layout,
            "--vmrule-crd is only used for the provider bundle render")
    if args.kata_chart_layout:
        require(shutil.which("helm") and shutil.which("kubeconform"),
                "chart render check requires helm and kubeconform")
    if args.provider_source:
        verify_source(args.provider_source, lock["kataProvider"]["sourceRevision"],
                      lock["compute"]["moduleVersion"])
        verify_source(args.compute_source, lock["compute"]["sourceRevision"])
    if args.provider_layout:
        validate_provider_render(args.provider_layout, args.vmrule_crd, lock)
    if args.kata_chart_layout:
        validate_chart_render(args.kata_chart_layout, lock)
    print("ARM Kata source, artifact and Flux contract is internally consistent.")
    if args.provider_layout and args.kata_chart_layout:
        print("Pinned provider overlay and Kata chart renders match the exact manager, helper, installer, and RuntimeClass pins.")
    print("Read-only check: strict Kubernetes schema validation may download public schemas; no Kubernetes API, Docker, Kind creation, Helm install, or VM probe was run.")
    print("Runtime readiness still requires separate Colima-VM and Kind-node KVM checks, then QEMU CPU and /dev/shm preparation.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.TimeoutExpired, yaml.YAMLError) as error:
        print(f"Kata prerequisite check failed: {error}", file=sys.stderr)
        sys.exit(1)
