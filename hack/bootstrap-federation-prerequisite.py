#!/usr/bin/env python3
"""Create a private one-member PR32 federation with pre-install content pins.

Only fresh, uniquely named Apps A1-T Kind clusters are accepted. On failure,
the task-owned clusters and private artifacts remain for diagnosis.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "3619e71d4686c3d1ead01f08a1136f7d272b3344"
CHART_SHA256 = "773ab194cb92dad81d479d2199b968a2c6b0b8366306ba47961afd67616834fe"
NODE_IMAGE = "kindest/node@sha256:c48c62eac5da28cdadcf560d1d8616cfa6783b58f0d94cf63ad1bf49600cb027"
IMAGE_LOCK = set(json.loads((ROOT / "components/federation/image-lock.json").read_text()))
VALUES = ROOT / "components/federation/karmada-values.yaml"
DEADLINE = time.monotonic() + 1800


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(argv: list[str], env: dict[str, str], *, cwd: Path | None = None,
        timeout: int = 60, capture: bool = True) -> str:
    remaining = DEADLINE - time.monotonic()
    require(remaining > 0, "federation bootstrap exceeded its 30-minute deadline")
    completed = subprocess.run(argv, env=env, cwd=cwd, text=True,
                               capture_output=capture, timeout=min(timeout, remaining), check=False)
    if completed.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} stage exited {completed.returncode}; "
                           "task-owned clusters/artifacts were retained for diagnosis")
    return completed.stdout.strip() if capture else ""


def render_kind_config(source: Path, dest: Path, port: int) -> None:
    rendered = source.read_text().replace("${KARMADA_API_NODEPORT}", str(port))
    require("${" not in rendered, f"unresolved Kind config variable in {source}")
    dest.write_text(rendered)
    dest.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="private checkout of pinned test-infra PR32")
    parser.add_argument("--docker-host", required=True, help="explicit dedicated Apps A1-T Docker socket")
    parser.add_argument("--hub-name", required=True)
    parser.add_argument("--member-name", required=True)
    parser.add_argument("--api-port", required=True, type=int)
    parser.add_argument("--artifacts", required=True, type=Path, help="new private directory for chart and rendered configs")
    parser.add_argument("--kubeconfigs", required=True, type=Path, help="new private directory for credential files")
    args = parser.parse_args()

    # The task must never create a 0644 Karmada admin kubeconfig, even briefly.
    os.umask(0o077)
    require(args.source.is_absolute() and args.source.is_dir(), "absolute pinned source checkout required")
    require(args.artifacts.is_absolute() and not args.artifacts.exists(), "new absolute artifact directory required")
    require(args.kubeconfigs.is_absolute() and not args.kubeconfigs.exists(), "new absolute kubeconfig directory required")
    require(args.artifacts != args.kubeconfigs, "artifact and kubeconfig directories must differ")
    require(args.hub_name != args.member_name, "hub and member must differ")
    require(all(re.fullmatch(r"apps-a1t-[a-z0-9-]+", n) for n in (args.hub_name, args.member_name)),
            "only task-owned apps-a1t-* Kind cluster names are accepted")
    require(30000 <= args.api_port <= 32767, "Karmada NodePort must be 30000..32767")
    require(args.docker_host.startswith("unix://"), "explicit Unix Docker socket required")
    socket_path = Path(args.docker_host.removeprefix("unix://"))
    require(len(socket_path.parts) >= 2
            and re.fullmatch(r"apps-a1t-[a-z0-9-]+", socket_path.parts[-2]) is not None
            and socket_path.is_socket(),
            "Docker socket must belong to an existing dedicated Apps A1-T profile")
    env = dict(os.environ)
    env.pop("DOCKER_CONTEXT", None)
    env["DOCKER_HOST"] = args.docker_host
    env["KUBECONFIG"] = str(args.kubeconfigs / "unused-global-kubeconfig")

    require(run(["git", "-C", str(args.source), "rev-parse", "HEAD"], env) == SOURCE_SHA,
            "federation source HEAD differs from pinned PR32 commit")
    require(not run(["git", "-C", str(args.source), "status", "--porcelain",
                     "--untracked-files=all"], env), "pinned PR32 source checkout has local changes")
    clusters = set(run(["kind", "get", "clusters"], env).splitlines())
    require(args.hub_name not in clusters and args.member_name not in clusters,
            "task-owned hub/member name already exists; refusing to adopt a cluster")
    with socket.socket() as port_probe:
        try:
            port_probe.bind(("127.0.0.1", args.api_port))
        except OSError as error:
            raise RuntimeError(f"Karmada host port {args.api_port} is occupied") from error

    args.artifacts.mkdir(mode=0o700)
    args.kubeconfigs.mkdir(mode=0o700)
    try:
        # Fetch from PR32's chart repository, but verify bytes before Helm is
        # allowed to install anything. Helm uses only this verified local file.
        run(["helm", "pull", "karmada", "--repo",
             "https://raw.githubusercontent.com/karmada-io/karmada/master/charts",
             "--version", "v1.16.0", "--destination", str(args.artifacts)], env, timeout=90)
        archives = list(args.artifacts.glob("*.tgz"))
        require(len(archives) == 1, "expected exactly one downloaded Karmada chart archive")
        chart = archives[0]
        require(hashlib.sha256(chart.read_bytes()).hexdigest() == CHART_SHA256,
                "downloaded Karmada chart bytes differ from pinned SHA-256; no cluster was created")
        chart.chmod(0o600)

        rendered = run(["helm", "template", "karmada", str(chart), "--namespace", "karmada-system",
                        "--include-crds", "-f", str(VALUES), "--set", f"apiServer.nodePort={args.api_port}"],
                       env, timeout=60)
        images = set(re.findall(r"^\s*image:\s*([^\s#]+)", rendered, re.MULTILINE))
        require(images == IMAGE_LOCK,
                f"rendered Karmada image lock mismatch: missing={sorted(IMAGE_LOCK - images)}, "
                f"unexpected={sorted(images - IMAGE_LOCK)}")
        print(f"Pre-install pin passed: chart sha256:{CHART_SHA256}, {len(images)} exact OCI image digests",
              flush=True)

        run(["docker", "pull", NODE_IMAGE], env, timeout=300, capture=False)
        node_id = run(["docker", "image", "inspect", NODE_IMAGE, "--format", "{{.Id}}"], env)
        require(node_id == NODE_IMAGE.split("@", 1)[1], "Kind node image content differs from pinned digest")
        hub_config = args.artifacts / "kind-hub.yaml"
        member_config = args.artifacts / "kind-member.yaml"
        render_kind_config(args.source / "cluster/federation/kind-hub.yaml", hub_config, args.api_port)
        render_kind_config(args.source / "cluster/federation/kind-member.yaml", member_config, args.api_port)
        hub_kubeconfig = args.kubeconfigs / f"{args.hub_name}.yaml"
        member_kubeconfig = args.kubeconfigs / f"{args.member_name}.yaml"
        for name, config, kubeconfig in ((args.hub_name, hub_config, hub_kubeconfig),
                                         (args.member_name, member_config, member_kubeconfig)):
            print(f"Creating task-owned Kind cluster {name}", flush=True)
            run(["kind", "create", "cluster", "--name", name, "--image", NODE_IMAGE,
                 "--config", str(config), "--kubeconfig", str(kubeconfig), "--retain"],
                env, timeout=300, capture=False)

        # PR32's Taskfile sees the two existing clusters and the already
        # installed Karmada deployment, then performs its tuning and join.
        # Its fallback Helm install path cannot run after this successful gate.
        require(hashlib.sha256(chart.read_bytes()).hexdigest() == CHART_SHA256,
                "Karmada chart archive changed before Helm installation")
        run(["helm", "install", "karmada", str(chart), "--namespace", "karmada-system",
             "--create-namespace", "--kubeconfig", str(hub_kubeconfig), "-f", str(VALUES),
             "--set", f"apiServer.nodePort={args.api_port}", "--wait", "--timeout", "10m"],
            env, timeout=720, capture=False)
        print("Pinned Karmada Helm release deployed; joining the DFW member through PR32", flush=True)
        run(["task", "federation-up", f"FEDERATION_HUB_CLUSTER={args.hub_name}",
             f"FEDERATION_MEMBERS={args.member_name}=dfw",
             "FEDERATION_MEMBER_LABEL_KEY=topology.datum.net/city-code",
             f"KARMADA_API_NODEPORT={args.api_port}",
             f"FEDERATION_KUBECONFIG_DIR={args.kubeconfigs}",
             f"LOCALBIN={args.artifacts / 'bin'}", "KARMADA_VERSION=v1.16.0",
             "KARMADACTL_VERSION=v1.16.0", "KARMADA_WAIT_TIMEOUT=10m", "WAIT_TIMEOUT=300s"],
            env, cwd=args.source, timeout=720, capture=False)

        karmada_kubeconfig = args.kubeconfigs / "karmada.yaml"
        run(["kubectl", "--kubeconfig", str(karmada_kubeconfig), "--request-timeout=10s",
             "label", "cluster", args.member_name, "topology.datum.net/location=dfw",
             "infra.datum.net/gateways=enabled", "--overwrite"], env, timeout=25)
        for kubeconfig in args.kubeconfigs.glob("*.yaml"):
            kubeconfig.chmod(0o600)
        run([sys.executable, str(ROOT / "hack/check-federation-prerequisite.py"),
             "--source", str(args.source), "--chart", str(chart),
             "--hub-kubeconfig", str(hub_kubeconfig),
             "--member-kubeconfig", str(member_kubeconfig),
             "--karmada-kubeconfig", str(karmada_kubeconfig),
             "--hub-name", args.hub_name, "--member-name", args.member_name,
             "--api-port", str(args.api_port)], env, timeout=180, capture=False)
    finally:
        # Tighten even an incomplete PR32 output before reporting a failure.
        args.kubeconfigs.chmod(0o700)
        for kubeconfig in args.kubeconfigs.glob("*.yaml"):
            kubeconfig.chmod(0o600)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(f"Federation bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
