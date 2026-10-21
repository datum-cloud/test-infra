# Scoped Catalog fixture installer

This prerequisite creates only a dedicated static-token User, an exact root Milo
ClusterRole/Binding, and a host Secret containing its CA-verified kubeconfig.
It does not install Catalog schemas or start an operator. It uses native APIs;
no etcd or private storage adapter is involved.

`hack/bootstrap-apps-native-catalog-installer.py` requires explicit dedicated
host/Milo kubeconfigs, host context, loopback Milo port and private evidence file.
Without `--execute` it performs read-only preflight. It binds the host kube-system
UID, root API and original Organization/three Project UIDs using the existing
private bootstrap journal. Milo source must be guarded commit
`200de48a305732720377cb83308d57c84fd5f688` with image index
`sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c`.

The role permits CRD/webhook CREATE (native RBAC cannot name-restrict CREATE),
with subsequent access restricted to the exact 22 reviewed CRD and two webhook
names. It grants no Secret reads, RBAC changes or impersonation. The legacy broad
`milo-controller-manager-kubeconfig` is never copied. Runtime kubeconfig verifies
the actual Milo Service DNS SAN and public CA. The fresh token and before/after
static-auth values remain only in the exclusive private recovery directory and
host Secret; never publish them or upload this private directory.

Setup journals intents and UIDs before/after API calls, CAS-updates static auth,
and suspends the exact Milo core reconciliation while the token is active.
Shared checks bind the current Secret content, live role/binding content, token
and endpoint/CA to that exact intent rather than trusting labels or UIDs alone.
Cleanup refuses while any dependent graph, target APIs/data or operator remains.
It uses UID/RV operations, restores the original token configuration, requires
new ready Milo replicas and absence of old terminating Pods, then resumes core
with the recorded suspend-state contract. Failures retain the journal and
capabilities; no automatic foreign-object adoption or broad credential fallback.

Run `PYTHONDONTWRITEBYTECODE=1 task test-apps-native-offline`. This slice adds six
installer and four scope failure tests to the signed baseline suite. Those are
offline API doubles, not proof of real Milo permission, revocation or teardown.
Do not deploy until independent review. The later schema/operator slices must
use this verified scoped installer and preserve its cleanup ordering.

Project teardown while this installer is active must use the explicit evidence
path described in [ordered cleanup](apps-native-catalog-crds.md#ordered-cleanup-while-the-installer-holds-milo-core).
Setup records the source UID/generation/full spec and applied core revision
before suspension. An older journal missing this proof cannot authorize that
cleanup exception and is retained for separate recovery review.
