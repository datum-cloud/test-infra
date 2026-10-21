"""Offline teardown ordering, exact installer suspension and retained-namespace gates."""
import base64
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml

from test_bootstrap_apps_native_projects import bootstrap
from test_bootstrap_apps_native_catalog_installer import installer
from test_bootstrap_apps_native_catalog_crds import catalog
import apps_native_fixture_scope as scope

CA = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----").decode()
REV = installer.MILO_SHA


def config(context, server, user="fixture", token="private-fixture"):
    return {"current-context": context, "contexts": [{"name": context, "context": {"cluster": "one", "user": user}}],
            "clusters": [{"name": "one", "cluster": {"server": server, "certificate-authority-data": CA}}],
            "users": [{"name": user, "user": {"token": token}}]}


class Fixture:
    def __init__(self, directory):
        self.private = Path(directory).resolve()
        self.context = "kind-apps-a1t-ci-cleanup"
        self.rootpath = self.private / "root.yaml"
        self.hostpath = self.private / "host.yaml"
        self.bootstrap_path = self.private / "apps-a1t-project-bootstrap-evidence.json"
        self.installer_path = self.private / "installer.json"
        self.state_path = self.private / "apps-a1t-catalog-installer-private/state.json"
        self.state_path.parent.mkdir(mode=0o700)
        self.hostconfig = config(self.context, "https://127.0.0.1:6443")
        self.rootconfig = config("milo-root", "https://127.0.0.1:43443")
        self.uids = {n: "original-" + n for n in (scope.ORG, *scope.PROJECTS)}
        self.bootstrap = {"host_context": self.context, "organization": scope.ORG, "projects": list(scope.PROJECTS),
                          "run_id": "a" * 24, "uids": self.uids, "attempted": self.uids, "cleanup": "not-requested"}
        self.projects = {name: {"metadata": {"uid": uid, "resourceVersion": "1", "labels": {scope.RUN_LABEL: "a" * 24}},
                         "spec": {"type": "Standard"} if name == scope.ORG else {"ownerRef": {"kind": "Organization", "name": scope.ORG}}}
                         for name, uid in self.uids.items()}
        original = {"path": "./config/overlays/test-infra", "sourceRef": {"kind": "GitRepository", "name": "milo"},
                    "images": [{"name": "ghcr.io/milo-os/milo", "digest": installer.MILO_IMAGE}], "prune": True}
        self.core = {"metadata": {"uid": "core", "generation": 8}, "spec": {**original, "suspend": True},
                     "status": {"lastAppliedRevision": "sha1:" + REV}}
        self.source = {"metadata": {"uid": "source", "generation": 2},
                       "spec": {"url": "https://github.com/milo-os/milo.git", "ref": {"commit": REV}, "interval": "1m"},
                       "status": {"artifact": {"revision": "sha1:" + REV}, "observedGeneration": 2,
                                  "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 2}]}}
        self.authvalue = base64.b64encode(b'fixture-token,apps-a1t-catalog-installer,apps-a1t-catalog-installer,system:authenticated\n').decode()
        self.auth = {"metadata": {"uid": "auth"}, "data": {"tokens.csv": self.authvalue}}
        role, binding = installer.exact_role_manifests()
        remote = config("scoped", "https://milo-apiserver.milo-system.svc.cluster.local:6443", installer.USER, "fixture-token")
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": installer.SECRET, "namespace": "milo-system"},
                  "type": "Opaque", "data": {"kubeconfig": base64.b64encode(yaml.safe_dump(remote).encode()).decode()}}
        self.objects = {"role": role, "binding": binding, "secret": secret}
        for key, obj in self.objects.items():
            obj["metadata"].update(uid=key, labels={scope.RUN_LABEL: "b" * 24})
        self.host = SimpleNamespace(config=self.hostpath, k=self.host_k, json=self.host_json,
                                    optional=lambda *args: self.objects["secret"])
        self.milo = SimpleNamespace(config=self.rootpath, k=self.root_k, json=self.root_json, optional=self.root_optional)
        for path, obj in ((self.hostpath, self.hostconfig), (self.rootpath, self.rootconfig), (self.bootstrap_path, self.bootstrap)):
            installer.atomic_json(path, obj)
        identity = scope.scope_identity(self.host, self.milo, self.context, 43443)
        self.evidence = {"complete": True, "cleanup": "not-requested", "run_id": "b" * 24, "milo_source": REV,
                         "scope_identity": identity, "attempted": {key: key for key in self.objects},
                         "intents": {key: scope.catalog_installer_intent_digest(obj) for key, obj in self.objects.items()}}
        self.state = {"run_id": "b" * 24, "auth_uid": "auth", "new_b64": self.authvalue, "auth_intent": True,
                      "core_uid": "core", "core_generation": 7, "suspended_generation": 8, "core_spec": original,
                      "core_applied_revision": "sha1:" + REV, "core_suspend_intent": True,
                      "source_uid": "source", "source_generation": 2, "source_spec": copy.deepcopy(self.source["spec"])}
        self.args = SimpleNamespace(cleanup=True, retain_root_kubeconfig=True,
             installer_evidence_file=self.installer_path, host_context=self.context,
             milo_port=43443, deadline_seconds=120, host_kubeconfig=self.hostpath, root_kubeconfig=self.rootpath)
        self.sync()
    def sync(self):
        for path, obj in ((self.bootstrap_path,self.bootstrap),(self.installer_path,self.evidence),(self.state_path,self.state)):
            installer.atomic_json(path,obj)
    def host_k(self,*args,**kw):
        return json.dumps(self.hostconfig) if "config" in args else "ok"
    def root_k(self,*args,**kw):
        return json.dumps(self.rootconfig) if "config" in args else "ok"
    def host_json(self,*args):
        if "config" in args:return copy.deepcopy(self.hostconfig)
        if "namespace" in args:return {"metadata":{"uid":"host-system"}}
        if "secret" in args:return copy.deepcopy(self.auth)
        if "gitrepository" in args:return copy.deepcopy(self.source)
        if "kustomization" in args:return copy.deepcopy(self.core)
        raise AssertionError(args)
    def root_json(self,*args):
        if "config" in args:return copy.deepcopy(self.rootconfig)
        return copy.deepcopy(self.projects[args[-1]])
    def root_optional(self,kind,name,*args):
        if kind in ("clusterrole","clusterrolebinding"):
            return copy.deepcopy(self.objects["role" if kind=="clusterrole" else "binding"])
        return copy.deepcopy(self.projects.get(name))
    def guard(self):
        bootstrap.installer_cleanup_guard(self.host,self.milo,self.args,self.bootstrap)


class CleanupOrderTests(unittest.TestCase):
    def test_exact_installer_suspension_and_partial_uid_retry_pass(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);f.guard()
            f.bootstrap["cleanup"]="incomplete"
            del f.projects[scope.PROJECTS[0]]
            f.projects[scope.PROJECTS[1]]["metadata"]["deletionTimestamp"]="2026-10-21T00:00:00Z"
            f.sync();f.guard()
            f.projects[scope.PROJECTS[1]]["metadata"]["uid"]="replacement"
            with self.assertRaises(RuntimeError):f.guard()

    def test_foreign_core_source_auth_evidence_and_uid_fail_before_cleanup(self):
        mutations=[lambda f:f.core["spec"].update(prune=False),
                   lambda f:f.core["metadata"].update(uid="foreign"),
                   lambda f:f.core["metadata"].update(generation=9),
                   lambda f:f.source["spec"].update(secretRef={"name":"foreign"}),
                   lambda f:f.source["metadata"].update(uid="foreign"),
                   lambda f:f.auth["data"].update({"tokens.csv":"changed"}),
                   lambda f:f.objects["role"]["rules"][0].update(verbs=["*"]),
                   lambda f:f.evidence.update(run_id="c"*24),
                   lambda f:f.evidence["scope_identity"]["host"].update(kube_system_uid="foreign"),
                   lambda f:f.state.pop("source_uid"),
                   lambda f:f.bootstrap["attempted"].update({scope.PROJECTS[0]:"foreign"})]
        for mutate in mutations:
            with self.subTest(mutation=mutate),tempfile.TemporaryDirectory() as d:
                f=Fixture(d);mutate(f);f.sync()
                with patch.object(bootstrap,"Kube",side_effect=[f.host,f.milo]),patch.object(bootstrap,"cleanup_evidenced") as cleanup:
                    with self.assertRaises(RuntimeError):bootstrap.run(f.args)
                    cleanup.assert_not_called()
                self.assertTrue(f.rootpath.exists())

    def test_installer_cleanup_requires_retention_before_delete(self):
        for retained in (None, False):
            with self.subTest(retained=retained), tempfile.TemporaryDirectory() as d:
                f = Fixture(d)
                if retained is None:
                    del f.args.retain_root_kubeconfig
                else:
                    f.args.retain_root_kubeconfig = retained
                with patch.object(bootstrap, "Kube", side_effect=[f.host, f.milo]), patch.object(bootstrap, "cleanup_evidenced") as cleanup:
                    with self.assertRaisesRegex(RuntimeError, "requires --retain-root-kubeconfig"):
                        bootstrap.run(f.args)
                    cleanup.assert_not_called()
                self.assertTrue(f.rootpath.exists())
                self.assertEqual(f.rootpath.stat().st_mode & 0o777, 0o600)
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as d:
                f = Fixture(d)
                def cleanup(*args):
                    if fail:
                        raise RuntimeError("owned cleanup failed")
                    args[3]["cleanup"] = "complete"
                    bootstrap.write_json_atomic(args[2], args[3])
                with patch.object(bootstrap, "Kube", side_effect=[f.host, f.milo]), patch.object(bootstrap, "cleanup_evidenced", side_effect=cleanup) as called:
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "owned cleanup failed"):
                            bootstrap.run(f.args)
                    else:
                        bootstrap.run(f.args)
                    called.assert_called_once()
                self.assertTrue(f.rootpath.exists())
                self.assertEqual(f.rootpath.stat().st_mode & 0o777, 0o600)
                saved = json.loads(f.bootstrap_path.read_text())
                self.assertEqual(saved.get("root_kubeconfig_retained", False), not fail)
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(d)
            f.args.installer_evidence_file = None
            with patch.object(bootstrap, "Kube", side_effect=[f.host, f.milo]), patch.object(bootstrap, "cleanup_evidenced") as cleanup:
                with self.assertRaises(RuntimeError):
                    bootstrap.run(f.args)
                cleanup.assert_not_called()

    def test_retained_namespace_requires_exact_journal_and_successful_empty_census(self):
        for census in ({"items":[]},{},{"items":None},{"items":[{"kind":"ReplicaSet"}]}):
            with self.subTest(census=census),tempfile.TemporaryDirectory() as d:
                root=Path(d);path=root/'catalog.json';locations_path=root/'locations.json'
                namespace={"metadata":{"uid":"namespace","labels":{scope.RUN_LABEL:"run"}}}
                evidence={"run_id":"catalog","attempted":{},"scope_identity":{"fixture":"exact"}}
                locations={"cleanup":"complete-with-retained-namespace","scope_identity":evidence["scope_identity"],
                           "run_id":"run","retained_namespace":{"name":"locations-system","uid":"namespace"}}
                installer.atomic_json(locations_path,locations)
                host=Mock();host.optional.side_effect=lambda kind,name,*_:namespace if name=='locations-system' else None
                host.json.return_value=census
                milo=Mock();milo.json.return_value={"items":[]};milo.optional.return_value=None
                with patch.object(catalog,"root_inventory",return_value=(set(),set())),patch.object(catalog,"APIProxy") as proxy:
                    if census=={"items":[]}:
                        catalog.cleanup(host,milo,evidence,path,locations_path)
                    else:
                        with self.assertRaises(RuntimeError):catalog.cleanup(host,milo,evidence,path,locations_path)
                        proxy.assert_not_called()
                    proxy.return_value.delete.assert_not_called()
                if census=={"items":[]}:
                    namespace["metadata"]["uid"]="replacement"
                    with patch.object(catalog,"APIProxy") as proxy:
                        with self.assertRaises(RuntimeError):catalog.cleanup(host,milo,evidence,path,locations_path)
                        proxy.assert_not_called()


if __name__=="__main__":unittest.main()
