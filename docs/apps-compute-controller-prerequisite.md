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

The management overlay also mounts the separate
`compute-a1t-quota-client` Secret and
`compute-a1t-quota-kubeconfig` ConfigMap at `/etc/quota-credentials`. Its
kubeconfig must point to the three projected certificate files at
`/etc/quota-credentials/{ca.crt,tls.crt,tls.key}`. This sets the upstream
`quotaKubeconfigPath`; without it Compute explicitly starts with quota
enforcement disabled. The cell overlay already composes upstream's optional
`quota-credentials` component; the DFW cluster needs its own test-owned
credential Secret and ConfigMap. Credential contents and kubeconfigs are never
stored in this repository.

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
2. Determine and test the exact Milo authorization for the manager identity.
   Compute's upstream `config/components/controller_rbac` ClusterRole and
   binding are for the local Kubernetes API; they do not specify a Milo
   Project-scoped grant. The upstream user-facing `compute-admin` IAM Role is
   not an operator role: it omits WorkloadDeployment/Instance writes,
   NetworkBinding writes, and quota ResourceClaim access. The manager uses the
   in-cluster identity for Project discovery and per-Project clients, while
   quota uses the separately configured kubeconfig. Before install, prove
   narrowly scoped access for the same manager principal to exactly the three
   test Projects, including the actual List/watch/create/update/patch/delete
   verbs exercised by upstream controllers. Do not bind `system:authenticated`
   or use the Milo bootstrap kubeconfig as a runtime credential.
3. Compute's source explicitly requests `networkbindings` create/update/patch/
   delete and reads `networkcontexts` (see
   `config/base/downstream-rbac/rbac.yaml`). Confirm the NSO/Milo authorization
   path grants those operations in each Project. The existing Compute
   user-facing IAM roles do not contain networking permissions.
4. Provision a dedicated Karmada hub identity for the management manager with
   only the upstream downstream ClusterRole's resource verbs. Apply that
   upstream ClusterRole/Binding to the separate hub using a temporary,
   test-owned bootstrap credential; the running manager credential must be a
   different Secret and must authenticate as the patched user name. The hub
   must contain the joined DFW member and its expected placement labels.
5. Provision the separate quota client credential and kubeconfig. Verify the
   upstream quota evaluator and `ResourceClaim` API are available in all three
   Projects, with only the claim operations Compute uses (get/list/watch,
   create, delete); the claim status is read, not written. Confirm the matching
   project budget/allowance is configured. A missing AllowanceBucket leaves
   claims pending and prevents instances from becoming ready.
6. Install and verify the remaining native dependencies before creating a
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
7. Confirm the test pod/runtime images for any NSO extension, Envoy Gateway,
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

The exact Compute role sources are `config/components/controller_rbac/role.yaml`
and `config/base/downstream-rbac/rbac.yaml`. The former grants the manager its
local API permissions, including cell-side claims, Compute status/finalizer
updates, Location reads and ResourceClaim get/list/watch/create/delete. The
latter grants the federation client Karmada WorkloadDeployment,
InstanceType/Instance, NetworkBinding/NetworkContext, policy, Cluster and
ResourceBinding operations. These roles are not substitutes for Milo's
Project-scoped authorization. In particular, they do not create a service
identity grant on the three Milo Project control planes or grant the manager
NetworkService API writes there. That authorization must be independently
resolved and verified before enabling any of the optional Kustomizations.

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
```

No cluster apply, Docker operation, or secret creation was performed for this
stage.
