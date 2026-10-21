# Kata `Netns` networking candidate

This is an offline, test-cell-only provider-manager candidate. It replaces the
existing `apps-a1t-kata-provider` Flux Kustomization and enables the provider's
interface-injection opt-in while keeping the pinned QEMU handler. It is not
included in the default `cluster/kustomization.yaml`; applying it would change
the member provider configuration. Do not apply baseline and candidate as
separate providers: they intentionally share one Flux object identity and
manager.

The candidate is not yet a complete Netns profile. The locked provider
`./overlays/cell` renders 12 objects and contains no Compute `RuntimeClass`.
That object comes from a distinct `./components/runtime_classes` publisher
path, which renders exactly one `general-purpose` class with
`networkAttachment: HypervisorDeclared`. The candidate intentionally has no
RuntimeClass patch and does not change that class. A separate source-owned
Flux publisher using an approved scoped Milo API credential is still required
before any claim that Compute selects `Netns`; the broad configuration
credential used by existing test infrastructure is not a substitute.

The source contract is plausible at these pins:

- Kata 4.1.0 is tag commit `ddcb1ad8d23cbb4323f86c209f132b89592902df`.
  Its Linux Go shim scans the sandbox network namespace and recognizes veth
  interfaces (`src/runtime/virtcontainers/network_linux.go:203-205,405-461`).
  The release publishes `kata-go-static-4.1.0-arm64.tar.zst` and
  `kata-static-4.1.0-arm64.tar.zst`; the pinned Kata Deploy chart is 4.1.0.
- Compute source `88a7101c2b91f7195c2bb5433c50f54c384a90ce` resolves a workload's
  RuntimeClass `networkAttachment` into the WorkloadDeployment, then copies it
  to each NetworkInterfaceClaim (`internal/controller/workload_controller.go:568-599,627-629`;
  `workloaddeployment_controller.go:78-90,887-899`). `Netns` is a supported
  value and means the interface is for a network namespace.
- NSO source `79245f00d6220c128df3d024475ff7b1566ce305` maps `Netns` to the
  `galactic-veth` plugin. Its controller writes that CNI config into the
  NetworkAttachmentDefinition (`internal/galactic/controller/networkinterface_controller.go:212-236,274-289`).
  Galactic module `v0.5.8-0.20261016143526-45d3c3514bec` creates the veth;
  its result builder records the returned interface's sandbox as `args.Netns`
  and applies the assigned IP configuration in that namespace
  (`internal/cni/ops_add.go:28-40,138-139`;
  `internal/cni/result.go:17-50,53-95`).
- The Kata provider pin is `9578d304201428f6d7370460b4a6e9933ef1b25f`, with
  ARM64 manager image locked in `components/apps-native-kata/image-lock.json`.
  Its current class says `HypervisorDeclared` and provider networking is
  disabled by the baseline profile. This candidate changes only the provider
  opt-in. In `internal/controller/vpc_networking.go:16-32` the provider adds
  `networking.datumapis.com/inject-interfaces=true` only when networking is
  enabled and the instance requests interfaces; `instance_controller.go:456-459`
  applies/removes that label. It does not change the separately published
  Compute class.

This establishes compatible component-level contracts, not a working cell.
After a separately scoped RuntimeClass publisher selects `Netns`, the key
unverified point is whether the CNI `args.Netns` namespace for the
Workload is the namespace the Kata 4.1.0 shim scans after QEMU starts. The
candidate must be exercised on an isolated ARM cell with `EnableVPCNetworking`
enabled, a genuine NetworkContext/VPC and allocation, a real
NetworkInterfaceClaim and NAD, and the actual `galactic-veth` CNI plugin. Do
not treat a claim address, CNI result, Pod IP, Ready condition, or fabricated
status as guest attachment evidence.

The live check should fail closed if the shim scans a different namespace, if
the `eth0` guest endpoint lacks the assigned address/route, if the NSO
NetworkService member never reaches `HolderAvailable=True`, or if a request
through the real Gateway `HTTPRoute` → `NetworkService` backend fails. It must
also verify that a container with an unsupported interface/route cannot be
mistaken for a successful guest attachment. No live resources are created by
this candidate or its offline tests.

## Project catalog publication candidate

At Milo revision `200de48a305732720377cb83308d57c84fd5f688`, the provider
RuntimeClass cannot be published only in Milo's Platform storage
and assumed visible in a Project API. Milo's Project router selects a separate
per-Project storage prefix (`pkg/server/filters/projects.go` and
`internal/apiserver/storage/project/mux.go`). The discovery annotation on
the CRD controls discovery visibility; it does not copy catalog objects across
those prefixes. Compute's workload webhook and WorkloadDeployment controller
at Compute revision `88a7101c2b91f7195c2bb5433c50f54c384a90ce` read RuntimeClasses
using the engaged Project client (`internal/webhook/v1alpha/workload_webhook.go`
and `internal/controller/workload_controller.go`). Compute currently has an
InstanceType projector, but no RuntimeClass projector. The Project therefore
needs an explicit, platform-owned Project-scoped provisioning step.

`components/apps-native-kata-runtimeclass-project` is an offline candidate for
the single fixture Project `apps-a1t-consumer-a`. It points Flux at the pinned
provider bundle's source-owned `components/runtime_classes` Kustomize
Component and changes only `spec.networkAttachment` from
`HypervisorDeclared` to `Netns`. It is deliberately `suspend: true`, absent
from all default cluster roots, and references a Secret that is not provided.
The candidate carries bootstrap sentinels for the Project endpoint and UID; it
cannot be activated until a future fixture bootstrap verifies that Project's
UID and creates `apps-a1t-runtimeclass-project-kubeconfig` for the exact
Project endpoint. No credential is included in this repository.

That future identity must be a no-impersonation static Milo identity limited to
`compute.datumapis.com/runtimeclasses` operations needed by Flux (`get`, `list`,
`watch`, `create`, `patch`, and `delete`). Milo's static-token/RBAC fixture does
not provide a Project-only boundary for that GVR: the endpoint pins this one
fixture route, but the credential's matching GVR rights can also be used against
other Project routes. This limitation makes the candidate unsuitable as a
production delegation mechanism.

No status is fabricated. Compute treats missing or Unknown RuntimeClass
availability as Pending and allows it during provider rollout; it rejects only
a class whose Available condition is False
(`internal/validation/runtimeclass_validation.go:87-99`,
`pkg/runtimeclass/catalog.go:101-118`). This candidate does not claim that the
provider currently reports an Available condition.

The rendered catalog alone also does not make the real cell placeable. Compute
selects Karmada cells using both the Location label and
`compute.datumapis.com/runtime-class.general-purpose=true`
(`internal/controller/workloaddeployment_federator.go:470-474,838-844`). The
provider candidate does not publish that label, and the pinned provider
controller does not write the Karmada Cluster object. A later real Compute
placement trial needs a source-owned cell-serving signal that is set only after
the provider is verified ready; manually adding the label would not prove that
path.

The candidate is not a supported or runnable root. Its offline contract test
checks that it remains suspended and that the source-owned Component is wrapped
through Flux. To render it against the pinned provider artifact at revision
`9578d304201428f6d7370460b4a6e9933ef1b25f` in the review
environment, use:

```sh
python3 hack/render_runtimeclass_project_candidate.py \
  --provider-layout /path/to/apps-kata-provider-oci-layout \
  --compute-source /path/to/clean-compute-88a7101-source \
  --output /tmp/runtimeclass-candidate.yaml
```

The renderer requires a clean Compute checkout at `88a7101c2b91f7195c2bb5433c50f54c384a90ce`, verifies the source CRD file SHA-256 in `schema-lock.json`, derives the RuntimeClass schema from that CRD, and strictly validates the rendered object with zero skips. It also renders the repository-pinned Flux v2.7.5 installer CRD and checks its locked Kustomization schema hash before validating the suspended Flux object. The optional output file is written only after validation succeeds. This proves only that the source-owned provider component produces one RuntimeClass and the candidate patch changes only `networkAttachment`; it does not prove a Milo credential, Project admission, catalog availability, cell placement, guest interface attachment, or HTTP traffic.

The source and registry pins for this comparison are kept in
[`network-contract-lock.json`](../components/apps-native-kata-netns/network-contract-lock.json).
The immutable provider and NSO image/bundle digests were read from their OCI
manifests and image config labels; the Kata 4.1.0 source revision comes from
the public Git tag. Kata's release asset list names both arm64 Go-shim and
static archives. This does not establish that the local Kind node has working
KVM or that CNI is installed in that node.

Run the profile contract checks with:

```sh
task test-apps-native-offline
kustomize build components/apps-native-kata-netns
```

With the pinned provider OCI layout, run the complete offline provider render
and strict schema check (the source directories below are inputs from the
artifact review environment):

```sh
python3 hack/render_kata_netns_candidate.py \
  --provider-layout /path/to/apps-kata-provider-oci-layout \
  --vmrule-crd /path/to/victoria-metrics-k8s-stack/charts/victoria-metrics-operator/crd.yaml
```

The renderer uses Flux build with the actual candidate Kustomization and exact
locked bundle, including its target namespace and patches. It
requires each target to exist exactly once, then strictly validates all 12
rendered objects using Kubernetes schemas plus the pinned VMRule schema. It
separately renders the provider's
`components/runtime_classes` path to make clear that the class is a different
publisher input and remains `HypervisorDeclared`. Tests reject a missing or
duplicate patch target. None of these checks calls Kubernetes or proves
admission, placement, allocation, guest networking, or tenant traffic.

Lead validation on 2026-10-21 passed all 12 provider resources with zero errors
or skips, and the candidate Flux object passed its pinned Flux 2.7.5 CRD schema.
The full offline suite passed 105 tests. The first sandbox-only schema fetch
failed on restricted network access; the read-only schema-download retry passed.
The misplaced RuntimeClass patch was caught by rendering before any deployment
and removed. No RuntimeClass publication or live networking result is claimed.

Independent lead verification on 2026-10-21 repeated the full offline suite and
the artifact-backed renderer. Both the one RuntimeClass and one Flux
Kustomization passed strict source-schema validation with zero skipped objects.
The rendered RuntimeClass is 2,904 bytes; validation does not make this
suspended candidate a supported deployable root or prove live admission.
