# Apps A1-T federation prerequisite (2026-10-20)

This optional stage checks the real Karmada control plane and one DFW member
needed by Compute and the Network Services Operator (NSO). It does **not**
assert a native Workload, NetworkContext, propagated edge configuration, or a
live Apps route.

The topology source is the existing test-infra federation PR32 at commit
`3619e71d4686c3d1ead01f08a1136f7d272b3344`. Its `federation-up` task
creates a separate Kind hub and member, installs Karmada Helm chart `v1.16.0`,
joins the member and emits host/internal kubeconfigs. The chart archive fetched
from that task's repository at the time of this trial had SHA-256
`773ab194cb92dad81d479d2199b968a2c6b0b8366306ba47961afd67616834fe`.
The chart repository is a URL under `master`, and the chart defaults to mutable
`latest` init images. Checking a subsequently downloaded archive does not prove
which chart bytes Helm installed. A CI-native stage must verify a pinned chart
**before** installation and use digest-pinned runtime images. This local trial
records the installed image IDs below; it does not claim the stronger supply
chain gate.

The trial ran in the private Colima profile `apps-a1t-20261020`, resized from
6 GiB to 16 GiB memory and 6 CPUs. Before adding federation, the existing Milo
Kind node, Flux `milo-core` at
`sha1:ba4b06e8dde99dd70e9a2f34e011d635e4408a76`, front-proxy Certificate
and Milo `/api/v1` were Ready. Only the dedicated Docker socket was used;
the global Docker context remained `colima`, and the global kubectl context
remained `datum-project-datum-cloud`.

The exact local invocation from a checkout of the pinned PR32 commit was:

```sh
env -u DOCKER_CONTEXT \
  DOCKER_HOST=unix:///Users/scotwells/.colima/apps-a1t-20261020/docker.sock \
  task federation-up \
  FEDERATION_HUB_CLUSTER=apps-a1t-hub-20261020 \
  FEDERATION_MEMBERS=apps-a1t-pop-dfw-20261020=dfw \
  FEDERATION_MEMBER_LABEL_KEY=topology.datum.net/city-code \
  KARMADA_API_NODEPORT=32453 \
  FEDERATION_KUBECONFIG_DIR=/tmp/apps-a1t-federation-kubeconfigs \
  LOCALBIN=/tmp/apps-a1t-federation-bin \
  KARMADA_VERSION=v1.16.0 KARMADACTL_VERSION=v1.16.0 \
  KARMADA_WAIT_TIMEOUT=10m WAIT_TIMEOUT=300s
```

The dedicated VM's default `fs.inotify.max_user_instances=128` was exhausted
when the third Kind node began: its systemd PID 1 exited 255 with `Failed to
create control group inotify object: Too many open files`. Raising **only this
profile's VM** to 1024 via `colima ssh --profile apps-a1t-20261020 -- sudo
sysctl -w fs.inotify.max_user_instances=1024`, then deleting only the failed
DFW node, let the bounded rerun complete. This setting lasts only until that VM
reboots. The Karmada join took three of its five configured attempts after
transient API EOFs while Karmada's etcd restarted.

PR32's default label is `topology.datum.net/city-code=dfw`. Compute's
federator selects `topology.datum.net/location=dfw`, and NSO's propagation
policy selects `infra.datum.net/gateways=enabled`. Apply those two additional
labels to the **same task-owned Karmada Cluster**, with its explicit kubeconfig:

```sh
kubectl --kubeconfig /tmp/apps-a1t-federation-kubeconfigs/karmada.yaml \
  label cluster apps-a1t-pop-dfw-20261020 \
  topology.datum.net/location=dfw infra.datum.net/gateways=enabled --overwrite
```

PR32 emitted the Karmada admin kubeconfigs as mode `0644` in a `0755`
directory. The trial immediately restricted the private directory to `0700`
and all its YAML kubeconfigs to `0600`, without logging credential contents.
Before running the check, use a private output directory and enforce those
modes. This should be fixed in PR32 before it is reused in shared CI.

From this test-infra branch, the bounded read-only gate is:

```sh
python3 hack/check-federation-prerequisite.py \
  --source /tmp/apps-a1t-federation-src \
  --chart /tmp/karmada-chart-v1.16.0.tgz \
  --hub-kubeconfig /tmp/apps-a1t-federation-kubeconfigs/apps-a1t-hub-20261020.yaml \
  --member-kubeconfig /tmp/apps-a1t-federation-kubeconfigs/apps-a1t-pop-dfw-20261020.yaml \
  --karmada-kubeconfig /tmp/apps-a1t-federation-kubeconfigs/karmada.yaml \
  --hub-name apps-a1t-hub-20261020 \
  --member-name apps-a1t-pop-dfw-20261020 \
  --api-port 32453
```

The gate checks the source and downloaded chart digests, private kubeconfig
permissions and exact contexts/localhost endpoints, live Kubernetes APIs and
Ready nodes, all six observed Karmada deployments and etcd, the deployed
Karmada `v1.16.0` Helm release, the member's Karmada `Ready=True` condition,
Docker-bridge API endpoint, and all three placement labels. It uses only the
given kubeconfig paths and bounded requests. It exits nonzero on any missing
evidence.

The trial returned exit 0. Karmada reported
`apps-a1t-pop-dfw-20261020` `Ready=True`, API endpoint
`https://172.18.0.4:6443`, and the three labels above. The DFW node was
Kubernetes `Ready`. The six Karmada deployments and its etcd were Ready; the
Helm release was `karmada-v1.16.0` / `deployed`. After installation, the
dedicated VM had about 11 GiB available memory and 26 GiB free Docker disk.
Its Kind node image was `kindest/node:v1.32.0` at
`sha256:c48c62eac5da28cdadcf560d1d8616cfa6783b58f0d94cf63ad1bf49600cb027`.

Observed arm64 Karmada runtime image IDs were:

| Component | Image digest |
| --- | --- |
| etcd | `registry.k8s.io/etcd@sha256:50dd70d8ce19d9fa1688408fecc34186b6c100dd477e4ace8913fe829c5e5907` |
| aggregated API server | `docker.io/karmada/karmada-aggregated-apiserver@sha256:48ae97901a9103846b5bdc540be7baed3a1057ea1f7843efa5bf3aa97662eca5` |
| API server | `registry.k8s.io/kube-apiserver@sha256:b9d7c117f8ac52bed4b13aeed973dc5198f9d93a926e6fe9e0b384f155baa902` |
| controller manager | `docker.io/karmada/karmada-controller-manager@sha256:52205a594fcec448e9591b384b381a52fe4352c64c14b3ecfc7344ef754c284e` |
| Kubernetes controller manager | `registry.k8s.io/kube-controller-manager@sha256:2bf47c1b01f51e8963bf2327390883c9fa4ed03ea1b284500a2cba17ce303e89` |
| scheduler | `docker.io/karmada/karmada-scheduler@sha256:8ad8d9a2d43a71d3a5ceb7428aa8bcacd69d8a3dfe1c25872a90a7f146ec9c20` |
| webhook | `docker.io/karmada/karmada-webhook@sha256:fed3dcf88904cd606850bdeb540ff0ff3116ed733e6033bbcc296f1bb5c08cc6` |
| init kubectl (`latest` in chart) | `docker.io/bitnamisecure/kubectl@sha256:9b58231669c7c371ecd976626a61d72ae7e46326f30932e8934a087f5f26fbd8` |
| init cfssl (`latest` in chart) | `docker.io/cfssl/cfssl@sha256:a32a9048078819d3035f5dc46637346a0e7da9df297ab234994c9a80cde10b98` |

The native continuation needs those real operators and their own project
contracts: a scoped Milo identity and project control planes for Compute and
NSO, the Locations/ServingLocation CRDs, project IPAM, and downstream edge
Envoy Gateway. The current Compute production overlay assumes Karmada is
co-located in its management cluster, while this reusable PR32 topology has a
separate hub; its E2E wiring must be adapted with explicit hub credentials.
No Workload or route success is inferred from federation readiness alone.
