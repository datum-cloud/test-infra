#!/usr/bin/env python3
"""Check pinned Compute CRD discovery in the real, private Milo API.

This is a schema prerequisite only. It does not assert a reconciled Workload.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


SOURCE_SHA = "88a7101c2b91f7195c2bb5433c50f54c384a90ce"
REVISION = f"sha1:{SOURCE_SHA}"
CRDS = {
    "instanceconsolesessions.compute.datumapis.com": "instanceconsolesessions",
    "instances.compute.datumapis.com": "instances",
    "instancetypes.compute.datumapis.com": "instancetypes",
    "runtimeclasses.compute.datumapis.com": "runtimeclasses",
    "workloaddeployments.compute.datumapis.com": "workloaddeployments",
    "workloads.compute.datumapis.com": "workloads",
}
DEADLINE = time.monotonic() + 90


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def run(*argv: str) -> str:
    remaining = DEADLINE - time.monotonic()
    require(remaining > 0, "Compute API check exceeded its 90-second deadline")
    completed = subprocess.run(argv, text=True, capture_output=True, check=False,
                               timeout=min(15, remaining))
    if completed.returncode:
        raise RuntimeError(f"{argv[0]} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def k(config: Path, context: str, *args: str) -> str:
    return run("kubectl", "--kubeconfig", str(config), "--context", context,
               "--request-timeout=10s", *args)


def kjson(config: Path, context: str, *args: str) -> dict:
    return json.loads(k(config, context, *args, "-o", "json"))


def ready_condition(obj: dict) -> bool:
    generation = obj["metadata"]["generation"]
    return obj.get("status", {}).get("observedGeneration", 0) >= generation and any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        and condition.get("observedGeneration", generation) >= generation
        for condition in obj.get("status", {}).get("conditions", []))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", required=True, type=Path)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--milo-kubeconfig", required=True, type=Path)
    parser.add_argument("--milo-context", required=True)
    args = parser.parse_args()

    require(args.host_kubeconfig != args.milo_kubeconfig,
            "host and Milo kubeconfig paths must differ")
    for path in (args.host_kubeconfig, args.milo_kubeconfig):
        require(path.is_absolute() and path.is_file(), f"absolute kubeconfig file required: {path}")
        require(path.stat().st_mode & 0o077 == 0, f"kubeconfig must be private: {path}")
    require(args.host_context.startswith("kind-apps-a1t-"),
            "host context must name an Apps A1-T Kind cluster")
    require(args.milo_context == "milo-root", "expected isolated Milo root context")
    for config, context in ((args.host_kubeconfig, args.host_context),
                            (args.milo_kubeconfig, args.milo_context)):
        server = k(config, context, "config", "view", "--minify", "-o",
                   "jsonpath={.clusters[0].cluster.server}")
        require(server.startswith("https://127.0.0.1:"),
                f"{context} must use a private localhost API endpoint")

    source = kjson(args.host_kubeconfig, args.host_context, "get", "gitrepository",
                   "compute-a1t", "-n", "flux-system")
    require(source["spec"]["ref"] == {"commit": SOURCE_SHA},
            "Compute Flux source is not pinned to the exact source commit")
    require(ready_condition(source), "Compute Flux GitRepository is not observed Ready")
    require(source["status"]["artifact"]["revision"] == REVISION,
            "Compute source artifact revision differs from the pinned commit")

    release = kjson(args.host_kubeconfig, args.host_context, "get", "kustomization",
                    "compute-a1t-milo-api-crds", "-n", "milo-system")
    require(ready_condition(release), "Compute Milo CRD Kustomization is not observed Ready")
    require(release["status"].get("lastAppliedRevision") == REVISION,
            "Compute CRD Kustomization did not apply the pinned source revision")
    inventory = {entry["id"] for entry in release["status"].get("inventory", {}).get("entries", [])}
    expected_inventory = {
        f"_{name}_apiextensions.k8s.io_CustomResourceDefinition" for name in CRDS
    }
    require(inventory == expected_inventory,
            "Compute Flux inventory differs from the exact six source CRDs")

    for name in sorted(CRDS):
        crd = kjson(args.milo_kubeconfig, args.milo_context, "get", "crd", name)
        require(crd["spec"]["group"] == "compute.datumapis.com",
                f"unexpected API group on {name}")
        require(any(version.get("name") == "v1alpha" and version.get("served")
                    for version in crd["spec"]["versions"]),
                f"{name} does not serve v1alpha")
        require(any(condition.get("type") == "Established" and condition.get("status") == "True"
                    for condition in crd.get("status", {}).get("conditions", [])),
                f"{name} CRD is not Established")

    resources = json.loads(k(args.milo_kubeconfig, args.milo_context, "get", "--raw",
                             "/apis/compute.datumapis.com/v1alpha"))
    require(resources.get("kind") == "APIResourceList", "Compute API discovery was not returned")
    served = {resource["name"] for resource in resources.get("resources", [])}
    require(set(CRDS.values()) <= served, "Milo discovery omitted one or more Compute resources")
    print(f"Compute API prerequisite passed: Flux and Milo serve {len(CRDS)} CRDs from {SOURCE_SHA}")
    print("No native Workload, deployment, route, or Apps A1-T readiness is asserted.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Compute API prerequisite failed: {error}", file=sys.stderr)
        sys.exit(1)
