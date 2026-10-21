#!/usr/bin/env python3
"""Build pinned clean Milo/Apps sources and run the local aggregation proof."""
import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

MILO_PIN = "ba4b06e8dde99dd70e9a2f34e011d635e4408a76"
APPS_PIN = "19b331f6812bd94c93d7b09d8754f2016353ae87"
MILO_LAUNCHER = pathlib.Path(__file__).with_name("apps-aggregation-launcher") / "main.go"
HARNESS = pathlib.Path(__file__).with_name("test-apps-authoring-aggregation.py")
MILO_ENTRY = pathlib.Path("cmd/milo/appsaggregationprobe/main.go")


def run(args, *, cwd=None, env=None, timeout=30, capture=True, check=True):
    return subprocess.run(
        args, cwd=cwd, env=env, check=check, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True if capture else False,
    )


def source_root(path):
    root = pathlib.Path(run(["git", "-C", str(path), "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if root != path.resolve():
        raise RuntimeError(f"source path must be the Git checkout root: {path}")
    return root


def check_source(path, expected):
    root = source_root(path)
    revision = run(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    if revision != expected:
        raise RuntimeError(f"{root} is at {revision}, expected pinned commit {expected}")
    status = run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"]).stdout
    if status:
        raise RuntimeError(f"pinned source checkout is not clean: {root}")
    return root


def verify_build_info(binary, revision, modified):
    info = run(["go", "version", "-m", str(binary)]).stdout
    required = (f"build\tvcs.revision={revision}", f"build\tvcs.modified={str(modified).lower()}")
    if any(item not in info for item in required):
        raise RuntimeError(f"built binary does not match pinned revision/modified state: {binary.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milo-source", required=True, type=pathlib.Path)
    parser.add_argument("--apps-source", required=True, type=pathlib.Path)
    parser.add_argument("--etcd-binary", required=True, type=pathlib.Path)
    args = parser.parse_args()
    if not args.etcd_binary.is_file() or not os.access(args.etcd_binary, os.X_OK):
        raise RuntimeError("--etcd-binary must name an executable etcd binary")
    milo = check_source(args.milo_source, MILO_PIN)
    apps = check_source(args.apps_source, APPS_PIN)
    launcher = MILO_LAUNCHER.read_bytes()
    tracked = run(["git", "-C", str(milo), "ls-files", "--error-unmatch", str(MILO_ENTRY)], check=False)
    if tracked.returncode == 0:
        raise RuntimeError("the pinned Milo checkout unexpectedly tracks the probe launcher path")

    # The only permitted Milo source-tree change is the harness launcher, and
    # these exact bytes are part of this test-infra revision.
    entry = milo / MILO_ENTRY
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(launcher)
    try:
        status = run(["git", "-C", str(milo), "status", "--porcelain=v1", "--untracked-files=all"]).stdout.splitlines()
        if status != [f"?? {MILO_ENTRY.as_posix()}"] or entry.read_bytes() != launcher:
            raise RuntimeError("Milo source changed beyond the exact checked-in launcher addition")

        with tempfile.TemporaryDirectory(prefix="apps-authoring-build-") as temp:
            out = pathlib.Path(temp)
            milo_bin, apps_bin = out / "milo-apiserver", out / "apps-apiserver"
            common_env = os.environ.copy()
            common_env["GOTOOLCHAIN"] = "local"
            milo_env = common_env.copy()
            milo_env["GOCACHE"] = str(out / "milo-cache")
            milo_env["GOMODCACHE"] = str(out / "milo-modcache")
            apps_env = common_env.copy()
            apps_env["GOCACHE"] = str(out / "apps-cache")
            apps_env["GOMODCACHE"] = milo_env["GOMODCACHE"]
            print(f"Milo source: {MILO_PIN}; only untracked addition is {MILO_ENTRY}", flush=True)
            print(f"Apps source: {APPS_PIN}; checkout clean", flush=True)
            print("Building pinned Milo launcher and Apps apiserver (temporary caches)...", flush=True)
            run(["go", "build", "-buildvcs=true", "-o", str(milo_bin), "./cmd/milo/appsaggregationprobe"], cwd=milo, env=milo_env, timeout=600, capture=False)
            run(["go", "build", "-buildvcs=true", "-o", str(apps_bin), "./cmd/apps-apiserver"], cwd=apps, env=apps_env, timeout=600, capture=False)
            verify_build_info(milo_bin, MILO_PIN, True)
            verify_build_info(apps_bin, APPS_PIN, False)
            run([
                sys.executable, str(HARNESS),
                "--milo-binary", str(milo_bin), "--apps-binary", str(apps_bin),
                "--etcd-binary", str(args.etcd_binary.resolve()),
                "--milo-source-sha", MILO_PIN, "--apps-source-sha", APPS_PIN,
            ], timeout=150, capture=False)
    finally:
        # Restore the initially clean source tree, including when a build or
        # integration check fails, so a fresh run starts from the same inputs.
        if entry.is_file() and entry.read_bytes() == launcher:
            entry.unlink()
            entry.parent.rmdir()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
