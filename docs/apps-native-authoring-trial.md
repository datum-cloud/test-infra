# Native Apps authoring through Milo Project APIs

This fixture stages the source-owned Apps controller through Flux in the isolated
test-infra host cluster, and stages its two CRDs and two fail-closed webhook
configurations through remote Flux into Milo root. It uses controller-runtime
against Kubernetes APIs. It does not deploy Apps installation controllers,
fabricate an App install, or connect to storage directly.

The initial exact-definition trial used controller source
`b28933ec578cfa1ce785948b3063f45698bbd1bf`. Its registry-verified multi-platform
index is `ghcr.io/datum-labs/apps@sha256:da35e283ff2169a28e2a06f294b8fe8a5628bb0298dac024897abc1931587c12`,
with arm64 manifest `sha256:5a202d5ca1416edf006a8b4a779a1a8df6be11774d93b3c4ffc3e484f0c7a798`
and config `sha256:5ea39c7a90472c9cd506ef5862c73c7fbf8ad3237edd1272a16442795dce2709`.
The current provider runtime pin and rerun appear in the final section. Both
runtimes require the exact `spec.definitionJSON` and `spec.digest` string contract.
The runner rejects a source/image mismatch. The lead live API trial passed, including complete cleanup; see the execution
report below.

For offline review, use a clean Apps checkout at the final exact source commit:

```sh
python3 hack/test-apps-native-authoring.py --offline \
  --apps-source /absolute/path/to/clean/apps \
  --apps-revision <full-40-character-commit> \
  --image-evidence /absolute/path/to/published-image.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v hack/test_apps_native_harness.py
```

With the verified pins, the lead can run the
read-only preflight by omitting `--offline`, or opt in to `--execute` with
`--host-kubeconfig`, `--host-context`, and `--milo-kubeconfig`.
`--execute` also requires the verified multi-arch OCI evidence JSON; both
platforms must identify the exact controller source revision. The runner
requires separate private files and loopback endpoints; it does not switch
global Docker or kubectl contexts. The trial is bounded to 1,800 seconds by
default, with a separate 600-second cleanup window. It writes a non-secret
JSON evidence file outside the checkout.

The fixture gives four freshly generated static-token Users separate discovery,
project-controller, author, and temporary installer roles. It checks allowed
operations and denials of Secret, RBAC, impersonation, revision forgery, and
customer status writes before the first credential is mounted in a Pod. The
temporary installer can create CRDs and webhook configurations because
Kubernetes RBAC cannot constrain `create` by resource name. Its read, patch,
and delete grants name only the four source-rendered objects. This isolated
static RBAC proves API operations, not production IAM/OpenFGA tenant isolation.
The author binding is withheld until both root webhook configurations and their
CA are verified; the pre-install author probe must deny template writes.

The runner publishes **only** a clean `config/` tree to a task-owned private
ClusterIP registry via a loopback port-forward, pulls its exact OCI digest,
and compares the whole tree hash before Flux sees it. CRD schema hashes and
Flux applied revisions must match the source artifact. The Apps manager is
two replicas with an exact image pin; the serving certificate must chain to
the task CA and cover `apps-controller.apps-system.svc`. Only that public CA
is injected into the source-rendered root webhook configurations. Their
source rules, fail-closed policy, and service targets are checked exactly.

The traffic stage requires discovery and authoring at two real Project API
paths. Milo also advertises these CRDs in root discovery; a separate real root
write must fail through Apps admission. Discovery is not authorization. A task-owned held leader Lease leaves the webhook replicas serving
while one admitted template remains unreconciled; a second fresh-resource-
version spec edit must be denied. Releasing the exact Lease permits one
immutable revision followed by matching status. A wrong public CA must cause
the named validating webhook's TLS/x509 failure on a dry-run request, after
which the prior CA is restored with UID/resource-version preconditions.
Two replacement manager Pods must preserve the full template and sorted
revision records, excluding only Kubernetes resource-version and managed-field
metadata. Deleting a template must retain history, and recreating the same
name with a new UID must start a separate chain.

The opaque-definition regression uses the exact JSON numeric values
`9007199254740993`, `18446744073709551615`,
`0.123456789012345678901`, and `90071992547409930`. Their expected canonical
spellings are, respectively, `9007199254740993`, `18446744073709551615`,
`123456789012345678901e-21`, and `9007199254740993e1`. The expected digest of
the complete fixed precision definition is
`sha256:c0f3fc1db9b5ea79b660a4809a126055cd364980439e2746f8fdc155562274c7`,
verified with the Apps `snapshot.Parse` and `templatecontract.Validate`
packages in an isolated source clone. A mismatched digest must fail admission
before the valid definition is submitted; persisted template and immutable
revision strings must then match byte for byte.

Cleanup first performs a read-only census of every Milo Project UID and both
authoring resources in every Project. Foreign data or an ambiguous Project
identity leaves the webhooks, manager, CA, and credentials in place. It then
deletes and confirms absence of the root webhook Flux stage and both webhooks.
If this fails, it retains the manager, CA, and credentials so
the fail-closed webhook does not point to a removed server. It then removes
the task author binding and rechecks the complete inventory before removing
only allowlisted template and revision UIDs. The final inventory must be empty.
The host Flux stage must be absent before
the Deployment, CA, or mounted credentials are removed; the CRD Flux stage
must be absent before the temporary installer is revoked. Every task object
uses an exact run label and UID/resource-version deletion preconditions.
Ambiguous cleanup stops and reports the retained dependencies for recovery.
These Kubernetes API checks narrow the race; they do not make the Project
inventory atomic with concurrent API writes.

The runner accepts an explicit `--expected-architecture arm64|amd64` and
verifies the matching node and running image IDs. Both platform manifests and
config digests must match the independently verified image evidence. The
executed live trials below used arm64; amd64 currently has offline contract
coverage only. This is a prerequisite for Apps authoring, not the A1-T native
Workload-to-route or G4 result. Fresh-cluster CI still needs reviewed Project
bootstrap, adequate runner capacity, and a caller-owned clean private Apps
checkout. No CI source-access or live amd64 result is claimed.

## Initial lead execution and corrected assertions

The initial local attempts are retained under `docs/evidence/apps-native-authoring-attempt-*`.
The first failed because discovery-based permission checks ran before CRD
installation; explicit SelfSubjectAccessReview attributes fixed the probe.
The second incorrectly required root discovery to hide native CRDs; the test
now records discovery separately from its mandatory real root-write denial.
The third omitted the validator's HTTP 403 pending-edit denial and treated a
controller-created successor Lease as failed removal of the held fixture.
The fixture now deletes only its inspected UID and accepts absence of that UID,
never deleting the controller's successor. All task objects were later removed,
but that attempt honestly remains marked cleanup-incomplete due to its assertion.
The fourth omitted HTTP 403 for a digest mismatch; that assertion is corrected
without weakening the required digest error or absence of a persisted object.
All subsequent attempts require a fresh collision-free preflight.

The lead trial uses Apps config `bef6514f0fa5f9ffa50a301415b4a4a26f2a0ff9`,
Milo source `ba4b06e8dde99dd70e9a2f34e011d635e4408a76` and image
`sha256:3a89b1edb93eb3d4bb74808cc031897055e6130bf36aea56d978fcf252464afd`.
This Milo deployment does not include the later compiled guard drafts.
The controller binary is the exact-definition revision above, not the later
provider-timeout follow-up. The local host is Kubernetes 1.32.0 Linux arm64.

Reproduction requires the prepared isolated host, its three Ready Projects,
verified exact image preload, and private kubeconfig files. The lead invocation was:

```sh
python3 hack/test-apps-native-authoring.py \
  --apps-source /Users/scotwells/.codex/worktrees/9a02/apps \
  --apps-revision bef6514f0fa5f9ffa50a301415b4a4a26f2a0ff9 \
  --image-evidence /Users/scotwells/.codex/worktrees/9a02/apps/apps-execution/evidence/controller-runtime-exact-image.json \
  --host-kubeconfig /tmp/apps-a1t-kubeconfig \
  --host-context kind-apps-a1t-gateway-20261020 \
  --milo-kubeconfig /tmp/apps-a1t-milo-host-kubeconfig \
  --milo-port 41443 --direct-milo-port 41445 --registry-port 41500 --execute
```

The OCI importer requires a tar of the layout directory and an explicit
`org.opencontainers.image.ref.name` annotation on the outer layout descriptor.
That transport annotation does not change the verified image index blob.
The source/image contract and running image IDs are checked independently.

Lead final result: **PASS**, 118.812s total (85.847s trial, 32.965s cleanup). See
[exact run evidence](evidence/apps-native-authoring-lead-2026-10-20.json).
Twelve offline regression cases also pass. This is a local live authoring result,
not native CI, production IAM, provider outage, installation or release proof.

## Optional exported datumctl command trial

Add `--datumctl-source /tmp/apps-datumctl-native-definition-cli` using clean
source `91da75c3123282aac7ba276f120ebf93d2a80177`. The helper builds a private
copy before cluster access and then runs history, diff, server dry-run restore,
actual restore to a third revision, server dry-run delete, UID/RV delete, and
history by the deleted template UID. No-write operations compare every stored
field, including metadata. Actual restore compares exact definition strings and
digests. The root OAuth factory is intentionally bypassed; no production login
or tenant IAM result follows.

Lead live result: **PASS**, 110.932s total, with complete cleanup. See
[CLI evidence](evidence/apps-native-cli-lead-2026-10-20.json). The same native
admission, restart, numeric and cleanup assertions run alongside the CLI cases.
`task test-apps-native-offline` passes fourteen harness and three CLI failure
regressions. Its dedicated path-filtered CI workflow is offline regression only;
it is not a replacement for the live native trial or the full G4 gate.

## Platform contract follow-up

The platform checks add exact amd64 manifest/config validation and reject a
wrong node architecture or running image from the other platform. Lead review
and `task test-apps-native-offline` passed all sixteen harness and three CLI
cases. Both live and read-only preflight now require verified image evidence.
The image pin is unchanged; a later provider-runtime pin requires a new live
trial rather than reusing the older runtime result.

## Provider update rerun

The current runner pins controller source
`0b1c574d8c7ee6e65ce165dee689a9af9bc1e6dc`, image index
`sha256:bfa9514b9fab3c36efff6cd1e9b700029ecce6d73e41da4b772ae5ddb8686ee9`,
arm64 manifest `sha256:b945467041c253514d45c2b5e18784a35c80ed43c298b023a3109c2278cffea4`
and config `sha256:b14c53fa1f41f7579857fa5a25f8cbbd7b086f1e7a8f62845a3a41f6856faee3`,
plus amd64 manifest `sha256:a6acb1a97d420d248a7ae1c98db1177b9a24fba61dcaf134c690296702900f6a`
and config `sha256:dc6608b84639ba1f618dddd671012f1809229d201fd874d337ca93bb00518b76`.
Independent registry reads verified both platforms' source, user and entrypoint.

The lead used Apps config `a8a8ea621e4e27f2c2fc69424d0b1a752dd0a056` and the
same isolated endpoints/CLI source as above, with the new
`controller-runtime-provider-image.json` evidence path. The combined API/CLI
trial passed in **115.586s** (93.272s assertions, 22.313s cleanup), with no
cleanup errors. See [provider run evidence](evidence/apps-native-provider-cli-lead-2026-10-21.json).
The first offline invocation used a short SHA and was rejected before cluster
access; the corrected full-SHA check and first live attempt passed.
All nineteen offline regressions pass. This proves normal deployed authoring
with the new public provider timeout, not full outage isolation or installation.
