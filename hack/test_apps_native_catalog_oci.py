"""Offline failure gates for the exact Catalog OCI source transition."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from apps_native_catalog_oci import OCI_DIGEST, SOURCE_NAME, exact_manifest
from apps_native_support import RUN_LABEL, _flux_spec
import yaml


SCRIPT = Path(__file__).with_name("transition-apps-native-catalog-oci.py")
SPEC = importlib.util.spec_from_file_location("catalog_oci_transition", SCRIPT)
transition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transition)


class SourceSafetyTests(unittest.TestCase):
    @staticmethod
    def source():
        return {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "OCIRepository",
            "metadata": {"name": SOURCE_NAME, "namespace": "flux-system",
                         "labels": {transition.RUN_LABEL: "a" * 24}},
            "spec": {"interval": "10m", "url": "oci://ghcr.io/milo-os/service-catalog-kustomize",
                     "ref": {"digest": OCI_DIGEST}},
        }

    def test_source_intent_allows_only_flux_reconcile_marker(self):
        source = self.source()
        expected = transition.source_intent(source)
        reconciled = copy.deepcopy(source)
        reconciled["metadata"]["annotations"] = {
            "reconcile.fluxcd.io/requestedAt": "2026-10-21T18:00:00Z"}
        reconciled["spec"].update(provider="generic", timeout="60s")
        self.assertEqual(transition.source_intent(reconciled), expected)
        changed = copy.deepcopy(reconciled)
        changed["spec"]["ref"] = {"tag": "latest"}
        self.assertNotEqual(transition.source_intent(changed), expected)
        changed = copy.deepcopy(reconciled)
        changed["metadata"]["annotations"]["unreviewed"] = "value"
        self.assertNotEqual(transition.source_intent(changed), expected)
        changed = copy.deepcopy(reconciled)
        changed["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
        with self.assertRaisesRegex(RuntimeError, "lifecycle"):
            transition.source_intent(changed)

    def test_mutated_oci_manifest_rejected_before_cluster_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schemaVersion":2,"layers":[]}')
            with self.assertRaisesRegex(RuntimeError, "digest differs"):
                exact_manifest(path)

    def test_dependent_flux_stage_blocks_prune_before_proxy(self):
        class Host:
            def optional(self, kind, name, namespace=None):
                if kind == "kustomization" and name == "apps-a1t-locationbinding-crd":
                    return {"metadata": {"uid": "dependent"}}
                return None
        evidence = {"phase": "complete", "scope_identity": {}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(transition, "APIProxy") as proxy:
            with self.assertRaisesRegex(RuntimeError, "dependent Flux Kustomization"):
                transition.cleanup(Host(), object(), {}, {"cleanup": "not-requested"},
                                   {}, evidence, Path(directory) / "journal.json", None)
            proxy.assert_not_called()

    def test_changed_known_uid_oci_source_blocks_prune_before_proxy(self):
        source = self.source()
        expected = transition.source_intent(source)
        changed = copy.deepcopy(source)
        changed["spec"]["ref"] = {"tag": "mutable"}
        changed["metadata"]["uid"] = "owned-source-uid"
        class Host:
            def optional(self, kind, name, namespace=None):
                return changed if kind == "ocirepository" else None
        evidence = {"phase": "complete", "scope_identity": {}, "run_id": "a" * 24,
                    "oci_uid":
                    "owned-source-uid", "oci_intent": expected}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(transition, "assert_no_catalog_dependents"), \
             mock.patch.object(transition, "APIProxy") as proxy:
            with self.assertRaisesRegex(RuntimeError, "intent changed"):
                transition.cleanup(Host(), object(), {"source": source},
                                   {"cleanup": "not-requested"}, {}, evidence,
                                   Path(directory) / "journal.json", None)
            proxy.assert_not_called()

    def test_terminating_oci_source_recovers_only_journaled_uid_and_intent(self):
        expected = self.source()
        live = copy.deepcopy(expected)
        live["metadata"].update(uid="owned-source-uid",
                                deletionTimestamp="2026-10-21T18:00:00Z")
        evidence = {"phase": "source-delete-intent", "run_id": "a" * 24,
                    "oci_uid": "owned-source-uid",
                    "oci_intent": transition.source_intent(expected)}
        class Host:
            def __init__(self, item):
                self.item = item
            def optional(self, *_):
                return self.item
        contract = {"source": expected}
        transition.validate_oci_source(Host(live), contract, evidence,
                                       ready_required=False, allow_terminating=True)
        with self.assertRaisesRegex(RuntimeError, "lifecycle"):
            transition.validate_oci_source(Host(live), contract, evidence,
                                           ready_required=False)
        wrong_phase = copy.deepcopy(evidence)
        wrong_phase["phase"] = "complete"
        with self.assertRaisesRegex(RuntimeError, "journaled delete phase"):
            transition.validate_oci_source(Host(live), contract, wrong_phase,
                                           ready_required=False, allow_terminating=True)
        replaced = copy.deepcopy(live)
        replaced["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "UID or exact digest intent"):
            transition.validate_oci_source(Host(replaced), contract, evidence,
                                           ready_required=False, allow_terminating=True)
        changed = copy.deepcopy(live)
        changed["spec"]["ref"] = {"tag": "latest"}
        with self.assertRaisesRegex(RuntimeError, "UID or exact digest intent"):
            transition.validate_oci_source(Host(changed), contract, evidence,
                                           ready_required=False, allow_terminating=True)

    def test_flux_delete_lost_reply_persists_intent_and_recovers_same_uid(self):
        target = yaml.safe_load(transition.ROOT.joinpath(
            "components/service-catalog/api-crds.yaml").read_text())
        target["spec"]["sourceRef"] = {
            "kind": "OCIRepository", "name": SOURCE_NAME, "namespace": "flux-system"}
        target["spec"]["path"] = "./overlays/e2e-milo/milo-resources"
        rendered = [{"apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": "unit-fixture", "namespace": "milo-system"}}]
        contract = {"target": target, "rendered": rendered}
        graph = {"run_id": "g" * 24}
        evidence = {"stage_uid": "owned-stage-uid", "phase": "api-delete-intent"}
        live = {"apiVersion": target["apiVersion"], "kind": target["kind"],
                "metadata": {"name": target["metadata"]["name"],
                             "namespace": target["metadata"]["namespace"],
                             "uid": "owned-stage-uid", "resourceVersion": "9",
                             "labels": {RUN_LABEL: graph["run_id"]}},
                "spec": _flux_spec(target["spec"]), "status": {"inventory": {"entries": []}}}
        class Host:
            item = live
            def optional(self, *_):
                return self.item
        host = Host()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transition.json"
            owner, _ = transition.target_flux_state(host, contract, graph,
                                                    evidence, path)
            class LostReply:
                def delete(self, *_):
                    stored = json.loads(path.read_text())
                    self_test.assertTrue(stored["target_flux_state"]["delete_intent"])
                    raise RuntimeError("delete reply lost")
            self_test = self
            with self.assertRaisesRegex(RuntimeError, "reply lost"):
                owner.delete(LostReply(), graph["run_id"])
            terminating = copy.deepcopy(live)
            terminating["metadata"]["deletionTimestamp"] = "2026-10-21T18:00:00Z"
            host.item = terminating
            recovered, _ = transition.target_flux_state(host, contract, graph,
                                                        evidence, path)
            recovered._validate(terminating, graph["run_id"])
            class Confirmed:
                def delete(self, *_):
                    host.item = None
            recovered.delete(Confirmed(), graph["run_id"])
            replaced = copy.deepcopy(terminating)
            replaced["metadata"]["uid"] = "foreign"
            with self.assertRaisesRegex(RuntimeError, "identity/lifecycle"):
                recovered._validate(replaced, graph["run_id"])
            drifted = copy.deepcopy(terminating)
            drifted["spec"]["path"] = "./foreign"
            with self.assertRaisesRegex(RuntimeError, "spec drift"):
                recovered._validate(drifted, graph["run_id"])

    def test_setup_rejects_conflicted_source_switch_with_intent_retained(self):
        original = yaml.safe_load(transition.ROOT.joinpath(
            "components/service-catalog/api-crds.yaml").read_text())
        target = copy.deepcopy(original)
        target["spec"]["sourceRef"] = {
            "kind": "OCIRepository", "name": SOURCE_NAME, "namespace": "flux-system"}
        target["spec"]["path"] = "./overlays/e2e-milo/milo-resources"
        contract = {"original": original, "target": target,
                    "source": self.source(),
                    "rendered": [{"apiVersion": "v1", "kind": "ConfigMap",
                                  "metadata": {"name": "unit-fixture", "namespace": "milo-system"}}]}
        graph = {"run_id": "g" * 24}
        evidence = {"run_id": "a" * 24, "phase": "source-create-intent",
                    "original_git_uid": "original-source-uid",
                    "stage_uid": "owned-stage-uid", "oci_uid": "owned-source-uid",
                    "oci_intent": transition.source_intent(contract["source"])}
        old = {"apiVersion": original["apiVersion"], "kind": original["kind"],
               "metadata": {"name": original["metadata"]["name"],
                            "namespace": original["metadata"]["namespace"],
                            "uid": "owned-stage-uid", "resourceVersion": "9",
                            "labels": {RUN_LABEL: graph["run_id"]}},
               "spec": _flux_spec(original["spec"]), "status": {"inventory": {"entries": []}}}
        class Host:
            def __init__(self):
                self.live = old
                self.patch_calls = 0
                self.commands = mock.Mock()
            def optional(self, *_):
                return None
            def json(self, *_):
                return self.live
            def patch_json(self, _, kind, name, operations, namespace):
                self.patch_calls += 1
                self_test.assertEqual((kind, name, namespace),
                                      ("kustomization", "service-catalog-api-crds", "milo-system"))
                self_test.assertEqual(operations[:3], [
                    {"op": "test", "path": "/metadata/uid", "value": "owned-stage-uid"},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": "9"},
                    {"op": "test", "path": "/spec", "value": old["spec"]}])
                self_test.assertEqual(operations[3],
                                      {"op": "replace", "path": "/spec",
                                       "value": _flux_spec(target["spec"])})
                raise RuntimeError("CAS conflict or lost reply")
        self_test = self
        host = Host()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(transition, "original_git", return_value={
                 "metadata": {"uid": "original-source-uid"}}), \
             mock.patch.object(transition, "validate_oci_source", return_value={
                 "metadata": {"uid": "owned-source-uid"}}), \
             mock.patch.object(transition, "wait_for", side_effect=lambda predicate, *_: predicate()):
            path = Path(directory) / "transition.json"
            with self.assertRaisesRegex(RuntimeError, "patch outcome uncertain"):
                transition.setup(host, contract, graph, {}, evidence, path)
            stored = json.loads(path.read_text())
            self.assertEqual(stored["phase"], "api-patch-intent")
            self.assertTrue(stored["patch_intent"])
            self.assertEqual(stored["stage_uid"], "owned-stage-uid")
            self.assertEqual(host.patch_calls, 1)
            host.commands.run.assert_not_called()

    def test_source_create_lost_reply_recovers_only_exact_owned_intent(self):
        source = self.source()
        observed = copy.deepcopy(source)
        observed["metadata"].update(uid="owned-source-uid", resourceVersion="3")
        evidence = {"run_id": "a" * 24, "phase": "source-create-intent",
                    "original_git_uid": "original-source-uid", "oci_uid": None,
                    "oci_intent": transition.source_intent(source)}
        class Host:
            current = None
            def optional(self, *_):
                return self.current
            def create(self, *_):
                self.current = observed
                raise RuntimeError("create reply lost")
        host = Host()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(transition, "original_git", return_value={
                 "metadata": {"uid": "original-source-uid"}}), \
             mock.patch.object(transition, "wait_for", side_effect=RuntimeError("next phase")):
            path = Path(directory) / "transition.json"
            transition.journal(path, evidence)
            with self.assertRaisesRegex(RuntimeError, "create reply lost"):
                transition.setup(host, {"source": source}, {}, {}, evidence, path)
            self.assertIsNone(json.loads(path.read_text())["oci_uid"])
            with self.assertRaisesRegex(RuntimeError, "next phase"):
                transition.setup(host, {"source": source}, {}, {}, evidence, path)
            self.assertEqual(json.loads(path.read_text())["oci_uid"], "owned-source-uid")
            altered = copy.deepcopy(observed)
            altered["spec"]["ref"] = {"tag": "foreign"}
            host.current = altered
            evidence["oci_uid"] = None
            with self.assertRaisesRegex(RuntimeError, "exact intent"):
                transition.setup(host, {"source": source}, {}, {}, evidence, path)


if __name__ == "__main__":
    unittest.main()
