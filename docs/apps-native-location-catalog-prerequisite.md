# Native Catalog schemas and Locations readiness prerequisite

This final dependent slice builds on the [scoped installer](apps-native-catalog-installer.md)
and [source-bound API graph](apps-native-catalog-crds.md). Their teardown contracts
remain in force.

This is a bounded dependency fixture for the isolated Apps native trial. It
does not create a ServiceEntitlement, Workload, Instance, customer install, or
route, and it cannot establish serving traffic. Flux reads Service Catalog
commit `5f2b663b6c5e039c4c7b666ef3e959803851b675` and Billing OCI digest
`sha256:9e772461787d0b0f2561ed348109a6d80e171d04eeea9fee36a4e5a67d37f20c`
to apply their source-owned schemas and webhook registrations to Milo's API.
The expected result is 22 Established Catalog, Billing, and Locations CRDs and
two Catalog webhook configurations. The latter have no serving CA or Catalog
operator at this stage; Catalog custom-resource writes are not claimed.

The next stage runs the Locations manager from public source commit
`507ac2cbd48c0228f09baea64199882eca91e865`, with pinned image index
`ghcr.io/milo-os/locations@sha256:c888576073f66e14a3401c450df6bcf47dcb69259914bbb93acc654084c4d400`.
Its source-owned base manager runs in the host cluster with a separate,
CA-verified Milo kubeconfig and one non-root replica. The verified root API
client directly creates exactly a source-derived Milo ClusterRole and
ClusterRoleBinding. The role is reduced to LocationClass and Location readiness
verbs; the dedicated User cannot read Secrets, change RBAC, impersonate a User,
or publish ServingLocations. The manager's publisher remains disabled because
no federation hub kubeconfig is mounted. A real controller must report
`Accepted=True` on class `datum-managed` and `Ready=True` on Location `dfw`
with city code `DFW`. These conditions are read with generation checks, and
the actual running image ID is recorded. Projection into all Project APIs
remains a separate observation; it is not inferred from root readiness.

The fixtures use the task-owned Kind context and private Milo root kubeconfig
explicitly. Their journals record attempted names and UIDs before and after
each API create, so partial setup can be recovered. The Locations credential
recovery file contains a fresh token and the before/after static-auth values;
it must stay in the exclusive mode-0700 private run directory and is never
checked in or uploaded. The runners suspend the exact owned `milo-core` Flux
Kustomization while this token is active, restore static auth with a compare
and swap, reload Milo, and resume Flux after teardown. Neither runner uses
etcd or an Apps storage adapter.

From a checkout with both scripts and an exact clean Locations source clone,
run offline checks and preflight before `--execute`. The loopback Milo forward
must be active and its certificate must verify against the private kubeconfig.
Replace only the task-owned private paths with this run's actual paths:

```sh
PYTHONDONTWRITEBYTECODE=1 task test-apps-native-offline
flux build kustomization apps-a1t-locations-host --path /private/tmp/apps-locations-source-20261021/config/base/manager --kustomization-file components/locations/host.yaml --dry-run
python3 hack/bootstrap-apps-native-catalog-crds.py --catalog-source /private/tmp/apps-service-catalog-source-20261021 --billing-artifact /private/tmp/apps-billing-kustomize-9e772461.tar --host-kubeconfig /private/tmp/apps-native-vz-host-20261021/host.kubeconfig --host-context kind-apps-a1t-ci-vz-20261021 --milo-kubeconfig /private/tmp/apps-native-vz-projects3-20261021/milo-root.kubeconfig --milo-port 43443 --evidence-file /private/tmp/apps-native-vz-projects3-20261021/catalog-api-evidence.json --installer-evidence-file /private/tmp/apps-native-vz-projects3-20261021/catalog-installer-evidence.json
```

Once reviewed, add `--execute` to that final command. Then run the Locations
preflight with the same host and Milo options, `--locations-source` set to the
clean pinned clone, `--installer-evidence-file` set to the active installer journal,
and `--evidence-file` set to a different private path.
Add `--execute` only after reviewing both preflights. Both stages retain their
owned resources for the next dependency. Teardown runs `--cleanup` for
Locations first. Then remove the task-owned Projects through their own
UID-safe bootstrap helper. Catalog `--cleanup` refuses a completed stage while
Projects or either operator namespace remains, preventing silent deletion of
project-scoped data. A failed prune keeps its source objects and journal for
recovery. Do not switch global Docker or kubectl contexts.

Current evidence: the fresh VZ Kind baseline and three real Project APIs were
Ready; the Catalog API graph's read-only preflight found no collisions. No
Catalog or Locations resource from this new fixture has yet been installed.

## Source-bound Flux cleanup

Catalog's two CRD stages and Locations' host stage use the shared
`FluxOwned` guard. Both known and lost-response UIDs require the exact complete
Kustomization spec, source artifact revision and source-rendered inventory subset
before UID/RV DELETE. Defaults are limited to the shared pinned Flux contract;
changed kubeconfig/source/patches or any foreign inventory entry prevents prune.
The complete intent and a recovered UID are fsynced to the existing private
operator journal before destructive operations. Generic object ownership and the
Project, custom-resource, namespace, operator and credential census are unchanged.

Catalog now requires `--catalog-source` and `--billing-artifact` for preflight,
execute and cleanup. Locations validates its existing `--locations-source` on
cleanup as well. Catalog source is clean commit `5f2b663b6c5e039c4c7b666ef3e959803851b675`;
Locations is clean commit `507ac2cbd48c0228f09baea64199882eca91e865`. Render uses the
same source-owned Flux spec and patches as the actual controller for those three stages. Catalog also
fetches the upstream overlay's immutable raw Locations CRD references while
rendering; an unavailable source fails closed.

The billing OCI manifest `sha256:9e772461787d0b0f2561ed348109a6d80e171d04eeea9fee36a4e5a67d37f20c`
contains exactly one content layer, `sha256:71bf45b57750df1e214e7689a10a8e98adabce7373ab432a18aab88fd45f9cc6`.
Its exact decompressed 394240-byte tar hashes to
`sha256:c5aba6d4f649280af327b49bf128a59cca64e008c9d8fc6c9c6783d1890dd44f`.
The supplied tar is checked against that digest and extracted with regular-file/
directory-only path checks before rendering the pinned `base/crd/bases` path.
No registry tag or caller-defined resource inventory is accepted.

A pre-upgrade UID-only journal is insufficient for Flux pruning and is retained
for a separately reviewed recovery; this runner does not automatically migrate
or reconstruct missing durable intent. Successful source rendering is not live
Milo evidence. Neither inventory validation nor UID/RV CAS freezes referenced
Secret contents, child object UIDs or a multi-object census. The dedicated
fixture must remain exclusively owned; retain all capabilities on uncertainty.

## Locations RBAC uses the root API directly

There is no Locations RBAC Flux Kustomization or remote credential reference.
The legacy `milo-controller-manager-kubeconfig` is forbidden: its upstream test
credential is not this fixture's scoped credential. Before any write, the runner
renders the exact pinned Locations controller-RBAC component through a temporary
local Kustomize root plus `readiness-role-patches.yaml`. Only ClusterRole and
ClusterRoleBinding `locations-manager` are accepted. It creates those two objects
using the already CA-verified root native client, not a new credential or remote
installer privilege. The runtime manager still receives only its dedicated
readiness User kubeconfig; its CA and actual Milo Service DNS SAN are verified.

The native RBAC journal precedes CREATE and records the exact rendered public
payload digest, run label and returned UID. Known and uncertain UIDs must match
all rules, subjects, roleRef and non-server metadata on cleanup; a recovered UID
is fsynced before UID/RV DELETE. Role binding is removed before role, after the
host manager disappears. Owner references, finalizers, deleting objects and
unreviewed payload changes retain cleanup protection. Old remote RBAC stage
presence fails preflight; this runner does not migrate or delete it implicitly.
