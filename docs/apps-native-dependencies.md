# Apps A1-T native dependency feasibility (2026-10-20)

The existing Milo baseline is not an Apps or native Workload end-to-end result.
This slice pins the next real service sources and installs only Service Catalog's
native CRDs into the Milo API. It leaves the operator and tenant traffic gated
on the prerequisites below.

| Service | Git commit | Source-matched OCI image index digest |
| --- | --- | --- |
| Service Catalog | `milo-os/service-catalog` `5f2b663b6c5e039c4c7b666ef3e959803851b675` | `ghcr.io/milo-os/service-catalog@sha256:a5c5e4c31c9f89edac64d6a4c02a70320c04b4e1ca95aabab6567a0a1357b1e2` |
| Compute | `datum-cloud/compute` `88a7101c2b91f7195c2bb5433c50f54c384a90ce` | `ghcr.io/datum-cloud/compute@sha256:1e213d5dcff0be340788d80df6fbdc235b691830667526234ee131240a89e679` |
| Network Services Operator | `datum-cloud/network-services-operator` `4c4c48ecb5e7f0f4d1950e6441ebe0c02afcfb96` | `ghcr.io/datum-cloud/network-services-operator@sha256:1b22f9cd1280602c63963fb9d1d1ef0f7207550018cd9d65de773728f5bd3444` |

For all three images, the amd64 and arm64 OCI labels report the full Git SHA
above as `org.opencontainers.image.revision` and the matching GitHub repository
as `org.opencontainers.image.source`. A short tag alone was not used as proof.

The pinned Service Catalog `config/base/crd` renders eight real CRDs. The
`components/service-catalog` Flux graph applies those CRDs to Milo's API using
the test-only kubeconfig already provided by the pinned Milo overlay. In the
isolated `apps-a1t-gateway-20261020` cluster, Flux fetched
`sha1:5f2b663b6c5e039c4c7b666ef3e959803851b675` and the
`service-catalog-api-crds` Kustomization reached `Ready=True`. Its inventory
contained exactly eight CustomResourceDefinitions, including `Services` and
`ServiceEntitlements`; no operator was installed and no entitlement was
asserted. A live authenticated request through the Gateway to
`/apis/services.miloapis.com/v1alpha1` returned HTTP 200 and an
`APIResourceList` containing `services` and `serviceentitlements`. The
separate Colima profile and private kubeconfig were retained;
no shared cluster or Docker state was changed.

Service Catalog's real Milo E2E deployment splits the operator overlay
(`config/overlays/e2e-milo/operator`) from Milo API CRDs/webhooks
(`config/overlays/e2e-milo/milo-resources`). The operator needs a distinct Milo
access kubeconfig, webhook certificate and CA injection, billing and Locations
CRDs, an organization, ready projects, and their project control planes. Its
standalone E2E bootstrap provisions these explicitly. That bootstrap currently
references the billing bundle by the floating `v0.0.0-main` tag, so the Apps
harness must resolve and record an exact OCI digest before including it.
Installing only the
operator Deployment against the current baseline would leave these contracts
unmet, so it has not been added to CI.

Compute's management-plane overlay renders a real controller but also includes
`GrafanaDashboard` and `VMRule` resources and expects a downstream kubeconfig,
Karmada token, webhook certificate, and cell controllers. Its own E2E setup
creates a control-plane cluster, Karmada API, and two POP clusters before
deploying the operators. Network Services Operator's E2E overlay likewise
needs upstream and downstream clusters, Karmada hub, project-scoped IPAM,
downstream Envoy Gateway, DNS and billing dependencies, and kubeconfig Secrets.
Its rendered upstream overlay contains a separate extension-server image tag,
so blindly replacing only the manager image would not pin the whole runtime.
These are required real services for a native Workload-to-route assertion;
a plain Kubernetes Deployment or Gateway fixture cannot stand in for them.

The next harness stage should compose these existing E2E contracts with Milo
and Apps' own test overlay, assign pinned image/bundle digests to every
controller and extension, and run on an isolated multi-cluster environment
with enough disk and memory. CI must wait for native Workload readiness and
send traffic through its actual route before reporting A1-T/G4 success.
