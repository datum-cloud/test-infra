"""API-double failure tests for source-bound operator Flux recovery, never a cluster."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import yaml

from test_bootstrap_apps_native_catalog_crds import catalog
from test_bootstrap_apps_native_locations import locations
from test_apps_native_flux_prune import NativeAPI


class OperatorFluxTests(unittest.TestCase):
    def cases(self):
        manifests = catalog.manifest_set()
        for index in (2, 3):
            row = catalog.GRAPH[index]
            yield catalog, row, manifests[index], catalog.journal_key(row), (
                "sha1:" + catalog.CATALOG_SHA if index == 2 else catalog.BILLING_DIGEST)
        for index, file in ((4, "host.yaml"),):
            row = locations.ROWS[index]
            yield locations, row, yaml.safe_load((locations.ROOT / "components/locations" / file).read_text()), row[0], "sha1:" + locations.LOCATIONS_SHA

    def create(self, module, api, row, manifest, evidence, path, contracts):
        if module is catalog:
            return module.create_recorded(api, row, manifest, evidence, path, contracts)
        return module.row_create(api, row, manifest, evidence, path, flux_contracts=contracts)

    def delete(self, module, api, row, evidence, path, contracts):
        if module is catalog:
            return module.delete_recorded(api, api, row, evidence, path, contracts)
        return module.owned_delete(api, api, row, evidence, path, contracts)

    def contract(self, key, manifest, revision):
        # Small unit fixture identity; actual immutable source renders are checked separately.
        return {key: {"manifest": manifest, "revision": revision, "rendered": [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "fixture", "namespace": "fixture"}}]}}

    def test_all_three_stages_recover_lost_reply_only_with_durable_exact_intent(self):
        for module, row, manifest, key, revision in self.cases():
            for lost in (False, True):
                with self.subTest(stage=key, lost=lost), tempfile.TemporaryDirectory() as d:
                    api = NativeAPI()
                    api.lost_create = lost
                    evidence, path = {"run_id": "run", "attempted": {}}, Path(d) / "journal.json"
                    contracts = self.contract(key, manifest, revision)
                    if lost:
                        with self.assertRaisesRegex(RuntimeError, "response lost"):
                            self.create(module, api, row, manifest, evidence, path, contracts)
                    else:
                        self.create(module, api, row, manifest, evidence, path, contracts)
                    # Simulate process restart: use only reloaded durable data and fresh contract.
                    recovered = json.loads(path.read_text())
                    self.assertEqual(recovered["attempted"][key], None if lost else "uid")
                    api.obj["status"] = {"lastAppliedRevision": revision, "inventory": {"entries": [
                        {"id": "fixture_fixture__ConfigMap", "v": "v1"}]}}
                    def before_delete():
                        saved = json.loads(path.read_text())
                        self.assertEqual(saved["attempted"][key], "uid")
                        self.assertTrue(saved["flux_stages"][key]["delete_intent"])
                    api.before_delete = before_delete
                    self.delete(module, api, row, recovered, path, contracts)
                    self.assertEqual(len(api.delete_calls), 1)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_known_and_unknown_uids_reject_spec_inventory_revision_and_replacement(self):
        mutations = [lambda o: o["spec"].update(path="./foreign"),
                     lambda o: o["spec"].update(kubeConfig={"secretRef": {"name": "admin"}}),
                     lambda o: o["spec"].update(patches=[{"patch": "foreign"}]),
                     lambda o: o.update(status={"inventory": {"entries": [{"id": "_foreign__Namespace", "v": "v1"}]}}),
                     lambda o: o.update(status={"lastAppliedRevision": "sha1:foreign"}),
                     lambda o: o["metadata"].update(ownerReferences=[{"uid": "foreign"}])]
        for module, row, manifest, key, revision in self.cases():
            for lost in (False, True):
                for mutate in mutations:
                    with self.subTest(stage=key, lost=lost, mutation=mutate), tempfile.TemporaryDirectory() as d:
                        api, evidence = NativeAPI(), {"run_id": "run", "attempted": {}}
                        api.lost_create = lost
                        path, contracts = Path(d) / "journal.json", self.contract(key, manifest, revision)
                        try:
                            self.create(module, api, row, manifest, evidence, path, contracts)
                        except RuntimeError:
                            if not lost:
                                raise
                        mutate(api.obj)
                        with self.assertRaises(RuntimeError):
                            self.delete(module, api, row, json.loads(path.read_text()), path, contracts)
                        self.assertFalse(api.delete_calls)

    def test_old_uid_only_journal_or_changed_source_cannot_authorize_prune(self):
        for module, row, manifest, key, revision in self.cases():
            with self.subTest(stage=key), tempfile.TemporaryDirectory() as d:
                api, evidence = NativeAPI(), {"run_id": "run", "attempted": {key: "uid"}}
                path, contracts = Path(d) / "journal.json", self.contract(key, manifest, revision)
                with self.assertRaises(RuntimeError):
                    self.delete(module, api, row, evidence, path, contracts)
                self.assertFalse(api.delete_calls)
                evidence["attempted"] = {}
                self.create(module, api, row, manifest, evidence, path, contracts)
                contracts[key]["rendered"][0]["metadata"]["name"] = "other"
                with self.assertRaises(RuntimeError):
                    self.delete(module, api, row, evidence, path, contracts)
                self.assertFalse(api.delete_calls)

    def test_known_uid_replacement_and_inconsistent_journal_never_delete(self):
        for module, row, manifest, key, revision in self.cases():
            with self.subTest(stage=key), tempfile.TemporaryDirectory() as d:
                api, evidence = NativeAPI(), {"run_id": "run", "attempted": {}}
                path, contracts = Path(d) / "journal.json", self.contract(key, manifest, revision)
                self.create(module, api, row, manifest, evidence, path, contracts)
                api.obj["metadata"]["uid"] = "replacement"
                with self.assertRaises(RuntimeError):
                    self.delete(module, api, row, evidence, path, contracts)
                api.obj["metadata"]["uid"] = "uid"
                evidence["attempted"][key] = "inconsistent"
                with self.assertRaises(RuntimeError):
                    self.delete(module, api, row, evidence, path, contracts)
                self.assertFalse(api.delete_calls)

    def test_ci_executes_operator_flux_and_triggers_on_its_sources(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/apps-native-authoring.yml").read_text())
        scripts = [step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", [])]
        self.assertTrue(any("-p 'test_apps_native_operator_flux.py' -q" in script for script in scripts))
        offline = yaml.safe_load((root / ".github/workflows/apps-native-offline.yml").read_text())
        events = offline.get("on", offline.get(True))
        for event in ("pull_request", "push"):
            self.assertLessEqual({"components/locations/**", "components/service-catalog/api-crds.yaml", "components/service-catalog/billing-crds.yaml",
                "hack/apps_native_operator_flux.py", "hack/test_apps_native_operator_flux.py"},
                set(events[event]["paths"]))

    def test_billing_archive_must_match_verified_content_before_render(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            commands = Mock()
            commands.run.side_effect = [catalog.CATALOG_SHA, ""]
            artifact = Path(d) / "billing.tar"
            artifact.write_bytes(b"x" * 394240)
            with self.assertRaisesRegex(RuntimeError, "verified immutable OCI"):
                catalog.source_contracts(commands, Path(d), artifact)
            self.assertEqual(commands.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
