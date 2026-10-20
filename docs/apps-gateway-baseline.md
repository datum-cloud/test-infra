# Apps A1-T Gateway baseline

This is the first infrastructure check for the Apps end-to-end environment. It
proves that the existing Envoy Gateway accepts an HTTPRoute and sends a live
request to a backend in an isolated namespace. The backend is a plain Kubernetes
Deployment. A passing run does **not** prove that Milo, a project control plane,
the native Workload API, Apps, or ownership admission works.

The `bootstrap-kind-test` workflow runs the fixture on every test-infra pull
request and main push. It checks out the exact workflow commit when its reusable
bootstrap action clones test-infra. The fixture image is injected at an exact OCI
digest. The job always uploads cluster, Flux, Gateway, route, event, and cleanup
diagnostics as `bootstrap-kind-diagnostics`, including on a bootstrap failure.

To run locally, use an isolated kind cluster with the repository's Flux,
cert-manager, and Envoy Gateway components installed and a private kubeconfig.
The context and image digest must be supplied explicitly:

```bash
export KUBECONFIG=/absolute/path/to/isolated-kind-kubeconfig
export GATEWAY_SMOKE_CONTEXT=kind-your-isolated-cluster
export GATEWAY_SMOKE_IMAGE=docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
# Set this when the isolated kind cluster maps container port 30080 elsewhere.
export GATEWAY_SMOKE_HTTP_PORT=30080
bash hack/test-gateway-route.sh
```

The script refuses to use an ambient kubeconfig or a different current context.
It also refuses an existing `apps-a1t-smoke` namespace. It records the created
namespace UID and uses an API deletion with that UID as a precondition during
cleanup. It waits for `Gateway Programmed`, the backend Deployment, and the
route's current-generation `Accepted` and `ResolvedRefs` conditions. The final
assertion requires HTTP 200 and the exact fixture response. The default test
deadline is 240 seconds, with an additional bounded cleanup wait. Set
`GATEWAY_SMOKE_DIAG_DIR` to retain local diagnostics at a chosen path.

The CI workflow also runs the separate pinned Milo Flux prerequisite described
in [apps-milo-flux-baseline.md](apps-milo-flux-baseline.md). A1-T still requires
Service Catalog, Compute Workload, networking, and the Apps test overlay, then
distinct project API identities and a live route to a native Workload.
