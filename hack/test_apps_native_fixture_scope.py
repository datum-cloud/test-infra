"""Offline identity and remote-target failure gates for native fixtures."""

import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml


HACK = Path(__file__).resolve().parent
sys.path.insert(0, str(HACK))
import apps_native_fixture_scope as scope


CA = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----").decode()


def config(server: str, *, insecure: bool = False) -> dict:
    return {"current-context": "selected", "contexts": [{"name": "selected",
            "context": {"cluster": "one", "user": "one"}}],
            "clusters": [{"name": "one", "cluster": {
                "server": server, "certificate-authority-data": CA,
                "insecure-skip-tls-verify": insecure}}],
            "users": [{"name": "one", "user": {"token": "private-not-printed"}}]}


class ScopeTest(unittest.TestCase):
    def test_authenticated_root_reconnect_retries_only_ready_get(self):
        milo = mock.Mock()
        milo.k.side_effect = [RuntimeError("transient disconnect"), "ok"]
        with mock.patch("apps_native_support.time.sleep", return_value=None):
            scope.wait_authenticated_root(milo, seconds=0.2)
        self.assertEqual(milo.k.call_args_list,
                         [mock.call("get", "--raw", "/readyz")] * 2)

    def test_authenticated_root_reconnect_terminal_failure(self):
        milo = mock.Mock()
        milo.k.side_effect = RuntimeError("root unavailable")
        with mock.patch("apps_native_support.time.sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "authenticated Milo root reconnect"):
                scope.wait_authenticated_root(milo, seconds=0.01)
        self.assertTrue(milo.k.call_args_list)
        self.assertTrue(all(call == mock.call("get", "--raw", "/readyz")
                            for call in milo.k.call_args_list))

    def test_loopback_target_rejects_userinfo_proxy_missing_ca_or_insecure(self):
        good = config("https://127.0.0.1:43443")
        self.assertEqual(scope._selected_cluster(good, "selected", host=False, port=43443)[
            "server"], "https://127.0.0.1:43443")
        for bad in (
            config("https://127.0.0.1:43443@outside.example"),
            config("https://127.0.0.1:43443/other"),
            config("https://127.0.0.1:43443", insecure=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "CA-verified loopback"):
                scope._selected_cluster(bad, "selected", host=False, port=43443)
        bad = config("https://127.0.0.1:43443")
        bad["clusters"][0]["cluster"].pop("certificate-authority-data")
        with self.assertRaisesRegex(RuntimeError, "CA-verified loopback"):
            scope._selected_cluster(bad, "selected", host=False, port=43443)
        bad = config("https://127.0.0.1:43443")
        bad["clusters"][0]["cluster"]["proxy-url"] = "https://outside.example"
        with self.assertRaisesRegex(RuntimeError, "CA-verified loopback"):
            scope._selected_cluster(bad, "selected", host=False, port=43443)

    def test_remote_flux_secret_rejects_upstream_insecure_config(self):
        remote = config("https://milo-apiserver.milo-system.svc.cluster.local:6443",
                        insecure=True)
        remote["users"][0]["name"] = "apps-a1t-catalog-installer"
        remote["contexts"][0]["context"]["user"] = "apps-a1t-catalog-installer"
        raw = base64.b64encode(yaml.safe_dump(remote).encode()).decode()
        host = mock.Mock()
        host.json.return_value = {"type": "Opaque", "data": {"kubeconfig": raw}}
        identity = {"milo": {"ca_sha256": scope.hashlib.sha256(
            base64.b64decode(CA)).hexdigest()}}
        with self.assertRaisesRegex(RuntimeError, "verified Milo service"):
            scope.verified_flux_remote_secret(host, identity)
        remote["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = False
        host.json.return_value = {"type": "Opaque", "data": {"kubeconfig":
            base64.b64encode(yaml.safe_dump(remote).encode()).decode()}}
        scope.verified_flux_remote_secret(host, identity)
        with self.assertRaisesRegex(RuntimeError, "verified Milo service"):
            scope.verified_flux_remote_secret(host, identity,
                                              expected_token="different-token")
        remote["clusters"][0]["cluster"]["certificate-authority-data"] = base64.b64encode(
            b"-----BEGIN CERTIFICATE-----\nforeign\n-----END CERTIFICATE-----").decode()
        host.json.return_value = {"type": "Opaque", "data": {"kubeconfig":
            base64.b64encode(yaml.safe_dump(remote).encode()).decode()}}
        with self.assertRaisesRegex(RuntimeError, "CA differs"):
            scope.verified_flux_remote_secret(host, identity)

    def test_active_installer_rejects_changed_role_or_secret_same_uid(self):
        run_id = "a" * 24
        identity = {"host": "isolated", "milo": {"ca_sha256": "public-ca"}}
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                "metadata": {"name": "apps-a1t-catalog-installer", "uid": "role-uid",
                             "labels": {scope.RUN_LABEL: run_id}},
                "rules": [{"apiGroups": ["apiextensions.k8s.io"],
                           "resources": ["customresourcedefinitions"], "verbs": ["get"]}]}
        binding = {"apiVersion": "rbac.authorization.k8s.io/v1",
                   "kind": "ClusterRoleBinding",
                   "metadata": {"name": "apps-a1t-catalog-installer", "uid": "binding-uid",
                                "labels": {scope.RUN_LABEL: run_id}},
                   "roleRef": {"kind": "ClusterRole", "name": "apps-a1t-catalog-installer"},
                   "subjects": [{"kind": "User", "name": "apps-a1t-catalog-installer"}]}
        secret = {"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "apps-a1t-catalog-installer-kubeconfig",
                               "namespace": "milo-system", "uid": "secret-uid",
                               "labels": {scope.RUN_LABEL: run_id}},
                  "type": "Opaque", "data": {"kubeconfig": "scoped-config"}}
        objects = {"role": role, "binding": binding, "secret": secret}
        host, milo = mock.Mock(), mock.Mock()
        host.optional.side_effect = lambda *_: objects["secret"]
        milo.optional.side_effect = lambda kind, *_: objects[
            "role" if kind == "clusterrole" else "binding"]
        evidence = {"complete": True, "cleanup": "not-requested", "run_id": run_id,
                    "milo_source": "200de48a305732720377cb83308d57c84fd5f688",
                    "scope_identity": identity,
                    "attempted": {key: obj["metadata"]["uid"]
                                  for key, obj in objects.items()},
                    "intents": {key: scope.catalog_installer_intent_digest(obj)
                                for key, obj in objects.items()}}
        new_auth = base64.b64encode(
            b'fixture-token,apps-a1t-catalog-installer,apps-a1t-catalog-installer,"system:authenticated"\n').decode()
        host.json.side_effect = lambda *args: (
            {"metadata": {"uid": "auth-uid"}, "data": {"tokens.csv": new_auth}}
            if "secret" in args else
            {"metadata": {"uid": "core-uid", "generation": 7},
             "spec": {"suspend": True}})
        state = {"run_id": run_id, "auth_uid": "auth-uid", "new_b64": new_auth,
                 "core_uid": "core-uid", "suspended_generation": 7}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            private = Path(directory) / "apps-a1t-catalog-installer-private"
            private.mkdir(mode=0o700)
            path.write_text(json.dumps(evidence))
            (private / "state.json").write_text(json.dumps(state))
            path.chmod(0o600)
            (private / "state.json").chmod(0o600)
            with mock.patch.object(scope, "verified_flux_remote_secret") as verified:
                scope.active_catalog_installer(host, milo, identity, path)
                verified.assert_called_once_with(host, identity, secret, "fixture-token")
                objects["secret"] = copy.deepcopy(secret)
                objects["secret"]["data"]["kubeconfig"] = "changed-credential"
                with self.assertRaisesRegex(RuntimeError, "scoped installer secret changed"):
                    scope.active_catalog_installer(host, milo, identity, path)
                objects["secret"] = secret
                objects["role"] = copy.deepcopy(role)
                objects["role"]["rules"][0]["verbs"].append("delete")
                with self.assertRaisesRegex(RuntimeError, "scoped installer role changed"):
                    scope.active_catalog_installer(host, milo, identity, path)

    def test_static_auth_rollout_proves_new_owned_ready_pod(self):
        before = {"metadata": {"uid": "deployment-uid", "generation": 1},
                  "spec": {"selector": {"matchLabels": {"app": "milo-apiserver"}},
                           "template": {"metadata": {"annotations": {}},
                                        "spec": {"containers": [{"image": "pinned"}]}}}}
        after = copy.deepcopy(before)
        after["metadata"]["generation"] = 2
        after["spec"]["template"]["metadata"]["annotations"] = {
            "kubectl.kubernetes.io/restartedAt": "new-revision"}
        after["status"] = {"observedGeneration": 2, "readyReplicas": 1,
                           "updatedReplicas": 1, "availableReplicas": 1}
        rs = {"metadata": {"uid": "new-rs", "ownerReferences": [
            {"kind": "Deployment", "uid": "deployment-uid", "controller": True}]},
            "spec": {"template": {"metadata": {"annotations": {
                "kubectl.kubernetes.io/restartedAt": "new-revision"}}}}}
        pod = {"metadata": {"uid": "new-pod", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "new-rs", "controller": True}]},
            "status": {"phase": "Running", "conditions": [
                {"type": "Ready", "status": "True"}]}}
        host = mock.Mock()
        host.json.side_effect = [before, after, {"items": [rs]}, {"items": [pod]}]
        self.assertEqual(scope.rollout_static_auth(host), {
            "deployment_uid": "deployment-uid", "generation": 2,
            "pod_uids": ["new-pod"]})
        self.assertEqual(host.k.call_count, 2)
        old_pod = copy.deepcopy(pod)
        old_pod["metadata"].update(uid="old-pod", deletionTimestamp="now")
        old_pod["metadata"]["ownerReferences"][0]["uid"] = "old-rs"
        host.json.side_effect = [before, after, {"items": [rs]}, {"items": [pod, old_pod]}]
        with mock.patch.object(scope, "wait_for", side_effect=lambda probe, *_: probe()):
            self.assertFalse(scope.rollout_static_auth(host))
        empty = copy.deepcopy(after)
        empty["spec"]["replicas"] = 0
        host.json.side_effect = [before, empty]
        with mock.patch.object(scope, "wait_for", side_effect=lambda probe, *_: probe()):
            with self.assertRaisesRegex(RuntimeError, "replica count"):
                scope.rollout_static_auth(host)
        host.json.side_effect = [before, before]
        with mock.patch.object(scope, "wait_for", side_effect=lambda probe, *_: probe()):
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                scope.rollout_static_auth(host)
        changed_image = copy.deepcopy(after)
        changed_image["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign"
        host.json.side_effect = [before, changed_image]
        with mock.patch.object(scope, "wait_for", side_effect=lambda probe, *_: probe()):
            with self.assertRaisesRegex(RuntimeError, "did not advance"):
                scope.rollout_static_auth(host)


if __name__ == "__main__":
    unittest.main()
