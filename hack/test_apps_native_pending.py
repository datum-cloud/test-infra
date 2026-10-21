"""Offline pending trial failure controls; not real Milo admission evidence."""
import base64
import copy
import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("pending_trial_harness", Path(__file__).with_name("test-apps-native-authoring.py"))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
p = h.pending_trial


def fixture():
    desired = {"templateRef": {"projectRevision": {"projectUID": "project-uid", "templateName": "source",
        "templateUID": "source-uid", "revisionName": "revision", "revisionUID": "revision-uid", "digest": "sha256:" + "1" * 64}},
        "inputsJSON": "{}", "inputsDigest": p.digest({}), "uninstallPolicy": "Delete"}
    intent = {"name": "pending", "project": "project", "project_uid": "project-uid", "spec": desired, "nonce": "nonce"}
    obj = {"apiVersion": "apps.miloapis.com/v1alpha1", "kind": "App", "metadata": {
        "name": "pending", "uid": "app-uid", "resourceVersion": "7", "labels": {h.RUN_LABEL: "run", p.INTENT_LABEL: "nonce"},
        "annotations": p.annotations(desired, "project-uid"), "finalizers": [p.FINALIZER]}, "spec": desired,
        "status": {"projectUID": "project-uid", "admittedGeneration": 1, "phase": "Pending"}}
    return intent, obj


class Proxy:
    def __init__(self, apps=None, revisions=None, project_uid="project-uid"):
        self.apps, self.revisions = apps or [], revisions or []
        self.project_uid = project_uid
        self.deleted = []
    def get(self, path):
        if path == h.PROJECT_API:
            return {"items": [{"metadata": {"name": "project", "uid": self.project_uid}}]}
        if path.endswith("/apprevisions"):
            return {"items": self.revisions}
        if path.endswith("/apps"):
            return {"items": self.apps}
        raise RuntimeError("unexpected test request")
    def delete(self, path, uid, rv):
        self.deleted.append((path, uid, rv))
        self.apps = []
    def close(self):
        pass


class Host:
    def __init__(self):
        self.patches = []
    def optional(self, *a):
        return None
    def patch_json(self, *a, **kw):
        self.patches.append((a, kw))


class Owner:
    def __init__(self):
        self.calls = 0
    def delete(self, *a):
        self.calls += 1


class PendingControls(unittest.TestCase):
    def test_pending_flux_ca_lost_reply_recovers_exact_journal_and_blocks_drift(self):
        from test_apps_native_flux_prune import NativeAPI, CA, REVISION, MANIFEST, RENDER
        api = NativeAPI()
        manifest = copy.deepcopy(MANIFEST)
        manifest["metadata"]["name"] = p.STAGE
        manifest["spec"]["path"] = "./config/experimental/pending-app-api"
        hook = copy.deepcopy(RENDER[1])
        hook["metadata"]["name"] = p.WC
        crd = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
               "metadata": {"name": p.CRDS[0]}}
        source = {"pending": {"crds": [crd], "admission": [hook]}}
        fixtures = {"pending": {"api": [manifest]}}
        refs = {}
        with tempfile.TemporaryDirectory() as d:
            state = {"host": api, "run_id": "run", "private": Path(d),
                     "pending_evidence_file": Path(d) / "evidence.json"}
            live = copy.deepcopy(hook)
            live["metadata"]["labels"] = {h.RUN_LABEL: "run"}
            live["webhooks"][0]["clientConfig"]["caBundle"] = base64.b64encode(CA).decode()
            state["milo"] = SimpleNamespace(json=lambda *args: live)
            with patch.object(h, "flux_reconcile"), patch.object(h, "exact_flux"), \
                 patch.object(p, "check_crds", return_value={"checked": True}):
                p.install_api(h, fixtures, source, state, refs, REVISION)
                self.assertIsInstance(refs["pending-api"][0], h.FluxOwned)
                api.lost_patch = True
                with self.assertRaisesRegex(RuntimeError, "patch response lost"):
                    p.enable_hooks(h, source, state, refs, CA, REVISION)
                journal = Path(d) / "evidence-flux-pending-api.json"
                saved = json.loads(journal.read_text())
                self.assertEqual(saved["uid"], "uid")
                api.lost_patch = False
                p.enable_hooks(h, source, state, refs, CA, REVISION)
                self.assertEqual(len(api.patch_calls), 1)
                self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
                api.obj["spec"]["kubeConfig"]["secretRef"]["name"] = "foreign"
                with self.assertRaises(RuntimeError):
                    refs["pending-api"][0].delete(api, "run")
                self.assertFalse(api.delete_calls)

    def test_pending_flux_foreign_inventory_never_prunes_even_known_uid(self):
        from test_apps_native_flux_prune import NativeAPI, REVISION, MANIFEST, RENDER
        api, refs = NativeAPI(), {}
        with tempfile.TemporaryDirectory() as d:
            h.register_flux_owned(refs, "pending-api", api, MANIFEST, "run", RENDER,
                                  REVISION, Path(d) / "evidence.json")
            api.obj["status"] = {"inventory": {"entries": [
                {"id": "_foreign__Namespace", "v": "v1"}]}}
            with self.assertRaises(RuntimeError):
                refs["pending-api"][0].delete(api, "run")
            self.assertFalse(api.delete_calls)

    def test_ci_runs_pending_failures_and_triggers_for_pending_sources(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/apps-native-authoring.yml").read_text())
        scripts = [step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", [])]
        self.assertTrue(any("-p 'test_apps_native_pending.py' -q" in script for script in scripts))
        offline = yaml.safe_load((root / ".github/workflows/apps-native-offline.yml").read_text())
        events = offline.get("on", offline.get(True))  # PyYAML YAML1.1 'on'
        expected = {"components/apps-native-pending/**", "hack/apps_native_pending.py",
                    "hack/apps_native_profiles.py", "hack/test_apps_native_pending.py"}
        for event in ("pull_request", "push"):
            self.assertLessEqual(expected, set(events[event]["paths"]))

    def test_closed_profiles_preserve_exact_default(self):
        self.assertIs(h.selected(False), h.AUTHORING)
        self.assertEqual(h.AUTHORING.binary, h.APPS_BINARY_SHA)
        self.assertEqual(h.AUTHORING.image, h.APPS_IMAGE)
        self.assertEqual(dict(h.AUTHORING.platforms), h.PLATFORM_DIGESTS)
        self.assertIs(h.selected(True), h.PENDING)
        with self.assertRaises(TypeError):
            h.PENDING.platforms["arm64"] = ("changed", "changed")
        with self.assertRaises(Exception):
            h.PENDING.image = "moving:tag"
        h.fixture_contract(h.expected_fixture())
        h.source_role_guards(h.expected_fixture())

    def test_group_token_is_one_csv_field_and_only_pending_user(self):
        old = base64.b64encode(b"admin-token,admin,admin,system:masters\n").decode()
        rows = list(csv.reader(base64.b64decode(h.token_csv(old, {p.REQUESTER: "new", h.AUTHOR: "author"}, pending=True)).decode().splitlines()))
        self.assertEqual(rows[-2], ["new", p.REQUESTER, p.REQUESTER, "system:authenticated," + p.GROUP])
        self.assertEqual(rows[-1][-1], "system:authenticated")
        normal = h.token_csv(old, {h.AUTHOR: "author"})
        self.assertNotIn(p.GROUP, base64.b64decode(normal).decode())

    def test_authoring_annotation_is_bound_to_source_owned_key(self):
        self.assertEqual(h.AUTHORING_PROJECT_ANNOTATION, "apps.miloapis.com/authoring-project-uid")
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            controller = source / "internal/templatecontroller/controller.go"
            controller.parent.mkdir(parents=True)
            controller.write_text('const ProjectAnnotation = "apps.miloapis.com/authoring-project-uid"')
            h.authoring_annotation_contract(source)
            controller.write_text('const ProjectAnnotation = "apps.miloapis.com/project-uid"')
            with self.assertRaises(RuntimeError):
                h.authoring_annotation_contract(source)

    def test_interrupted_discovery_restore_retains_entire_chain(self):
        state, _ = self.state()
        state.update(host=Host(), milo=Host(), pending_api_intent=True, crds_installed=False,
                     pending_evidence={"discovery_get_probe": {"restored": False}})
        refs = {n: [Owner()] for n in ("pending-bindings", "lease", "admission", "host", "pending-api", "crds", "rbac")}
        with tempfile.TemporaryDirectory() as d:
            h.private_write(Path(d) / "evidence.json", "{}")
            errors = h.cleanup(SimpleNamespace(), h.Commands(), state, refs, Proxy(), Proxy(), [], {}, Path(d) / "evidence.json")
        self.assertTrue(errors)
        self.assertTrue(all(ref[0].calls == 0 for ref in refs.values()))

    def test_held_lease_records_intent_and_uses_uid_rv_spec_cas(self):
        role = {"metadata": {"uid": "lease", "resourceVersion": "12"},
                "spec": {"holderIdentity": "controller", "leaseDurationSeconds": 15}}
        class Cluster:
            def json(self, _get, kind, *args):
                if kind == "namespace":
                    return {"metadata": {"uid": "namespace", "labels": {h.RUN_LABEL: "run"}}}
                return copy.deepcopy(role)
            def patch_json(self, _private, _kind, _name, ops, _namespace):
                journal = json.loads(state["pending_evidence_file"].read_text())
                assert journal["pending_leader_hold"]["lease_uid"] == "lease"
                assert ops[:3] == [{"op": "test", "path": "/metadata/uid", "value": "lease"},
                                    {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
                                    {"op": "test", "path": "/spec", "value": role["spec"]}]
                assert ops[3]["value"]["holderIdentity"] == "run"
        refs = {"namespace": [SimpleNamespace(kind="namespace", name="apps-system", uid="namespace")]}
        state = {"host": Cluster(), "run_id": "run", "private": Path("/private"), "pending_intents": []}
        with tempfile.TemporaryDirectory() as d:
            state["pending_evidence_file"] = Path(d) / "journal.json"
            held = p.hold_controller(h, state, refs, {}, state["pending_evidence_file"])
        self.assertEqual(held.uid, "lease")
        self.assertTrue(held.intent)
        self.assertEqual(refs["lease"], [held])

    def test_extra_roles_exact_no_native_or_secret_privileges(self):
        roles = {o["metadata"]["name"]: o["rules"] for o in h.load_yaml(h.ROOT / "components/apps-native-pending/milo-rbac.yaml") if o["kind"] == "ClusterRole"}
        self.assertEqual(roles, p.expected_rules())
        for rules in roles.values():
            for rule in rules:
                self.assertNotIn("*", json.dumps(rule))
                self.assertNotIn("secrets", rule["resources"])
                self.assertNotIn("impersonate", rule["verbs"])
        self.assertEqual(roles[p.GROUP], [{"apiGroups": ["apps.miloapis.com"], "resources": ["apptemplates", "apptemplaterevisions"], "verbs": ["get"]}])

    def test_lost_reply_matches_exact_payload_but_not_replacement(self):
        intent, obj = fixture()
        self.assertEqual(p.match_intent(obj, intent, "run"), "app-uid")
        intent["uid"] = "known-old-uid"
        with self.assertRaisesRegex(RuntimeError, "unknown or changed"):
            p.match_intent(obj, intent, "run")

    def test_lost_reply_rejects_ambiguous_payload_and_gc_linkage(self):
        mutations = [lambda o: o["metadata"].update(ownerReferences=[{"uid": "foreign"}]),
                     lambda o: o["metadata"]["finalizers"].append("foreign/finalizer"),
                     lambda o: o["metadata"]["annotations"].update({"unexpected": "value"}),
                     lambda o: o["metadata"]["labels"].update({"foreign": "value"}),
                     lambda o: o["spec"].update(uninstallPolicy="Retain"),
                     lambda o: o["status"].update(phase="Accepted"),
                     lambda o: o["metadata"].update(finalizers=[])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                intent, obj = fixture()
                mutate(obj)
                with self.assertRaises(RuntimeError):
                    p.match_intent(obj, intent, "run")

    def state(self):
        intent, obj = fixture()
        return {"run_id": "run", "projects": {"project": "project-uid"}, "pending_intents": [intent]}, obj

    def test_census_rejects_project_reuse_unknown_app_or_any_revision(self):
        state, obj = self.state()
        self.assertEqual(len(p.census(h, Proxy([obj]), state)), 1)
        for proxy in (Proxy([obj], project_uid="replacement-project"), Proxy([obj], [{"metadata": {"uid": "any"}}])):
            with self.assertRaises(RuntimeError):
                p.census(h, proxy, state)
            self.assertFalse(proxy.deleted)
        state["pending_intents"][0]["uid"] = "old-app"
        with self.assertRaises(RuntimeError):
            p.census(h, Proxy([obj]), state)

    def test_missing_resource_list_is_not_empty(self):
        state, _ = self.state()
        class Unavailable(Proxy):
            def get(self, path):
                if path.endswith("/apprevisions"):
                    raise RuntimeError("404 or unavailable")
                return super().get(path)
        with self.assertRaises(RuntimeError):
            p.census(h, Unavailable(), state)

    def test_drain_revokes_before_census_and_uid_rv_delete(self):
        state, obj = self.state()
        proxy = Proxy([obj])
        events = []
        with patch.object(h, "delete_group", side_effect=lambda _r, g, *_a: events.append(g)), \
             patch.object(p, "census", wraps=p.census) as census:
            p.drain(h, state, {}, Host(), proxy)
        self.assertEqual(events, ["pending-bindings", "lease"])
        self.assertGreaterEqual(census.call_count, 2)
        self.assertEqual(proxy.deleted, [(h.project_path("project", "apps", "pending"), "app-uid", "7")])

    def test_unknown_pending_retains_all_capability_stages(self):
        state, obj = self.state()
        state.update(host=Host(), milo=Host(), pending_api_intent=True, crds_installed=False)
        state["pending_intents"] = []
        refs = {n: [Owner()] for n in ("admission", "host", "pending-api", "crds", "installer-secret", "rbac")}
        with tempfile.TemporaryDirectory() as d:
            h.private_write(Path(d) / "evidence.json", "{}")
            errors = h.cleanup(SimpleNamespace(), h.Commands(), state, refs, Proxy(), Proxy([obj]), [], {}, Path(d) / "evidence.json")
        self.assertTrue(errors)
        self.assertTrue(all(ref[0].calls == 0 for ref in refs.values()))

    def test_pending_stage_failure_retains_manager_and_credentials(self):
        state, _ = self.state()
        state.update(host=Host(), milo=Host(), pending_api_intent=True, crds_installed=False)
        refs = {n: [Owner()] for n in ("host", "crds", "installer-secret", "rbac")}
        with tempfile.TemporaryDirectory() as d, patch.object(p, "drain"), patch.object(p, "prune_api", side_effect=RuntimeError("partial prune")):
            h.private_write(Path(d) / "evidence.json", "{}")
            errors = h.cleanup(SimpleNamespace(), h.Commands(), state, refs, Proxy(), Proxy(), [], {}, Path(d) / "evidence.json")
        self.assertTrue(errors)
        self.assertTrue(all(ref[0].calls == 0 for ref in refs.values()))

    def test_failed_revocation_does_not_delete_data(self):
        state, obj = self.state()
        proxy = Proxy([obj])
        with patch.object(h, "delete_group", side_effect=RuntimeError("conflict")):
            with self.assertRaises(RuntimeError):
                p.drain(h, state, {}, Proxy(), proxy)
        self.assertFalse(proxy.deleted)

    def test_lost_reply_uid_is_journaled_before_delete(self):
        state, obj = self.state()
        events = []
        class Observed(Proxy):
            def delete(self, path, uid, rv):
                self_uid = json.loads(state["pending_evidence_file"].read_text())["pending_intents"][0]["uid"]
                events.append(self_uid)
                super().delete(path, uid, rv)
        with tempfile.TemporaryDirectory() as d:
            state["pending_evidence_file"] = Path(d) / "journal.json"
            state["pending_evidence"] = {"run_id": "run"}
            with patch.object(h, "delete_group"):
                p.drain(h, state, {}, Proxy(), Observed([obj]))
            self.assertEqual(state["pending_evidence_file"].stat().st_mode & 0o777, 0o600)
        self.assertEqual(events, ["app-uid"])

    def test_journal_failure_prevents_destructive_request(self):
        state, obj = self.state()
        proxy = Proxy([obj])
        state.update(pending_evidence_file=Path("/unused"), pending_evidence={})
        with patch.object(h, "delete_group"), patch.object(p, "persist_intents", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                p.drain(h, state, {}, Proxy(), proxy)
        self.assertFalse(proxy.deleted)

    def test_unavailable_probe_restores_exact_role_after_assertion_failure(self):
        rules = [{"apiGroups": ["resourcemanager.miloapis.com"], "resources": ["projects"], "verbs": ["get", "list", "watch"]}]
        role = {"kind": "ClusterRole", "metadata": {"name": h.DISCOVERY, "uid": "role", "resourceVersion": "1", "labels": {h.RUN_LABEL: "run"}}, "rules": rules}
        class Roles:
            def __init__(self):
                self.role = copy.deepcopy(role)
                self.patches = []
            def json(self, *args):
                return copy.deepcopy(self.role)
            def patch_json(self, _private, _kind, _name, ops):
                self.patches.append(ops)
                assert ops[0] == {"op": "test", "path": "/metadata/uid", "value": "role"}
                assert ops[1]["value"] == self.role["metadata"]["resourceVersion"]
                assert ops[2]["value"] == self.role["rules"]
                self.role["rules"] = ops[3]["value"]
                self.role["metadata"]["resourceVersion"] = str(int(self.role["metadata"]["resourceVersion"]) + 1)
        roles = Roles()
        state = {"milo": roles, "run_id": "run", "private": Path("/unused"), "pending_intents": []}
        ref = SimpleNamespace(kind="clusterrole", name=h.DISCOVERY, uid="role")
        fixtures = {"milo-rbac": [role]}
        # Deliberately wrong denial: even this test failure must restore the role.
        with patch.object(p, "persist_intents"), patch.object(p, "wait_for", return_value=True), \
             patch.object(h, "milo_request", return_value=(500, {})):
            with self.assertRaisesRegex(RuntimeError, "not denied"):
                p.unavailable_project_probe(h, SimpleNamespace(direct_milo_port=1), state, {"rbac": [ref]}, fixtures,
                    {h.DISCOVERY: "token", p.REQUESTER: "token"}, Path("/ca"), {}, {}, Path("/journal"))
        self.assertEqual(roles.role["rules"], rules)
        self.assertEqual(len(roles.patches), 2)

    def test_api_stage_is_only_apps_crd_owner_and_early_host_dependency(self):
        api = h.load_yaml(h.ROOT / "components/apps-native-pending/api.yaml")[0]
        host = h.load_yaml(h.ROOT / "components/apps-native-pending/host.yaml")[0]
        self.assertEqual(api["spec"]["path"], "./config/experimental/pending-app-api")
        self.assertEqual(api["spec"]["kubeConfig"]["secretRef"]["name"], h.INSTALLER_SECRET)
        self.assertIn({"name": p.STAGE, "namespace": "milo-system"}, host["spec"]["dependsOn"])
        self.assertEqual(host["spec"]["images"][0]["digest"], h.PENDING.image.split("@")[1])
        self.assertEqual({o["kind"] for o in h.load_yaml(h.ROOT / "components/apps-native-pending/host.yaml")}, {"Kustomization"})


if __name__ == "__main__":
    unittest.main()
