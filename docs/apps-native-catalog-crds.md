# Source-bound Catalog API graph

This slice depends on the scoped Catalog installer fixture. It installs only the
source-owned 22 Catalog/Billing/Locations CRDs and two fail-closed Catalog hooks;
it starts no operator and claims no Catalog resource readiness or Apps install.

`hack/bootstrap-apps-native-catalog-crds.py` requires the existing private host
and root kubeconfigs, dedicated host context/loopback port, installer evidence,
separate private graph evidence, `--catalog-source` and `--billing-artifact`.
The clean Catalog checkout must be commit
`5f2b663b6c5e039c4c7b666ef3e959803851b675`. The Billing OCI manifest is
`sha256:9e772461787d0b0f2561ed348109a6d80e171d04eeea9fee36a4e5a67d37f20c`,
with sole gzip content layer
`sha256:71bf45b57750df1e214e7689a10a8e98adabce7373ab432a18aab88fd45f9cc6`.
The exact decompressed 394240-byte tar must hash to
`sha256:c5aba6d4f649280af327b49bf128a59cca64e008c9d8fc6c9c6783d1890dd44f`.
Public manifest/layer reads verified this chain; arbitrary exports are refused.
Safe local extraction and the exact source Flux specs produce the inventory.
Catalog's source overlay fetches immutable Locations raw CRD references;
unavailable rendering fails closed before API mutation.

Both remote stages reference only `apps-a1t-catalog-installer-kubeconfig`.
The shared scope checker verifies its current endpoint/CA/token and live RBAC
content against the dedicated installer journal. Source-bound `FluxOwned`
validates complete stage spec and source-rendered inventory for known and
uncertain UIDs before UID/RV DELETE. Exact intent and recovered UIDs are fsynced
first. Old UID-only journals are retained for separate reviewed recovery.

Without `--execute` the runner performs read-only preflight. After independent
review, `--execute` retains the graph for later dependencies. `--cleanup` requires
the same source inputs and refuses while Projects, custom resources or dependent
operators remain. It removes remote stages first, verifies APIs absent, then
removes sources. Partial failures retain the graph and installer capability.
Neither inventory checks nor API CAS fence child UIDs or make cross-object census
atomic; exclusive fixture ownership remains necessary.

The Project bootstrap helper adds cleanup-only `--retain-root-kubeconfig` for
ordered dependent API teardown. It retains the mode0600 root config only after
successful owned cleanup and records this in the private journal. Default cleanup
removes the file; failed cleanup always retains it for recovery. Do not publish
or archive this credential with noncredential test evidence.

Run `PYTHONDONTWRITEBYTECODE=1 task test-apps-native-offline`. This slice adds
Catalog graph and Flux mutation/lost-reply tests plus a focused Project cleanup
flag regression. Offline tests and strict renders do not establish live Milo
lifecycle or authorization evidence. The later Locations slice remains separate.

## Ordered cleanup while the installer holds Milo core

Keep the scoped installer active until its Catalog CRD graph has been pruned.
After Locations cleanup, run Project bootstrap cleanup with the original host
and root kubeconfigs, context, loopback port, Organization and three Project
names, adding all three options:

```sh
--cleanup --retain-root-kubeconfig --installer-evidence-file /absolute/private/run/catalog-installer-evidence.json
```

Installer-evidence mode requires `--retain-root-kubeconfig`; omitting it or
passing false through a programmatic caller rejects before any Project deletion.
The explicit evidence path allows only the installer's recorded suspension. It
checks the original bootstrap UID set, live installer credentials and exact RBAC,
CA/endpoint binding, current core UID/generation/full spec, and source
UID/generation/full spec plus the exact guarded source and image. The core's
only spec difference from its recorded original must be `suspend: true`.
Fresh host/root readiness remains required; a suspended core is not required to
report Ready for its new generation. No source, credential or suspension is
changed by this check. Without this option, the ordinary unsuspended Ready
source guard remains in force.

Retry may find original Projects already absent or terminating, but never a
replacement UID. A successful Project cleanup retains the private root config
only with `--retain-root-kubeconfig`; by default it deletes the file. Failure
always retains it. Next prune Catalog with its original source inputs and
Locations evidence, then clean up the scoped installer to restore auth and
resume Milo core. The retained empty Locations namespace belongs to the fixture
owner; it is not a reason to cascade-delete host resources.

Installer setup now journals the source UID, generation and full spec, plus the
core's applied revision, before mutation. Older private installer journals
without these fields cannot authorize suspended-core Project cleanup. Do not
fill them from a current GET or treat a matching label as evidence of the old
intent; retain the recovery capabilities for separate review. These checks are
not a cross-object lock. Exclusive fixture ownership and a resilient Milo
forward remain prerequisites throughout cleanup.

`test_apps_native_cleanup_order.py` exercises the actual scope/credential checks
with offline API doubles: changed core/source/auth/RBAC/evidence/UID denial,
partial cleanup retry, default/failure credential retention, and exact retained
namespace identity plus successful empty workload census. This evidence does
not establish live Project deletion or operator teardown.
