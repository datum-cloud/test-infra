"""Offline failure gates for the source-owned Catalog API bootstrap."""

import importlib.util
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("bootstrap-apps-native-catalog-crds.py")
SPEC = importlib.util.spec_from_file_location("catalog_crds", SCRIPT)
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


class CatalogAPIBootstrapTest(unittest.TestCase):
    def test_exact_source_refs_and_remote_milo_targets(self):
        manifests = catalog.manifest_set()
        self.assertEqual(len(manifests), 4)
        self.assertEqual(manifests[0]["spec"]["ref"], {"commit": catalog.CATALOG_SHA})
        self.assertEqual(manifests[1]["spec"]["ref"], {"digest": catalog.BILLING_DIGEST})
        self.assertEqual({item["metadata"]["name"] for item in manifests[2:]},
                         {"service-catalog-api-crds", "service-catalog-billing-crds"})
        for item in manifests[2:]:
            self.assertEqual(item["spec"]["kubeConfig"]["secretRef"],
                             {"name": "apps-a1t-catalog-installer-kubeconfig",
                              "key": "kubeconfig"})

    def test_source_ref_or_unreviewed_mutation_fails_before_create(self):
        original = catalog.COMPONENT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, *_ in catalog.GRAPH:
                (root / filename).write_text((original / filename).read_text())
            item = catalog.yaml.safe_load((root / "api-crds.yaml").read_text())
            item["spec"]["sourceRef"] = {"kind": "GitRepository", "name": "foreign",
                                          "namespace": "flux-system"}
            (root / "api-crds.yaml").write_text(catalog.yaml.safe_dump(item))
            with mock.patch.object(catalog, "COMPONENT", root):
                with self.assertRaisesRegex(RuntimeError, "remote Catalog graph"):
                    catalog.manifest_set()
            item["spec"]["sourceRef"] = {"kind": "GitRepository",
                                          "name": "service-catalog", "namespace": "flux-system"}
            item["spec"]["postBuild"] = {"substitute": {"evil": "true"}}
            (root / "api-crds.yaml").write_text(catalog.yaml.safe_dump(item))
            with mock.patch.object(catalog, "COMPONENT", root):
                with self.assertRaisesRegex(RuntimeError, "remote Catalog graph"):
                    catalog.manifest_set()

    def test_lost_create_response_keeps_preposted_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory) / "journal.json"
            evidence = {"run_id": "a" * 24, "attempted": {}}
            catalog.atomic_json(evidence_file, evidence)
            host = mock.Mock()
            host.create.side_effect = RuntimeError("response lost")
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                catalog.create_recorded(host, catalog.GRAPH[0],
                                        catalog.manifest_set()[0], evidence,
                                        evidence_file)
            self.assertEqual(json.loads(evidence_file.read_text())["attempted"],
                             {"flux-system/service-catalog": None})

    def test_partial_stage_refuses_project_data_before_flux_prune(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory) / "journal.json"
            evidence = {"run_id": "b" * 24, "attempted": {}, "complete": False}
            host = mock.Mock()
            host.optional.return_value = None
            milo = mock.Mock()
            milo.json.return_value = {"items": [{"metadata": {"name": "consumer"}}]}
            with mock.patch.object(catalog, "APIProxy") as proxy:
                with self.assertRaisesRegex(RuntimeError, "remove owned Projects"):
                    catalog.cleanup(host, milo, evidence, evidence_file)
                proxy.assert_not_called()

    def test_missing_full_flux_journal_retains_sources_without_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_file = Path(directory) / "journal.json"
            evidence = {"run_id": "c" * 24, "attempted": {
                catalog.journal_key(row): "uid-" + str(index)
                for index, row in enumerate(catalog.GRAPH)}, "complete": False}
            host = mock.Mock()
            host.optional.side_effect = lambda kind, name, namespace=None: None if kind == "namespace" else {
                "metadata": {"uid": evidence["attempted"][namespace + "/" + name],
                             "resourceVersion": "9"}}
            milo = mock.Mock()
            milo.json.return_value = {"items": []}
            milo.optional.return_value = None
            with mock.patch.object(catalog, "root_inventory", return_value=(set(), set())):
                with mock.patch.object(catalog, "APIProxy") as proxy_class:
                    proxy_class.return_value.delete.side_effect = RuntimeError("precondition failed")
                    with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                        catalog.cleanup(host, milo, evidence, evidence_file)
                    self.assertEqual(proxy_class.return_value.delete.call_count, 0)
            self.assertEqual(json.loads(evidence_file.read_text())["cleanup"],
                             "incomplete")

    def test_unknown_uid_requires_exact_intent_and_persists_before_delete(self):
        row = catalog.GRAPH[0]
        manifest = catalog.manifest_set()[0]
        manifest["metadata"]["labels"] = {catalog.RUN_LABEL: "c" * 24}
        digest = catalog.intent_digest(manifest)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            evidence = {"run_id": "c" * 24,
                        "attempted": {catalog.journal_key(row): None},
                        "intents": {catalog.journal_key(row): digest}}
            catalog.atomic_json(path, evidence)
            host = mock.Mock()
            actual = copy.deepcopy(manifest)
            actual["metadata"].update({"uid": "owned", "resourceVersion": "7"})
            host.optional.side_effect = [actual, None]
            proxy = mock.Mock()
            proxy.delete.side_effect = lambda *_: self.assertEqual(
                json.loads(path.read_text())["attempted"][catalog.journal_key(row)], "owned")
            catalog.delete_recorded(host, proxy, row, evidence, path)
            proxy.delete.assert_called_once()
            evidence["attempted"][catalog.journal_key(row)] = None
            actual["spec"]["url"] = "https://foreign.example/repo"
            host.optional.side_effect = [actual]
            proxy.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "unknown-UID"):
                catalog.delete_recorded(host, proxy, row, evidence, path)
            proxy.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
