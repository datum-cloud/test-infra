from __future__ import annotations

import json
import base64
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_apps_native_kata_runtime_smoke as smoke
import check_apps_native_kata_runtime_smoke as checker


HOST = "apps-a1t-ci-vz-test"
MEMBER = "apps-a1t-kata-vz-test"
PROFILE = "apps-native-vz-test"
NODE_ID = "a" * 64
NODE_UID = "b" * 36
LOCK = smoke.load_lock()


def write_host_evidence(root: Path, *, profile: str = PROFILE, cluster: str = HOST,
                        legacy: bool = False) -> Path:
    evidence = root / "host-evidence"
    evidence.mkdir(mode=0o700)
    intent = f"{profile}\n{cluster}\n" if legacy else json.dumps({
        "profile": profile, "cluster": cluster, "sourceRevision": "c" * 40,
    })
    (evidence / "cluster-create-intent").write_text(intent)
    (evidence / "cluster-node-id").write_text(NODE_ID + "\n")
    (evidence / "host.kubeconfig").write_text(json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": f"kind-{cluster}",
        "clusters": [{"name": f"kind-{cluster}", "cluster": {
            "server": "https://127.0.0.1:6443",
            "certificate-authority-data": base64.b64encode(b"fixture-ca").decode("ascii")}}],
        "contexts": [{"name": f"kind-{cluster}", "context": {"cluster": f"kind-{cluster}",
                      "user": "fixture"}}],
        "users": [{"name": "fixture", "user": {"token": "test-only"}}],
    }))
    for path in evidence.iterdir():
        path.chmod(0o600)
    return evidence


class SmokeContractTests(unittest.TestCase):
    def test_source_component_is_member_local_and_digest_pinned(self) -> None:
        smoke.verify_source_manifest_contract()
        lock = smoke.load_lock()
        self.assertEqual(lock["smokeImage"].split("@", 1)[1],
                         "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662")
        self.assertEqual(lock["smokeImageArm64Manifest"],
                         "sha256:bd44eb136a95dcc8dc58995e43abc40a413f2e8e3d4a2aae6bccbe94686acb05")

    def test_source_contract_rejects_remote_kubeconfig_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "image-lock.json").write_bytes((smoke.COMPONENT / "image-lock.json").read_bytes())
            (root / "kata-chart-source.yaml").write_bytes((smoke.COMPONENT / "kata-chart-source.yaml").read_bytes())
            (root / "kind-kata-config.yaml").write_bytes((smoke.COMPONENT / "kind-kata-config.yaml").read_bytes())
            release = (smoke.COMPONENT / "kata-runtime.yaml").read_text()
            (root / "kata-runtime.yaml").write_text(release + "\n  kubeConfig: {secretRef: {name: remote}}\n")
            with mock.patch.object(smoke, "COMPONENT", root):
                with self.assertRaises(smoke.PreflightError):
                    smoke.verify_source_manifest_contract()

    def test_source_contract_rejects_writable_modules_mount_or_broader_sysctls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("image-lock.json", "kata-chart-source.yaml", "kata-runtime.yaml", "kind-kata-config.yaml"):
                (root / name).write_bytes((smoke.COMPONENT / name).read_bytes())
            config = (root / "kind-kata-config.yaml").read_text()
            (root / "kind-kata-config.yaml").write_text(config.replace("readOnly: true", "readOnly: false"))
            with mock.patch.object(smoke, "COMPONENT", root):
                with self.assertRaisesRegex(smoke.PreflightError, "read-only kernel modules"):
                    smoke.verify_source_manifest_contract()

    def test_docker_endpoint_is_process_local_and_ignores_context_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            socket_path = home / ".colima" / PROFILE / "docker.sock"
            socket_path.parent.mkdir(parents=True)
            with mock.patch.object(smoke.Path, "home", return_value=home), \
                 mock.patch.object(smoke.Path, "is_socket", return_value=True):
                with mock.patch.dict(os.environ, {"DOCKER_CONTEXT": "foreign", "DOCKER_HOST": "tcp://foreign"}):
                    actual = smoke.docker_env(PROFILE)
                    self.assertEqual(actual["DOCKER_HOST"], "unix://" + str(socket_path))
                    self.assertNotIn("DOCKER_CONTEXT", actual)
                    self.assertEqual(os.environ["DOCKER_CONTEXT"], "foreign")

    def test_member_docker_exec_requires_immutable_container_id(self) -> None:
        container_id = "e" * 64
        self.assertEqual(smoke.docker_member_exec_args(container_id, "sh", "-ec", "true"),
                         ["docker", "exec", container_id, "sh", "-ec", "true"])
        with self.assertRaisesRegex(smoke.PreflightError, "immutable container ID"):
            smoke.docker_member_exec_args(MEMBER + "-control-plane", "sh", "-ec", "true")

    def test_vm_inotify_capacity_is_read_from_explicit_profile(self) -> None:
        with mock.patch.object(smoke, "run", return_value="1024") as run:
            self.assertEqual(smoke.read_inotify_capacity(PROFILE, {}), 1024)
            run.assert_called_once_with(["colima", "ssh", "--profile", PROFILE, "--", "cat",
                                         "/proc/sys/fs/inotify/max_user_instances"], {})
        for value in ("128", "1023", "", "-1", "unknown", "1024\nother"):
            with self.subTest(value=value), mock.patch.object(smoke, "run", return_value=value):
                with self.assertRaisesRegex(smoke.PreflightError, "max_user_instances >= 1024"):
                    smoke.read_inotify_capacity(PROFILE, {})
        with mock.patch.object(smoke, "run", return_value="8192"):
            self.assertEqual(smoke.read_inotify_capacity(PROFILE, {}), 8192)

    def test_member_modules_mount_uses_resolved_path_and_requires_ro(self) -> None:
        # Ubuntu's /lib alias resolves to /usr/lib, the actual Kind mountpoint
        # observed in the dedicated VZ member on 2026-10-21.
        ro = ("2820 2778 253:1 /usr/lib/modules /usr/lib/modules ro,relatime "
              "shared:455 - ext4 /dev/root rw,discard")
        smoke.verify_readonly_modules_mount(ro, "/usr/lib/modules")
        with self.assertRaisesRegex(smoke.PreflightError, "not mounted read-only"):
            smoke.verify_readonly_modules_mount(ro, "/lib/modules")
        with self.assertRaisesRegex(smoke.PreflightError, "not mounted read-only"):
            smoke.verify_readonly_modules_mount(ro.replace(" ro,relatime", " rw,relatime"),
                                                "/usr/lib/modules")
        with self.assertRaisesRegex(smoke.PreflightError, "not mounted read-only"):
            smoke.verify_readonly_modules_mount(ro + "\n" + ro, "/usr/lib/modules")
        with self.assertRaisesRegex(smoke.PreflightError, "observation is invalid"):
            smoke.verify_readonly_modules_mount(ro, "../usr/lib/modules")

    def test_low_inotify_blocks_creation_but_not_cleanup_preflight(self) -> None:
        for cleanup in (False, True):
            with self.subTest(cleanup=cleanup), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                evidence = write_host_evidence(root)
                output = root / "new-smoke-private"
                seen = []

                def fake_run(argv, env, **kwargs):
                    seen.append(argv)
                    if argv[:2] == ["docker", "info"]:
                        return "aarch64" if argv[-1] == "{{.Architecture}}" else "6"
                    if argv[:2] == ["colima", "ssh"]:
                        return "128" if argv[-1].endswith("max_user_instances") else ""
                    if argv == ["kind", "get", "clusters"]:
                        return HOST + ("\n" + MEMBER if cleanup else "")
                    if argv[:3] == ["kind", "get", "nodes"]:
                        return argv[-1] + "-control-plane"
                    if argv[:2] == ["docker", "inspect"]:
                        return NODE_ID
                    if argv[-2:] == ["config", "current-context"]:
                        return "kind-" + HOST
                    raise AssertionError(f"unexpected preflight command: {argv}")

                with mock.patch.object(smoke, "docker_env", return_value={}), \
                     mock.patch.object(smoke, "get_node_uid", return_value=(NODE_UID, "kernel", {})), \
                     mock.patch.object(smoke, "run", side_effect=fake_run):
                    if cleanup:
                        result = smoke.preflight(PROFILE, HOST, evidence, MEMBER, output, cleanup=True)
                        self.assertTrue(result["memberPresent"])
                        self.assertIsNone(result["inotifyMaxUserInstances"])
                    else:
                        with self.assertRaisesRegex(smoke.PreflightError, "max_user_instances >= 1024"):
                            smoke.preflight(PROFILE, HOST, evidence, MEMBER, output)
                self.assertFalse(output.exists())
                self.assertFalse(any(argv[:3] == ["kind", "create", "cluster"] for argv in seen))
                self.assertEqual(any(argv[-1].endswith("max_user_instances") for argv in seen), not cleanup)

    def test_host_evidence_requires_matching_profile_cluster_and_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_host_evidence(Path(tmp))
            node_id, kubeconfig, api_server, ca_hash = smoke.read_host_evidence(evidence, PROFILE, HOST)
            self.assertEqual(node_id, NODE_ID)
            self.assertEqual(kubeconfig.name, "host.kubeconfig")
            self.assertTrue(api_server.startswith("https://"))
            self.assertEqual(len(ca_hash), 64)
            with self.assertRaises(smoke.PreflightError):
                smoke.read_host_evidence(evidence, PROFILE, "apps-a1t-ci-vz-other")
            (evidence / "cluster-node-id").chmod(0o644)
            with self.assertRaises(smoke.PreflightError):
                smoke.read_host_evidence(evidence, PROFILE, HOST)

    def test_host_api_kubeconfig_rejects_tls_bypass_proxy_and_nonloopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_host_evidence(Path(tmp))
            kubeconfig = evidence / "host.kubeconfig"
            baseline = json.loads(kubeconfig.read_text())
            controls = (
                ({"insecure-skip-tls-verify": True}, "TLS verification"),
                ({"insecure-skip-tls-verify": False}, "TLS verification"),
                ({"proxy-url": "http://127.0.0.1:3128"}, "proxy"),
                ({"server": "https://example.com:6443"}, "malformed|loopback HTTPS"),
                ({"server": "http://127.0.0.1:6443"}, "loopback HTTPS"),
                ({"server": "https://127.0.0.1:6443/path"}, "loopback HTTPS"),
            )
            for patch, message in controls:
                with self.subTest(patch=patch):
                    current = copy.deepcopy(baseline)
                    current["clusters"][0]["cluster"].update(patch)
                    kubeconfig.write_text(json.dumps(current))
                    with self.assertRaisesRegex(smoke.PreflightError, message):
                        smoke.read_host_evidence(evidence, PROFILE, HOST)

    def test_legacy_two_line_host_intent_and_json_intent_are_read_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_host_evidence(Path(tmp), legacy=True)
            intent = evidence / "cluster-create-intent"
            original = intent.read_bytes()
            self.assertEqual(smoke.read_host_evidence(evidence, PROFILE, HOST)[0], NODE_ID)
            self.assertEqual(intent.read_bytes(), original)
            intent.write_text(f"{PROFILE}\n{HOST}\nextra\n")
            with self.assertRaises(smoke.PreflightError):
                smoke.read_host_evidence(evidence, PROFILE, HOST)

    def test_flux_component_uses_both_locked_flux_crd_schemas_without_skips(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        component_root = source_root / "components/apps-native-kata-runtime-smoke"
        docs = [yaml.safe_load((component_root / name).read_text()) for name in
                ("kata-chart-source.yaml", "kata-runtime.yaml")]
        rendered = yaml.safe_dump_all(docs, sort_keys=False)
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            copied_component = temp_root / "components/apps-native-kata-runtime-smoke"
            copied_component.mkdir(parents=True)
            copied_components = temp_root / "components"
            (copied_components / "flux").mkdir()
            (copied_components / "flux/kustomization.yaml").write_bytes(
                (source_root / "components/flux/kustomization.yaml").read_bytes())
            schema_dir = copied_component / "schemas"
            schema_dir.mkdir()
            for file in (component_root / "schemas").iterdir():
                (schema_dir / file.name).write_bytes(file.read_bytes())
            with mock.patch.object(checker, "COMPONENT", copied_component), \
                 mock.patch.object(checker, "checked_command", return_value=
                                   "Summary: 2 resources found\nValid: 2\nInvalid: 0\nErrors: 0\nSkipped: 0") as validate:
                checker.validate_flux_crs(rendered, docs)
                self.assertIn("-strict", validate.call_args.args[0])
            crd = schema_dir / "helmreleases.helm.toolkit.fluxcd.io.yaml"
            crd_object = yaml.safe_load(crd.read_text())
            crd_object["metadata"]["name"] = "mutated.invalid"
            crd.write_text(yaml.safe_dump(crd_object, sort_keys=False))
            with mock.patch.object(checker, "COMPONENT", copied_component), \
                 mock.patch.object(checker, "checked_command") as validate:
                with self.assertRaisesRegex(smoke.PreflightError, "source differs from lock"):
                    checker.validate_flux_crs(rendered, docs)
                validate.assert_not_called()

    def test_failure_diagnostics_are_private_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            previous = smoke._FAILURE_DIR
            previous_stage = smoke._FAILURE_STAGE
            try:
                smoke._FAILURE_DIR = directory
                smoke._FAILURE_STAGE = "runtime-install"
                smoke._persist_failure("kubectl", "exit-1", "authorization: Bearer abc123",
                                       "token: supersecret " + "x" * 9000)
                files = list(directory.glob("failure-*.json"))
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
                record = json.loads(files[0].read_text())
                self.assertEqual(record["stage"], "runtime-install")
                self.assertEqual(record["classification"], "exit-1")
                self.assertNotIn("abc123", record["stdout"])
                self.assertNotIn("supersecret", record["stderr"])
                self.assertLessEqual(len(record["stderr"]), 8200)
            finally:
                smoke._FAILURE_DIR = previous
                smoke._FAILURE_STAGE = previous_stage

    def test_command_failure_writes_stdout_stderr_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            previous = smoke._FAILURE_DIR
            previous_stage = smoke._FAILURE_STAGE
            try:
                smoke._FAILURE_DIR = directory
                smoke._FAILURE_STAGE = "create-pod"
                failed = __import__("subprocess").CompletedProcess(
                    ["kubectl"], 1, stdout="response body", stderr="forbidden")
                with mock.patch.object(smoke.subprocess, "run", return_value=failed):
                    with self.assertRaisesRegex(smoke.PreflightError, "diagnostics were retained"):
                        smoke.run(["kubectl", "create", "-f", "-"], {}, stdin="not persisted")
                record = json.loads(next(directory.glob("failure-*.json")).read_text())
                self.assertEqual(record["stage"], "create-pod")
                self.assertEqual(record["stdout"], "response body")
                self.assertEqual(record["stderr"], "forbidden")
            finally:
                smoke._FAILURE_DIR = previous
                smoke._FAILURE_STAGE = previous_stage

    def test_current_flux_generations_and_daemonset_image_ids_are_required(self) -> None:
        chart_digest = LOCK["kataChart"].rsplit("@", 1)[1]
        source = {"metadata": {"generation": 2}, "status": {
            "observedGeneration": 2, "artifact": {
                "revision": chart_digest, "digest": "sha256:" + "a" * 64},
            "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 2}]},
            "spec": {"url": "oci://quay.io/kata-containers/kata-deploy-charts/kata-deploy",
                     "ref": {"digest": chart_digest}}}
        release = {"metadata": {"generation": 4}, "status": {
            "observedGeneration": 4, "lastAttemptedGeneration": 4,
            "lastAttemptedRevision": LOCK["kataChartVersion"] + "+" + chart_digest.split(":", 1)[1][:12],
            "lastAttemptedRevisionDigest": chart_digest,
            "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 4}]}}
        daemonset = {"metadata": {"uid": "ds-uid", "generation": 3},
                     "spec": {"selector": {"matchLabels": {"app": "kata-deploy"}}},
                     "status": {"observedGeneration": 3, "numberReady": 1}}
        installer = LOCK["kataInstallerImage"]
        pod = {"metadata": {"uid": "pod-uid", "ownerReferences": [
                    {"kind": "DaemonSet", "uid": "ds-uid"}]},
               "spec": {"containers": [{"name": "installer", "image": installer},
                                          {"name": "helper", "image": LOCK["kataKubectlImage"]}]},
               "status": {"phase": "Running", "containerStatuses": [
                   {"name": "helper", "ready": True,
                    "imageID": "containerd://" + LOCK["kataKubectlArm64Manifest"]},
                   {"name": "installer", "ready": True,
                    "imageID": "containerd://" + LOCK["kataInstallerArm64Manifest"]}]}}

        def response(_kubeconfig, _context, _env, *args):
            if "ocirepository" in args:
                return source
            if "helmrelease" in args:
                return release
            if "daemonset" in args:
                return daemonset
            if "pods" in args:
                return {"items": [pod]}
            raise AssertionError(args)

        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            evidence = smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        self.assertEqual(evidence["sourceArtifactRevision"], chart_digest)
        self.assertEqual(evidence["daemonSetUID"], "ds-uid")
        self.assertEqual([item["name"] for item in evidence["installerImages"]], ["installer", "helper"])
        self.assertEqual(evidence["releaseRevisionDigest"], chart_digest)
        source["status"]["conditions"][0]["observedGeneration"] = 1
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            with self.assertRaisesRegex(smoke.PreflightError, "source is not Ready"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        source["status"]["conditions"][0]["observedGeneration"] = 2
        source["spec"]["ref"]["digest"] = "sha256:" + "0" * 64
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            with self.assertRaisesRegex(smoke.PreflightError, "locked Kata chart digest"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        source["spec"]["ref"]["digest"] = chart_digest
        source["status"]["artifact"]["revision"] = "sha256:" + "0" * 64
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            with self.assertRaisesRegex(smoke.PreflightError, "current generation"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        source["status"]["artifact"]["revision"] = chart_digest
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            release["status"]["observedGeneration"] = 3
            with self.assertRaisesRegex(smoke.PreflightError, "current generation"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        release["status"]["observedGeneration"] = 4
        release["status"]["lastAttemptedRevisionDigest"] = "sha256:" + "0" * 64
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            with self.assertRaisesRegex(smoke.PreflightError, "locked OCI digest"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        release["status"]["lastAttemptedRevisionDigest"] = chart_digest
        pod["status"]["containerStatuses"][1]["imageID"] = "containerd://sha256:" + "9" * 64
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            with self.assertRaisesRegex(smoke.PreflightError, "imageID differs"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})
        pod["status"]["containerStatuses"][1]["imageID"] = "containerd://" + LOCK["kataInstallerArm64Manifest"]
        pod["status"]["containerStatuses"][1]["name"] = "unmatched"
        with mock.patch.object(smoke, "kubectl_json", side_effect=response):
            with self.assertRaisesRegex(smoke.PreflightError, "container statuses do not match declared names"):
                smoke.verify_flux_and_installer(Path("member.kubeconfig"), "member", {})

    def test_signature_check_fails_before_private_output_directory_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "new-private-evidence"
            evidence = write_host_evidence(root)
            with mock.patch.object(smoke, "verify_source_manifest_contract"), \
                 mock.patch.object(smoke, "verify_execution_source",
                                   side_effect=smoke.PreflightError("signature failure")), \
                 mock.patch.object(smoke, "preflight") as preflight:
                with self.assertRaisesRegex(smoke.PreflightError, "signature failure"):
                    smoke.setup_member(PROFILE, HOST, evidence, MEMBER, output)
                preflight.assert_not_called()
            self.assertFalse(output.exists())

    def test_preflight_refuses_unexpected_cluster_before_any_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = write_host_evidence(root)
            private = root / "new-smoke-private"
            response = {
                "docker info --format {{.Architecture}}": "aarch64",
                "docker info --format {{.NCPU}}": "6",
                "kind get clusters": f"{HOST}\nforeign-cluster\n",
            }
            seen = []

            def fake_run(argv, env, **kwargs):
                seen.append(argv)
                key = " ".join(argv)
                for suffix, result in response.items():
                    if key == suffix:
                        return result
                return ""

            with mock.patch.object(smoke, "docker_env", return_value={}), \
                 mock.patch.object(smoke, "run", side_effect=fake_run):
                with self.assertRaisesRegex(smoke.PreflightError, "cluster census"):
                    smoke.preflight(PROFILE, HOST, evidence, MEMBER, private)
            self.assertFalse(private.exists())
            self.assertFalse(any(argv[:3] == ["kind", "create", "cluster"] for argv in seen))

    def test_preflight_rejects_host_node_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = write_host_evidence(root)
            private = root / "new-smoke-private"

            def fake_run(argv, env, **kwargs):
                joined = " ".join(argv)
                if joined.endswith("info --format {{.Architecture}}"):
                    return "aarch64"
                if joined.endswith("info --format {{.NCPU}}"):
                    return "6"
                if joined == "kind get clusters":
                    return HOST
                if joined == f"kind get nodes --name {HOST}":
                    return HOST + "-control-plane"
                if joined.startswith("docker inspect"):
                    return "d" * 64
                return ""

            with mock.patch.object(smoke, "docker_env", return_value={}), \
                 mock.patch.object(smoke, "run", side_effect=fake_run):
                with self.assertRaisesRegex(smoke.PreflightError, "container no longer matches"):
                    smoke.preflight(PROFILE, HOST, evidence, MEMBER, private)
            self.assertFalse(private.exists())

    def test_guest_proof_requires_actual_arm64_manifest_and_distinct_guest_kernel(self) -> None:
        run_id = "0123456789ab"
        pod = {"metadata": {"labels": {smoke.LABEL_KEY: run_id}},
               "spec": {"runtimeClassName": "kata-qemu", "nodeName": MEMBER + "-control-plane",
                        "nodeSelector": {"kubernetes.io/hostname": MEMBER + "-control-plane",
                                         "katacontainers.io/kata-runtime": "true"},
                        "automountServiceAccountToken": False},
               "status": {"phase": "Running", "containerStatuses": [{
                   "imageID": "docker-pullable://busybox@" + LOCK["smokeImageArm64Manifest"]}]}}
        pod["metadata"]["uid"] = "pod-uid"
        pod["spec"]["containers"] = [{"image": LOCK["smokeImage"]}]
        pod["status"]["containerStatuses"][0].update({"ready": True, "containerID": "containerd://abc"})
        smoke.validate_guest_evidence(pod, MEMBER + "-control-plane", run_id, "pod-uid",
                                      pod["status"]["containerStatuses"][0]["imageID"],
                                      "6.1.0-kata", "6.1.0-kind", "aarch64")
        smoke.validate_guest_evidence(pod, MEMBER + "-control-plane", run_id, "pod-uid",
                                      "containerd://" + LOCK["smokeImageArm64Config"],
                                      "6.1.0-kata", "6.1.0-kind", "arm64")
        smoke.validate_guest_evidence(pod, MEMBER + "-control-plane", run_id, "pod-uid",
                                      "containerd://" + LOCK["smokeImage"].rsplit("@", 1)[1],
                                      "6.1.0-kata", "6.1.0-kind", "arm64")
        failures = [
            (pod, "wrong-run", pod["status"]["containerStatuses"][0]["imageID"], "6.1.0-kata", "6.1.0-kind", "aarch64"),
            (pod, run_id, "busybox:1.36", "6.1.0-kata", "6.1.0-kind", "aarch64"),
            (pod, run_id, "containerd://sha256:" + "9" * 64, "6.1.0-kata", "6.1.0-kind", "aarch64"),
            (pod, run_id, pod["status"]["containerStatuses"][0]["imageID"], "6.1.0-kind", "6.1.0-kind", "aarch64"),
            (pod, run_id, pod["status"]["containerStatuses"][0]["imageID"], "6.1.0-kata", "6.1.0-kind", "x86_64"),
        ]
        for candidate, label, image_id, guest_kernel, node_kernel, arch in failures:
            with self.subTest(label=label, arch=arch, image_id=image_id):
                with self.assertRaises(smoke.PreflightError):
                    smoke.validate_guest_evidence(candidate, MEMBER + "-control-plane", label, "pod-uid",
                                                  image_id, guest_kernel, node_kernel, arch)
        wrong_uid = copy.deepcopy(pod)
        wrong_uid["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(smoke.PreflightError, "Pod UID"):
            smoke.validate_guest_evidence(wrong_uid, MEMBER + "-control-plane", run_id, "pod-uid",
                                          pod["status"]["containerStatuses"][0]["imageID"],
                                          "6.1.0-kata", "6.1.0-kind", "aarch64")
        not_ready = copy.deepcopy(pod)
        not_ready["status"]["containerStatuses"][0]["ready"] = False
        with self.assertRaisesRegex(smoke.PreflightError, "readiness"):
            smoke.validate_guest_evidence(not_ready, MEMBER + "-control-plane", run_id, "pod-uid",
                                          pod["status"]["containerStatuses"][0]["imageID"],
                                          "6.1.0-kata", "6.1.0-kind", "aarch64")

    def test_cleanup_refuses_replaced_member_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            (private / "runtime-smoke.json").write_text(json.dumps({
                "phase": "smoke-passed", "profile": PROFILE, "hostCluster": HOST,
                "memberCluster": MEMBER, "hostEvidence": "/private/host-evidence",
                "sourceRevision": "c" * 40,
                "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                "memberNodeID": "e" * 64, "memberNodeUID": "f" * 36,
            }))
            (private / "runtime-smoke.json").chmod(0o600)
            baseline = {"dockerEnv": {}, "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                        "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                        "memberNode": MEMBER + "-control-plane", "memberPresent": True}
            with mock.patch.object(smoke, "preflight", return_value=baseline), \
                 mock.patch.object(smoke, "verify_execution_source", return_value="c" * 40), \
                 mock.patch.object(smoke, "run", return_value="d" * 64) as run_command:
                with self.assertRaisesRegex(smoke.PreflightError, "container was replaced"):
                    smoke.cleanup_member(PROFILE, HOST, Path("/private/host-evidence"), MEMBER, private)
            self.assertFalse(any(call.args[0][:3] == ["kind", "delete", "cluster"]
                                 for call in run_command.call_args_list))

    def test_failed_runtime_with_recorded_uids_can_be_explicitly_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            (private / "runtime-smoke.json").write_text(json.dumps({
                "phase": "kata-ready", "profile": PROFILE, "hostCluster": HOST,
                "memberCluster": MEMBER, "hostEvidence": "/private/host-evidence",
                "sourceRevision": "c" * 40, "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                "memberNodeID": "e" * 64, "memberNodeUID": "f" * 36,
            }))
            (private / "runtime-smoke.json").chmod(0o600)
            (private / "member.kubeconfig").write_text("private fixture")
            (private / "member.kubeconfig").chmod(0o600)
            baseline = {"dockerEnv": {}, "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                        "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                        "memberNode": MEMBER + "-control-plane", "memberPresent": True}

            def fake_run(argv, env, **kwargs):
                if argv[:2] == ["docker", "inspect"]:
                    return "e" * 64
                if argv[:3] == ["kind", "get", "clusters"]:
                    return HOST
                return ""

            with mock.patch.object(smoke, "verify_execution_source", return_value="c" * 40), \
                 mock.patch.object(smoke, "preflight", return_value=baseline), \
                 mock.patch.object(smoke, "get_node_uid", return_value=("f" * 36, "", {})), \
                 mock.patch.object(smoke, "run", side_effect=fake_run) as run_command:
                smoke.cleanup_member(PROFILE, HOST, Path("/private/host-evidence"), MEMBER, private)
            self.assertTrue(any(call.args[0][:3] == ["kind", "delete", "cluster"]
                                for call in run_command.call_args_list))
            self.assertEqual(json.loads((private / "runtime-smoke.json").read_text())["phase"],
                             "member-deleted")

    def test_cleanup_only_accepts_completed_or_recoverable_delete_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            file = private / "runtime-smoke.json"
            file.write_text(json.dumps({"phase": "member-created", "profile": PROFILE,
                                        "hostCluster": HOST, "memberCluster": MEMBER,
                                        "hostEvidence": "/private/host-evidence"}))
            file.chmod(0o600)
            with self.assertRaises(smoke.PreflightError):
                smoke.cleanup_member(PROFILE, HOST, Path("/private/host-evidence"), MEMBER, private)

    def test_lost_delete_reply_recovers_from_durable_intent_without_adopting_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            journal_path = private / "runtime-smoke.json"
            journal = {"phase": "delete-intent", "profile": PROFILE,
                       "hostCluster": HOST, "memberCluster": MEMBER,
                       "hostEvidence": "/private/host-evidence",
                       "sourceRevision": "c" * 40,
                       "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                       "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                       "memberNodeID": "e" * 64, "memberNodeUID": "f" * 36}
            journal_path.write_text(json.dumps(journal))
            journal_path.chmod(0o600)
            baseline = {"dockerEnv": {}, "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                        "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                        "memberNode": MEMBER + "-control-plane", "memberPresent": False}
            with mock.patch.object(smoke, "verify_execution_source", return_value="c" * 40), \
                 mock.patch.object(smoke, "preflight", return_value=baseline) as preflight, \
                 mock.patch.object(smoke, "run") as run_command:
                smoke.cleanup_member(PROFILE, HOST, Path("/private/host-evidence"), MEMBER, private)
            self.assertTrue(preflight.call_args.kwargs["cleanup_intent"])
            run_command.assert_not_called()
            saved = json.loads(journal_path.read_text())
            self.assertEqual(saved["phase"], "member-deleted")

    def test_completed_cleanup_is_idempotent_only_while_member_stays_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            journal_path = private / "runtime-smoke.json"
            journal_path.write_text(json.dumps({
                "phase": "member-deleted", "profile": PROFILE, "hostCluster": HOST,
                "memberCluster": MEMBER, "hostEvidence": "/private/host-evidence",
                "sourceRevision": "c" * 40,
                "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                "memberNodeID": "e" * 64, "memberNodeUID": "f" * 36,
            }))
            journal_path.chmod(0o600)
            baseline = {"dockerEnv": {}, "hostNodeID": NODE_ID, "hostNodeUID": NODE_UID,
                        "hostAPIServer": "https://127.0.0.1:6443", "hostCASHA256": "1" * 64,
                        "memberNode": MEMBER + "-control-plane", "memberPresent": False}
            with mock.patch.object(smoke, "verify_execution_source", return_value="c" * 40), \
                 mock.patch.object(smoke, "preflight", return_value=baseline) as preflight, \
                 mock.patch.object(smoke, "run") as run_command:
                smoke.cleanup_member(PROFILE, HOST, Path("/private/host-evidence"), MEMBER, private)
            self.assertTrue(preflight.call_args.kwargs["cleanup_intent"])
            run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
