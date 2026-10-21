#!/usr/bin/env python3
"""Create, prove, or explicitly remove one dedicated local Kata QEMU member.

This runtime-only smoke never provisions a VM, changes the host Kind cluster,
or claims Compute Workload/network behavior. All cluster commands target a
process-local Docker socket and an explicit private kubeconfig.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "components/apps-native-kata-runtime-smoke"
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PROFILE = re.compile(r"^apps-native-vz-[a-z0-9-]+$")
HOST_CLUSTER = re.compile(r"^apps-a1t-ci-vz-[a-z0-9-]+$")
MEMBER_CLUSTER = re.compile(r"^apps-a1t-kata-vz-[a-z0-9-]+$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_ID = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_CLASS = "kata-qemu"
NAMESPACE_PREFIX = "kata-runtime-smoke-"
POD_NAME = "guest-kernel"
LABEL_KEY = "apps-native-kata-smoke.datum.net/run-id"
_FAILURE_DIR: Path | None = None
_FAILURE_STAGE = "preflight"


class PreflightError(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise PreflightError(message)


def load_lock() -> dict:
    lock = json.loads((COMPONENT / "image-lock.json").read_text(encoding="utf-8"))
    for field in ("hostNode", "kataChart", "kataInstallerImage", "kataKubectlImage", "smokeImage"):
        require("@" in lock[field] and SHA256.fullmatch(lock[field].rsplit("@", 1)[1]) is not None,
                f"{field} must be pinned by an immutable digest")
    for field in ("kataInstallerArm64Manifest", "kataInstallerArm64Config",
                  "kataKubectlArm64Manifest", "kataKubectlArm64Config",
                  "smokeImageArm64Manifest", "smokeImageArm64Config"):
        require(SHA256.fullmatch(lock[field]) is not None, f"{field} must be an immutable arm64 manifest digest")
    require(lock["runtimeHandler"] == RUNTIME_CLASS and lock["runtimeClass"] == RUNTIME_CLASS,
            "runtime handler and RuntimeClass must agree")
    require(lock.get("smokeImageSource") == "https://github.com/docker-library/busybox" and
            lock.get("smokeImageRevision") == "b6ab7c14ba46700181395f420545aee1ab297934" and
            lock.get("smokeImageVersion") == "1.36.1-glibc",
            "smoke image provenance must match the verified ARM64 registry manifest")
    return lock


def verify_source_manifest_contract() -> None:
    lock = load_lock()
    source = yaml.safe_load((COMPONENT / "kata-chart-source.yaml").read_text(encoding="utf-8"))
    release = yaml.safe_load((COMPONENT / "kata-runtime.yaml").read_text(encoding="utf-8"))
    kind_config = yaml.safe_load((COMPONENT / "kind-kata-config.yaml").read_text(encoding="utf-8"))
    require(source["kind"] == "OCIRepository" and
            source["spec"]["url"] == "oci://quay.io/kata-containers/kata-deploy-charts/kata-deploy" and
            source["spec"]["ref"]["digest"] == lock["kataChart"].rsplit("@", 1)[1] and
            source["spec"].get("layerSelector") == {
                "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                "operation": "copy"},
            "source-owned Kata chart reference differs from its artifact lock")
    spec = release.get("spec", {})
    require(release.get("kind") == "HelmRelease" and "kubeConfig" not in spec,
            "member runtime HelmRelease must not target a remote cluster")
    require(spec.get("targetNamespace") == "kube-system" and
            spec.get("releaseName") == "kata-deploy" and
            spec.get("chartRef") == {"kind": "OCIRepository",
                                      "name": "apps-native-kata-runtime-smoke-chart",
                                      "namespace": "flux-system"},
            "unexpected member-local Kata HelmRelease target")
    values = spec.get("values", {})
    require(values.get("image") == {"reference": lock["kataInstallerImage"], "tag": ""} and
            values.get("kubectlImage") == {"reference": lock["kataKubectlImage"], "tag": ""},
            "Kata installer and helper images must use their locked index digests")
    require(values.get("shims") == {"disableAll": True, "qemu": {"enabled": True}} and
            values.get("defaultShim") == {"amd64": "qemu", "arm64": "qemu"} and
            values.get("runtimeClasses") == {"enabled": True, "createDefault": False},
            "member release must enable only the named QEMU RuntimeClass")
    require(values.get("nodeSelector") == {"node-role.kubernetes.io/control-plane": ""} and
            values.get("tolerations") == [{"key": "node-role.kubernetes.io/control-plane",
                                           "operator": "Exists", "effect": "NoSchedule"}] and
            values.get("imagePullPolicy") == "IfNotPresent" and
            values.get("enableVPCNetworking", False) is False,
            "Kata chart must remain on the single member node with networking disabled")
    nodes = kind_config.get("nodes", [])
    require(kind_config.get("kind") == "Cluster" and len(nodes) == 1,
            "Kind runtime fixture must contain exactly one member node")
    patches = nodes[0].get("kubeadmConfigPatches", [])
    require(len(patches) == 1 and yaml.safe_load(patches[0]) == {
        "kind": "KubeletConfiguration", "allowedUnsafeSysctls": [
            "net.ipv6.conf.all.accept_dad", "net.ipv6.conf.default.accept_dad"]},
        "Kind node must allow exactly the two required Kata guest sysctls")
    mounts = nodes[0].get("extraMounts", [])
    require(len([item for item in mounts if item.get("hostPath") == "/dev/kvm" and
                 item.get("containerPath") == "/dev/kvm"]) == 1 and
            len([item for item in mounts if item.get("hostPath") == "/lib/modules" and
                 item.get("containerPath") == "/lib/modules" and item.get("readOnly") is True]) == 1,
            "Kind node must map one KVM device and read-only kernel modules")


def _safe_private_file(path: Path, expected_mode: int = 0o600) -> None:
    require(not path.is_symlink() and path.is_file(), f"required private evidence file is absent: {path.name}")
    mode = stat.S_IMODE(path.stat().st_mode)
    require(mode == expected_mode, f"private evidence file has unexpected permissions: {path.name}")
    require(path.stat().st_uid == os.getuid(), f"private evidence file has an unexpected owner: {path.name}")


def _host_intent(path: Path, profile: str, cluster: str) -> None:
    raw = path.read_text(encoding="utf-8")
    if raw.lstrip().startswith("{"):
        try:
            intent = json.loads(raw)
        except (ValueError, OSError) as exc:
            raise PreflightError("host creation intent is malformed") from exc
        require(isinstance(intent, dict) and intent.get("profile") == profile and
                intent.get("cluster") == cluster,
                "host creation intent does not match the explicitly selected VZ host")
        return
    # The reviewed historical host bootstrap wrote exactly two newline-terminated
    # lines: profile name, then Kind cluster name. Do not rewrite its evidence.
    lines = raw.splitlines()
    require(len(lines) == 2 and lines == [profile, cluster],
            "legacy host creation intent must contain exactly the selected profile and cluster")


def _host_api_identity(kubeconfig: Path, cluster: str) -> tuple[str, str]:
    try:
        config = yaml.safe_load(kubeconfig.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PreflightError("private host kubeconfig is unreadable") from exc
    require(isinstance(config, dict) and config.get("current-context") == f"kind-{cluster}",
            "private host kubeconfig context does not match the selected host cluster")
    contexts = [entry.get("context", {}) for entry in config.get("contexts", [])
                if entry.get("name") == f"kind-{cluster}"]
    require(len(contexts) == 1 and contexts[0].get("cluster"),
            "private host kubeconfig lacks the exact selected cluster context")
    clusters = [entry.get("cluster", {}) for entry in config.get("clusters", [])
                if entry.get("name") == contexts[0]["cluster"]]
    require(len(clusters) == 1, "private host kubeconfig cluster entry is ambiguous")
    cluster_config = clusters[0]
    require("insecure-skip-tls-verify" not in cluster_config,
            "private host kubeconfig must not disable API TLS verification")
    require("proxy-url" not in cluster_config,
            "private host kubeconfig must not route the host API through a proxy")
    server = cluster_config.get("server", "")
    ca_data = cluster_config.get("certificate-authority-data", "")
    try:
        endpoint = urlsplit(server)
        endpoint_ip = ipaddress.ip_address(endpoint.hostname or "")
        endpoint_port = endpoint.port
    except ValueError as exc:
        raise PreflightError("private host kubeconfig API endpoint is malformed") from exc
    require(endpoint.scheme == "https" and endpoint_ip.is_loopback and
            endpoint_port is not None and 1 <= endpoint_port <= 65535 and
            endpoint.path in ("", "/") and not endpoint.username and not endpoint.password and
            not endpoint.query and not endpoint.fragment and ca_data,
            "private host kubeconfig must pin a loopback HTTPS API endpoint and CA certificate")
    try:
        ca_bytes = base64.b64decode(ca_data, validate=True)
    except (ValueError, TypeError) as exc:
        raise PreflightError("private host kubeconfig CA certificate is malformed") from exc
    require(bool(ca_bytes), "private host kubeconfig CA certificate is empty")
    return server, hashlib.sha256(ca_bytes).hexdigest()


def read_host_evidence(path: Path, profile: str, cluster: str) -> tuple[str, Path, str, str]:
    require(path.is_absolute() and path.is_dir() and not path.is_symlink(),
            "an existing private host evidence directory is required")
    require(stat.S_IMODE(path.stat().st_mode) == 0o700 and path.stat().st_uid == os.getuid(),
            "host evidence directory must be user-owned mode 0700")
    intent_path = path / "cluster-create-intent"
    node_path = path / "cluster-node-id"
    kubeconfig = path / "host.kubeconfig"
    for item in (intent_path, node_path, kubeconfig):
        _safe_private_file(item)
    _host_intent(intent_path, profile, cluster)
    recorded_node_id = node_path.read_text(encoding="ascii").strip()
    require(NODE_ID.fullmatch(recorded_node_id) is not None,
            "host evidence has an invalid Kind node container identity")
    api_server, ca_sha256 = _host_api_identity(kubeconfig, cluster)
    return recorded_node_id, kubeconfig, api_server, ca_sha256


def docker_env(profile: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DOCKER_CONTEXT", None)
    env.pop("KUBECONFIG", None)
    socket = Path.home() / ".colima" / profile / "docker.sock"
    require(socket.is_socket(), "the selected VZ profile Docker socket is missing")
    env["DOCKER_HOST"] = "unix://" + str(socket)
    return env


def run(argv: list[str], env: dict[str, str], *, stdin: str | None = None,
        timeout: int = 30, stage: str | None = None) -> str:
    global _FAILURE_STAGE
    if stage:
        _FAILURE_STAGE = stage
    try:
        result = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                                env=env, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _persist_failure(Path(argv[0]).name, "timeout-or-start-error",
                         _diagnostic_text(getattr(exc, "stdout", "")),
                         _diagnostic_text(getattr(exc, "stderr", "")) or str(exc))
        raise PreflightError(f"{Path(argv[0]).name} did not complete within its bounded command window") from exc
    if result.returncode != 0:
        _persist_failure(Path(argv[0]).name, f"exit-{result.returncode}", result.stdout, result.stderr)
        raise PreflightError(f"{Path(argv[0]).name} returned a failure; private diagnostics were retained")
    return result.stdout.strip()


def _redact_diagnostic(value: str) -> str:
    value = value[:8192]
    value = re.sub(r"(?im)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+",
                   r"\1[REDACTED]", value)
    value = re.sub(r"(?im)(authorization|token|password|client-key-data|client-certificate-data|certificate-authority-data)(\s*[:=]\s*)([^\s,}]+)",
                   r"\1\2[REDACTED]", value)
    value = re.sub(r"(?im)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    return value


def _diagnostic_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _persist_failure(command: str, classification: str, stdout: str, stderr: str) -> None:
    if _FAILURE_DIR is None or not _FAILURE_DIR.is_dir():
        return
    record = {
        "stage": _FAILURE_STAGE,
        "classification": classification,
        "executable": command,
        "stdout": _redact_diagnostic(stdout),
        "stderr": _redact_diagnostic(stderr),
        "createdAtUnix": int(time.time()),
    }
    target = _FAILURE_DIR / f"failure-{uuid.uuid4().hex}.json"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fsync_dir(_FAILURE_DIR)
    except OSError:
        # Preserve the original failure. The journal still records its last
        # durable phase, which is the cleanup/recovery authority.
        pass


def kubectl_args(kubeconfig: Path, context: str, *args: str) -> list[str]:
    return ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context,
            "--request-timeout=15s", *args]


def get_node_uid(kubeconfig: Path, context: str, name: str, env: dict[str, str],
                 *, require_ready: bool = True) -> tuple[str, str, dict]:
    raw = run(kubectl_args(kubeconfig, context, "get", "node", name, "-o", "json"), env, timeout=25)
    node = json.loads(raw)
    meta = node.get("metadata", {})
    status = node.get("status", {})
    require(meta.get("name") == name and meta.get("uid"), "selected Kind API returned an unexpected node")
    if require_ready:
        require(any(cond.get("type") == "Ready" and cond.get("status") == "True"
                    for cond in status.get("conditions", [])), "selected Kind node is not Ready")
    return meta["uid"], status.get("nodeInfo", {}).get("kernelVersion", ""), node


def read_inotify_capacity(profile: str, env: dict[str, str]) -> int:
    value = run(["colima", "ssh", "--profile", profile, "--", "cat",
                 "/proc/sys/fs/inotify/max_user_instances"], env)
    require(re.fullmatch(r"[0-9]{1,10}", value) is not None and int(value) >= 1024,
            "dedicated VM requires fs.inotify.max_user_instances >= 1024 before adding a member; "
            f"observed {value[:64]!r}")
    return int(value)


def preflight(profile: str, host_cluster: str, host_evidence: Path,
              member_cluster: str, private_dir: Path, *, cleanup: bool = False,
              cleanup_intent: bool = False) -> dict:
    require(PROFILE.fullmatch(profile) is not None, "explicit dedicated Apps VZ profile name is required")
    require(HOST_CLUSTER.fullmatch(host_cluster) is not None, "explicit dedicated Apps host cluster name is required")
    require(MEMBER_CLUSTER.fullmatch(member_cluster) is not None, "explicit Kata member cluster name is required")
    require(member_cluster != host_cluster, "member cluster must be distinct from the existing host cluster")
    require(private_dir.is_absolute() and not private_dir.is_symlink(),
            "private smoke evidence must use an absent absolute path")
    host_node_id, host_kubeconfig, host_api_server, host_ca_sha256 = read_host_evidence(
        host_evidence, profile, host_cluster)
    env = docker_env(profile)
    architecture = run(["docker", "info", "--format", "{{.Architecture}}"], env)
    require(architecture in ("aarch64", "arm64"), "selected VZ Docker daemon is not ARM64")
    cpu_count_text = run(["docker", "info", "--format", "{{.NCPU}}"], env)
    require(cpu_count_text.isdigit() and int(cpu_count_text) >= 6,
            "selected VZ Docker daemon has fewer than six CPUs")
    run(["colima", "ssh", "--profile", profile, "--", "test", "-c", "/dev/kvm"], env)
    clusters_text = run(["kind", "get", "clusters"], env)
    clusters = sorted(line.strip() for line in clusters_text.splitlines() if line.strip())
    allowed = [sorted([host_cluster])]
    if cleanup:
        allowed = [sorted([host_cluster, member_cluster])]
        if cleanup_intent:
            allowed.append([host_cluster])
    require(clusters in allowed,
            "Kind cluster census differs from the exact dedicated host/member set; refusing adoption or deletion")
    host_node = f"{host_cluster}-control-plane"
    host_nodes = sorted(line.strip() for line in run(["kind", "get", "nodes", "--name", host_cluster], env).splitlines()
                        if line.strip())
    require(host_nodes == [host_node], "dedicated host Kind node identity changed")
    actual_host_id = run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}", host_node], env)
    require(actual_host_id == host_node_id, "dedicated host node container no longer matches its creation evidence")
    host_context = f"kind-{host_cluster}"
    current = run(["kubectl", "--kubeconfig", str(host_kubeconfig), "config", "current-context"], env)
    require(current == host_context, "private host kubeconfig context is not the selected dedicated host")
    host_uid, _, host_node_doc = get_node_uid(host_kubeconfig, host_context, host_node, env)
    member_present = member_cluster in clusters
    inotify_capacity = None
    if cleanup:
        if member_present:
            member_node = f"{member_cluster}-control-plane"
            member_nodes = sorted(line.strip() for line in run(["kind", "get", "nodes", "--name", member_cluster], env).splitlines()
                                 if line.strip())
            require(member_nodes == [member_node], "member Kind node identity changed")
        else:
            require(cleanup_intent, "member cluster disappeared before an authorized cleanup intent")
    else:
        require(member_cluster not in clusters, "member cluster already exists; refusing adoption")
        require(not private_dir.exists(), "private smoke evidence path already exists; refusing reuse")
        # Host and member containers share the VM root UID's inotify allowance.
        # Low capacity must prevent creation, but must never obstruct cleanup.
        inotify_capacity = read_inotify_capacity(profile, env)
    return {"dockerEnv": env, "hostKubeconfig": host_kubeconfig,
            "hostNodeID": host_node_id, "hostNodeUID": host_uid,
            "hostAPIServer": host_api_server, "hostCASHA256": host_ca_sha256,
            "hostNode": host_node, "cpuCount": int(cpu_count_text),
            "inotifyMaxUserInstances": inotify_capacity,
            "memberNode": f"{member_cluster}-control-plane", "memberPresent": member_present}


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_journal(path: Path, journal: dict, *, create: bool = False) -> None:
    target = path / "runtime-smoke.json"
    temp = path / f".runtime-smoke-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(journal, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temp, target)
            temp.unlink()
        else:
            os.replace(temp, target)
        fsync_dir(path)
    finally:
        temp.unlink(missing_ok=True)


def verify_execution_source() -> str:
    dirty = run(["git", "-C", str(ROOT), "status", "--porcelain"], os.environ.copy())
    require(not dirty, "execution requires a clean source checkout")
    revision = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], os.environ.copy())
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
            "source revision is not a full Git commit identity")
    run(["git", "-C", str(ROOT), "verify-commit", revision], os.environ.copy(), timeout=15)
    return revision


def validate_private_directory(path: Path) -> None:
    require(path.is_absolute() and not path.is_symlink() and path.is_dir(),
            "private smoke evidence directory is missing or unsafe")
    metadata = path.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid() and
            stat.S_IMODE(metadata.st_mode) == 0o700,
            "private smoke evidence directory must be owned mode 0700")


def read_journal(path: Path) -> dict:
    _safe_private_file(path / "runtime-smoke.json")
    try:
        value = json.loads((path / "runtime-smoke.json").read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise PreflightError("private runtime journal is malformed") from exc
    require(isinstance(value, dict), "private runtime journal has an invalid shape")
    return value


def apply_yaml(kubeconfig: Path, context: str, manifest: str, env: dict[str, str], timeout: int = 60) -> None:
    run(kubectl_args(kubeconfig, context, "apply", "-f", "-"), env, stdin=manifest, timeout=timeout)


def kustomize_apply(path: Path, kubeconfig: Path, context: str, env: dict[str, str]) -> None:
    rendered = run(["kustomize", "build", str(path)], env, timeout=90)
    require(bool(rendered.strip()), f"source component rendered empty: {path.name}")
    apply_yaml(kubeconfig, context, rendered, env, timeout=90)


def validate_guest_evidence(pod_doc: dict, member_node: str, run_id: str,
                            expected_uid: str, image_id: str, guest_kernel: str,
                            node_kernel: str, guest_arch: str) -> None:
    lock = load_lock()
    statuses = pod_doc.get("status", {}).get("containerStatuses", [])
    metadata = pod_doc.get("metadata", {})
    containers = pod_doc.get("spec", {}).get("containers", [])
    require(pod_doc.get("status", {}).get("phase") == "Running" and
            pod_doc.get("spec", {}).get("runtimeClassName") == RUNTIME_CLASS and
            pod_doc.get("spec", {}).get("nodeName") == member_node and
            pod_doc.get("spec", {}).get("nodeSelector") == {
                "kubernetes.io/hostname": member_node,
                "katacontainers.io/kata-runtime": "true"} and
            pod_doc.get("spec", {}).get("automountServiceAccountToken") is False and
            metadata.get("labels", {}).get(LABEL_KEY) == run_id and
            metadata.get("uid") == expected_uid and len(statuses) == 1 and len(containers) == 1 and
            containers[0].get("image") == lock["smokeImage"] and
            statuses[0].get("ready") is True and statuses[0].get("containerID"),
            "smoke Pod UID, image, runtime, readiness, container or node identity differs from the request")
    match = re.search(r"(sha256:[0-9a-f]{64})$", image_id)
    require(match is not None and match.group(1) in {
        lock["smokeImageArm64Manifest"], lock["smokeImageArm64Config"],
        lock["smokeImage"].rsplit("@", 1)[1]},
        "container runtime imageID does not match the locked ARM64 child, config or index")
    require(guest_kernel and guest_kernel != node_kernel,
            "guest kernel does not differ from the Kind node kernel")
    require(guest_arch in ("aarch64", "arm64"), "guest did not report ARM64 architecture")


def kubectl_json(kubeconfig: Path, context: str, env: dict[str, str], *args: str) -> dict:
    return json.loads(run(kubectl_args(kubeconfig, context, *args, "-o", "json"), env))


def docker_member_exec_args(container_id: str, *args: str) -> list[str]:
    require(NODE_ID.fullmatch(container_id) is not None,
            "member Docker operations require the journaled immutable container ID")
    return ["docker", "exec", container_id, *args]


def verify_readonly_modules_mount(mountinfo: str, resolved_path: str) -> None:
    """Check the node's effective mount, including /lib -> /usr/lib aliases."""
    require(resolved_path.startswith("/") and ".." not in Path(resolved_path).parts and
            len(resolved_path) <= 256 and len(mountinfo) <= 1_000_000,
            "member module mount observation is invalid")
    matches = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[4] == resolved_path:
            matches.append(fields[5].split(","))
    require(len(matches) == 1 and "ro" in matches[0] and "rw" not in matches[0],
            "member node module tree is not mounted read-only at its resolved path")


def create_yaml(kubeconfig: Path, context: str, manifest: str, env: dict[str, str],
                *, stage: str) -> dict:
    created = run(kubectl_args(kubeconfig, context, "create", "-f", "-", "-o", "json"),
                  env, stdin=manifest, timeout=60, stage=stage)
    value = json.loads(created)
    require(bool(value.get("metadata", {}).get("uid")),
            f"API server did not return a UID for the newly created {value.get('kind', 'object')}")
    return value


def verify_flux_and_installer(kubeconfig: Path, context: str, env: dict[str, str]) -> dict:
    source = kubectl_json(kubeconfig, context, env, "-n", "flux-system", "get",
                          "ocirepository", "apps-native-kata-runtime-smoke-chart")
    release = kubectl_json(kubeconfig, context, env, "-n", "flux-system", "get",
                           "helmrelease", "apps-native-kata-runtime-smoke")
    source_meta, source_status = source.get("metadata", {}), source.get("status", {})
    release_meta, release_status = release.get("metadata", {}), release.get("status", {})
    source_generation = source_meta.get("generation")
    release_generation = release_meta.get("generation")
    chart_digest = load_lock()["kataChart"].rsplit("@", 1)[1]
    source_spec = source.get("spec", {})
    require(source_spec.get("url") ==
            "oci://quay.io/kata-containers/kata-deploy-charts/kata-deploy" and
            source_spec.get("ref", {}).get("digest") == chart_digest,
            "Flux chart source does not reference the locked Kata chart digest")
    require(source_generation and source_status.get("observedGeneration") == source_generation and
            source_status.get("artifact", {}).get("revision") == chart_digest and
            SHA256.fullmatch(source_status.get("artifact", {}).get("digest", "")),
            "Flux chart source has not published an artifact for its current generation")
    source_ready = [condition for condition in source_status.get("conditions", [])
                    if condition.get("type") == "Ready" and condition.get("status") == "True"]
    require(source_ready and all(condition.get("observedGeneration") == source_generation
                                 for condition in source_ready),
            "Flux chart source is not Ready for its current generation")
    expected_revision = load_lock()["kataChartVersion"] + "+" + chart_digest.split(":", 1)[1][:12]
    require(release_generation and release_status.get("observedGeneration") == release_generation and
            release_status.get("lastAttemptedGeneration") == release_generation and
            release_status.get("lastAttemptedRevision") == expected_revision and
            release_status.get("lastAttemptedRevisionDigest") == chart_digest,
            "Kata HelmRelease has not reconciled the current generation and locked OCI digest")
    ready = [condition for condition in release_status.get("conditions", [])
             if condition.get("type") == "Ready" and condition.get("status") == "True"]
    require(ready and all(condition.get("observedGeneration", release_generation) == release_generation
                          for condition in ready),
            "Kata HelmRelease is not Ready for its current generation")
    source_revision = source_status["artifact"]["revision"]
    applied_revision = release_status["lastAttemptedRevision"]

    daemonset = kubectl_json(kubeconfig, context, env, "-n", "kube-system", "get", "daemonset", "kata-deploy")
    ds_meta, ds_spec, ds_status = daemonset.get("metadata", {}), daemonset.get("spec", {}), daemonset.get("status", {})
    ds_generation = ds_meta.get("generation")
    require(ds_generation and ds_status.get("observedGeneration") == ds_generation and
            ds_status.get("numberReady", 0) >= 1,
            "Kata DaemonSet has not observed its current generation with a Ready node")
    selector_labels = ds_spec.get("selector", {}).get("matchLabels", {})
    require(selector_labels, "Kata DaemonSet has no exact pod selector")
    selector = ",".join(f"{key}={value}" for key, value in sorted(selector_labels.items()))
    pods = kubectl_json(kubeconfig, context, env, "-n", "kube-system", "get", "pods", "-l", selector)
    matches = [item for item in pods.get("items", [])
               if any(owner.get("kind") == "DaemonSet" and owner.get("uid") == ds_meta.get("uid")
                      for owner in item.get("metadata", {}).get("ownerReferences", []))]
    require(matches, "Kata DaemonSet has no pods owned by its current UID")
    image_lock = load_lock()
    expected_by_image = {
        image_lock["kataInstallerImage"]: {image_lock["kataInstallerImage"].rsplit("@", 1)[1],
                                            image_lock["kataInstallerArm64Manifest"],
                                            image_lock["kataInstallerArm64Config"]},
        image_lock["kataKubectlImage"]: {image_lock["kataKubectlImage"].rsplit("@", 1)[1],
                                          image_lock["kataKubectlArm64Manifest"],
                                          image_lock["kataKubectlArm64Config"]},
    }
    observed_images = []
    for pod in matches:
        pod_status = pod.get("status", {})
        container_statuses = pod_status.get("containerStatuses", [])
        init_statuses = pod_status.get("initContainerStatuses", [])
        spec_containers = pod.get("spec", {}).get("containers", [])
        init_containers = pod.get("spec", {}).get("initContainers", [])
        require(pod_status.get("phase") == "Running" and container_statuses and
                all(status.get("ready") is True for status in container_statuses) and
                len(init_statuses) == len(init_containers) and
                all(status.get("state", {}).get("terminated", {}).get("exitCode") == 0 or
                    status.get("ready") is True for status in init_statuses),
                "Kata DaemonSet pod is not fully Ready")
        require(len(container_statuses) == len(spec_containers),
                "Kata installer pod image status does not match its declared containers")
        for spec_list, status_list in ((spec_containers, container_statuses),
                                       (init_containers, init_statuses)):
            spec_by_name = {container.get("name"): container for container in spec_list}
            status_by_name = {status.get("name"): status for status in status_list}
            require(None not in spec_by_name and None not in status_by_name and
                    len(spec_by_name) == len(spec_list) and len(status_by_name) == len(status_list) and
                    set(spec_by_name) == set(status_by_name),
                    "Kata installer pod container statuses do not match declared names")
            for name, container in spec_by_name.items():
                status = status_by_name[name]
                image = container.get("image")
                require(image in expected_by_image, "Kata DaemonSet declared an image outside its immutable lock")
                image_id = status.get("imageID", "")
                digest = re.search(r"(sha256:[0-9a-f]{64})$", image_id)
                require(digest is not None and digest.group(1) in expected_by_image[image],
                        "Kata installer container imageID differs from its locked index or ARM64 child")
                observed_images.append({"name": name, "image": image, "imageID": image_id})
    return {"sourceArtifactRevision": source_revision, "releaseRevision": applied_revision,
            "releaseRevisionDigest": release_status["lastAttemptedRevisionDigest"],
            "sourceGeneration": source_generation, "releaseGeneration": release_generation,
            "daemonSetUID": ds_meta.get("uid"), "daemonSetGeneration": ds_generation,
            "installerImages": observed_images}


def setup_member(profile: str, host_cluster: str, host_evidence: Path,
                 member_cluster: str, private_dir: Path) -> dict:
    global _FAILURE_DIR, _FAILURE_STAGE
    _FAILURE_DIR = None
    _FAILURE_STAGE = "source-preflight"
    verify_source_manifest_contract()
    # Source cleanliness/signature are checked before creating the evidence
    # directory or starting any Docker/Kind operation.
    source_revision = verify_execution_source()
    baseline = preflight(profile, host_cluster, host_evidence, member_cluster, private_dir)
    env = baseline["dockerEnv"]
    private_dir.mkdir(mode=0o700)
    _FAILURE_DIR = private_dir
    private_stat = private_dir.lstat()
    require(stat.S_ISDIR(private_stat.st_mode) and private_stat.st_uid == os.getuid() and
            stat.S_IMODE(private_stat.st_mode) == 0o700,
            "new private evidence directory did not retain its owner-only mode")
    lock = load_lock()
    journal = {
        "version": 1, "profile": profile, "hostCluster": host_cluster,
        "hostEvidence": str(host_evidence), "hostNodeID": baseline["hostNodeID"],
        "hostNodeUID": baseline["hostNodeUID"],
        "hostAPIServer": baseline["hostAPIServer"],
        "hostCASHA256": baseline["hostCASHA256"],
        "memberCluster": member_cluster,
        "sourceRevision": source_revision, "runtimeClass": lock["runtimeClass"],
        "smokeImage": lock["smokeImage"], "smokeImageArm64Manifest": lock["smokeImageArm64Manifest"],
        "cpuCountAtPreflight": baseline["cpuCount"], "phase": "create-intent",
        "inotifyMaxUserInstancesAtPreflight": baseline["inotifyMaxUserInstances"],
        "createdAtUnix": int(time.time()),
    }
    write_journal(private_dir, journal, create=True)
    _FAILURE_STAGE = "member-create"
    config_path = private_dir / "kind-member.yaml"
    config = yaml.safe_load((COMPONENT / "kind-kata-config.yaml").read_text(encoding="utf-8"))
    require(config.get("kind") == "Cluster" and len(config.get("nodes", [])) == 1,
            "checked-in member Kind fixture changed shape")
    node = config["nodes"][0]
    mounts = node.get("extraMounts", [])
    require(len([m for m in mounts if m.get("hostPath") == "/dev/kvm" and m.get("containerPath") == "/dev/kvm"]) == 1 and
            len([m for m in mounts if m.get("hostPath") == "/lib/modules" and m.get("containerPath") == "/lib/modules" and m.get("readOnly") is True]) == 1,
            "member fixture must map one KVM device and read-only kernel modules")
    config["name"] = member_cluster
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    os.chmod(config_path, 0o600)
    member_kubeconfig = private_dir / "member.kubeconfig"
    node_image = lock["hostNode"]
    run(["docker", "pull", "--platform", "linux/arm64", node_image], env, timeout=300)
    try:
        run(["kind", "create", "cluster", "--name", member_cluster, "--image", node_image,
             "--config", str(config_path), "--kubeconfig", str(member_kubeconfig), "--retain"],
            env, timeout=600, stage="kind-create")
    except PreflightError:
        # Kind can create the node and then lose its client response. Record
        # only the exact deterministic node container if it exists; never
        # infer identity from a cluster census alone.
        try:
            partial_node_id = run(["docker", "inspect", "--type", "container", "--format",
                                    "{{.Id}}", baseline["memberNode"]], env, stage="kind-create-recovery")
        except PreflightError:
            raise
        require(NODE_ID.fullmatch(partial_node_id) is not None,
                "partially created member has no verifiable node container identity")
        journal.update({"memberNodeID": partial_node_id, "phase": "member-created-no-api-uid"})
        write_journal(private_dir, journal)
        raise
    member_node = baseline["memberNode"]
    actual_node_id = run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}", member_node],
                         env, stage="member-identity-record")
    require(NODE_ID.fullmatch(actual_node_id) is not None,
            "created member node has no valid container identity")
    journal.update({"memberNodeID": actual_node_id, "phase": "member-created-no-api-uid"})
    write_journal(private_dir, journal)
    os.chmod(member_kubeconfig, 0o600)
    context = f"kind-{member_cluster}"
    require(run(["kubectl", "--kubeconfig", str(member_kubeconfig), "config", "current-context"], env) == context,
            "Kind wrote an unexpected member kubeconfig context")
    run(["kubectl", "--kubeconfig", str(member_kubeconfig), "--context", context,
         "wait", f"node/{member_node}", "--for=condition=Ready", "--timeout=180s"], env, timeout=195)
    member_uid, node_kernel, node_doc = get_node_uid(member_kubeconfig, context, member_node, env)
    require(node_doc["status"].get("nodeInfo", {}).get("architecture") == "arm64",
            "member node did not report ARM64 architecture")
    require(bool(node_kernel), "member node did not report a kernel version")
    journal.update({"memberNodeUID": member_uid,
                    "nodeKernelVersion": node_kernel, "phase": "member-created"})
    write_journal(private_dir, journal)
    _FAILURE_STAGE = "member-runtime-preflight"
    resolved_modules = run(docker_member_exec_args(actual_node_id, "sh", "-ec",
                            "test -c /dev/kvm && test -d /lib/modules && readlink -f /lib/modules"),
                           env, timeout=30)
    node_mountinfo = run(docker_member_exec_args(actual_node_id, "cat", "/proc/self/mountinfo"),
                         env, timeout=30)
    verify_readonly_modules_mount(node_mountinfo, resolved_modules)

    _FAILURE_STAGE = "flux-install"
    kustomize_apply(ROOT / "components/flux", member_kubeconfig, context, env)
    for deployment in ("source-controller", "helm-controller", "kustomize-controller", "notification-controller"):
        run(kubectl_args(member_kubeconfig, context, "-n", "flux-system", "wait",
                         f"deployment/{deployment}", "--for=condition=Available", "--timeout=240s"),
            env, timeout=255)
    journal["phase"] = "member-flux-ready"
    write_journal(private_dir, journal)
    _FAILURE_STAGE = "kata-install"
    kustomize_apply(COMPONENT, member_kubeconfig, context, env)
    run(kubectl_args(member_kubeconfig, context, "-n", "flux-system", "wait",
                     "helmrelease/apps-native-kata-runtime-smoke", "--for=condition=Ready", "--timeout=600s"),
        env, timeout=615)
    run(kubectl_args(member_kubeconfig, context, "-n", "kube-system", "rollout", "status",
                     "daemonset/kata-deploy", "--timeout=300s"), env, timeout=315)
    flux_evidence = verify_flux_and_installer(member_kubeconfig, context, env)
    runtime_class = json.loads(run(kubectl_args(member_kubeconfig, context, "get", "runtimeclass",
                                               RUNTIME_CLASS, "-o", "json"), env))
    require(runtime_class.get("handler") == "kata-qemu", "RuntimeClass handler is not kata-qemu")
    member_node_doc = json.loads(run(kubectl_args(member_kubeconfig, context, "get", "node", member_node,
                                                  "-o", "json"), env))
    require(member_node_doc.get("metadata", {}).get("labels", {}).get("katacontainers.io/kata-runtime") == "true",
            "Kata DaemonSet has not marked the member node runtime-ready")
    journal.update({"phase": "kata-ready", **flux_evidence})
    write_journal(private_dir, journal)

    _FAILURE_STAGE = "guest-smoke"
    workaround = r'''set -eu
configs=$(find /opt/kata/share/defaults/kata-containers -name configuration-qemu.toml -type f)
test -n "$configs"
for config in $configs; do
  grep -q '^cpu_features = ' "$config"
  sed -i 's/^cpu_features = .*/cpu_features = ""/' "$config"
  grep -q '^cpu_features = ""$' "$config"
done
mount -o remount,size=8G /dev/shm
blocks=$(df -k /dev/shm | awk 'NR==2 {print $2}')
test "$blocks" -ge 8388608
'''
    run(docker_member_exec_args(actual_node_id, "sh", "-ec", workaround), env, timeout=45)

    run_id = uuid.uuid4().hex[:12]
    namespace = NAMESPACE_PREFIX + run_id
    labels = {LABEL_KEY: run_id}
    namespace_doc = {"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": namespace, "labels": labels}}
    journal.update({"runID": run_id, "smokeNamespace": namespace,
                    "namespaceCreateIntent": namespace_doc, "phase": "namespace-create-intent"})
    write_journal(private_dir, journal)
    namespace_created = create_yaml(member_kubeconfig, context, json.dumps(namespace_doc), env,
                                    stage="namespace-create")
    require(namespace_created.get("metadata", {}).get("name") == namespace and
            namespace_created.get("metadata", {}).get("labels", {}).get(LABEL_KEY) == run_id,
            "created smoke Namespace differs from its exact intent")
    journal["smokeNamespaceUID"] = namespace_created["metadata"]["uid"]
    journal["phase"] = "namespace-created"
    write_journal(private_dir, journal)
    namespace_live = kubectl_json(member_kubeconfig, context, env, "get", "namespace", namespace)
    require(namespace_live.get("metadata", {}).get("uid") == journal["smokeNamespaceUID"] and
            namespace_live.get("metadata", {}).get("labels", {}).get(LABEL_KEY) == run_id,
            "smoke Namespace was replaced after creation")
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": POD_NAME, "namespace": namespace, "labels": labels},
        "spec": {
            "runtimeClassName": RUNTIME_CLASS,
            "automountServiceAccountToken": False,
            "nodeSelector": {"kubernetes.io/hostname": member_node,
                             "katacontainers.io/kata-runtime": "true"},
            "tolerations": [{"key": "node-role.kubernetes.io/control-plane",
                             "operator": "Exists", "effect": "NoSchedule"}],
            "containers": [{"name": "guest", "image": lock["smokeImage"],
                             "imagePullPolicy": "IfNotPresent",
                             "command": ["sh", "-ec", "uname -r; uname -m; sleep 3600"],
                             "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                           "limits": {"cpu": "1", "memory": "2Gi"},
                             },
                             "securityContext": {"allowPrivilegeEscalation": False,
                                                 "capabilities": {"drop": ["ALL"]},
                                                 "seccompProfile": {"type": "RuntimeDefault"}}}],
        },
    }
    journal.update({"smokePod": POD_NAME, "podCreateIntent": pod, "phase": "pod-create-intent"})
    write_journal(private_dir, journal)
    pod_created = create_yaml(member_kubeconfig, context, json.dumps(pod), env, stage="smoke-pod-create")
    require(pod_created.get("metadata", {}).get("name") == POD_NAME and
            pod_created.get("metadata", {}).get("namespace") == namespace and
            pod_created.get("metadata", {}).get("labels", {}).get(LABEL_KEY) == run_id and
            pod_created.get("spec", {}).get("runtimeClassName") == RUNTIME_CLASS and
            pod_created.get("spec", {}).get("containers", [{}])[0].get("image") == lock["smokeImage"] and
            pod_created.get("spec", {}).get("automountServiceAccountToken") is False,
            "created smoke Pod differs from its exact intent")
    journal.update({"smokePodUID": pod_created["metadata"]["uid"], "phase": "smoke-created"})
    write_journal(private_dir, journal)
    run(kubectl_args(member_kubeconfig, context, "-n", namespace, "wait", f"pod/{POD_NAME}",
                     "--for=condition=Ready", "--timeout=300s"), env, timeout=315)
    pod_doc = json.loads(run(kubectl_args(member_kubeconfig, context, "-n", namespace,
                                          "get", "pod", POD_NAME, "-o", "json"), env))
    namespace_live = kubectl_json(member_kubeconfig, context, env, "get", "namespace", namespace)
    require(namespace_live.get("metadata", {}).get("uid") == journal["smokeNamespaceUID"] and
            namespace_live.get("metadata", {}).get("labels", {}).get(LABEL_KEY) == run_id,
            "smoke Namespace changed while the guest was running")
    statuses = pod_doc.get("status", {}).get("containerStatuses", [])
    require(len(statuses) == 1, "smoke Pod did not report exactly one container")
    require(pod_doc.get("metadata", {}).get("uid") == journal["smokePodUID"],
            "smoke Pod UID changed after creation")
    image_id = statuses[0].get("imageID", "")
    guest_kernel = run(kubectl_args(member_kubeconfig, context, "-n", namespace, "exec", POD_NAME,
                                   "--", "uname", "-r"), env, timeout=30)
    guest_arch = run(kubectl_args(member_kubeconfig, context, "-n", namespace, "exec", POD_NAME,
                                  "--", "uname", "-m"), env, timeout=30)
    validate_guest_evidence(pod_doc, member_node, run_id, journal["smokePodUID"], image_id,
                            guest_kernel, node_kernel, guest_arch)
    journal.update({"phase": "smoke-passed", "smokeImageID": image_id,
                    "guestKernelVersion": guest_kernel, "guestArchitecture": guest_arch,
                    "completedAtUnix": int(time.time())})
    write_journal(private_dir, journal)
    return journal


def cleanup_member(profile: str, host_cluster: str, host_evidence: Path,
                   member_cluster: str, private_dir: Path) -> None:
    global _FAILURE_DIR, _FAILURE_STAGE
    validate_private_directory(private_dir)
    _FAILURE_DIR = private_dir
    _FAILURE_STAGE = "cleanup"
    journal = read_journal(private_dir)
    phase = journal.get("phase")
    clean_revision = verify_execution_source()
    require(phase in ("member-created-no-api-uid", "member-created", "member-flux-ready",
                      "kata-ready", "namespace-create-intent", "namespace-created",
                      "pod-create-intent", "smoke-created", "smoke-passed",
                      "delete-intent", "member-deleted") and
            journal.get("profile") == profile and journal.get("hostCluster") == host_cluster and
            journal.get("memberCluster") == member_cluster and
            journal.get("hostEvidence") == str(host_evidence) and
            journal.get("sourceRevision") == clean_revision and
            NODE_ID.fullmatch(journal.get("memberNodeID", "")) is not None,
            "cleanup requires the matching runtime smoke journal")
    baseline = preflight(profile, host_cluster, host_evidence, member_cluster, private_dir,
                         cleanup=True, cleanup_intent=(phase in ("delete-intent", "member-deleted")))
    env = baseline["dockerEnv"]
    require(journal.get("hostNodeID") == baseline["hostNodeID"] and
            journal.get("hostNodeUID") == baseline["hostNodeUID"] and
            journal.get("hostAPIServer") == baseline["hostAPIServer"] and
            journal.get("hostCASHA256") == baseline["hostCASHA256"],
            "host cluster identity changed since the smoke was created")
    if not baseline["memberPresent"]:
        require(phase in ("delete-intent", "member-deleted"),
                "member cluster disappeared before an authorized cleanup intent")
        if phase == "member-deleted":
            return
        journal["phase"] = "member-deleted"
        journal["deletedAtUnix"] = int(time.time())
        write_journal(private_dir, journal)
        return
    require(phase != "member-deleted", "a member cluster reappeared after cleanup was recorded")
    node_name = baseline["memberNode"]
    node_id = run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}", node_name], env)
    require(node_id == journal.get("memberNodeID"), "member node container was replaced; refusing cleanup")
    member_kubeconfig = private_dir / "member.kubeconfig"
    context = f"kind-{member_cluster}"
    if journal.get("memberNodeUID"):
        _safe_private_file(member_kubeconfig)
        node_uid, _, _ = get_node_uid(member_kubeconfig, context, node_name, env, require_ready=False)
        require(node_uid == journal.get("memberNodeUID"), "member API node UID changed; refusing cleanup")
    else:
        require(phase in ("member-created-no-api-uid", "delete-intent") and
                (phase == "member-created-no-api-uid" or journal.get("cleanupWithoutAPINodeUID") is True),
                "cleanup requires the recorded member API node UID")
        if member_kubeconfig.exists():
            _safe_private_file(member_kubeconfig)
            try:
                recovered_uid, _, _ = get_node_uid(member_kubeconfig, context, node_name, env,
                                                   require_ready=False)
            except PreflightError:
                recovered_uid = ""
            if recovered_uid:
                journal["memberNodeUID"] = recovered_uid
    if not journal.get("memberNodeUID"):
        journal["cleanupWithoutAPINodeUID"] = True
    journal["phase"] = "delete-intent"
    journal["deleteIntentAtUnix"] = int(time.time())
    write_journal(private_dir, journal)
    try:
        run(["kind", "delete", "cluster", "--name", member_cluster], env, timeout=180)
    except PreflightError:
        remaining = [line.strip() for line in run(["kind", "get", "clusters"], env).splitlines() if line.strip()]
        require(sorted(remaining) in ([host_cluster], sorted([host_cluster, member_cluster])),
                "member cluster deletion outcome is uncertain; preserving journal and diagnostics")
    remaining = [line.strip() for line in run(["kind", "get", "clusters"], env).splitlines() if line.strip()]
    require(sorted(remaining) == [host_cluster], "member deletion left an unexpected Kind cluster census")
    journal["phase"] = "member-deleted"
    journal["deletedAtUnix"] = int(time.time())
    write_journal(private_dir, journal)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--host-cluster", required=True)
    parser.add_argument("--host-evidence", required=True, type=Path)
    parser.add_argument("--member-cluster", required=True)
    parser.add_argument("--private-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="create and run the isolated member smoke")
    mode.add_argument("--cleanup", action="store_true", help="delete only a completed, identity-verified member cluster")
    args = parser.parse_args()
    try:
        verify_source_manifest_contract()
        if args.cleanup:
            cleanup_member(args.profile, args.host_cluster, args.host_evidence,
                           args.member_cluster, args.private_dir)
            print("exact member cluster deleted; host VZ and host Kind cluster were retained")
            return 0
        if not args.execute:
            plan = preflight(args.profile, args.host_cluster, args.host_evidence,
                             args.member_cluster, args.private_dir)
            print(f"read-only preflight passed; daemon reports {plan['cpuCount']} CPUs; this is not a capacity sufficiency claim")
            return 0
        result = setup_member(args.profile, args.host_cluster, args.host_evidence,
                              args.member_cluster, args.private_dir)
        print("Kata QEMU runtime smoke passed; this proves member guest-kernel isolation only")
        print(f"private evidence directory: {args.private_dir}")
        print("cleanup is an explicit separate command; no host or VM resources were removed")
        return 0
    except PreflightError as exc:
        _persist_failure("runtime-smoke", "validation-error", "", str(exc))
        print(f"runtime smoke stopped: {exc}", file=sys.stderr)
        if args.private_dir.exists():
            print(f"private evidence retained at: {args.private_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
