# Apps A1-T Gateway baseline evidence (2026-10-20)

This evidence covers the infrastructure Gateway slice only. It does not count
as the full Apps A1-T or G4 end-to-end result.

## Revisions and isolation

- `datum-cloud/test-infra` starting commit: `da6b1250d7f6b0ae46feb5aff49446e074510258`.
- Apps enhancement/architecture source: `datum-cloud/enhancements` PR #929 head
  `4db04b51b9c1274e24bf1e34ffafcb0f1b2f0d05`.
- kind v0.31.0, Kubernetes node v1.32.0, Flux CLI v2.8.2, Docker 29.6.2.
- Envoy Gateway v1.8.2 OCI source revision:
  `sha256:bd2d3e5ebeab47cbf06fd3ab31a1546e8b1b2d2d9a98d4e9378b880be8751249`.
  The live Flux OCIRepository reported that same revision and a ready artifact.
- Backend image: `docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10`.
- The local run used a separate Colima profile `apps-a1t-20261020` and kind
  cluster `apps-a1t-gateway-20261020`, with a private kubeconfig under `/tmp`.
  The global Docker and Kubernetes contexts were not changed. The cluster's
  container ports 30080/30443 were mapped to host ports 41080/41443.

The default Colima profile had a full 20 GiB Docker disk. An initial isolated
kind bootstrap failed while Flux downloaded an OCI artifact with `no space left
on device`. Its diagnostics were retained outside the repository. No shared
Docker data was pruned. The separate 40 GiB profile then bootstrapped Flux,
cert-manager, Envoy Gateway, and the Gateway resource successfully.

## Executed assertion

From `/tmp`, with `KUBECONFIG` set to the private file, the context set to
`kind-apps-a1t-gateway-20261020`, `GATEWAY_SMOKE_HTTP_PORT=41080`, and
`GATEWAY_SMOKE_IMAGE` set to the digest above, this command exited 0:

```bash
/usr/bin/time -p env KUBECONFIG=/tmp/apps-a1t-kubeconfig \
  GATEWAY_SMOKE_CONTEXT=kind-apps-a1t-gateway-20261020 \
  GATEWAY_SMOKE_IMAGE=docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 \
  GATEWAY_SMOKE_HTTP_PORT=41080 \
  GATEWAY_SMOKE_DIAG_DIR=/tmp/apps-a1t-timed-diagnostics \
  bash /tmp/apps-test-infra-a1t/hack/test-gateway-route.sh
```

The route reported current-generation `Accepted=True` and
`ResolvedRefs=True`. A real request returned HTTP 200 with the exact fixture
body `datum-a1t-gateway-ok`. The script's UID-precondition cleanup completed:
`namespace/apps-a1t-smoke condition met`. Timed result: **real 11.13 seconds,
user 0.80 seconds, sys 0.90 seconds**. The generated diagnostics remained at
`/tmp/apps-a1t-timed-diagnostics`; they are not committed because live cluster
objects can contain credentials.

The CI workflow formerly checked for two mutable preloaded images without
executing traffic. It now runs the digest-pinned backend and HTTPRoute and
asserts the response. The optional `kind-load-image` helper remains available
to external callers. CI results for this change must be checked on the PR;
the local run does not establish that GitHub Actions has passed.
