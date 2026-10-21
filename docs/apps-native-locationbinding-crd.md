# Native Catalog LocationBinding schema prerequisite

The live VZ fixture has Catalog and Billing CRDs, eight Catalog admission
webhooks without CA bundles, three real Projects, and the Locations controller's
`datum-managed` class and `dfw` Location. It has no
`locationbindings.networking.datumapis.com` CRD. Catalog source
`5f2b663b6c5e039c4c7b666ef3e959803851b675` still projects that kind
alongside `locations.miloapis.com/v1alpha1` objects, using Milo's native
multicluster provider. This stage installs the real schema only. It deploys no
Catalog operator, creates no entitlement, and proves no Workload or traffic.

The generated CRD is copied without modification from
[`datum-cloud/network-services-operator` at `79245f00d6220c128df3d024475ff7b1566ce305`](https://github.com/datum-cloud/network-services-operator/blob/79245f00d6220c128df3d024475ff7b1566ce305/config/crd/bases/networking.datumapis.com_locationbindings.yaml).
The exact source bytes have SHA-256
`73fc1945ec88f651ee6ace8518a4593792c005af0d52dbc8da3e22d1f173f875`.
This is the same schema digest previously used at NSO revision `4c4c48e`;
the current revision still serves cluster-scoped `networking.datumapis.com/v1alpha`
`LocationBinding` with the Catalog projection's `locationRef`, class, topology,
and conditions fields. NSO's `config/crd/downstream` bundle excludes the
LocationBinding CRD and includes unrelated APIs, so this fixture has its own
one-object Kustomize root.

The runner generates a `GitRepository` at the **clean, signed current
test-infra commit** and verifies its fetched artifact revision before it
creates a remote Flux Kustomization. There is no mutable branch or unpinned
source. Flux uses the existing CA-verified, scoped Catalog installer Secret;
it never receives the Milo admin kubeconfig. A temporary exact-name role grants
that same installer User only get, patch, update, and delete on this CRD; the
original installer role already grants CRD create. The temporary role and
binding are UID-journaled and remain until Flux prune has removed the CRD.
The runner tests that another CRD name, Secrets, RBAC creation, and
impersonation remain denied.

Before any cleanup prune, the runner also requires the source-owned Catalog
operator Flux stages to be absent, the host `services-system` namespace to
contain no workloads or operator credential, and no selected Catalog manager
Pods anywhere in the host cluster. It binds the original Catalog webhook UIDs
and requires all eight hooks to retain their fail-closed, empty-CA Service
registration. A writer, changed webhook, or unknown object retains the CRD
and the scoped installer for review. These are API census checks in an
exclusive test fixture; they cannot make a concurrent external write atomic
with deletion.

The private evidence path must be under the task-owned 0700 directory beside
the existing Catalog installer, Catalog API graph, and Locations journals.
A signed, published
test-infra commit is required before `--execute`, because Flux must fetch that
exact commit from GitHub. Invoke the runner from any directory with explicit
host and Milo kubeconfigs and contexts; first omit `--execute` for preflight,
then add it after review. Keep the original evidence file for `--cleanup`.

```sh
python3 hack/bootstrap-apps-native-locationbinding-crd.py \
  --host-kubeconfig /private/tmp/apps-native-vz-host-20261021/host.kubeconfig \
  --host-context kind-apps-a1t-ci-vz-20261021 \
  --milo-kubeconfig /private/tmp/apps-native-vz-projects3-20261021/milo-root.kubeconfig \
  --milo-port 43443 \
  --installer-evidence-file /private/tmp/apps-native-vz-projects3-20261021/catalog-installer-attempt2-evidence.json \
  --catalog-api-evidence-file /private/tmp/apps-native-vz-projects3-20261021/catalog-api-evidence.json \
  --locations-evidence-file /private/tmp/apps-native-vz-projects3-20261021/locations-evidence.json \
  --evidence-file /private/tmp/apps-native-vz-projects3-20261021/locationbinding-crd-evidence.json
```

Only the isolated test owner should add `--execute` or `--cleanup` after
reviewing the corresponding signed source and retained journals.
Cleanup verifies the same host, root CA, Projects, prerequisite journals,
source commit, run marker, Flux spec and inventory, CRD UID/schema, and zero
LocationBinding objects in root and all three Project APIs. A changed or
foreign object causes a retained prerequisite and a private recovery journal.
If Flux applied the CRD but failed before recording it in inventory, the
runner uses a UID/resourceVersion-conditional delete only after the exact
Flux Kustomization is absent and the empty-object census passes. It records
that fallback intent before the delete request.
The Catalog operator and any entitlements must be removed before invoking
this CRD cleanup.

No live execution has been done for this slice. The test-infra offline suite
exercises source-byte drift, exact-name grant scope, foreign Project/binding
prune refusal, changed Git source refusal, and partial Flux cleanup ordering.
