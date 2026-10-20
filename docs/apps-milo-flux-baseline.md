# Apps A1-T Milo Flux prerequisite

The `components/milo-core` graph reconciles the exact
`milo-os/milo` commit `ba4b06e8dde99dd70e9a2f34e011d635e4408a76`
and its matching image index digest
`sha256:3a89b1edb93eb3d4bb74808cc031897055e6130bf36aea56d978fcf252464afd`.
Both amd64 and arm64 images carry that full commit in
`org.opencontainers.image.revision` and
`https://github.com/milo-os/milo` in `org.opencontainers.image.source`.
The resource-metrics CRD source is pinned to commit
`0bb44b7ed38b37bf5266596f135a50947baf03e1`.

Flux applies the Milo test-infra overlay to the infrastructure cluster,
waits for etcd, Argo Events, the API server, and controller manager, and then
installs the infrastructure ProjectControlPlane CRD. Separate Flux
Kustomizations use Milo's existing test-only controller-manager kubeconfig
Secret to install the ResourceMetricsPolicy CRD, two test users, webhooks,
and service configuration **into the Milo API**. The users source directory
also contains infrastructure resources; Flux removes those from its rendered
inventory before applying to Milo. The Argo Events release has its optional
ServiceMonitor disabled because base test-infra does not install the
Prometheus Operator CRD. The pinned Milo service bundle omits the namespace
on one PolicyBinding, so the graph supplies its `milo-system` namespace and
matching role reference.

The `bootstrap-kind-test` workflow runs `hack/test-milo-flux.sh` after the
Gateway fixture. The script requires an explicit private `KUBECONFIG` and
matching `MILO_FLUX_CONTEXT`; it can run from any working directory. It
reconciles each pinned Flux source and dependency in order, checks deployed
images and two-user inventory, verifies the Milo route's current-generation
conditions, and sends an authenticated request through the live HTTPS
Gateway. It requires HTTP 200 with an `APIResourceList` response. Its output
and CI diagnostics provide a prerequisite result; **it does not exercise an
Apps object or a native Workload**.

Local isolated-cluster example:

```bash
export KUBECONFIG=/absolute/path/to/private-kind-kubeconfig
export MILO_FLUX_CONTEXT=kind-your-isolated-cluster
export MILO_FLUX_HTTPS_PORT=30443
bash hack/test-milo-flux.sh
```

On 2026-10-20, the isolated `apps-a1t-gateway-20261020` cluster reconciled
both exact Git revisions. All six Milo Flux Kustomizations were `Ready=True`:
`milo-core`, `milo-infra-crds`, `milo-api-resource-metrics-crd`,
`milo-api-test-users`, `milo-api-webhooks`, and `milo-api-services`.
Etcd, Argo Events, API server, and controller manager were ready; the
ProjectControlPlane CRD was Established. Both deployed Milo containers used
the image index digest above. The user inventory contained exactly `admin`
and `test-user`, and the API request through the Gateway returned HTTP 200.
An idempotent timed script run exited 0 in **18.02 seconds** (user 1.08,
sys 0.92). These are local results; the GitHub Actions run is tracked on the PR.

The remaining A1-T harness work is to pin and reconcile Service Catalog,
Compute Workload, networking, and the Apps test overlay, then run tenant
create/update/delete, quota and duplicate-name assertions with distinct
identities and verify a live route to a native Workload.
