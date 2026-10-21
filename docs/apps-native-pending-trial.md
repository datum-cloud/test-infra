# Internal pending-only App trial through Milo

This is an opt-in extension of `hack/test-apps-native-authoring.py`, not an
installer. The default authoring profile still requires binary
`37404c9f4364e456b23e77a57c9f01a65a250666` and its existing published image.
The only additional profile is `--internal-pending-apps`, with these immutable
pins (there is no image, host-root or runtime-revision override):

- Apps configuration: `1bba80e01c02bffe49e1547b8e4d2ed67069c6cc`.
- Apps runtime: `d3b84cfb90b1720f951c25e407adc7363980368b`.
- Image: `ghcr.io/datum-labs/apps@sha256:7246924c7e551403e79098d1c88816ac6608853fef691589bb2cfa0b6400cce6`.
- Image evidence: `apps-execution/evidence/controller-runtime-pending-image.json`
  in that exact clean Apps checkout. Both platform manifest/config identities
  are checked, and the selected architecture must be loaded on the dedicated node.
- Milo: source `200de48a305732720377cb83308d57c84fd5f688`, image
  `sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c`
  from the existing guarded authoring preflight.

No live run of this extension is claimed. The test-infra branch must first be
reviewed and committed; live execution rejects a dirty harness. This patch is based on signed test-infra `00a2b62993be5ebdf7d70b4e4560b4673fcb6c74`,
which includes the #60 fresh history census after Template deletion, #61 guard
quota prerequisite and source-bound Flux prune safety. Pending source Template
POSTs use the same durable pre-POST intent/returned-UID helper as authoring.

## Source and stage ownership

`components/apps-native-pending` contains only two Flux Kustomizations and
four additional scoped ClusterRoles/bindings. It is a runner fixture inventory,
not an unattended `kubectl apply` entrypoint: the runner withholds requester
and source-reader bindings until the relevant checks pass. It does not define
a second Deployment, CRD, webhook, certificate or admission bypass.

The runner retains the original authoring CRD stage and deploys one additional
remote Flux stage, `apps-native-a1t-pending-api`, referencing the Apps-owned
`config/experimental/pending-app-api` root. This is the **sole owner** of the
App/AppRevision CRDs and both pending webhook configurations. It runs before
the host manager, with empty public CA bundles and `failurePolicy: Fail`.
No requester CREATE permission is granted at this point. Native GET/list/watch
is not subject to these write hooks. This permits the pending controller's
initial list/watch without exposing an unprotected write interval.

The host Flux stage references `config/experimental/pending-app-host`, which
wraps the normal source-owned host and pins the published runtime above. The
runner checks both Pods, the serving certificate chain/SAN, source-owned webhook
Service and exact source rules. It adds only the observed public CA to the
pending API stage using UID/resourceVersion/spec JSON Patch tests, reconciles,
and compares the installed hooks before binding the requester. The API stage
is never duplicated in the ordinary authoring CRD stage.

The original four static fixture Users remain scoped. A fifth requester gets
App CRUD and status/history mutation verbs solely to prove admission denials;
it never receives native resource write, Secret, RBAC or impersonation rights.
The manager additionally gets App read/update, App status update and Project
SubjectAccessReview create. The temporary installer gets name-restricted
read/patch/delete for the two extra CRDs and pending webhook names; its original
CREATE grants remain as required by native Kubernetes RBAC. No installer
credential is mounted in Apps.

## Intended real API assertions

The runner first completes the existing authoring tests, then adds independent
pending source templates so their history cannot alter earlier test counts.
It checks both real consumer Project paths with independent App UIDs:

1. App CREATE is denied while the requester lacks source GET, despite the
   manager's own source GET rights. A binding granted only to the requester's
   additional static group permits source reads and then pending creation.
2. Supplied provenance/ownership annotations and a foreign Project UID pin are
   denied. The returned object must contain the exact effective actor UID,
   Project UID, full-spec digest, pending protocol and lifecycle finalizer.
3. PUT, merge PATCH and forced SSA cannot change the desired spec or protected
   provenance. A live request cannot remove its finalizer. Accepted status and
   otherwise schema-valid AppRevision CREATE are denied; the App remains exact.
4. Removing only GET from the fixture discovery role makes the trusted fresh
   root Project read fail, and App CREATE must report the resolver denial.
   The role is restored by UID/RV/rules CAS. This is an authorization-denial
   probe, not evidence of arbitrary provider outage isolation.
5. Native GET/list/watch work. The runner CAS-holds the leader Lease within its
   exact run-owned namespace and restarts both manager Pods. While the held
   Lease prevents reconciliation, DELETE must leave the exact App UID with a
   deletion timestamp and pending finalizer. Releasing that exact Lease allows
   cancellation. Same-name creation gets a fresh UID; stale UID/RV DELETE and
   PUT cannot touch the replacement.

Arbitrary UserInfo extras fidelity remains source/unit evidence; the static
fixture observes effective UID/group behavior, not OAuth/OpenFGA production
identity or revocation bounds. There is no acceptance grant, accepted history,
native resource write, release or Project-drain claim. The pending-only
protocol can never become Accepted, including after a controller upgrade.
Quota is not disabled or bypassed; an unavailable required quota fails the run.

## Cleanup and interrupted runs

Before every intended successful App POST, the runner atomically writes and
fsyncs a private noncredential intent with exact Project UID/name, fixture spec
(only `{}` inputs), nonce and expected actor. It records returned UIDs. A lost
reply can be recovered only against that exact payload/provenance, run label
and nonce; extra owner references, finalizers, annotations or fields are refused.
A recovered UID is journaled before DELETE. Known UIDs never adopt replacements.

Cleanup first verifies the discovery-role probe restored its exact original
UID/rules. A reduced role, ambiguous restore, unavailable Project, unknown App
UID or any AppRevision retains the entire serving capability chain. It revokes
the dedicated requester/group bindings, releases only its held Lease, then
censuses all initial Project UIDs and all Apps/AppRevisions. Known requests get
UID/RV DELETE while hooks, controller and credentials remain active, and must
become absent. Only then does ordinary authoring cleanup begin. Before pruning
the pending API stage it rechecks the empty census, while the manager is still
serving. It verifies the sole pending CRD/WC stage and its objects are absent
before removing the host manager or scoped credentials.

Every Flux stage uses `FluxOwned`: full desired spec, exact source artifact,
source-rendered allowed inventory and UID/RV are checked before pruning, including
lost CREATE responses. The pending stage's CA patch is the helper's sole allowed
spec transition: exact before/after specs are durably journaled before PATCH,
and a lost response recovers only that exact transition. A foreign inventory
entry or changed kubeconfig reference stops pruning. Private `*-flux-*.json`
journals remain available for explicit recovery; they contain no credentials.
This does not lock referenced Secret contents or prove child-resource UIDs;
exclusive fixture ownership and the separate resource census remain required.

A hard process kill is not an automatic recovery pass. The evidence journal
includes a discovery-role restore record and Lease hold identity; operators must
recover those exact fixture objects before resuming cleanup. A fresh run fails
preflight on leftover fixtures. Never remove hooks, credentials or CRDs merely
to make a failed trial disappear. These API reads and UID/RV operations do not
make a multi-object census atomic; the dedicated fixture must remain exclusively
owned by its live test operator. Root administrators remain outside this proof.

## Offline verification and reviewed execution

```sh
PYTHONDONTWRITEBYTECODE=1 task test-apps-native-offline
PYTHONDONTWRITEBYTECODE=1 python3 hack/test-apps-native-authoring.py \
  --offline --internal-pending-apps \
  --apps-source /absolute/path/to/clean/apps-1bba80e \
  --apps-revision 1bba80e01c02bffe49e1547b8e4d2ed67069c6cc \
  --image-evidence /absolute/path/to/clean/apps-1bba80e/apps-execution/evidence/controller-runtime-pending-image.json
```

The same source checks run before read-only live preflight (omit `--offline`)
or reviewed execution (`--execute`). Supply the same explicit private host/root
kubeconfigs, dedicated context, architecture and distinct loopback ports as the
ordinary native runner. Optional datumctl mode is rejected for this bounded
profile. The existing reusable CI continues to use the default authoring profile;
this patch does not silently switch CI or enable a customer deployment.

Offline evidence for this patch: 21 pending failure controls; the combined
Taskfile suite passes 107 tests; both fixed source
profiles pass their source/image contract; the pending source CRDs pass native
Kubernetes/CEL validation, both hooks and ten host resources pass Kubernetes
schema validation. The new test-infra fixture renders ten resources and passes
strict validation against Kubernetes 1.33 and Flux v2.7.5 Kustomization schemas.
Offline failure controls are not evidence that the intended real Milo assertions
have executed.
