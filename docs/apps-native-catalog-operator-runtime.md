# Native Service Catalog operator fixture

This optional A1-T fixture installs the real Catalog manager in the dedicated
Apps VZ test cluster. It does not create an entitlement, Service, Workload,
Instance, App, or route. The retained prerequisites are the scoped Catalog
installer, the 22 source-owned Catalog/Billing/Locations CRDs, the exact OCI
Catalog API transition, the Locations manager/class/Location, and the single
source-owned LocationBinding CRD. The fixture is not a production IAM test:
Milo's static RBAC test mode grants the dedicated operator User across Project
paths.

The reviewed Catalog source is `5f2b663b6c5e039c4c7b666ef3e959803851b675`.
The published Kustomize OCI source is pinned to
`sha256:d34680228b91a5a5d46fedb40700f805b9b2ce97f13232e178cffa2db2cae431`.
The manager index is
`sha256:a5c5e4c31c9f89edac64d6a4c02a70320c04b4e1ca95aabab6567a0a1357b1e2`;
its registry-observed arm64 manifest is
`sha256:eb02b6b364d7ba21234a9049ae549ed934e06a12a937c2e4259577bf9c490485`.
The arm64 image labels identify the full Catalog commit and repository. The
fixture checks the exact running Pod image ID as well as the Flux spec image.
The diagnostic TLS Pod uses the pinned curl index
`sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6`
and mounts only a public CA ConfigMap; it has no service-account token.

The source overlay renders eight objects. The host Flux stage consumes the
same OCI source and renders six: it omits the upstream Namespace and
self-signed Issuer, changes only the manager image, the source-config
`locationSource` to `locations.miloapis.com/v1alpha1`, and the serving
Certificate's issuerRef. A separate test-infra GitRepository pinned to the
runner's signed commit installs one self-signed Issuer, one CA Certificate, and
one CA Issuer. The serving private key stays in the host cluster. Before
binding webhook trust, the runner verifies the public CA, serving SAN and
chain, exact Ready manager replica and image, host Service TLS, and a
no-token Pod's DNS/TLS connection from the Milo namespace. NetworkPolicy
selectors must exclude both the Milo API and probe Pods; the target namespace
must have no NetworkPolicy. The existing Catalog API Kustomization is the sole
owner of its eight Milo webhook registrations. The runner temporarily extends
only that same source-bound Flux spec with the public CA, then checks all eight
source rules, Service targets, failure policies, UIDs, and bundles.

The operator receives a fresh random static Milo User bound to the exact
source ClusterRole. A dedicated CA-backed kubeconfig containing only that User
enters `services-system/milo-kubeconfig`. Root setup uses a documented,
UID-journaled direct-API bootstrap for that exact role and binding because the
retained Catalog installer is deliberately limited to CRDs and webhooks. The
operator identity must allow Project/Service/Location reads and Service status
writes while denying Secrets, RBAC creation, impersonation, and Compute
Workload creation. No existing admin token is copied to the manager.

Use one clean signed test-infra checkout, the exact retained journals, and
explicit private contexts. Omit `--execute` for the read-only preflight; add
it only after review. `--cleanup` uses the *same* signed source and evidence
file, removes the public-CA patch before stopping the manager, checks empty
Catalog/LocationBinding data in root and all three Projects, prunes source-owned
host/CA stages, proves all selected manager Pods gone, restores the original
four-user static auth with a fresh Milo API Pod rollout, and removes the
scoped role, binding, Secret and sources by UID/resourceVersion. The exact
task-created `services-system` Namespace is retained to avoid cascading
deletion of foreign contents. An uncertain resource, foreign data, or partial
Flux prune retains prerequisites and the private journal for recovery.
If cert-manager creates a generated TLS Secret but the runner loses its UID
before journaling it, cleanup retains that Secret and the CA prerequisites for
manual source/UID review. It does not infer ownership from the Secret name.

The runner accepts explicit `--host-kubeconfig`, `--host-context`,
`--milo-kubeconfig`, `--milo-port`, and `--evidence-file`, plus paths to the
retained installer, Catalog API, Catalog OCI, Locations, and LocationBinding
evidence and the clean Catalog source and OCI manifest/layer. It has an
overall bounded deadline. Its default mode is read-only; no current live
operator result is claimed by this document.

Offline review commands:

```sh
task test-apps-native-offline
kustomize build components/service-catalog/operator-ca
python3 -m unittest discover -s hack -p 'test_apps_native_catalog_operator.py' -v
```

The clean external Catalog checkout and the cached exact OCI layer are needed
for the full source/tree/Flux render gate. Local source verification has
rendered exactly three CA and six host objects. The draft fixture has not yet
been reviewed or executed against the live cluster. A nonpersisting server
dry-run of the no-token TLS probe Pod against the dedicated Kind API confirmed
the exact Kubernetes default-field normalization used for lost-CREATE cleanup.
Positive entitlement,
Workload admission, native instance networking, guest traffic and full Apps
installation remain separate gates.
