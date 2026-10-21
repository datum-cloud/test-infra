# Member-local Kata QEMU runtime smoke

This optional runtime stage exercises one ARM64 Kata QEMU guest inside a new,
one-node Kind member of the already-running dedicated Colima VZ profile. It
does not create, start, stop, resize, or otherwise manage Colima. The helper
refuses an existing member cluster, reads the host API to verify the dedicated
host identity, and never mutates or deletes the host Kind cluster. Keep the
smoke member dedicated to this trial.

A passing smoke proves only that this member can start a Kata QEMU guest with
a separate ARM64 kernel using the pinned image. It does not prove Compute
Workload admission or placement, Location or quota behavior, NetworkService,
Gateway/HTTPRoute routing, HTTP traffic, or Apps installation. No ordinary
Pod is used as a substitute for a native Workload in those later gates.

The source-owned local component is
[`components/apps-native-kata-runtime-smoke`](../components/apps-native-kata-runtime-smoke).
It installs Flux in the member, then lets that member's own source and Helm
controllers reconcile the pinned Kata Deploy OCI chart. The `HelmRelease` has
no `spec.kubeConfig`; it cannot target a separate cluster. The Kata DaemonSet
installs only QEMU and creates a named `kata-qemu` RuntimeClass without making
it the default. The member Kind node maps `/dev/kvm` and a read-only
`/lib/modules` tree and allowlists only the two guest DAD sysctls used by the
upstream Kata provider fixture.

The smoke uses `docker.io/library/busybox:1.36`, pinned to index
`sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662`.
Read-only registry inspection on 2026-10-21 resolved the linux/arm64/v8 child
manifest to `sha256:bd44eb136a95dcc8dc58995e43abc40a413f2e8e3d4a2aae6bccbe94686acb05`.
That child binds config digest
`sha256:b7c873bd97bdc9046fc894e0b216c8aef31e0db4c5b761fb53022e40577ab6c0`.
Its image config reports `architecture=arm64`, `variant=v8`; its OCI revision
is `b6ab7c14ba46700181395f420545aee1ab297934` from
`https://github.com/docker-library/busybox`. The lock also includes the
verified ARM-compatible Kind node image and the Kata chart, installer, and
kubectl helper index, ARM64 manifest, and config digests. The full artifact
check validates each public index-to-ARM64-child-to-config relationship. The
runtime does not depend on mutable image tags.

## Read-only manifest validation

The repository's offline task validates the rendered Flux `OCIRepository` and
`HelmRelease` against checked-in source-schema snapshots pinned to Flux
`v2.7.5` and its commit. It requires strict kubeconform output with zero skips.
The CI path filter runs this task for changes to the component, checker,
runner, tests, docs, or workflow. Use the chart artifact matching the digest
in the lock for the fuller read-only render; this fetch writes only the local
OCI layout:

```sh
crane pull --format=oci \
  quay.io/kata-containers/kata-deploy-charts/kata-deploy@sha256:33f102f6db70083de4fc8238af4439c4245a601bcb6a72e49bc80529098aefc0 \
  /private/tmp/apps-kata-chart-layout
python3 hack/check_apps_native_kata_runtime_smoke.py \
  --chart-layout /private/tmp/apps-kata-chart-layout \
  --verify-smoke-image-registry
python3 -m unittest hack.test_apps_native_kata_runtime_smoke -v
task validate-apps-native-kata-runtime-smoke
```

The checker verifies the OCI manifest/layer digests and chart metadata, renders
the exact values with `helm template`, checks the emitted images and
`kata-qemu` RuntimeClass, and runs strict `kubeconform` against Kubernetes
1.33.0 schemas. Flux custom resources are checked against the source-owned
contract, including the absence of a remote `kubeConfig`; the rendered chart
resources are validated against Kubernetes schemas. Schema retrieval may
need network access on a fresh machine.

## First execution and validator correction

The first signed `08500ee7db603f1dc85847522f6631be67524050` trial on
2026-10-21 created its member, then stopped at the module-mount preflight.
KVM was present and Docker reported the modules bind read-only, but the node's
`/lib` symlink made the effective mountpoint `/usr/lib/modules`. The old check
searched only `/lib/modules`. No Flux, Kata or guest resources were installed.
The original signed helper subsequently removed the exact recorded member;
the host remained Ready. The failure journal is retained separately from any
retry, which must use a new private evidence directory.

The same source's offline CI passed its focused tests but failed the final
schema validator. CI pins kubeconform v0.6.7, whose local schema reader treats
`file://` as a literal path; the initial local check used v0.7.0, which accepted
the URI. The checker now supplies an absolute filesystem path, and reports
bounded validator errors for its public manifests. Both versions validate
the two Flux resources with zero errors or skips. The full offline task also
passes with CI's exact v0.6.7. These corrections do not establish guest boot.

## Dedicated VM inotify prerequisite

Host and member containers share the VM's per-UID inotify instance allowance.
The second trial reached Flux installation, but its kube-proxy reported
`too many open files` with root holding 127 instances against the VM limit of
128. Node memory/disk/PID pressure was absent and the images had pulled.
This is the same VM prerequisite documented in
[the federation fixture](apps-federation-prerequisite.md).

Before adding a member, the read-only preflight requires at least 1,024 inotify
instances and records the observed value in the creation journal. This is a
minimum for this fixture, not a general capacity guarantee. Cleanup does not
require that allowance, so exhausted capacity cannot block member removal.
The runner never changes a kernel setting automatically.

For the explicitly selected, task-owned VM, inspect the value and apply the
documented setting if it is lower; preserve a higher existing setting:

```sh
colima ssh --profile apps-native-vz-20261021 -- \
  cat /proc/sys/fs/inotify/max_user_instances
colima ssh --profile apps-native-vz-20261021 -- \
  sudo sysctl -w fs.inotify.max_user_instances=1024
```

Record the original and resulting values with the fixture evidence. This
command changes only the running dedicated VM and is not persistent across
VM restarts. Run the read-only preflight after every restart and prepare this
prerequisite again when necessary. No host macOS or unrelated profile setting
is changed. Recovery observations from an already-failed member are separate
diagnostics; a complete smoke requires a fresh run and evidence directory.

## Proposed execution sequence

Choose exact names for the existing dedicated VZ profile and host cluster.
The host evidence directory must be the private directory emitted by the
reviewed host bootstrap; it contains the original intent, private host
kubeconfig and host Kind node container ID. Choose a fresh private output path
and a new member cluster name. The helper uses a process-local Docker socket
for that VZ profile, clears any inherited `DOCKER_CONTEXT`, and passes the
explicit host or member kubeconfig and context to every Kubernetes command.
It never changes the caller's Docker or kubectl context.

The read-only preflight verifies the host intent and node identity, ARM64
daemon, `/dev/kvm` on the VZ guest, exact one-node host cluster, host API
identity, absent member name, and absent private output directory. It reports
the Docker daemon CPU count; the existing six-CPU minimum is only a measured
preflight condition, not evidence that six CPUs are sufficient for QEMU.
Memory, shared memory, disk, and guest boot readiness must be judged by the
actual bounded run.

The host bootstrap evidence reader accepts the current JSON intent and the
reviewed legacy format of exactly two lines (profile name, then Kind cluster
name). It reads both without rewriting. The host node container ID, Kubernetes
Node UID, HTTPS API endpoint, and CA certificate SHA256 are bound into the
smoke journal and rechecked before cleanup. Execution verifies a clean,
signature-verifiable source before creating the private output directory.

```sh
python3 hack/run_apps_native_kata_runtime_smoke.py \
  --profile apps-native-vz-20261021 \
  --host-cluster apps-a1t-ci-vz-20261021 \
  --host-evidence /private/tmp/apps-native-vz-host-20261021 \
  --member-cluster apps-a1t-kata-vz-20261021 \
  --private-dir /private/tmp/apps-kata-runtime-smoke-20261021
```

Only the same command with `--execute` creates the new member. Before creating
Kind it fsyncs a private creation intent. Immediately after `kind create`, it
records the exact member node container ID; after the API responds it also
records the member Node UID before installing Flux or Kata. It verifies
`/dev/kvm` and the effective read-only `/lib/modules` mount inside the member node,
resolving a possible `/lib` to `/usr/lib` alias before checking mountinfo, then waits for local
Flux, the pinned Kata HelmRelease and DaemonSet, and checks that the real
RuntimeClass handler is `kata-qemu`.
The exact RuntimeClass scheduling selector is checked before Pod creation.
Kubernetes admission merges its `kata-deploy.katacontainers.io/default=true`
selector into the smoke Pod; the final Pod must have exactly that source-owned
key and the two requested node selectors, with no extra selector.

The member node applies the two upstream nested-QEMU workarounds: clear
`cpu_features` in every installed QEMU configuration and remount its `/dev/shm`
to 8 GiB, then verify the resulting capacity. The helper checks that the Flux
source and HelmRelease observed their current generations. It compares the OCI
source's exact `spec.ref.digest` and `.status.artifact.revision` to the locked
manifest digest. Flux defines `.status.artifact.digest` as the digest of its
generated archive, so the helper validates its format without equating it to
the upstream OCI digest. For the HelmRelease it checks the current attempted
generation, the OCI-specific full `lastAttemptedRevisionDigest`, and the
documented short `lastAttemptedRevision` (`chart-version+12-hex-digest`). It
also requires Ready conditions for current generations. It verifies every Kata DaemonSet pod is owned
by the exact DaemonSet UID, Ready, and running the locked installer/helper
image index, ARM64 child, or exact ARM64 config digest. A run-labeled Namespace and Pod are created
with API `CREATE` after their exact intents are fsynced to the journal;
returned UIDs are recorded and checked on subsequent reads. The Pod is pinned
to that member node, requests `runtimeClassName: kata-qemu`, disables service
account token mounting, and uses the immutable BusyBox digest. Success
requires the exact Pod UID and image reference, a ready container, and an image
ID matching the locked ARM64 child, its config digest, or the locked index
digest. Index digests are accepted only with the verified ARM64 member-node
architecture. The guest must report ARM64, and `uname -r` in the guest must
differ from the member node's recorded kernel version.

Failures preserve the journal and member for diagnosis. Bounded stdout/stderr
from failed commands is written into separate mode-0600 records with stage and
failure class after secret-pattern redaction; command input is never recorded.
The journal phase is the cleanup/recovery authority. A failure after the
member node ID and Node UID have been recorded can be cleaned with explicit
`--cleanup`; a failure before the helper can record a node ID is retained for
manual review. Before cleanup the helper rechecks the clean signed source at
the journaled revision, private evidence directory, host API/CA and node
identities, member container ID and (when available) member Node UID, plus the
complete Kind cluster census. A lost deletion reply is recoverable from the
durable delete intent. Cleanup never deletes the VZ profile or host cluster.
The helper has not been run against the live VZ environment; no guest-boot
result is claimed here.

```sh
python3 hack/run_apps_native_kata_runtime_smoke.py \
  --profile apps-native-vz-20261021 \
  --host-cluster apps-a1t-ci-vz-20261021 \
  --host-evidence /private/tmp/apps-native-vz-host-20261021 \
  --member-cluster apps-a1t-kata-vz-20261021 \
  --private-dir /private/tmp/apps-kata-runtime-smoke-20261021 \
  --execute
```

After reviewing retained evidence, explicitly remove the member with the same
identities and `--cleanup`. Cleanup can handle a failed runtime stage only
when the helper durably recorded the exact member node identity; never run it
automatically on failure.

```sh
python3 hack/run_apps_native_kata_runtime_smoke.py \
  --profile apps-native-vz-20261021 \
  --host-cluster apps-a1t-ci-vz-20261021 \
  --host-evidence /private/tmp/apps-native-vz-host-20261021 \
  --member-cluster apps-a1t-kata-vz-20261021 \
  --private-dir /private/tmp/apps-kata-runtime-smoke-20261021 \
  --cleanup
```
