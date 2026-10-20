# Apps A1-T Service Catalog operator prerequisite (2026-10-20)

This is a dependency stage, not an Apps Workload or route result. The isolated
`apps-a1t-gateway-20261020` cluster runs Milo from source commit
`ba4b06e8dde99dd70e9a2f34e011d635e4408a76`. Milo's real controller made
`apps-a1t-provider`, `apps-a1t-consumer-a`, and `apps-a1t-consumer-b` Projects
`Ready=True` and created three ProjectControlPlanes. The provider project
accepted a temporary Namespace; reads of it through each consumer project
path returned NotFound. The temporary Namespace was deleted.

`components/service-catalog` now pins the published Billing OCI bundle to
`sha256:9e772461787d0b0f2561ed348109a6d80e171d04eeea9fee36a4e5a67d37f20c`
(Billing source revision `e73d053255bdbdea5fc5609133474fa971b583b9`)
and applies the exact Service Catalog `e2e-milo/milo-resources` overlay from
`5f2b663b6c5e039c4c7b666ef3e959803851b675` to Milo. That overlay has
eight Catalog CRDs, three real Locations CRDs pinned by the upstream overlay
to `milo-os/locations` `507ac2cbd48c0228f09baea64199882eca91e865`,
and both Catalog webhook configurations. The isolated cluster reports the
Catalog API Kustomization `Ready=True` at `sha1:5f2b663...` and Billing CRDs
`Ready=True` at the exact OCI digest. The Milo API contains all 22 expected
Catalog, Locations and Billing CRDs. The webhook configurations have no CA
bundle yet; no Catalog custom resource writes are claimed.

`operator.yaml` is an opt-in Flux Kustomization for the real upstream
`e2e-milo/operator` overlay. It pins the operator image index to
`ghcr.io/milo-os/service-catalog@sha256:a5c5e4c31c9f89edac64d6a4c02a70320c04b4e1ca95aabab6567a0a1357b1e2`.
The amd64 and arm64 images report the full Catalog source commit and matching
repository in their OCI revision/source labels. It gates on the real Deployment
and serving Certificate. `operator-rbac.yaml` references the upstream
`controller_rbac` ClusterRole, which lists the operator's actual API group,
resource and verb requirements, and changes only the binding subject to the
dedicated Milo User `apps-a1t-services-operator`. Neither optional file is in
the top-level Kustomization while its credential prerequisite is unmet.

The pinned Milo test overlay runs `AUTHORIZATION_MODE=RBAC` and authenticates
static tokens from `milo-apiserver-auth-tokens`. Its existing admin token belongs
to `system:masters`; its test-user token belongs to `system:authenticated`.
The upstream Catalog Milo E2E also uses a `system:masters` operator token, but
copying the existing broad admin kubeconfig to the operator was rejected by
automatic approval review. No operator credential Secret or Deployment was
installed. A scoped deployment needs a task-only, randomly generated token for
the dedicated User, its exact upstream ClusterRoleBinding in Milo, and an
in-cluster CA-backed kubeconfig containing only that token in
`services-system/milo-kubeconfig`. The static token file is currently owned by
Milo's Flux Kustomization, so issuance must be reconciled with that owner and
the API server must pick up the new token. The bound role is resource-scoped,
but Milo's RBAC-only mode still applies a root grant across project paths;
this environment cannot prove per-project authorization.

After the certificate is Ready, the Catalog webhook configurations in Milo
need its issued CA bundle, as the upstream `hack/e2e-milo/up.sh` does. The
operator must then become Ready and log engagement of all three Projects;
those checks have not been run. The real NSO `LocationBinding` CRD and IPAM's
aggregated `IPClass` API are separate dependencies for Catalog projection and
provisioning, and are not installed here. No entitlement, native Workload, or
network route result follows from this prerequisite stage.
