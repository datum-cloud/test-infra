# Milo Apps ownership guard API probe

`hack/test-apps-ownership-guard.py` verifies the published Milo #870 guard
through the actual root Project API and the selected Project control-plane API.
It only uses Kubernetes API requests through `kubectl proxy`; it does not read
or modify API-server storage. The probe refuses to run outside the dedicated
`kind-apps-a1t-ci-*` fixture or unless both kubeconfigs select one verified-CA
loopback API endpoint. The Milo root kubeconfig, bootstrap evidence and output
evidence must share an owned mode-0700 directory.

Before any resource write, it verifies the root Project UID against the private
Project-bootstrap evidence, checks the exact Organization owner and bootstrap
run label, and confirms the bootstrap Project inventory. It also verifies the
Milo Flux source commit `200de48a305732720377cb83308d57c84fd5f688`, the
immutable image
`ghcr.io/milo-os/milo@sha256:784997aec9c3b1cf5dfc993e578b108e7ef627ff835982cb58e74d1b86378a5c`,
the Deployment rollout and the running apiserver image IDs. It accepts the
verified OCI index digest or the expected amd64/arm64 child digest for the
host node architecture.

The Milo kubeconfig must resolve to the explicit `--expected-user`; use the
fixture's root/platform administrator identity with permission to create a
Namespace and ConfigMaps through the selected Project route. Before each write
and cleanup mutation, the probe re-reads the Project and checks its UID, owner,
run label and active state. A successful ordinary Namespace and ConfigMap
CREATE/GET/UPDATE/GET/UID+resourceVersion DELETE sequence proves those requests
reached the API. Six marker CREATEs and six marker PATCHes attempt each
reserved key in both labels and annotations with an empty value. Every denied
response must be HTTP 403 with the precise Apps-ownership-specific denial
message, and GET must confirm denied creates are absent and denied updates are
unchanged. This attributes denials to the guard rather than generic RBAC.

Run the offline failure-contract tests with:

```sh
python3 hack/test-apps-ownership-guard.py --offline
```

The live invocation uses the bootstrap output and its private credentials:

```sh
python3 hack/test-apps-ownership-guard.py \
  --host-kubeconfig /private/run/host.kubeconfig \
  --host-context kind-apps-a1t-ci-example \
  --milo-kubeconfig /private/run/milo-root.kubeconfig \
  --milo-context milo-root \
  --milo-port 18443 \
  --bootstrap-evidence /private/run/apps-a1t-project-bootstrap-evidence.json \
  --evidence-file /private/run/apps-a1t-ownership-guard-evidence.json \
  --project apps-a1t-provider \
  --expected-user admin \
  --deadline-seconds 180 \
  --cleanup-deadline-seconds 60
```

Use the fixture's actual loopback port and file paths. The mode-0600 output
evidence is created before any resource write and updated atomically with
intents, observed UIDs, results, failures and cleanup status. It contains no
credential material. On failure, the command reports the evidence path so an
operator can inspect or recover an incomplete fixture.

Cleanup touches only exact ConfigMap names created by this probe, using their
recorded UIDs. If a create response was lost, cleanup accepts the object only
when its full intended data, labels and annotations match and it has no
unexpected binary data, immutable setting, owner references, finalizers or
deletion timestamp. Each marker PATCH uses JSON Patch `test` operations for
the seed object's UID and resourceVersion before adding a marker, so a stale
seed cannot apply to a replacement object. Replacements, unexpected protected
markers, failed reads and failed deletes are retained for operator review.

The probe never deletes the Namespace. A namespace may contain resources this
probe does not inventory, and deleting it would cascade into those resources.
After known ConfigMaps are cleaned, private evidence records the retained
Namespace UID for a separately authorized teardown of the dedicated fixture.
The `configmaps-clean` result means only the probe's known ConfigMaps were
cleaned; it does not mean the Namespace or all namespace contents were removed.

The public fixture never creates a marked old object, so marker removal cannot
be tested safely without pre-seeding a protected object. That case is reported
as not attempted. This probe covers default denial behavior only; it does not
test configured status/finalizer writer exceptions, service identity
authorization, customer IAM, ownership acquisition, or Project deletion
lifecycle. It has not been run against a live Milo instance by its author.
