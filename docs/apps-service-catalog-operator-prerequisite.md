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
automatic approval review. A scoped deployment instead uses a task-only, randomly generated token for
the dedicated User, its exact upstream ClusterRoleBinding in Milo, and an
in-cluster CA-backed kubeconfig containing only that token in
`services-system/milo-kubeconfig`. The static token file is currently owned by
Milo's Flux Kustomization. `hack/test-service-catalog-operator.py` stages this
as a bounded trial: it suspends that one Kustomization, checks the auth Secret's
UID and exact two-user baseline, adds a fresh random `system:authenticated`
token for the dedicated User, restarts Milo, binds the exact upstream role,
and creates an operator kubeconfig containing only the new token and Milo CA.
The token stays out of source, stdout, and diagnostics. It verifies that the
identity can list Projects and Services and update Service status, while it
cannot read Secrets, create ClusterRoleBindings, or impersonate users. The
script restores the original auth Secret, removes its optional Flux objects
and operator namespace, restarts Milo, and resumes root Flux reconciliation on
success or failure. It retains the pinned real NSO LocationBinding CRD it
installs as a separate prerequisite. If interrupted before cleanup, remove
the optional operator/RBAC Flux objects and operator namespace, resume and
reconcile `milo-core`, then restart `milo-apiserver` to reload the original
Flux-owned token file. The bound role is resource-scoped,
but Milo's RBAC-only mode still applies a root grant across project paths;
this environment cannot prove per-project authorization.

The isolated trial was invoked from this repository with its two private
kubeconfigs and no ambient context changes:

```sh
KUBECONFIG=/tmp/apps-a1t-kubeconfig \
CATALOG_MILO_KUBECONFIG=/tmp/apps-a1t-milo-host-kubeconfig \
CATALOG_HOST_CONTEXT=kind-apps-a1t-gateway-20261020 \
CATALOG_MILO_HTTPS_PORT=41443 \
python3 hack/test-service-catalog-operator.py
```

The scoped trial passed on the isolated cluster. The role denied Secret reads,
ClusterRoleBinding creation, and user impersonation; it allowed Project and
Service lists and Service status updates. Flux reconciled the source-matched
Catalog operator image. Its Deployment and serving Certificate became Ready,
and the manager logged engagement of the provider and both consumer Projects.
The script injected the certificate CA into Milo's real webhook configurations
after issuance, as upstream `hack/e2e-milo/up.sh` does. Afterward it removed
the temporary operator, credential Secret, role, namespace, and CA injection,
restored the exact original two-user auth Secret, restarted Milo, and resumed
root Flux. Read-only follow-up found `milo-core` Ready at the original source
revision, no `services-system` namespace or Catalog operator role, and empty
CA bundles again.

The trial installed the real NSO `LocationBinding` CRD from source commit
`4c4c48ecb5e7f0f4d1950e6441ebe0c02afcfb96` with file SHA-256
`73fc1945ec88f651ee6ace8518a4593792c005af0d52dbc8da3e22d1f173f875`.
It remains in Milo as a separate prerequisite. IPAM's aggregated `IPClass`
API is not installed, so Catalog provisioning was not tested. No entitlement,
native Workload, or network route result follows from this prerequisite stage.
