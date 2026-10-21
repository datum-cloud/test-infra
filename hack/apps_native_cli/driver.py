"""Task-only exported datumctl Apps Command proof through real Milo Project APIs.

The Go entry point is built in a disposable clone of pinned datumctl and loads
an explicit scoped kubeconfig file. The normal datumctl root OAuth factory is
intentionally bypassed; this proves the exported Apps Command and HTTP API,
not production login/IAM behavior. No token is accepted on the command line.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Callable


CLI_SHA = "91da75c3123282aac7ba276f120ebf93d2a80177"
PROJECT_PATH = "/apis/resourcemanager.miloapis.com/v1alpha1/projects/"
MAX_OUTPUT = 1 << 16
Snapshot = Callable[[], dict]


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def command(*args: str, cwd: Path | None = None, timeout: float = 60,
            env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=timeout, check=False)
    require(result.returncode == 0,
            f"{args[0]} failed during native Apps CLI preparation")
    require(len(result.stdout) <= MAX_OUTPUT,
            "native Apps CLI preparation output exceeded bound")
    return result.stdout.strip()


def without_ambient_credentials() -> dict[str, str]:
    environment = os.environ.copy()
    for name in list(environment):
        if name.endswith("_TOKEN") or name.endswith("_KEY") or name in (
                "KUBECONFIG", "GH_TOKEN", "GITHUB_TOKEN", "AWS_PROFILE"):
            environment.pop(name, None)
    return environment


def build_private_cli(source: Path, private: Path) -> Path:
    """No network, source edits, or long-lived binary; caller owns private dir."""
    require(source.is_absolute() and source.is_dir() and private.is_absolute() and
            private.is_dir() and stat.S_IMODE(private.stat().st_mode) & 0o077 == 0,
            "absolute clean datumctl source and mode-0700 private directory required")
    require(command("git", "-C", str(source), "rev-parse", "HEAD") == CLI_SHA and
            command("git", "-C", str(source), "status", "--porcelain",
                    "--untracked-files=all") == "",
            "datumctl source checkout differs from clean pinned commit")
    clone = private / "datumctl-source"
    require(not clone.exists(), "private datumctl clone collision")
    command("git", "clone", "--no-hardlinks", "--quiet", str(source), str(clone),
            timeout=120)
    command("git", "-C", str(clone), "checkout", "--quiet", "--detach", CLI_SHA)
    require(command("git", "-C", str(clone), "rev-parse", "HEAD") == CLI_SHA,
            "private datumctl clone changed revision")
    driver_dir = clone / "a1t-native-cli"
    driver_dir.mkdir(mode=0o700)
    shutil.copyfile(Path(__file__).with_name("main.go"), driver_dir / "main.go")
    binary = private / "datumctl-apps-native"
    environment = without_ambient_credentials()
    environment.update({"GOWORK": "off", "GOFLAGS": "-mod=readonly",
                        "GOPROXY": "off", "GOSUMDB": "off",
                        "GOTOOLCHAIN": "local", "GOCACHE": str(private / "go-cache")})
    command("go", "build", "-o", str(binary), "./a1t-native-cli",
            cwd=clone, timeout=300, env=environment)
    binary.chmod(0o700)
    require(binary.is_file(), "pinned exported Apps Command did not build")
    return binary


class AppsCLI:
    def __init__(self, binary: Path, scoped_kubeconfig: Path,
                 project: str, port: int):
        require(binary.is_absolute() and binary.is_file() and
                scoped_kubeconfig.is_absolute() and scoped_kubeconfig.is_file() and
                stat.S_IMODE(scoped_kubeconfig.stat().st_mode) & 0o077 == 0,
                "private Apps CLI binary and scoped kubeconfig files required")
        require(re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", project) is not None and
                1 <= port <= 65535, "exact Milo Project/loopback port required")
        self.binary, self.config, self.project, self.port = (
            binary, scoped_kubeconfig, project, port)

    def run(self, *args: str) -> str:
        argv = [str(self.binary), "--kubeconfig", str(self.config),
                "--project", self.project, "--port", str(self.port), *args]
        result = subprocess.run(argv, text=True, capture_output=True,
                                timeout=55, check=False,
                                env=without_ambient_credentials())
        require(result.returncode == 0,
                "exported datumctl Apps Command failed (details withheld)")
        require(len(result.stdout) <= MAX_OUTPUT,
                "exported Apps Command output exceeded bound")
        return result.stdout

    def run_json(self, *args: str) -> dict:
        value = json.loads(self.run(*args))
        require(isinstance(value, dict), "Apps Command returned non-object JSON")
        return value


def normalized(value):
    """Normalize only list ordering; retain every persisted object field."""
    if isinstance(value, list):
        result = [normalized(item) for item in value]
        if result and all(isinstance(item, dict) and
                          item.get("metadata", {}).get("name") for item in result):
            result.sort(key=lambda item: item["metadata"]["name"])
        return result
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    return copy.deepcopy(value)


def unchanged(snapshot: Snapshot, before: dict, label: str) -> None:
    require(normalized(snapshot()) == normalized(before),
            f"{label} changed persisted template or immutable revisions")


def chain(revisions: list[dict], uid: str) -> list[dict]:
    result = sorted((item for item in revisions if item["spec"].get("templateUID") == uid),
                    key=lambda item: item["spec"]["sequence"])
    require(all(item["metadata"].get("uid") and
                item["metadata"].get("name") and
                item["spec"].get("definitionJSON") and
                item["spec"].get("digest") and
                item["spec"].get("effectiveActorUID") and
                item["spec"].get("acceptedAt") for item in result),
            "native revision chain contains incomplete identity/definition")
    return result


def check_history_output(output: dict, uid: str, revisions: list[dict]) -> None:
    wanted = chain(revisions, uid)
    rows = output.get("revisions")
    require(output.get("templateUID") == uid and isinstance(rows, list) and
            [(row.get("name"), row.get("sequence"), row.get("digest"),
              row.get("actorUID"), row.get("acceptedAt"))
             for row in rows] ==
            [(item["metadata"]["name"], item["spec"]["sequence"],
              item["spec"]["digest"], item["spec"].get("effectiveActorUID"),
              item["spec"].get("acceptedAt")) for item in wanted],
            "datumctl history differs from persisted immutable revision chain")


def check_summary(value: dict, template: dict, action: str,
                  dry_run: bool) -> None:
    meta = template["metadata"]
    require(value.get("name") == meta["name"] and value.get("uid") == meta["uid"] and
            value.get("action") == action and value.get("dryRun") is dry_run and
            value.get("resourceVersion"),
            "datumctl write summary lost exact template identity or dry-run state")


def exercise_history_diff_restore(cli: AppsCLI, snapshot: Snapshot,
                                  wait_sequence: Callable[[int], object]) -> dict:
    """From accepted sequence 2, prove CLI read/dry-run and actual restore to 3."""
    before = snapshot()
    template = before["template"]
    name, uid = template["metadata"]["name"], template["metadata"]["uid"]
    require(template.get("status", {}).get("latestSequence") == 2,
            "CLI integration requires an accepted native template at sequence 2")
    revisions = chain(before["revisions"], uid)
    require([item["spec"]["sequence"] for item in revisions] == [1, 2],
            "CLI integration requires exactly two prior immutable revisions")
    selected = revisions[0]
    rev_name = selected["metadata"]["name"]
    history = cli.run_json("templates", "history", name)
    check_history_output(history, uid, revisions)
    unchanged(snapshot, before, "history")
    diff = cli.run("templates", "diff", name, "--revision", rev_name)
    changed_lines = [line for line in diff.splitlines()
                     if (line.startswith("-") and not line.startswith("---")) or
                     (line.startswith("+") and not line.startswith("+++"))]
    require("--- current" in diff and "+++ selected revision" in diff and
            any(line.startswith("-") for line in changed_lines) and
            any(line.startswith("+") for line in changed_lines) and
            selected["spec"]["definitionJSON"] != template["spec"]["definitionJSON"],
            "datumctl diff failed to compare distinct exact definitions")
    unchanged(snapshot, before, "diff")
    preview = cli.run_json("templates", "restore", name, "--revision", rev_name,
                           "--dry-run=server")
    check_summary(preview, template, "restored", True)
    unchanged(snapshot, before, "server dry-run restore")
    committed = cli.run_json("templates", "restore", name, "--revision", rev_name)
    check_summary(committed, template, "restored", False)
    wait_sequence(3)
    after = snapshot()
    restored = after["template"]
    next_chain = chain(after["revisions"], uid)
    require(restored["metadata"]["uid"] == uid and
            restored["status"]["latestSequence"] == 3 and
            [item["spec"]["sequence"] for item in next_chain] == [1, 2, 3] and
            restored["spec"] == {"definitionJSON": selected["spec"]["definitionJSON"],
                                 "digest": selected["spec"]["digest"]} and
            next_chain[-1]["spec"]["definitionJSON"] == selected["spec"]["definitionJSON"] and
            next_chain[-1]["spec"]["digest"] == selected["spec"]["digest"] and
            restored["status"]["latestRevision"] == next_chain[-1]["metadata"]["name"],
            "actual datumctl restore did not append exact earlier definition as sequence 3")
    check_history_output(cli.run_json("templates", "history", name), uid, after["revisions"])
    return {"template": restored, "revisions": after["revisions"],
            "checks": ["history", "diff", "server-dry-run-restore", "actual-restore"]}


def delete_with_cli(cli: AppsCLI, snapshot: Snapshot,
                    wait_deleted: Callable[[], object]) -> dict:
    """UID/RV conditional CLI delete; history remains queryable by prior UID."""
    before = snapshot()
    template = before["template"]
    name, uid, rv = (template["metadata"][key] for key in
                     ("name", "uid", "resourceVersion"))
    old_chain = chain(before["revisions"], uid)
    preview = cli.run_json("templates", "delete", name, "--uid", uid,
                           "--resource-version", rv, "--dry-run=server")
    check_summary(preview, template, "deleted", True)
    unchanged(snapshot, before, "server dry-run delete")
    committed = cli.run_json("templates", "delete", name, "--uid", uid,
                             "--resource-version", rv)
    check_summary(committed, template, "deleted", False)
    wait_deleted()
    after = snapshot()
    require(after["template"] is None and
            normalized(chain(after["revisions"], uid)) == normalized(old_chain),
            "actual CLI delete did not retain the exact immutable history")
    check_history_output(cli.run_json("templates", "history", "--uid", uid),
                         uid, after["revisions"])
    return {"template_uid": uid,
            "retained_revision_names": [item["metadata"]["name"] for item in old_chain],
            "checks": ["server-dry-run-delete", "uid-rv-delete", "history-after-delete"]}
