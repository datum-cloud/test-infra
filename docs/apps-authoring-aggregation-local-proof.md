# Local Milo to Apps aggregation proof

`hack/test-apps-authoring-aggregation.py` runs a disposable Milo API server,
Apps API server, and etcd instance on loopback. It exercises Milo's real
APIService proxy and Apps' real HTTPS handler. The script creates Projects,
RBAC bindings, and the APIService through Milo's public API; it does not
replace Project lookup or SubjectAccessReview handling with fake endpoints.

The verified source pins for the passing run were:

- Milo `ba4b06e8dde99dd70e9a2f34e011d635e4408a76` (the baseline used by the live test-infra deployment)
- Apps `ccb6cedf6e5a8b29ec1ac74e1dfdaa5ff3db6f23`
- datumctl `28f4d364302a9df451b7a453cce1ce1cb99ce0a0`
- Test-infra base `e53f1dba48358da60fff62a1122eec86799eee6c`

The Apps binary included the authoring quota and separate health-listener
flags from the stated pin. The Milo test launcher is kept in
`hack/apps-aggregation-launcher/main.go`. It invokes Milo's normal
Options/Complete/Validate/Run path and binds the egress-selector file to
`opts.EgressSelector.ConfigFile`; it initializes the two Milo package-level
storage settings through Milo's registered flags. It adds no authentication,
authorization, admission, or proxy behavior. This is a harness launcher, not
the deployed Milo wrapper.

The APIService uses the normal service name, namespace, port, and Apps serving
CA. Milo's normal `NewDefaultServiceResolver` constructs the
`<service>.<namespace>.svc` HTTPS URL. A local TLS/mTLS CONNECT bridge requires
a client certificate signed by its test CA and forwards only
`apps-authoring.apps-system.svc:8443` to loopback, because the local test
does not create a cluster DNS/service network. Its negative control rejects
`evil.invalid:443`. Apps verifies the Milo front-proxy CA and allowed
certificate name; Milo's aggregator verifies the Apps serving CA and hostname.

The local Milo apiserver disables kube-aggregator's remote Available-condition
controller. The harness therefore sets Available=True on its disposable
APIService through the real status endpoint, then independently requires
successful aggregated discovery and requests. This fixture status is not
evidence of deployment readiness. In the pinned deployment, Milo's
controller-manager registers `RemoteAPIServiceAvailabilityReconciler`, which
marks every APIService with `spec.service != nil` Available=True without
probing its backend. The existing IPAM prerequisite follows the same rule of
evidence as this harness: it does not wait on the Available condition and
instead requires a real project-routed `GET /apis/ipam.miloapis.com/v1alpha1/ippools`
to return an `IPPoolList`. That prerequisite supplies `caBundle` directly
from IPAM's cert-manager-issued Secret when it registers the APIService; it
does not rely on a CA injector for this field. Milo's status reconciler is the
Available-condition writer.

This run does not prove Service object creation or endpoint discovery. It
tests Milo's standard service-DNS resolver through the restricted CONNECT
bridge. The existing IPAM cluster prerequisite registers its real service
name, namespace, and CA and verifies a live API request, but that is not a
claim that this local fixture reproduces cluster networking.

The datumctl portion invokes the exact exported `internal/cmd/apps.Command`
from the pinned CLI commit through this launcher:
`hack/datumctl-apps-aggregation-launcher/main.go`. Its wrapper reads the
generated bearer-token kubeconfig and maps `--project` through the pinned
CLI's `miloapi.ProjectControlPlaneURL` helper. It does not run the full
datumctl OAuth login/session/root-factory path; production IAM is unproven.
Through Milo's real project APIService route, it verifies history, read-only
diff, dry-run restore with unchanged state, exact-definition restore creating
a new revision, foreign-Project revision rejection, dry-run delete with
unchanged state, UID/resourceVersion delete retaining history, same-name
recreation with a new UID, rejection of a stale delete, and history access by
the deleted UID.

The run verifies APIService discovery; template create/update and immutable
revision history; Project UID and effective actor UID attribution;
cross-Project isolation; spoofed incoming `X-Remote-*` headers ignored; an
unauthorized aggregate create denied; a separate real Milo SAR denial; and
the datumctl command behaviors above. Audit summaries confirm the Apps root
identity created SARs for allowed and denied subjects. The test-only RBAC
fixtures prove request plumbing and project resource scoping under Milo RBAC.
They do not prove production IAM/FGA policy, the full datumctl login path,
trusted ownership writers, native App installation, or live workload traffic.

## Reproduction

`hack/build-and-test-apps-authoring-aggregation.py` verifies each source
checkout is at the exact pinned `HEAD` and has no tracked, staged, or
untracked changes before building. It permits exactly one byte-verified
untracked launcher in Milo and one in datumctl; Apps remains clean. The helper
builds all three from those source trees, requires `vcs.modified=true` for the
two launcher builds and `vcs.modified=false` for Apps, then executes the
integration proof. It restores both source trees to clean status in a
`finally` block. Binary metadata supplements these source-tree checks;
revision metadata alone is not treated as provenance. All three builds use
temporary compiler caches and the supplied module cache; datumctl selects its
pinned Go 1.26.9 toolchain from that cache.

```sh
git clone https://github.com/milo-os/milo /tmp/milo-source
git -C /tmp/milo-source checkout ba4b06e8dde99dd70e9a2f34e011d635e4408a76
git clone https://github.com/datum-labs/apps /tmp/apps-source
git -C /tmp/apps-source checkout ccb6cedf6e5a8b29ec1ac74e1dfdaa5ff3db6f23
git clone https://github.com/datum-cloud/datumctl /tmp/datumctl-source
git -C /tmp/datumctl-source checkout 28f4d364302a9df451b7a453cce1ce1cb99ce0a0
ETCD_BIN=/path/to/etcd
DATUMCTL_GOMODCACHE=/path/to/go-mod-cache-with-go1.26.9
python3 hack/build-and-test-apps-authoring-aggregation.py \
  --milo-source /tmp/milo-source \
  --apps-source /tmp/apps-source \
  --datumctl-source /tmp/datumctl-source \
  --datumctl-mod-cache "$DATUMCTL_GOMODCACHE" \
  --etcd-binary "$ETCD_BIN"
```

The test requires permission to bind temporary loopback ports. It creates a
random temporary directory and tokens, keeps sensitive inputs mode-restricted,
does not print bearer tokens or full audit records, gives the entire run a
120-second alarm, bounds subprocess/TLS operations, closes tunnel sockets, and
shuts down the processes on exit.
