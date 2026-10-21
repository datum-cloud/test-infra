#!/usr/bin/env python3
"""Fail early when the isolated native CI runner cannot schedule the stack."""

import os
from pathlib import Path
import platform
from fractions import Fraction


MIN_CPUS = 4  # Trial floor, not a demonstrated sufficient capacity.


def cpu_quota(path: Path = Path("/sys/fs/cgroup/cpu.max")) -> Fraction | None:
    if not path.exists():
        return None
    fields = path.read_text(encoding="ascii").split()
    if len(fields) != 2 or not fields[1].isdigit() or int(fields[1]) <= 0:
        raise ValueError(f"invalid cgroup CPU quota at {path}")
    if fields[0] == "max":
        return None
    if not fields[0].isdigit():
        raise ValueError(f"invalid cgroup CPU quota at {path}")
    return Fraction(int(fields[0]), int(fields[1]))


def effective_cpus(affinity: int, quota: Fraction | None) -> Fraction:
    if affinity <= 0:
        raise ValueError("runner reports no usable CPUs")
    return min(Fraction(affinity), quota) if quota is not None else Fraction(affinity)


def check(system: str, machine: str, affinity: int, quota: Fraction | None) -> Fraction:
    if system != "Linux" or machine not in ("x86_64", "amd64"):
        raise ValueError(f"native CI requires Linux amd64; got {system} {machine}")
    available = effective_cpus(affinity, quota)
    if available < MIN_CPUS:
        raise ValueError(
            f"native CI needs at least {MIN_CPUS} effective vCPUs for a bounded trial; "
            f"runner has {float(available):g}. Configure Apps repository variable "
            "APPS_NATIVE_RUNNER with an existing Linux amd64 runner of adequate "
            "capacity. Four vCPUs is a trial floor, not proven sufficient."
        )
    return available


def main() -> None:
    system, machine = platform.system(), platform.machine()
    if system != "Linux":
        raise SystemExit(f"native CI requires Linux amd64; got {system} {machine}")
    affinity = len(os.sched_getaffinity(0))
    quota = cpu_quota()
    memtotal = next(
        (line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
         if line.startswith("MemTotal:")), "unknown"
    )
    available = effective_cpus(affinity, quota)
    print(f"runner: {system} {machine}; affinity CPUs: {affinity}; "
          f"quota CPUs: {str(quota) if quota is not None else 'unlimited'}; "
          f"effective CPUs: {float(available):g}; memory KiB: {memtotal}")
    try:
        check(system, machine, affinity, quota)
    except ValueError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
