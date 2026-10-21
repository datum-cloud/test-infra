# Pinned Apps authoring API prerequisite (A1-T)

`hack/test-apps-authoring-prerequisite.py` exercises the Apps PR14 authoring
overlay through Flux and the real Milo aggregation path in the task-owned
local cluster. It does not install customer Apps, run Compute workloads or
prove a complete A1-T route. This is an isolated Milo static-token RBAC
fixture; it does not prove production IAM/OpenFGA policy behavior.

The source is `datum-labs/apps` commit
`e0833804a92384502b9124e80002621cb6bc7549`. The source pins the Apps
image `ghcr.io/datum-labs/apps@sha256:6e6fa82647017fd80a3d15d62783b830a6664cbd990900b6f2afad191f110e28`
and etcd image `registry.k8s.io/etcd:3.5.21-0@sha256:d58c035df557080a27387d687092e3fc2b64c6d0e3162dc51453a115f847d121`.
The harness refuses a dirty or different Apps checkout and a changed 19-object
Kustomize render. Flux must report the exact source artifact digest before a
native registration is created. The repository is private and its policy
disables deploy keys (GitHub API returned HTTP 422). The local trial therefore
copies only the verified `config/` subtree into a task-owned, in-cluster OCI
registry, publishes it with the original Git source/revision annotations,
pulls it back by digest, and checks a deterministic file-tree hash before
creating a Flux OCIRepository pinned to that digest. The cluster receives no
GitHub credential and the artifact is never published outside the private
test cluster. CI could use an Apps-owned caller workflow to check out its own
private source with its existing `GITHUB_TOKEN`, then invoke this harness;
that caller workflow has not yet been built or tested.
The temporary registry runs the observed Linux/arm64 image
`docker.io/library/registry@sha256:fa647fc1e5d5df7d8d923fb6332aab8e78783f8fca1a1394efb4011f68f5a793`
as a non-root pod with a ClusterIP Service and ephemeral storage. The host
publishes only through a bounded loopback port-forward. Kind's anonymous GHCR
token request for the pinned Apps image returned HTTP 401 during the first
deployment attempt, even though a host-side anonymous manifest probe had
succeeded. Before running the harness, the host must pull the exact Apps OCI
index digest with its existing authorized Docker credentials and preload it
into only the task-owned Kind node. The observed image is Linux/arm64, and its
OCI labels identify Apps source revision
`19b331f6812bd94c93d7b09d8754f2016353ae87` and
`https://github.com/datum-labs/apps`. The exact index digest contains the
Linux/arm64 manifest
`sha256:bbe3a144888b0e5231f586bd94fcf76cad10f3129ab5685abbf70d7da78bacf2`,
whose config digest is
`sha256:be402edc785398f23497a8daab9e373fb79be4716c8c37260f37b2fabf1033f7`.
Docker's saved OCI index contains only
the local platform, so `kind load docker-image` with `--all-platforms` failed;
an arm64-only `ctr images import --digests` followed by tagging the original
index digest worked. Preflight requires that exact digest in the Kind node's
reported image list, exactly one Linux/arm64 Kind node, and no image-pull
Secret. CI must preload the same pinned digest from the authorized host,
without transmitting Docker credentials to Kubernetes.

The harness requires explicit private host and Milo kubeconfigs and the
task-owned Kind context. It refuses any existing `apps-system` Namespace,
Apps Flux objects, APIService or named Milo RBAC fixture. Before execution it
checks Milo core Ready, an available Milo controller-manager, cert-manager
1.16.2, dynamic storage, three non-deleting test Projects and the public Milo
CA/proxy/serving chains. The default invocation performs this read-only
preflight:

```sh
python3 hack/test-apps-authoring-prerequisite.py \
  --source /tmp/apps-a1t-apps-authoring \
  --host-kubeconfig /tmp/apps-a1t-kubeconfig \
  --host-context kind-apps-a1t-gateway-20261020 \
  --milo-kubeconfig /tmp/apps-a1t-milo-host-kubeconfig \
  --milo-port 41443 \
  --direct-milo-port 41444 \
  --registry-port 55000
```

After review, add `--execute` to run the bounded live trial. The test phase
has a 30-minute deadline and cleanup has a separate five-minute limit. It
creates two fresh random Milo static-token Users: the authority can only get
Projects and create SubjectAccessReviews; the author can only get/list/create/
update AppTemplates and get/list AppTemplateRevisions. It checks relevant
allows and denials with Milo authorization reviews and direct HTTP requests.
The root setup token is never copied into Apps. The authority Secret contains
only its scoped kubeconfig with the verified public CA. The Apps proxy trust
ConfigMap contains only the public CA; the proxy private key remains in Milo.
The existing root kubeconfig reaches a local test gateway with separate TLS
termination, so the two new scoped clients use a task-owned direct localhost
port-forward to Milo's serving Service. The harness verifies the serving
certificate against the public Milo CA and its `127.0.0.1` SAN before any
scoped traffic; it never passes the root kubeconfig's insecure TLS setting to
Apps or the new Users.

The live gates are exact Flux revision, two Ready Apps API replicas with
running container image IDs matching the verified index/arm64 digest chain, Ready
persistent etcd and certificates, manager-owned APIService Available, actual
project-scoped discovery through Milo, template create/update and immutable
history, consumer Project isolation, an `apps` resource absent from API
discovery plus author `POST /apps` denied by Milo RBAC, and full template and
revision-item equality after replacement of both API pod UIDs. A successful
APIService condition alone does not pass the trial; Milo does not publish a
`metadata.generation` for this APIService, so the condition is only a
nonprobing registration signal. The host's Kind network policy enforcement is
not asserted by this test.

The completed local run reported `PASS` with cleanup complete in
[the non-secret evidence file](evidence/apps-authoring-prerequisite-2026-10-20.json).
Its private OCI artifact was
`sha256:0ce05a8f6e8e4b75adfd1e0f6bc0eb394e898ddc5320407e1dcdf91a2b9938e8`,
and both Flux source and runtime reported that exact
digest stored in the evidence. The source `config/` file-tree hash was
`533e10548a2076c520499d1467839d26485e0893cc15309ec283a76285cb8d1e`
over 23 files; `flux pull artifact` returned only `config/` with that same
hash. Real Milo authoring returned HTTP 201/409/200 for create/duplicate/
update, 200 for history, and 404 for cross-Project reads. Four running API
Pod UIDs across the rollout reported the pinned OCI index digest as their
container image ID, and the full template and sorted revision items were
identical after the rollout. One earlier run got HTTP 503 on the immediate
first post-rollout GET while both new Pods were Ready; a bounded 120-second
real GET gate succeeded in subsequent runs, and the final diagnostic run did
not reproduce that 503. The harness now captures the first non-200 response,
Pod Ready/start times, EndpointSlice state and APIService condition if it
recurs. The source readiness endpoint checks storage policy, so the 503's
precise cause remains unproven.

Cleanup first removes the Milo APIService, then the task-owned Flux runtime,
OCI source, host Namespace, registry Service and Deployment by
UID/resourceVersion-preconditioned DELETE. The registry uses only an emptyDir;
teardown removes its local artifact bytes. Token CSV restoration compares the
current exact value before replacing it;
Milo core Flux suspension carries a unique run marker in the same CAS patch,
and resume checks that marker, UID, generation and unchanged spec. Unknown
ownership or concurrent changes cause an explicit cleanup failure instead of
adopting a resource. The task-owned Namespace deletion destroys only this
trial's etcd PVC and authoring history after checks complete. A private
`/tmp/apps-a1t-authoring-evidence-*.json` report records pins, non-secret
checks, result and cleanup state. It never contains tokens or private keys.

The lead independently reran the final fixture twice. The second run records
explicit phase timing in [its evidence](evidence/apps-authoring-prerequisite-lead-2026-10-20.json):
40.532 seconds for the trial and 27.372 seconds for cleanup
(67.904 seconds total, excluding read-only preflight).
Both runs passed the full object/history equality and cleanup assertions.
The timed run consumed OCI artifact `sha256:4b53b2fb21e88b627ce6a057bcdad6667de7237c940aee8bbabf0fad8430daa3`.
The earlier transient post-rollout 503 did not recur in these runs.
