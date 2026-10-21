"""Offline failure gates for the isolated, source-owned Catalog operator."""

import base64
import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from apps_native_support import Commands, RUN_LABEL
from apps_native_catalog_operator_contract import ca_contract, modified_config
import apps_native_catalog_operator_contract as source_contract


SOURCE = Path(__file__).with_name("bootstrap-apps-native-catalog-operator.py")
spec = importlib.util.spec_from_file_location("catalog_operator_fixture", SOURCE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def four_rows():
    rows = [
        ["admin-token", "admin", "admin", "system:masters"],
        ["test-token", "test-user", "test-user", "system:authenticated"],
        ["installer-token", "apps-a1t-catalog-installer",
         "apps-a1t-catalog-installer", "system:authenticated"],
        ["locations-token", "apps-a1t-locations", "apps-a1t-locations",
         "system:authenticated"],
    ]
    text = io.StringIO()
    csv.writer(text, lineterminator="\n").writerows(rows)
    return base64.b64encode(text.getvalue().encode()).decode()


class CatalogOperatorContractTests(unittest.TestCase):
    def test_ca_source_renders_only_exact_stable_chain(self):
        contract = ca_contract(Commands(), "1" * 40, "2" * 24)
        self.assertEqual(len(contract["rendered"]), 3)
        self.assertEqual({(obj["kind"], obj["metadata"]["name"])
                          for obj in contract["rendered"]}, {
            ("Issuer", "apps-a1t-services-selfsigned"),
            ("Issuer", "apps-a1t-services-ca"),
            ("Certificate", "apps-a1t-services-ca")})
        self.assertEqual(contract["stage"]["spec"]["sourceRef"], {
            "kind": "GitRepository", "name": fixture.CA_SOURCE,
            "namespace": "flux-system"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in source_contract.CA_ROOT.iterdir():
                (root / source.name).write_bytes(source.read_bytes())
            certificate = root / "certificate.yaml"
            certificate.write_text(certificate.read_text().replace(
                "isCA: true", "isCA: false"))
            with mock.patch.object(source_contract, "CA_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "task CA source"):
                    ca_contract(Commands(), "1" * 40, "2" * 24)

    def test_location_source_is_the_only_config_extension(self):
        raw = """apiVersion: apiserver.config.miloapis.com/v1alpha1
kind: ServicesOperator
metricsServer: {bindAddress: '0'}
webhookServer: {}
kubeconfigPath: /etc/milo/kubeconfig
"""
        changed = modified_config(raw)
        self.assertEqual(__import__("yaml").safe_load(changed), {
            "apiVersion": "apiserver.config.miloapis.com/v1alpha1",
            "kind": "ServicesOperator", "metricsServer": {"bindAddress": "0"},
            "webhookServer": {}, "kubeconfigPath": "/etc/milo/kubeconfig",
            "locationSource": "locations.miloapis.com/v1alpha1"})
        with self.assertRaisesRegex(RuntimeError, "source config defaults changed"):
            modified_config(raw + "locationSource: networking.datumapis.com/v1alpha\n")

    def test_four_row_auth_refuses_foreign_identity_and_operator_reuse(self):
        old = four_rows()
        self.assertEqual(len(fixture.auth_rows(old)), 4)
        new = fixture.extended_auth(old, "a" * 96)
        self.assertEqual(len(list(csv.reader(io.StringIO(
            base64.b64decode(new).decode())))), 5)
        with self.assertRaisesRegex(RuntimeError, "four-user predecessor"):
            fixture.auth_rows(new)
        rows = fixture.auth_rows(old)
        rows[3][1] = "foreign"
        stream = io.StringIO()
        csv.writer(stream, lineterminator="\n").writerows(rows)
        with self.assertRaisesRegex(RuntimeError, "four-user predecessor"):
            fixture.auth_rows(base64.b64encode(stream.getvalue().encode()).decode())

    def test_probe_rejects_private_mount_and_unbound_pod(self):
        ca = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        cm, pod = fixture.probe_manifests(ca)
        self.assertEqual(cm["data"], {"ca.crt": ca.decode()})
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertEqual(pod["spec"]["volumes"][0]["configMap"]["defaultMode"], 420)
        observed = copy.deepcopy(pod)
        observed["metadata"]["labels"] = {RUN_LABEL: "run"}
        fixture.probe_pod_matches(observed, pod, "run")
        observed["spec"]["volumes"].append({"name": "secret", "secret": {"secretName": "admin"}})
        with self.assertRaisesRegex(RuntimeError, "acquired privilege"):
            fixture.probe_pod_matches(observed, pod, "run")
        observed = copy.deepcopy(pod)
        observed["metadata"]["labels"] = {RUN_LABEL: "foreign"}
        with self.assertRaisesRegex(RuntimeError, "changed"):
            fixture.probe_pod_matches(observed, pod, "run")

    def test_lost_probe_create_recovers_only_full_defaulted_pod(self):
        ca = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        _, expected = fixture.probe_manifests(ca)
        observed = copy.deepcopy(expected)
        observed["metadata"].update(labels={RUN_LABEL: "a" * 24}, uid="probe-uid",
                                    resourceVersion="5")
        observed["spec"].update({
            "dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
            "serviceAccountName": "default", "serviceAccount": "default",
            "terminationGracePeriodSeconds": 30, "enableServiceLinks": True,
            "priority": 0, "preemptionPolicy": "PreemptLowerPriority",
            "nodeName": "task-node", "tolerations": [
                {"effect": "NoExecute", "key": "node.kubernetes.io/not-ready",
                 "operator": "Exists", "tolerationSeconds": 300},
                {"effect": "NoExecute", "key": "node.kubernetes.io/unreachable",
                 "operator": "Exists", "tolerationSeconds": 300}]})
        observed["spec"]["containers"][0].update({
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File"})

        class Host:
            def __init__(self, pod): self.pod = pod
            def optional(self, *_): return self.pod
            def json(self, *_): return {"items": [{"metadata": {
                "name": "task-node", "uid": "node-uid"}}]}
        class Proxy:
            def __init__(self, host): self.host, self.deletes = host, []
            def delete(self, *args):
                self.deletes.append(args)
                self.host.pod = None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            for mutate in (
                lambda obj: obj["spec"].update(ephemeralContainers=[{"name": "extra"}]),
                lambda obj: obj["spec"].update(serviceAccountName="privileged"),
                lambda obj: obj["spec"]["containers"][0].update(env=[{"name": "X"}]),
                lambda obj: obj["spec"]["containers"][0].update(lifecycle={}),
                lambda obj: obj["metadata"].update(annotations={"foreign": "value"}),
                lambda obj: obj["spec"].update(hostNetwork=True),
            ):
                changed = copy.deepcopy(observed)
                mutate(changed)
                host, evidence = Host(changed), {"run_id": "a" * 24,
                    "attempted": {"tls_pod": None}}
                proxy = Proxy(host)
                with self.assertRaises(RuntimeError):
                    fixture.delete_probe_object(host, proxy, "tls_pod", expected,
                                                evidence, path)
                self.assertIsNone(evidence["attempted"]["tls_pod"])
                self.assertEqual(proxy.deletes, [])
            host = Host(copy.deepcopy(observed))
            evidence = {"run_id": "a" * 24, "attempted": {"tls_pod": None}}
            proxy = Proxy(host)
            fixture.delete_probe_object(host, proxy, "tls_pod", expected,
                                        evidence, path)
            self.assertEqual(evidence["attempted"]["tls_pod"], "probe-uid")
            self.assertEqual(len(proxy.deletes), 1)

    def test_ready_manager_requires_single_recorded_container(self):
        class Host:
            def json(self, *args):
                if "deployment" in args:
                    return {"metadata": {"uid": "deployment", "generation": 1,
                                          "labels": {RUN_LABEL: "run"}},
                            "status": {"observedGeneration": 1,
                                       "readyReplicas": 1, "availableReplicas": 1},
                            "spec": {"replicas": 1, "template": {"spec": {
                                "containers": [{"image": "ghcr.io/milo-os/service-catalog@" +
                                                fixture.IMAGE_INDEX}]}}}}
                if "replicasets" in args:
                    return {"items": [{"metadata": {"uid": "replicaset",
                        "ownerReferences": [{"kind": "Deployment", "uid": "deployment",
                                             "controller": True}]}}]}
                if "pods" in args:
                    return {"items": [{"metadata": {"uid": "pod",
                        "ownerReferences": [{"kind": "ReplicaSet", "uid": "replicaset",
                                             "controller": True}]},
                        "status": {"phase": "Running", "conditions": [
                            {"type": "Ready", "status": "True"}],
                                   "containerStatuses": []}}]}
                raise AssertionError("node query must not follow missing container status")
        with self.assertRaisesRegex(RuntimeError, "exactly one manager container"):
            fixture.verified_pod(Host(), {"run_id": "run"})

    def test_create_intent_persists_before_request_and_unknown_uid_is_not_adopted(self):
        class Cluster:
            def __init__(self): self.calls = 0
            def create(self, *_args, **_kwargs):
                self.calls += 1
                raise RuntimeError("test-only lost reply")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            evidence = {"run_id": "a" * 24, "attempted": {}, "intents": {}}
            cluster = Cluster()
            with self.assertRaisesRegex(RuntimeError, "lost reply"):
                fixture.create_owned(cluster, "namespace",
                    {"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": fixture.NAMESPACE}}, evidence, path)
            self.assertEqual(cluster.calls, 1)
            observed = json.loads(path.read_text())
            self.assertIn("namespace", observed["attempted"])
            self.assertIsNone(observed["attempted"]["namespace"])
            self.assertEqual(len(observed["intents"]["namespace"]), 64)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_auth_restore_restarts_even_after_lost_restore_reply(self):
        old, new = four_rows(), fixture.extended_auth(four_rows(), "b" * 96)
        class Host:
            def json(self, *_):
                return {"metadata": {"uid": "auth-uid"},
                        "data": {"tokens.csv": old}}
        state = {"auth_intent": True, "old_b64": old, "new_b64": new,
                 "auth_uid": "auth-uid"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with mock.patch.object(fixture, "rollout_static_auth",
                                   return_value={"pod_uids": ["new-pod"]}) as rollout, \
                 mock.patch.object(fixture, "patch_auth") as patch:
                fixture.auth_restore(Host(), Path(directory), {}, state, path)
            patch.assert_not_called()
            rollout.assert_called_once()
            self.assertEqual(json.loads(path.read_text())["restored_auth_rollout"],
                             {"pod_uids": ["new-pod"]})

    def test_auth_restore_lost_patch_reply_then_foreign_value_denial(self):
        old, new = four_rows(), fixture.extended_auth(four_rows(), "b" * 96)
        class Host:
            def __init__(self): self.value = new
            def json(self, *_):
                return {"metadata": {"uid": "auth-uid"},
                        "data": {"tokens.csv": self.value}}
        host = Host()
        state = {"auth_intent": True, "old_b64": old, "new_b64": new,
                 "auth_uid": "auth-uid"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            def lost_reply(*_):
                host.value = old
                raise RuntimeError("test-only response loss after CAS commit")
            with mock.patch.object(fixture, "patch_auth", side_effect=lost_reply), \
                 mock.patch.object(fixture, "rollout_static_auth") as rollout:
                with self.assertRaisesRegex(RuntimeError, "response loss"):
                    fixture.auth_restore(host, Path(directory), {}, state, path)
                rollout.assert_not_called()
            with mock.patch.object(fixture, "patch_auth") as patch, \
                 mock.patch.object(fixture, "rollout_static_auth",
                                   return_value={"pod_uids": ["new-pod"]}) as rollout:
                fixture.auth_restore(host, Path(directory), {}, state, path)
                patch.assert_not_called()
                rollout.assert_called_once()
            self.assertEqual(json.loads(path.read_text())["restored_auth_rollout"],
                             {"pod_uids": ["new-pod"]})
            host.value = "foreign-csv-value"
            with mock.patch.object(fixture, "patch_auth") as patch, \
                 mock.patch.object(fixture, "rollout_static_auth") as rollout:
                with self.assertRaisesRegex(RuntimeError, "differs from exact"):
                    fixture.auth_restore(host, Path(directory), {}, state, path)
                patch.assert_not_called()
                rollout.assert_not_called()

    def test_foreign_data_blocks_cleanup_before_prune(self):
        class Milo:
            def json(self, *_):
                return {"items": [{"metadata": {"name": x}} for x in
                                  ("apps-a1t-provider", "apps-a1t-consumer-a",
                                   "apps-a1t-consumer-b")]}
            def k(self, *args):
                return json.dumps({"items": [{"metadata": {"name": "foreign"}}]})
        with self.assertRaisesRegex(RuntimeError, "data remains"):
            fixture.data_census(Milo())

    def test_generated_secret_replacement_denies_delete(self):
        class Host:
            def optional(self, *_):
                return {"metadata": {"uid": "replacement", "resourceVersion": "5"},
                        "type": "kubernetes.io/tls", "data": {"tls.crt": "x", "tls.key": "y"}}
        class Proxy:
            def delete(self, *_):
                raise AssertionError("replacement must not be deleted")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "cannot be proven owned"):
                fixture.generated_secret_delete(Host(), Proxy(), fixture.CA_SECRET,
                    "original", {}, Path(directory) / "evidence.json")

    def test_unknown_uid_changed_source_role_refuses_delete(self):
        expected = {"apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRole", "metadata": {"name": fixture.SOURCE_ROLE},
                    "rules": [{"apiGroups": ["services.miloapis.com"],
                               "resources": ["services"], "verbs": ["get"]}]}
        marked = copy.deepcopy(expected)
        marked["metadata"]["labels"] = {RUN_LABEL: "a" * 24}
        changed = copy.deepcopy(marked)
        changed["metadata"].update(uid="owned-uid", resourceVersion="5")
        changed["rules"][0]["verbs"].append("delete")
        class Cluster:
            def optional(self, *_):
                return changed
        class Proxy:
            def delete(self, *_):
                raise AssertionError("changed role must not be deleted")
        evidence = {"run_id": "a" * 24, "attempted": {"role": None},
                    "intents": {"role": fixture.intent_digest(marked)}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "differs from exact source"):
                fixture.delete_owned(Cluster(), Proxy(), "role", expected,
                                     evidence, Path(directory) / "evidence.json")

    def test_failed_webhook_restore_retains_manager_and_credential(self):
        evidence = {"scope_identity": {"task": "same"}, "run_id": "a" * 24,
                    "attempted": {"host_stage": "host-uid", "credential": "secret-uid"},
                    "api_flux_state": {"ca_bundle": "public-ca"},
                    "cleanup": "not-requested"}
        class Proxy:
            def __init__(self, *_): pass
            def start(self): pass
            def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            args = type("Args", (), {"evidence_file": path})()
            with mock.patch.object(fixture, "cleanup_predecessors", return_value={}), \
                 mock.patch.object(fixture, "APIProxy", Proxy), \
                 mock.patch.object(fixture, "data_census"), \
                 mock.patch.object(fixture, "restore_existing_webhooks",
                                   side_effect=RuntimeError("test-only CA drift")), \
                 mock.patch.object(fixture, "delete_stage") as delete:
                with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                    fixture.cleanup(object(), object(), Commands(), args,
                                    {}, evidence, {}, path, {"task": "same"})
                delete.assert_not_called()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["cleanup"], "incomplete")
            self.assertIn("source-owned public webhook CA restore failed",
                          saved["cleanup_errors"])


if __name__ == "__main__":
    unittest.main()
