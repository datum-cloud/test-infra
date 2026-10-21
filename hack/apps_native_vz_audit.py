#!/usr/bin/env python3
"""Create and validate a fresh private audit-policy directory in one VZ VM."""

import re
import subprocess
import sys
from typing import Callable


PROFILE_RE = re.compile(r"^apps-native-vz-[a-z0-9-]+$")
CLUSTER_RE = re.compile(r"^apps-a1t-ci-vz-[a-z0-9-]+$")


def create_private_audit_directory(profile: str, cluster: str,
                                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    if not PROFILE_RE.fullmatch(profile) or not CLUSTER_RE.fullmatch(cluster):
        raise ValueError("dedicated VZ profile and cluster names are required")
    template = f"/tmp/{cluster}-audit.XXXXXXXX"
    result = runner(
        ["colima", "ssh", "--profile", profile, "--", "sh", "-c",
         f"umask 077; mktemp -d {template}"],
        text=True, capture_output=True, check=False, timeout=30)
    if result.returncode != 0:
        raise ValueError("could not create private audit directory in the selected VZ profile")
    path = result.stdout.strip()
    if not re.fullmatch(rf"/tmp/{re.escape(cluster)}-audit\.[A-Za-z0-9]{{8}}", path):
        raise ValueError("selected VZ profile returned an unexpected audit directory")
    return path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: apps_native_vz_audit.py PROFILE CLUSTER", file=sys.stderr)
        return 2
    try:
        print(create_private_audit_directory(sys.argv[1], sys.argv[2]))
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"VZ audit directory setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
