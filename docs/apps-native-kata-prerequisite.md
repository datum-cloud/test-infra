# ARM Kata cell prerequisite

This is an optional runtime prerequisite for testing Compute's native
Workload-to-Instance path on an ARM Colima host. The manifests install the
published Kata provider into a dedicated Compute member cluster and install
Kata's QEMU runtime in that cluster. They are not part of the normal Apps or
Compute management composition. Applying them does not establish Workload
admission, placement, quota, network configuration, HTTPRoute delivery, or
Apps install behavior.

The source and artifact lock is
[`image-lock.json`](../components/apps-native-kata/image-lock.json). It pins
Compute `v0.12.10` at `88a7101c2b91f7195c2bb5433c50f54c384a90ce`, Kata provider
`v0.1.10` at `9578d304201428f6d7370460b4a6e9933ef1b25f`, the provider's OCI
bundle and multi-architecture image, Kata Deploy chart `4.1.0`, and its
installer and kubectl-helper image indexes plus ARM64 children. The OCI
artifacts were inspected read-only; the provider image's ARM64 config labels
identify the pinned repository and source revision. The Kata provider module
requires the same Compute version. No mutable `latest` image or chart
reference is used.

The provider's published cell overlay contains a `latest` manager image and
its base declares `v0.1.10`. The source-owned Flux patch replaces that manager
image with the locked multi-architecture digest. It also replaces the full
provider configuration with the reviewed QEMU selector/handler and leaves
VPC networking disabled. The rendered manager runs on the cell control-plane
node with the upstream control-plane toleration; the provider's configured
node selector is for the instance Pods and selects only Kata-labelled nodes.

## Runtime boundary

ARM64 must use Kata's `kata-qemu` handler: Kata 4.x does not ship a Cloud
Hypervisor shim for ARM64. The provider config sets `runtimeHandler: kata-qemu`
and selects only nodes labeled `katacontainers.io/kata-runtime=true`. The
remote HelmRelease installs the QEMU shim and creates the named `kata-qemu`
RuntimeClass without making it the cluster-wide default. The provider bundle
is the upstream `overlays/cell`. One patch configures its QEMU handler,
Kata-runtime node selector, control-plane toleration and disabled
VPC-networking option; a separate patch pins the provider manager image.

There are two KVM boundaries. The Colima Linux VM must expose working nested
KVM from the host it virtualizes. Separately, the dedicated Kind member node
container must receive that VM's `/dev/kvm` character device and `/lib/modules`
tree. The checked-in Kind fixture maps both paths, with modules read-only. The
provider always adds the two guest `accept_dad=0` Pod sysctls; its Kubelet
configuration therefore allowlists exactly `net.ipv6.conf.all.accept_dad` and
`net.ipv6.conf.default.accept_dad`, matching the upstream Kata E2E Kind config.
The fixture also tolerates the single node's `node-role.kubernetes.io/control-plane:NoSchedule`
taint in the provider's instance-Pod config. The Kata DaemonSet and provider
manager already have their own control-plane tolerations; those do not apply to
instance Pods. The node label selector still requires the Kata runtime label.
The config file maps both KVM paths, but
the file cannot prove that the running Colima VM has KVM or that Kind can use
it. Host KVM visibility alone is insufficient. The offline checker verifies
that the fixture requests both mounts and the required scheduling/sysctl
settings; a future runtime preflight must check the host VM and then the Kind
node independently before installing Kata.

Nested ARM QEMU also needs two node-level adjustments from the provider's
own [Kata E2E workaround](https://github.com/datum-labs/kata-provider/blob/9578d304201428f6d7370460b4a6e9933ef1b25f/hack/e2e/kata-workarounds.sh):
clear `cpu_features` in every installed `configuration-qemu.toml`, and expand
the Kind node's `/dev/shm` from its small container default to 8 GiB. Otherwise
QEMU requests a guest PMU unavailable under nested virtualization, or the
guest's shared-memory backing is too small. These changes are not represented
by a false RuntimeClass or a runc substitution. This prerequisite does not
apply them because no VM or cluster is being changed here. They must be made
idempotently by a separately reviewed runtime stage after Kata installation
and reapplied whenever the node is rebuilt.

The host VM also needs enough CPU, memory and disk for nested guests. The
provider's documented example uses 8 CPUs, 16 GiB memory and 60 GiB disk; the
actual resource request must be chosen for the intended instance count and
guest RAM. The `/dev/shm` capacity must cover concurrent guests, and 8 GiB is
the provider's test setup value, not a universal capacity guarantee.

## Apply ordering for a future runtime trial

Use a separate member Kind cluster inside the dedicated Colima VM. Do not
reuse the management cluster. First prove the Colima VM is ARM64 and has
working KVM; create the member with
[`kind-kata-config.yaml`](../components/apps-native-kata/kind-kata-config.yaml),
then prove `/dev/kvm` and `/lib/modules` are visible and usable from its node.
Only after those checks should the pinned Kata Deploy HelmRelease be reconciled
to the member. Apply the provider Kustomization after HelmRelease readiness,
because Flux `dependsOn` cannot express readiness on a HelmRelease; its
Kustomization currently waits for the provider Deployment and the real
`kata-qemu` RuntimeClass. Apply the QEMU PMU and shared-memory workaround and
check the Kata handler before proceeding to any Compute cell trial.

The component is rendered by `kustomize build
components/apps-native-kata`. Its remote Flux resources assume the existing
`compute-a1t-dfw-cell` Kustomization and a task-owned
`apps-a1t-dfw-member-kubeconfig` Secret in `flux-system`; neither is created
here. The runtime is deliberately kept out of the active management cluster.
The source pins and manifest contract can be checked without cluster access.
The render options below use `kustomize`, `helm template`, and strict
`kubeconform` validation for the rendered provider and Kata Kubernetes
resources; kubeconform may download the public Kubernetes JSON schemas for
version 1.33.0.

```sh
python3 hack/check_native_kata_prerequisite.py \
  --provider-source /path/to/clean/kata-provider \
  --compute-source /path/to/clean/compute \
  --provider-layout /path/to/pinned/provider-oci-layout \
  --kata-chart-layout /path/to/pinned/kata-chart-oci-layout \
  --vmrule-crd /path/to/victoria-metrics-k8s-stack/charts/victoria-metrics-operator/crd.yaml
python3 -m unittest hack.test_native_kata_prerequisite -v
kustomize build components/apps-native-kata
```

With the pinned OCI layouts, the checker verifies manifest, source annotation,
layer digest and artifact hash before extracting the actual upstream provider
cell overlay and Helm chart. It applies the Flux patches to the provider
overlay and renders both artifacts with source-owned values. It verifies the
resulting manager, installer and
helper image digests, upstream control-plane scheduling, Kata-labelled
instance selector and control-plane taint toleration for instance Pods, and the
emitted `kata-qemu` RuntimeClass handler. It also validates the exact unsafe
sysctl allowlist needed by the provider's injected guest DAD policy. It checks
clean source revisions, the provider's Compute module version,
digest-only Flux references, the RuntimeClass health wait, and exactly one
KVM device mount plus one read-only module mount in the Kind fixture. It never
calls a Kubernetes API, Docker, Kind creation, Helm installation, registry
mutation, or VM probes. Registry index and platform-child digests in the lock
were independently read and verified during preparation; a later registry
recheck is a separate read-only action.

The provider overlay includes a VictoriaMetrics `VMRule` custom resource.
Strict validation uses the served `VMRule` OpenAPI schema from
`charts/victoria-metrics-operator/crd.yaml` in the test-infra-pinned
`victoria-metrics-k8s-stack` chart version 0.58.3. The image lock pins the
SHA256 of that exact input file, and the checker verifies its bytes before
using the schema; the chart version alone does not establish schema identity.
The Flux resources in this small root are checked against their exact
source-owned contract by the checker; Kubeconform strictly validates the
rendered core Kubernetes objects in the provider and chart artifacts.

## Workload and network evidence still needed

The provider's E2E exercises native Compute `Instance` objects directly. Its
documentation explicitly says Compute `.status.Ready` and
`.status.QuotaGranted` remain Pending because the Compute controller is not
running there. A Kata provider Deployment becoming Ready therefore does not
prove a Compute Workload was admitted, scheduled or turned into an Instance.
The separate real Compute cell controller, genuine Location and InstanceType,
applicable entitlement and quota allowance, and an admitted native Workload
are still required. Kata runs with VPC networking disabled here. A real
Workload-to-HTTPRoute result additionally needs the NSO NetworkService and
Gateway path, including genuine address/endpoint propagation and an HTTP
request through the existing Gateway. No Service, endpoint, route status or
traffic is fabricated by these manifests.

Cleanup for any later runtime trial must remove only resources created by that
trial, using their recorded UID and resourceVersion preconditions. Do not
delete a namespace or member cluster until its complete owned inventory has
been checked. This document records a deployment prerequisite, not evidence
that a runtime trial or Workload route has passed.
