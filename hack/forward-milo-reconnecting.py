#!/usr/bin/env python3
"""Bounded loopback Milo service forward across API Pod restarts in an isolated Kind cluster."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import stat
import subprocess
import time


def private_file(path: Path) -> Path:
    path = path.expanduser().absolute()
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
        raise ValueError("kubeconfig must be a private regular file")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-kubeconfig", type=Path, required=True)
    parser.add_argument("--host-context", required=True)
    parser.add_argument("--local-port", type=int, required=True)
    parser.add_argument("--deadline-seconds", type=int, default=2400)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()
    if not 1024 <= args.local_port <= 65535:
        parser.error("local port must be between 1024 and 65535")
    if not 60 <= args.deadline_seconds <= 3600:
        parser.error("deadline must be between 60 and 3600 seconds")
    config = private_file(args.host_kubeconfig)
    log_path = args.log_file.expanduser().absolute()
    if stat.S_IMODE(log_path.parent.stat().st_mode) & 0o077:
        parser.error("log directory must be private")
    if log_path.exists() and (not log_path.is_file() or
                              stat.S_IMODE(log_path.stat().st_mode) & 0o077):
        parser.error("existing log file must be a private regular file")

    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    command = ["kubectl", "--kubeconfig", str(config), "--context", args.host_context,
               "--request-timeout=10s", "-n", "milo-system", "port-forward",
               "--address", "127.0.0.1", "service/milo-apiserver",
               f"{args.local_port}:6443"]
    deadline = time.monotonic() + args.deadline_seconds
    previous_umask = os.umask(0o077)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            while not stopping and time.monotonic() < deadline:
                child = subprocess.Popen(command, stdout=log, stderr=log)
                try:
                    while not stopping and time.monotonic() < deadline and child.poll() is None:
                        time.sleep(0.2)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=3)
                if not stopping:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
    finally:
        os.umask(previous_umask)
    if not stopping:
        raise SystemExit("Milo port-forward reconnect deadline expired")


if __name__ == "__main__":
    main()
