"""Offline failure gates for the source-owned LocationBinding prerequisite."""

import copy
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import yaml


SOURCE = Path(__file__).with_name("bootstrap-apps-native-locationbinding-crd.py")
spec = importlib.util.spec_from_file_location("locationbinding_fixture", SOURCE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
INSTALLER_SOURCE = Path(__file__).with_name("bootstrap-apps-native-catalog-installer.py")
installer_spec = importlib.util.spec_from_file_location("catalog_installer_source", INSTALLER_SOURCE)
installer_source = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer_source)


class RenderContractTests(unittest.TestCase):
    def test_predecessor_transition_requires_same_locations_run(self):
        identity = {"fixture": "owned"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            private = root / "apps-a1t-locations-private"
            private.mkdir(mode=0o700)
            installer_path = root / "installer.json"
            locations_path = root / "locations.json"
            catalog_path = root / "catalog.json"
            transition_path = root / "transition.json"
            rows = {
                locations_path: {"source": fixture.LOCATIONS_SHA,
                                 "cleanup": "not-requested", "setup_stage": "ready",
                                 "scope_identity": identity, "run_id": "a" * 24,
                                 "installer_run_id": "b" * 24},
                private / "state.json": {"run_id": "a" * 24,
                                          "auth_intent": True,
                                          "old_b64": "catalog-csv",
                                          "new_b64": "locations-csv"},
                catalog_path: {"complete": True, "cleanup": "not-requested",
                               "catalog_source": fixture.CATALOG_SHA,
                               "billing_digest": fixture.BILLING_DIGEST,
                               "scope_identity": identity,
                               "installer_run_id": "b" * 24},
                transition_path: {"locations_run_id": "c" * 24},
            }
            for path, row in rows.items():
                path.write_text(json.dumps(row))
                path.chmod(0o600)
            with mock.patch.object(fixture, "active_catalog_installer",
                                   return_value=({"run_id": "b" * 24},
                                                 {"new_b64": "catalog-csv"})), \
                 mock.patch.object(fixture, "verified_live_transition") as verified:
                with self.assertRaisesRegex(RuntimeError, "does not bind"):
                    fixture.live_dependencies(object(), object(), identity,
                        installer_path, catalog_path, locations_path,
                        transition_path, {})
                verified.assert_not_called()
                transition_path.write_text(json.dumps({"locations_run_id": "a" * 24}))
                verified.side_effect = RuntimeError("reached verified predecessor")
                with self.assertRaisesRegex(RuntimeError, "reached verified predecessor"):
                    fixture.live_dependencies(object(), object(), identity,
                        installer_path, catalog_path, locations_path,
                        transition_path, {})

    def test_clean_revision_requires_valid_signature(self):
        class Git:
            def __init__(self, reject=False):
                self.calls = []
                self.reject = reject
            def run(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                if "verify-commit" in args and self.reject:
                    raise RuntimeError("signature rejected")
                return "1" * 40 if "rev-parse" in args else ""
        accepted = Git()
        self.assertEqual(fixture.clean_revision(accepted), "1" * 40)
        self.assertTrue(any("verify-commit" in args and kwargs.get("sensitive")
                            for args, kwargs in accepted.calls))
        with self.assertRaisesRegex(RuntimeError, "signature rejected"):
            fixture.clean_revision(Git(reject=True))

    def test_exact_source_single_crd_and_run_label(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        rendered = docs["flux_contracts"]["stage"]["rendered"]
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["metadata"]["name"], fixture.CRD_NAME)
        self.assertEqual(rendered[0]["metadata"]["labels"][fixture.RUN_LABEL], "2" * 24)
        self.assertEqual(docs["source"]["spec"]["ref"], {"commit": "1" * 40})
        self.assertEqual(docs["role"]["rules"][0]["resourceNames"], [fixture.CRD_NAME])

    def test_unreviewed_crd_content_rejected_before_cluster_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kustomization.yaml").write_text(
                (fixture.SCHEMA_ROOT / "kustomization.yaml").read_text())
            source = fixture.SCHEMA_ROOT / "networking.datumapis.com_locationbindings.yaml"
            (root / source.name).write_bytes(source.read_bytes() + b"\n# changed\n")
            with mock.patch.object(fixture, "SCHEMA_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "CRD bytes changed"):
                    fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)

    def test_nonnamed_grant_rejected(self):
        original = fixture.COMPONENT
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            file = dest / "locationbinding-installer-rbac.yaml"
            file.write_text((original / file.name).read_text().replace(
                "resourceNames: [locationbindings.networking.datumapis.com]",
                "resourceNames: [foreign.networking.datumapis.com]"))
            with mock.patch.object(fixture, "COMPONENT", dest):
                with self.assertRaisesRegex(RuntimeError, "grant exceeds"):
                    fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)


class FakeMilo:
    def __init__(self, items=None, projects=None):
        self.items = items if items is not None else []
        self.projects = projects if projects is not None else list(fixture.PROJECTS)

    def json(self, *args):
        return {"items": [{"metadata": {"name": name}} for name in self.projects]}

    def k(self, *args):
        import json
        return json.dumps({"apiVersion": "networking.datumapis.com/v1alpha",
                           "kind": "LocationBindingList", "items": self.items})


class CleanupSafetyTests(unittest.TestCase):
    def test_failed_authorization_probe_records_only_operation_before_request(self):
        class Probe:
            calls = 0
            def can_i(self, *_):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("test-only SSAR failure")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            evidence = {"run_id": "a" * 24}
            def record(operation):
                evidence["authorization_probe"] = operation
                fixture.atomic_json(path, evidence)
            with self.assertRaisesRegex(RuntimeError, "SSAR failure"):
                fixture.authorization_probes(Probe(), record)
            observed = json.loads(path.read_text())
            self.assertEqual(observed["authorization_probe"], {
                "verb": "get",
                "resource": "customresourcedefinitions.apiextensions.k8s.io",
                "expected": False,
                "name": "foreign.networking.datumapis.com",
                "state": "started"})
            self.assertEqual(set(observed), {"run_id", "authorization_probe"})

    def test_installer_probe_uses_distinct_private_owned_files(self):
        ca = b"public test CA"
        source = installer_source.kubeconfig(
            base64.b64encode(ca).decode(), "test-only-token")
        class Host:
            def json(self, *args):
                return {"data": {"kubeconfig": base64.b64encode(
                    yaml.safe_dump(source).encode()).decode()}}
        identity = {"milo": {"ca_sha256": hashlib.sha256(ca).hexdigest()}}
        with tempfile.TemporaryDirectory() as directory:
            first_dir, first_path, first_context = fixture.installer_probe_config(
                Host(), identity, Path(directory), 43443)
            first_data = first_path.read_bytes()
            second_dir, second_path, second_context = fixture.installer_probe_config(
                Host(), identity, Path(directory), 45443)
            self.assertNotEqual(first_dir, second_dir)
            self.assertEqual(first_data, first_path.read_bytes())
            self.assertEqual(first_context, source["current-context"])
            self.assertEqual(second_context, source["current-context"])
            self.assertEqual(stat.S_IMODE(first_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(first_path.stat().st_mode), 0o600)
            self.assertEqual(yaml.safe_load(second_path.read_text())
                             ["clusters"][0]["cluster"]["server"],
                             "https://127.0.0.1:45443")
            changed = copy.deepcopy(source)
            changed["clusters"][0]["cluster"]["server"] = \
                "https://milo-apiserver.milo-system.svc:6443"
            class WrongHost:
                def json(self, *args):
                    return {"data": {"kubeconfig": base64.b64encode(
                        yaml.safe_dump(changed).encode()).decode()}}
            with self.assertRaisesRegex(RuntimeError, "CA or origin changed"):
                fixture.installer_probe_config(WrongHost(), identity,
                                               Path(directory), 43443)

    def test_foreign_project_or_binding_blocks_crd_prune(self):
        with self.assertRaisesRegex(RuntimeError, "foreign or missing Project"):
            fixture.no_locationbindings(FakeMilo(projects=["apps-a1t-provider"]))
        with self.assertRaisesRegex(RuntimeError, "existing LocationBinding"):
            fixture.no_locationbindings(FakeMilo(items=[{"metadata": {"name": "foreign"}}]))

    def test_source_drift_blocks_uid_known_delete(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        source = copy.deepcopy(docs["source"])
        source["metadata"]["labels"] = {fixture.RUN_LABEL: "2" * 24}
        saved = {"run_id": "2" * 24, "attempted": {"source": "uid-1"},
                 "intents": {"source": fixture.intent_digest(source)}}
        changed = copy.deepcopy(source)
        changed["spec"]["ref"] = {"commit": "3" * 40}
        changed["metadata"].update(uid="uid-1", resourceVersion="12")

        class Cluster:
            def optional(self, *_):
                return changed

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "evidence.json"
            with self.assertRaisesRegex(RuntimeError, "changed source payload"):
                fixture.delete_owned(Cluster(), object(), "source", docs["source"], saved,
                                     journal)

    def test_flux_failure_retains_grant_and_source(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        evidence = {"run_id": "2" * 24, "scope_identity": {},
                    "attempted": {"stage": "flux-uid", "role": "role-uid",
                                  "binding": "binding-uid", "source": "source-uid"},
                    "intents": {}, "cleanup": "not-requested",
                    "webhook_uids": {"services-mutating": "mutate-uid",
                                     "services-validating": "validate-uid"}}

        class Host:
            pass

        class Milo:
            def optional(self, kind, name):
                if kind == "crd":
                    return None
                raise AssertionError("grant was touched after Flux failure")

        class Proxy:
            def __init__(self, *_):
                pass
            def start(self):
                pass
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            with mock.patch.object(fixture, "APIProxy", Proxy), \
                 mock.patch.object(fixture, "writer_quiescent"), \
                 mock.patch.object(fixture, "delete_stage", side_effect=RuntimeError("drift")), \
                 mock.patch.object(fixture, "delete_owned") as delete:
                with self.assertRaisesRegex(RuntimeError, "Flux prune failed"):
                    fixture.cleanup(Host(), Milo(), evidence, path, docs, {})
                delete.assert_not_called()
            self.assertEqual(evidence["cleanup"], "incomplete")

    def test_partial_flux_apply_fallback_deletes_only_recorded_crd_after_stage_absent(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        crd = copy.deepcopy(docs["flux_contracts"]["stage"]["rendered"][0])
        crd["metadata"].update(uid="crd-uid", resourceVersion="9")
        evidence = {"run_id": "2" * 24, "scope_identity": {},
                    "attempted": {"stage": "stage-uid"}, "intents": {},
                    "cleanup": "not-requested",
                    "webhook_uids": {"services-mutating": "mutate-uid",
                                     "services-validating": "validate-uid"}}
        events = []

        class Host:
            def optional(self, *_):
                return None

        class Milo:
            exists = True
            def optional(self, kind, name):
                self_name = (kind, name)
                self.assert_crd(self_name)
                return crd if self.exists else None
            def assert_crd(self, pair):
                assert pair == ("crd", fixture.CRD_NAME)
            def json(self, *_):
                return crd

        milo = Milo()
        class Proxy:
            def __init__(self, kube, *_):
                self.kube = kube
            def start(self):
                pass
            def close(self):
                pass
            def delete(self, path, uid, rv):
                events.append(("delete", path, uid, rv))
                milo.exists = False

        def stage_deleted(*_):
            events.append(("stage-absent",))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            with mock.patch.object(fixture, "APIProxy", Proxy), \
                 mock.patch.object(fixture, "writer_quiescent"), \
                 mock.patch.object(fixture, "delete_stage", side_effect=stage_deleted), \
                 mock.patch.object(fixture, "no_locationbindings") as empty:
                fixture.cleanup(Host(), milo, evidence, path, docs, {})
            self.assertEqual(evidence["cleanup"], "complete")
            self.assertEqual(evidence["crd_uid"], "crd-uid")
            self.assertEqual(evidence["fallback_crd_delete_intent"], "crd-uid")
            self.assertEqual(events[0], ("stage-absent",))
            self.assertEqual(events[1][2:], ("crd-uid", "9"))
            self.assertEqual(empty.call_count, 2)

    def test_writer_or_changed_webhook_ca_blocks_prune(self):
        class Host:
            operator = False
            pods = False
            def optional(self, kind, name, namespace=None):
                if kind == "kustomization" and self.operator:
                    return {"metadata": {"name": name}}
                return None
            def json(self, *args):
                return {"items": [{"metadata": {"name": "manager"}}] if self.pods else []}
        class Milo:
            bad_ca = False
            def json(self, *args):
                name = args[-1]
                count = 2 if name == "services-mutating" else 6
                return {"metadata": {"uid": name + "-uid"},
                        "webhooks": [{"failurePolicy": "Fail", "clientConfig": {
                            "service": {"name": "services-webhook",
                                        "namespace": "services-system", "port": 443},
                            "caBundle": "changed" if self.bad_ca else ""}}
                            for _ in range(count)]}
        host, milo = Host(), Milo()
        uids = fixture.writer_quiescent(host, milo)
        self.assertEqual(uids["services-mutating"], "services-mutating-uid")
        host.operator = True
        with self.assertRaisesRegex(RuntimeError, "still can start a writer"):
            fixture.writer_quiescent(host, milo, uids)
        host.operator, host.pods = False, True
        with self.assertRaisesRegex(RuntimeError, "manager workload still exists"):
            fixture.writer_quiescent(host, milo, uids)
        host.pods, milo.bad_ca = False, True
        with self.assertRaisesRegex(RuntimeError, "empty-CA state"):
            fixture.writer_quiescent(host, milo, uids)
        milo.bad_ca = False
        with self.assertRaisesRegex(RuntimeError, "webhook UID changed"):
            fixture.writer_quiescent(host, milo,
                {"services-mutating": "replaced-uid",
                 "services-validating": "services-validating-uid"})

    def test_grant_probes_include_foreign_name_denials(self):
        calls = []
        class Probe:
            def can_i(self, *args):
                calls.append(args)
        fixture.authorization_probes(Probe())
        denied = [call for call in calls if call[2] is False]
        self.assertEqual({call[0] for call in denied if len(call) == 4},
                         {"get", "patch", "update", "delete"})
        self.assertIn(("get", "secrets", False), denied)
        self.assertIn(("impersonate", "users", False), denied)

    def test_known_crd_replacement_and_foreign_label_block_prune(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        expected = copy.deepcopy(docs["flux_contracts"]["stage"]["rendered"][0])
        expected["metadata"].update(uid="observed-uid", resourceVersion="7")
        evidence = {"run_id": "2" * 24, "crd_uid": "earlier-uid"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            with self.assertRaisesRegex(RuntimeError, "replaced LocationBinding"):
                fixture.validate_owned_crd(expected, evidence, docs, path)
            self.assertFalse(path.exists())
            evidence["crd_uid"] = None
            expected["metadata"]["labels"][fixture.RUN_LABEL] = "foreign"
            with self.assertRaisesRegex(RuntimeError, "foreign or changed"):
                fixture.validate_owned_crd(expected, evidence, docs, path)
            self.assertFalse(path.exists())

    def test_crd_spec_drift_blocks_prune(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        original = copy.deepcopy(docs["flux_contracts"]["stage"]["rendered"][0])
        original["metadata"].update(uid="owned-uid", resourceVersion="7")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            for changed in ({"conversion": {"strategy": "Webhook"}},
                            {"futureField": {"unsafe": True}}):
                current = copy.deepcopy(original)
                current["spec"].update(changed)
                with self.assertRaisesRegex(RuntimeError, "changed LocationBinding"):
                    fixture.validate_owned_crd(current,
                        {"run_id": "2" * 24, "crd_uid": "owned-uid"}, docs, path)
                self.assertFalse(path.exists())
            defaulted = copy.deepcopy(original)
            defaulted["spec"]["conversion"] = {"strategy": "None"}
            fixture.validate_owned_crd(defaulted,
                {"run_id": "2" * 24, "crd_uid": "owned-uid"}, docs, path)
            self.assertFalse(path.exists())

    def test_unknown_crd_uid_is_persisted_before_future_prune(self):
        docs = fixture.manifests(fixture.Commands(), "1" * 40, "2" * 24)
        expected = copy.deepcopy(docs["flux_contracts"]["stage"]["rendered"][0])
        expected["metadata"].update(uid="recovered-uid", resourceVersion="7")
        evidence = {"run_id": "2" * 24, "crd_uid": None}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            fixture.validate_owned_crd(expected, evidence, docs, path)
            self.assertEqual(evidence["crd_uid"], "recovered-uid")
            self.assertIn('"crd_uid":"recovered-uid"', path.read_text())


if __name__ == "__main__":
    unittest.main()
