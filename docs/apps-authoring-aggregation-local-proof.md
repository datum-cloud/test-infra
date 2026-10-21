# Local Milo to Apps aggregation proof

`hack/test-apps-authoring-aggregation.py` runs a disposable Milo API server,
Apps API server, and etcd instance on loopback. It exercises Milo's real
APIService proxy and Apps' real HTTPS handler. The script creates Projects,
RBAC bindings, and the APIService through Milo's public API; it does not
replace Project lookup or SubjectAccessReview handling with fake endpoints.

The verified source pins for the passing run were:

- Milo `ba4b06e8dde99dd70e9a2f34e011d635e4408a76` (the baseline used by the live test-infra deployment)
- Apps `19b331f6812bd94c93d7b09d8754f2016353ae87`
- Test-infra base `7c1987912760005b38146bfa8f1fef9452b7749e`

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

The run verified APIService discovery; template create, update, and two-entry
immutable revision history; Project UID and effective actor UID attribution;
cross-Project isolation; spoofed incoming `X-Remote-*` headers ignored; an
unauthorized aggregated create denied; and a separate request routed to Apps
with a real Milo SAR denial. Audit summaries confirmed the Apps root identity
created SARs for both the allowed and denied subject. The test-only RBAC
fixtures prove the request plumbing and resource scoping under Milo RBAC. They
do not prove production IAM/FGA policy, trusted ownership writers, native App
installation, or live workload traffic.

## Reproduction

`hack/build-and-test-apps-authoring-aggregation.py` verifies each source
checkout is at the exact pinned `HEAD` and has no tracked, staged, or
untracked changes before building. It then permits exactly one Milo source
addition, `cmd/milo/appsaggregationprobe/main.go`, and byte-compares it with
the launcher checked into this test-infra revision. Apps remains clean. The
helper builds both binaries from those checked-out sources with `-buildvcs`
and private temporary Go caches, requires Milo `vcs.modified=true` (the
single launcher addition) and Apps `vcs.modified=false`, then executes the
integration proof. Binary build metadata supplements these source-tree checks;
revision metadata alone is not treated as provenance.

```sh
git clone https://github.com/milo-os/milo /tmp/milo-source
git -C /tmp/milo-source checkout ba4b06e8dde99dd70e9a2f34e011d635e4408a76
git clone https://github.com/datum-labs/apps /tmp/apps-source
git -C /tmp/apps-source checkout 19b331f6812bd94c93d7b09d8754f2016353ae87
ETCD_BIN=/path/to/etcd
python3 hack/build-and-test-apps-authoring-aggregation.py \
  --milo-source /tmp/milo-source \
  --apps-source /tmp/apps-source \
  --etcd-binary "$ETCD_BIN"
```

The test requires permission to bind temporary loopback ports. It creates a
random temporary directory and tokens, keeps sensitive inputs mode-restricted,
does not print bearer tokens or full audit records, gives the entire run a
120-second alarm, bounds subprocess/TLS operations, closes tunnel sockets, and
shuts down the processes on exit.
