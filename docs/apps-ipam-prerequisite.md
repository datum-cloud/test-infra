# Apps A1-T IPAM prerequisite (2026-10-20)

This optional stage runs the published IPAM extension API through the real
Milo project control-plane paths. It does not create an Apps Workload or prove
the native route test.

`components/ipam/source.yaml` pins the IPAM OCI bundle to digest
`sha256:f006b5604a924b0ce33adf98644cb106234b75a80fcc937214f3f7e327f8c2eb`.
That bundle reports source revision
`2f31bea79f624c5a94437dc20d82f8f8bdac666e`. The runtime pins the
multi-platform IPAM image index to
`sha256:9811093733ccc8e0e56ab86ba959dda73d8035a08a6feb7d3a91c5cd10011a3e`;
its amd64 and arm64 OCI revision/source labels report that full commit and
`https://github.com/milo-os/ipam`. The database stage uses CloudNativePG
chart `0.29.0` with observed chart artifact digest
`sha256:668e065ff53508d58238788fd35b355a925060843629a951df0e6a9362e6d32f`,
pins its operator image to
`sha256:a2701eb97cdd2a34b1fdb2cb51987f544b706e40bec72ae7146cd8580efefebb`,
and pins the PostgreSQL 17.0 image to
`sha256:0a198589e4edca8f169399dd4d082e18de47d289c5f2b7670c9d7bbd9e6f323b`.
The tagged digest on PostgreSQL is required by CloudNativePG's upgrade
validation; a digest alone was rejected in the first trial.

The pinned Milo test overlay leaves its aggregation proxy client certificate
paths empty. An IPAM APIService can show `Available=True` while a real project
request reaches IPAM as `system:anonymous` and fails with HTTP 403. The Milo
baseline now requests a client-auth certificate from its existing CA and
mounts it into `milo-apiserver` before the root Flux health gate. The
bootstrap namespace matches the exact pinned Milo overlay. This certificate
is an aggregator identity for passing request headers; it is not an IPAM
administrator credential.

The opt-in script requires two explicit private kubeconfigs and a dedicated
Kind context:

```sh
KUBECONFIG=/tmp/apps-a1t-kubeconfig \
IPAM_MILO_KUBECONFIG=/tmp/apps-a1t-milo-host-kubeconfig \
IPAM_HOST_CONTEXT=kind-apps-a1t-gateway-20261020 \
IPAM_MILO_HTTPS_PORT=41443 \
python3 hack/test-ipam-prerequisite.py
```

The script creates a random PostgreSQL password and keeps it in the private
cluster. It uses the existing non-admin Milo `test-user` token only in a
temporary mode-0600 kubeconfig, with a temporary role limited to IPClass and
IPPool reads and writes. It checks denial of Secret reads, RBAC binding
creation, and impersonation. It refuses name collisions, bounds all external
calls, and deletes its resources with UID and resourceVersion preconditions
through private API proxies. Flux and CloudNativePG CRDs installed by their
charts remain as system prerequisites after this trial; the trial namespace,
database, credentials, and IPAM APIService are removed.

The isolated trial reached the actual project-scoped API with the non-admin
identity and received an `IPPoolList`. It created an `IPClass` and `IPPool` in
`apps-a1t-provider`, then listed the pool in that project and confirmed it was
absent from `apps-a1t-consumer-a`, `apps-a1t-consumer-b`, and Milo root. The
same identity was denied Secret reads, ClusterRoleBinding creation, and user
impersonation. The pinned CNPG database, IPAM Certificate, runtime Deployment,
and Flux Kustomizations all became Ready during the trial. The diagnostic log
is `/tmp/apps-a1t-ipam-trial-6.log` in the private local workspace; it contains
no token or database password. The final run exited 0 after checking the
deployed CNPG operator, PostgreSQL, and IPAM images against their exact
digests and the Flux IPAM last-applied revision against the OCI digest. A
read-only Flux HelmChart query during the run reported the chart artifact
digest above; the harness now checks that digest as a fail-closed gate too.
Post-cleanup reads found no trial namespaces, IPAM Flux objects, APIService,
or probe roles. Milo core remained Ready at its pinned source revision, the
front-proxy Certificate remained Ready, and the CloudNativePG Cluster CRD
remained installed as a prerequisite.

The isolated run is a prerequisite only. Service Catalog provisioning,
Compute, NSO federation and a native Apps route are separate stages.
