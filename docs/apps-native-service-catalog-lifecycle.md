# Native Compute Service and entitlement fixture

This bounded test-infra stage connects the retained Catalog operator to the
real Milo Project APIs. Its source is Service Catalog
`5f2b663b6c5e039c4c7b666ef3e959803851b675`. It creates the source sample
`compute.miloapis.com` Service with the fixture provider Project as owner,
publishes one configuration supporting the real `datum-managed` Location class,
and publishes DFW availability. It then creates one ServiceEntitlement in
`apps-a1t-consumer-a`. Only the source-owned Catalog controller may write
statuses, ServiceConsumer, LocationBinding, projected Location, and mirrored
availability. The unentitled `apps-a1t-consumer-b` must answer each relevant
Project API list and have no corresponding objects.

The runner requires an immutable signed test-infra revision containing the
reviewed operator fixture, a clean exact Catalog checkout, explicit private
host and Milo kubeconfigs, the completed operator and Locations journals, and
the existing three Project UIDs. The current retained operator run is
`26f1256c680ce3569e465b60`; the source DFW Location UID is
`e6ad040f-f176-4521-9dd8-134054971573`. The host-held Milo root config is
used only by this test fixture. These observations prove native API/controller
behavior in the static-RBAC test overlay, not production tenant IAM.

After the revision is signed, create a fresh mode-0700 private directory and
run the script first without `--execute` using these arguments:

```text
--host-kubeconfig /private/tmp/apps-native-vz-host-20261021/host.kubeconfig
--host-context kind-apps-a1t-ci-vz-20261021
--milo-kubeconfig /private/tmp/apps-native-vz-projects3-20261021/milo-root.kubeconfig
--milo-port 43443
--catalog-operator-evidence-file /private/tmp/apps-native-vz-projects3-20261021/catalog-operator-attempt2-evidence.json
--locations-evidence-file /private/tmp/apps-native-vz-projects3-20261021/locations-evidence.json
--catalog-source /tmp/apps-service-catalog-source-20261021
--evidence-file PRIVATE_DIRECTORY/service-evidence.json
--deadline-seconds 900
```

The read-only preflight checks clean source revisions, host/Milo CA and API
identity, retained Project UIDs, source-owned operator Pod/image/Flux/webhook
identities, DFW Location Ready and topology, and collision-free root and
Project paths. With `--execute`, each CREATE has a fsynced run-labelled intent
before the public API POST, and its returned UID is journaled immediately.
The runner waits for current-generation native Service, configuration,
availability, entitlement, and provider consumer conditions. Its
LocationBinding condition has no generation field in this source, so the
fixture requires an Available=True condition plus exact projected spec and
real source Location identity; it does not invent an observed generation.

For teardown, invoke the same signed script with `--execute --cleanup` and the
same evidence file. It first conditionally deletes the entitlement and waits
for its native finalizer and all source-owned consumer/location projections to
disappear. It then deletes root availability, configuration, and Service in
reverse order with UID and resourceVersion preconditions. Unknown UIDs after
a lost CREATE response are recovered only from exact source spec, run label,
and prior create-intent digest. Replaced, foreign, or unexpectedly terminating
objects stop cleanup and retain the journal and upstream operator/Projects for
review. Setup failures receive a separate 300-second cleanup budget. Source
operator, Projects, and Location remain retained for the next Compute stage.

This stage proves native Catalog gates and isolation only. It does not claim
Workload admission, an Instance, HTTPRoute traffic, Apps installation, or
production IAM. The controller's asynchronous projection sweep is not an
atomic cleanup transaction; this is an exclusive task-owned fixture and any
uncertain object or Project replacement fails closed.
