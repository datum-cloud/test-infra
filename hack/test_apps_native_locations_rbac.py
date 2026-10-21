"""Native exact-source Locations RBAC bootstrap; no remote admin credential."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import yaml
from test_bootstrap_apps_native_locations import locations
from test_apps_native_flux_prune import NativeAPI


class RBACAPI(NativeAPI):
    def create(self, manifest, namespace, **kw):
        lost, self.lost_create = self.lost_create, False
        super().create(manifest, namespace, **kw)
        self.obj["metadata"].pop("generation")
        if lost:
            raise RuntimeError("response lost")
        return copy.deepcopy(self.obj)


class LocationsRBAC(unittest.TestCase):
    def fixture(self):
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                "metadata": {"name": "locations-manager"}, "rules": [{"apiGroups": ["locations.miloapis.com"],
                "resources": ["locations"], "verbs": ["get", "list", "watch"]}]}
        binding = {"apiVersion": role["apiVersion"], "kind": "ClusterRoleBinding", "metadata": role["metadata"],
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "locations-manager"},
                   "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": locations.USER}]}
        return {"rbac-role": role, "rbac-binding": binding}

    def test_known_and_lost_reply_cleanup_validates_complete_rbac_and_journals_uid(self):
        for row in (locations.ROWS[1], locations.ROWS[7]):
            for lost in (False, True):
                with self.subTest(kind=row[1], lost=lost), tempfile.TemporaryDirectory() as d:
                    api, evidence, source = RBACAPI(), {"run_id": "run", "attempted": {}}, self.fixture()
                    api.lost_create = lost
                    path = Path(d) / "journal.json"
                    try:
                        locations.row_create(api, row, source[row[0]], evidence, path, rbac_manifests=source)
                    except RuntimeError:
                        if not lost:
                            raise
                    def before_delete():
                        self.assertEqual(json.loads(path.read_text())["attempted"][row[0]], "uid")
                    api.before_delete = before_delete
                    locations.owned_delete(api, api, row, json.loads(path.read_text()), path, rbac_manifests=source)
                    self.assertEqual(api.delete_calls[0][1:], ("uid", "1"))

    def test_known_and_unknown_payload_drift_blocks_delete(self):
        for row in (locations.ROWS[1], locations.ROWS[7]):
            for lost in (False, True):
                for mutation in ("privilege", "owner", "annotation", "replacement"):
                    if lost and mutation == "replacement":
                        continue  # no known UID exists after a lost CREATE reply
                    with self.subTest(kind=row[1],lost=lost,mutation=mutation), tempfile.TemporaryDirectory() as d:
                        api, evidence, source = RBACAPI(), {"run_id": "run", "attempted": {}}, self.fixture()
                        api.lost_create = lost
                        path = Path(d) / "journal.json"
                        try:
                            locations.row_create(api, row, source[row[0]], evidence, path, rbac_manifests=source)
                        except RuntimeError:
                            if not lost:
                                raise
                        if mutation == "privilege":
                            if row[1] == "clusterrole":
                                api.obj["rules"][0]["verbs"] = ["*"]
                            else:
                                api.obj["subjects"][0]["name"] = "foreign"
                        elif mutation == "owner":
                            api.obj["metadata"]["ownerReferences"] = [{"uid": "foreign"}]
                        elif mutation == "annotation":
                            api.obj["metadata"]["annotations"] = {"foreign": "true"}
                        else:
                            api.obj["metadata"]["uid"] = "replacement"
                        with self.assertRaises(RuntimeError):
                            locations.owned_delete(api, api, row, evidence, path, rbac_manifests=source)
                        self.assertFalse(api.delete_calls)

    def test_no_legacy_remote_role_stage_or_credential_can_reenter(self):
        root = locations.ROOT / "components/locations"
        self.assertFalse((root / "milo-rbac.yaml").exists())
        for file in root.glob("*.yaml"):
            text = file.read_text()
            self.assertNotIn("milo-controller-manager-kubeconfig", text)
            self.assertNotIn("test-admin-token", text)
            self.assertNotIn("apps-a1t-locations-rbac", text)
            if "kubeConfig:" in text:
                self.fail("Locations fixture contains remote Flux credentials")
        self.assertEqual([row[0] for row in locations.ROWS if row[1] == "kustomization"], ["host"])
        self.assertEqual(locations.ROWS[1][1:4], ("clusterrole", "locations-manager", None))
        self.assertEqual(locations.ROWS[7][1:4], ("clusterrolebinding", "locations-manager", None))

    def test_create_without_verified_source_contract_makes_no_api_call(self):
        with tempfile.TemporaryDirectory() as d:
            api = RBACAPI()
            with self.assertRaises(RuntimeError):
                locations.row_create(api, locations.ROWS[1], self.fixture()["rbac-role"],
                    {"run_id": "run", "attempted": {}}, Path(d)/"journal.json")
            self.assertIsNone(api.obj)


if __name__ == "__main__":
    unittest.main()
