#!/usr/bin/env python3
"""Check a real, private Karmada hub and one DFW member for Apps A1-T.

This is a prerequisite check, not a native Workload or route test. It reads
only explicitly named kubeconfigs and never prints their credentials.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


SOURCE_SHA = "3619e71d4686c3d1ead01f08a1136f7d272b3344"
CHART_SHA256 = "773ab194cb92dad81d479d2199b968a2c6b0b8366306ba47961afd67616834fe"
KARMADA_DEPLOYMENTS = {
    "karmada-aggregated-apiserver",
    "karmada-apiserver",
    "karmada-controller-manager",
    "karmada-kube-controller-manager",
    "karmada-scheduler",
    "karmada-webhook",
}
DEADLINE = time.monotonic() + 180


def run(*argv: str) -> str:
    remaining = DEADLINE - time.monotonic()
    require(remaining > 0, "federation check exceeded its 180-second deadline")
    completed = subprocess.run(argv, text=True, capture_output=True,
                               timeout=min(25, remaining), check=False)
    if completed.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1]} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def kubectl(config: Path, *args: str) -> str:
    return run("kubectl", "--kubeconfig", str(config), "--request-timeout=10s", *args)


def kjson(config: Path, *args: str) -> dict:
    return json.loads(kubectl(config, *args, "-o", "json"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def private_file(path: Path) -> None:
    require(path.is_absolute() and path.is_file(), f"explicit absolute file required: {path}")
    require(path.stat().st_mode & 0o077 == 0, f"credential file must be private (0600): {path}")
    require(path.parent.stat().st_mode & 0o077 == 0, f"credential directory must be private (0700): {path.parent}")


def ready_node(config: Path, name: str) -> str:
    nodes = kjson(config, "get", "nodes")["items"]
    matching = [node for node in nodes if node["metadata"]["name"] == name + "-control-plane"]
    require(len(matching) == 1, f"expected the {name} Kind control-plane node")
    node = matching[0]
    require(any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in node.get("status", {}).get("conditions", [])), f"{name} node is not Ready")
    addresses = node.get("status", {}).get("addresses", [])
    internal = [item["address"] for item in addresses if item.get("type") == "InternalIP"]
    require(len(internal) == 1, f"{name} node has no unambiguous internal IP")
    return internal[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="checkout of pinned test-infra PR32")
    parser.add_argument("--chart", required=True, type=Path, help="downloaded Karmada chart archive")
    parser.add_argument("--hub-kubeconfig", required=True, type=Path)
    parser.add_argument("--member-kubeconfig", required=True, type=Path)
    parser.add_argument("--karmada-kubeconfig", required=True, type=Path)
    parser.add_argument("--hub-name", required=True)
    parser.add_argument("--member-name", required=True)
    parser.add_argument("--api-port", required=True, type=int)
    args = parser.parse_args()

    require(args.source.is_absolute() and args.source.is_dir(), "explicit absolute PR32 checkout required")
    require(run("git", "-C", str(args.source), "rev-parse", "HEAD") == SOURCE_SHA,
            "federation source HEAD differs from pinned PR32 commit")
    require(args.chart.is_absolute() and args.chart.is_file(), "explicit absolute chart archive required")
    require(hashlib.sha256(args.chart.read_bytes()).hexdigest() == CHART_SHA256,
            "Karmada chart archive digest differs from observed v1.16.0 archive")
    require(30000 <= args.api_port <= 32767, "Karmada port must be in the NodePort range")
    require(args.hub_name != args.member_name, "hub and member names must differ")
    configs = (args.hub_kubeconfig, args.member_kubeconfig, args.karmada_kubeconfig)
    require(len(set(configs)) == 3, "hub, member, and Karmada kubeconfig files must differ")
    for config in configs:
        private_file(config)

    for config, name in ((args.hub_kubeconfig, args.hub_name),
                         (args.member_kubeconfig, args.member_name)):
        context = kubectl(config, "config", "current-context")
        require(context == f"kind-{name}", f"unexpected context for {name}: {context}")
        server = kubectl(config, "config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}")
        require(server.startswith("https://127.0.0.1:"), f"{name} kubeconfig is not private localhost")
        require(kubectl(config, "get", "--raw", "/readyz") == "ok", f"{name} API is not ready")
        ready_node(config, name)

    karmada_server = kubectl(args.karmada_kubeconfig, "config", "view", "--minify", "-o",
                             "jsonpath={.clusters[0].cluster.server}")
    require(karmada_server == f"https://127.0.0.1:{args.api_port}",
            "Karmada kubeconfig server differs from the explicit private NodePort")
    require(kubectl(args.karmada_kubeconfig, "get", "--raw", "/readyz") == "ok",
            "Karmada API is not ready")
    member_ip = ready_node(args.member_kubeconfig, args.member_name)

    deployments = kjson(args.hub_kubeconfig, "get", "deployments", "-n", "karmada-system")["items"]
    by_name = {item["metadata"]["name"]: item for item in deployments}
    require(KARMADA_DEPLOYMENTS <= by_name.keys(), "Karmada control-plane deployment missing")
    for name in sorted(KARMADA_DEPLOYMENTS):
        deployment = by_name[name]
        status = deployment.get("status", {})
        replicas = deployment["spec"].get("replicas", 1)
        require(replicas > 0 and status.get("availableReplicas", 0) >= replicas
                and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"],
                f"Karmada deployment {name} is not observed Available")
    etcd = kjson(args.hub_kubeconfig, "get", "statefulset", "etcd", "-n", "karmada-system")
    require(etcd.get("status", {}).get("readyReplicas", 0) >= etcd["spec"].get("replicas", 1),
            "Karmada etcd is not Ready")
    releases = json.loads(run("helm", "list", "--namespace", "karmada-system",
                              "--kubeconfig", str(args.hub_kubeconfig), "--output", "json"))
    require(any(item["name"] == "karmada" and item["chart"] == "karmada-v1.16.0"
                and item["status"] == "deployed" for item in releases),
            "Karmada v1.16.0 Helm release is not deployed")

    cluster = kjson(args.karmada_kubeconfig, "get", "cluster", args.member_name)
    labels = cluster["metadata"].get("labels", {})
    for key, value in {"topology.datum.net/city-code": "dfw",
                       "topology.datum.net/location": "dfw",
                       "infra.datum.net/gateways": "enabled"}.items():
        require(labels.get(key) == value, f"member missing required {key}={value}")
    require(cluster["spec"].get("apiEndpoint") == f"https://{member_ip}:6443",
            "Karmada Cluster does not target the real member bridge endpoint")
    require(any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in cluster.get("status", {}).get("conditions", [])),
            "Karmada member is not Ready")
    print(f"Federation prerequisite passed: PR32 {SOURCE_SHA}, chart archive sha256:{CHART_SHA256}")
    print(f"Karmada v1.16.0: {len(KARMADA_DEPLOYMENTS)} deployments and etcd Ready; "
          f"{args.member_name} Ready at {member_ip}, canonical DFW labels present")
    print("This check does not assert native Compute/NSO/Apps Workload or route readiness.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(f"Federation prerequisite failed: {error}", file=sys.stderr)
        sys.exit(1)
