# Apps native Compute controller prerequisite

The optional Compute package composes the native service's own overlays from
source `88a7101c2b91f7195c2bb5433c50f54c384a90ce`. The manager image is pinned to
`ghcr.io/datum-cloud/compute@sha256:1e213d5dcff0be340788d80df6fbdc235b691830667526234ee131240a89e679`.
NSO hub schemas come from `79245f00d6220c128df3d024475ff7b1566ce305`.
No floating source, replacement Deployment, or tenant Workload is introduced.
The package is excluded from the default cluster and installation tasks.

The bundle contains two source objects and four Flux stages: Compute hub
schemas, NSO hub schemas, management, and a prepared DFW cell. The bounded
[management fixture](apps-compute-management-startup-fixture.md) has independently
passed against the task-owned environment, including full cleanup, in 157.089
seconds. Its report retains exact revisions, schema hashes, replica UIDs, image
IDs, authorization denials and timings. Earlier failures and their repairs
remain recorded alongside the passing evidence. This local run is not CI or
an Apps acceptance gate.

## Runtime authority and bootstrap

Management uses four distinct credentials: Milo root Project discovery,
per-Project native operations, quota ResourceClaims, and the Karmada hub.
Three fresh Milo static-token users receive the checked-in test RBAC; a
short-lived Karmada ServiceAccount receives the exact upstream downstream role.
A separate short-lived CRD-only installer lets Flux install and prune the 16
source-owned hub schemas. Administrator kubeconfigs stay on the host.
Credential files are group-readable only to the nonroot manager group.

The static Milo Project controller role applies across all Project control
planes. It can read referenced Secrets and ConfigMaps, but cannot mutate them;
quota uses a separate principal. These are explicit test permissions, not
production IAM, tenant isolation or durable Apps delegation. Runtime probes
verify positive permissions and denials before mounting credentials. No quota
grant or native readiness status is fabricated.

The fixture directly bootstraps three task-owned cert-manager objects to
provide a CA Issuer for Compute's native CSI certificate mount. Only public
CA fields leave the Secret; the private key remains in the cluster. The
original management overlay supplies the controller, service and webhooks.
Seven optional observability objects are omitted because their APIs/operators
are absent. Resource metrics, billing telemetry and metering remain unproven.

Cleanup stops management before pruning its schemas, revokes runtime authority,
restores the exact Milo auth file and Flux state, then removes the installer.
UID/resourceVersion conditions and run ownership checks protect replacement
objects. An injected invalid Deployment proved cleanup after Flux's partial
apply left resources outside its inventory. See the fixture document for the
bounded commands, diagnostics and recovery evidence.

## Remaining native gates

The prepared cell overlay has not run and is not yet a complete federation
configuration. It still needs a dedicated downstream installer, hub writeback
credential and native serving-location dependencies. Do not apply it as a
working cell based on management readiness.

Compute's management webhooks currently exist in host Kubernetes. Milo Project
Workload admission still needs the exact native webhook configurations in
Milo's root store, verified serving CA and DNS, and actual Project API dry-run
responses. Manager readiness proves initial Project engagement and the active
hub Instance watch, not admission or native reconciliation.

Tenant execution additionally requires genuine Ready Locations and compute
ServiceAvailability, cell ServingLocation, applicable InstanceType catalog,
quota allowance and evaluator, IPAM, NSO management/cell networking, and the
Gateway/HTTPRoute data path. The newer NSO source requires its own verified
runtime image; an older digest is not evidence for that revision. Each native
resource and live endpoint must be asserted separately before claiming
Workload-to-route success.

Run the offline contract check shown in the management fixture document before
using this package. It verifies exact source trees, schemas, rendering, image
pins, credential mounts and reviewed RBAC without cluster access.
