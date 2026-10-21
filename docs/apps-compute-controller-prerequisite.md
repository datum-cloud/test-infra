# Apps A1-T native Compute controller prerequisite (2026-10-20)

This optional, uninstalled Flux stage composes Compute's own management and
cell overlays from source commit `88a7101c2b91f7195c2bb5433c50f54c384a90ce`.
It does not create a hand-written Kubernetes Deployment or a tenant Workload.
The manager image is pinned to the source-matched OCI index
`ghcr.io/datum-cloud/compute@sha256:1e213d5dcff0be340788d80df6fbdc235b691830667526234ee131240a89e679`.
The same image digest is used in both the management control plane and DFW
cell; the upstream manager contains the webhook and controller runtime.
The separate federation source is test-infra PR32 at
`3619e71d4686c3d1ead01f08a1136f7d272b3344`; the current optional NSO route
source being evaluated is `79245f00d6220c128df3d024475ff7b1566ce305`.
NSO's older source-matched digest in `docs/apps-native-dependencies.md` is not
valid evidence for this newer tree, so the NSO stage still needs a rebuilt and
source-verified image pin.

The reviewable render is in `components/compute-controller/`. It is deliberately
not included in `cluster/kustomization.yaml` or any default install task. It has
three separate Flux Kustomizations: management plane, DFW member cell, and
Karmada hub RBAC. Karmada remains a separate cluster as in the pinned PR32
federation. The management render patches Compute's upstream projected
in-cluster Karmada token to use a separately provisioned, test-only
`compute-a1t-karmada-kubeconfig` Secret. The Karmada RBAC render uses the
upstream `config/base/downstream-rbac` ClusterRole and changes only its subject
to `apps-a1t-compute-manager`, the user in that dedicated hub kubeconfig. It
does not copy the hub or member administrator kubeconfig into a workload pod.

The management overlay mounts three independent test-owned kubeconfig Secrets:
`compute-a1t-milo-discovery-kubeconfig`,
`compute-a1t-milo-project-kubeconfig`, and
`compute-a1t-quota-kubeconfig`. The first authenticates root Project discovery,
the second is copied into per-Project control-plane clients, and the third
performs quota ResourceClaim operations. The Compute configuration names all
three paths explicitly. Compute's config defaults discovery and Project API
clients to the pod's in-cluster credential when these paths are absent, which
cannot authenticate to the separate Milo API in this topology. Quota falls
back to the Project credential only when no separate quota path is configured;
this fixture deliberately keeps the quota principal separate. The cell overlay
continues to compose Compute's upstream optional quota component, then replaces
its default mTLS/ConfigMap projection with the same one-purpose static-token
quota kubeconfig Secret in the DFW member cluster. No credential bytes or
kubeconfig content is stored in this repository.

The management manifest removes exactly six upstream observability objects:
two optional `GrafanaDashboard`s, two `VMRule`s, and their two generated
dashboard ConfigMaps. The current test cluster does not install those CRDs.
All other resources in Compute's original management overlay remain in the
render, including the real manager, webhooks, leader election, RBAC, metrics,
management controllers, and CSI webhook certificate mount. The cell render is
the unmodified upstream cell overlay plus the source-matched image digest.

## Before enabling the Flux stages

These are credential and API prerequisites, not values this change can safely
invent:

1. Confirm Compute's six API CRDs are Established in Milo and the three
   task-owned Project control planes are Ready. The existing
   `compute-a1t-milo-api-crds` prerequisite only installs schemas; it does not
   grant the manager access to Project APIs.
2. `components/compute-controller/milo-rbac/` is a review fixture derived from
   Compute `88a7101c2b91f7195c2bb5433c50f54c384a90ce` controller RBAC and
   request paths, not a production IAM policy. It gives distinct static-token
   test users these duties: discovery reads root `projects`; the project
   controller reads native ConfigMap/Secret references and has Compute,
   networking, location, service-catalog and HPA permissions plus create on
   `authorization.k8s.io/subjectaccessreviews` for admission checks; quota can
   only operate quota `resourceclaims`. The manager role has no ConfigMap/Secret
   write verbs, and quota claim operations belong only to the separate quota
   user. Do not bind `system:authenticated`, reuse Milo's bootstrap kubeconfig,
   or use the user-facing `compute-admin` Role as an operator role. That IAM
   Role omits WorkloadDeployment/Instance and networking writes the controllers
   need.
3. The pinned Milo test overlay's auth token file currently defines only its
   existing bootstrap/test users. Before running this fixture, the isolated
   Milo test environment needs three separate one-purpose token entries whose
   usernames exactly match the ClusterRoleBinding subjects, plus three
   Secret-backed kubeconfigs using the API CA and those tokens. Generate and
   install these values through the test environment's secret bootstrap; never
   put token values in this source tree or pod spec. Apply the RBAC bundle to
   Milo with the existing bootstrap path only after review. No token, Secret,
   credential, or RBAC object was created by this preparation.
4. This RBAC fixture cannot scope the provider to the three fixture Projects.
   Compute's Milo provider starts an unfiltered root `Project` informer, so it
   needs `list/watch` on all Projects. Kubernetes RBAC `resourceNames` can
   constrain named `get`, and can constrain `list/watch` only when the client
   sends a matching `metadata.name` field selector; this provider does not.
   Milo's project router adds `iam.miloapis.com/parent-name` to authorization
   extras, but ordinary Kubernetes RBAC in the test overlay matches the same
   static user across all routed Projects. The project role therefore grants
   its listed verbs on every Project control plane; three fixture Projects do
   not create an authorization boundary. This is a test-only limitation, not
   evidence of production IAM/FGA policy or tenant isolation.
5. Compute must get/list/watch source Secrets and ConfigMaps because it reads
   referenced data and watches changes; the current informer pattern cannot
   limit those dynamic references by name. The role denies their
   create/update/patch/delete verbs, but its static test token can read
   unrelated Secret values in every Project. A negative test must expect
   unrelated Secret reads to succeed and verify write denial; it must not claim
   secret isolation. RBAC resources, user/role management, impersonation, and
   `system:masters` are absent from these roles. Discovery is read-only; quota
   cannot access Compute or core resources.
6. Before mounting any of the three runtime Secrets, exercise the generated
   identities directly against the isolated test Milo API. Test the discovery
   token at the root API and the project/quota tokens at a concrete URL of the
   form `/apis/resourcemanager.miloapis.com/v1alpha1/projects/<project>/control-plane`.
   Expect root `GET/LIST/WATCH projects` to succeed and project/Secret reads to
   fail for the discovery user. For the project user, expect native Workload,
   Instance, networking and referenced ConfigMap/Secret reads and the
   validator's SubjectAccessReview create to succeed; expect Secret
   create/update/patch/delete, ResourceClaim create, RBAC changes and
   impersonation to fail. Expect the quota user to create/read/list/watch/delete
   ResourceClaims, but fail reads/writes of Secrets and Compute resources,
   ResourceClaim status updates, RBAC changes and impersonation. The project
   user will also pass project-level read/write probes against another Project
   in this static RBAC setup; record this expected limitation rather than
   asserting tenant isolation. Use real API requests or trusted test bootstrap
   authorization probes with the exact bearer identity; do not infer access
   from a ClusterRole manifest alone. Do not create synthetic claims or fake
   grants: verify quota using an actual native Instance/ResourceClaim and the
   real quota evaluator, and report pending admission if no matching allowance
   is configured.
7. Compute's source explicitly requests `networkbindings` create/update/patch/
   delete and reads `networkcontexts` (see
   `config/base/downstream-rbac/rbac.yaml`). Confirm the NSO/Milo authorization
   path grants those operations in each Project. The existing Compute
   user-facing IAM roles do not contain networking permissions.
8. Provision a dedicated Karmada hub identity for the management manager with
   only the upstream downstream ClusterRole's resource verbs. Apply that
   upstream ClusterRole/Binding to the separate hub using a temporary,
   test-owned bootstrap credential; the running manager credential must be a
   different Secret and must authenticate as the patched user name. The hub
   must contain the joined DFW member and its expected placement labels.
9. Verify the upstream quota evaluator and `ResourceClaim` API are available
   in all three Projects, with only the claim operations Compute uses
   (get/list/watch, create, delete); the claim status is read, not written.
   Confirm the matching project budget/allowance is configured. A missing
   AllowanceBucket leaves claims pending and prevents instances from becoming
   ready. Do not create a synthetic claim or fake a grant to satisfy readiness.
10. Install and verify the remaining native dependencies before creating a
   tenant Workload: the Locations API, a Ready DFW `Location`, the DFW
   `locations.miloapis.com/ServingLocation`, and a mirrored Compute
   `ServiceAvailability` for DFW; an Active
   Compute `InstanceType` (the upstream test fixture is
   `test/e2e/instance-types/instance-type-d1-standard-2.yaml`); project IPAM;
   NSO management and cell controllers; an NSO `NetworkContext`/interface
   allocation for the selected network; and the downstream Envoy Gateway/edge
   resources required by the selected exposure path. The current Service
   Catalog prerequisite establishes CRDs only; the operator, entitlement,
   billing and Locations requirements remain separate gates.
11. Confirm the test pod/runtime images for any NSO extension, Envoy Gateway,
   IPAM, Locations, and Karmada components against their exact source revisions
   and OCI digests before adding those manifests. The existing NSO image digest
   in `docs/apps-native-dependencies.md` is matched to its earlier source pin,
   not the newer HTTPRoute-capable source under review. It cannot be reused as
   proof for that source.

The Flux health checks in these manifests only establish that the management
and cell `compute-manager` Deployments and management admission configurations
are available. They do not establish project authorization, a Ready
`ServingLocation`, an admitted or Ready Compute `Workload`, a propagated
`WorkloadDeployment`, quota grant, resolved NSO endpoint, or live route traffic.
The native tenant test must assert each of those outcomes separately before it
can claim Workload-to-route success.

The exact source path is Compute `cmd/main.go`'s Milo branch and
`pkg/multicluster-runtime/milo/provider.go`: it watches root Projects without
a selector, then builds a separate client for each Project control plane.
`internal/referenceddata/project_reader.go` performs direct named GETs for
source ConfigMaps and Secrets; `internal/controller/instance_controller.go`
watches reference kinds and runs companion synchronization. The same
controller declares quota ResourceClaim `get/list/watch/create/delete`, and
`internal/quota/client.go` creates a distinct client for each Project. The
workload admission validator uses the per-Project client to create
`authorization.k8s.io/subjectaccessreviews` for referenced data and network
selection, which is why that single create permission is included. The
fixture splits these operations among discovery, project and quota users.
Its project rule mirrors the relevant native groups and verbs from upstream
`config/components/controller_rbac/role.yaml`, removing core ConfigMap/Secret
mutation and moving quota operations to the separate quota principal. The
local upstream role and `config/base/downstream-rbac/rbac.yaml` still describe
local-cluster and separate Karmada-hub access, not production Milo Project
IAM. Milo `pkg/server/filters/projects.go` adds parent Project identity to the
authorization extras; test-infra sets `AUTHORIZATION_MODE=RBAC`, so the fixture
does not treat that extra as a Project-scoping grant.

Compute's native role does not grant NetworkService API writes, and the RBAC
fixture does not make HTTPRoute exposure or tenant entitlements ready. The
Apps path must separately verify service entitlement, networking APIs, quota
admission and route traffic.

## Offline render

With the pinned Compute checkout at `/tmp/apps-a1t-compute`, the following
commands render the exact remote Flux transformations without contacting a
cluster:

```sh
kustomize build components/compute-controller
flux build kustomization compute-a1t-management \
  --path /tmp/apps-a1t-compute/config/overlays/management-plane \
  --kustomization-file components/compute-controller/management.yaml --dry-run
flux build kustomization compute-a1t-dfw-cell \
  --path /tmp/apps-a1t-compute/config/overlays/cell \
  --kustomization-file components/compute-controller/cell.yaml --dry-run
flux build kustomization compute-a1t-karmada-rbac \
  --path /tmp/apps-a1t-compute/config/base/downstream-rbac \
  --kustomization-file components/compute-controller/karmada-rbac.yaml --dry-run
kustomize build components/compute-controller/milo-rbac
```

No cluster apply, Docker operation, or secret creation was performed for this
stage.
