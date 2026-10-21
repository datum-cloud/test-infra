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
