# Test Infrastructure

A comprehensive test‑infrastructure repository designed to support software testing across an organization. This repository provides **standardized Kubernetes test environments** with pre‑configured shared infrastructure components, enabling consistent and efficient testing workflows for services that operate within Kubernetes clusters.

## Overview

- 🚀 **Fast Test Environment Provisioning** – get a fully configured Kubernetes cluster in **2–3 minutes**.
- 🔄 **Standardized Infrastructure** – cert‑manager, Flux CD and Kyverno installed and version‑pinned out‑of‑the‑box.
- ⚡ **CI/CD Optimized** – purpose‑built for GitHub Actions with minimal resource overhead.
- 🎯 **Ephemeral by Design** – perfect for short‑lived test environments that can be created and destroyed on‑demand.
- 📦 **GitOps Ready** – pre‑configured Flux installation supports declarative infrastructure management.
- 🌐 **Gateway Ready** – Envoy Gateway with merged configuration provides HTTP/HTTPS ingress on non-privileged ports.

## Port Configuration

The cluster exposes several ports for easy access without requiring port-forwarding:

- **30443**: HTTPS Gateway (Envoy Gateway)
- **30000**: Grafana dashboard (after installing observability)

All ports use non-privileged ranges (>1024) to avoid requiring administrative privileges.

---

## Prerequisites

| Requirement | Version | Why |
|-------------|---------|-----|
| Docker      | ≥ 20.10 | KIND creates Docker containers that act as Kubernetes nodes |
| Bash (or PowerShell) | n/a | Scripts & Taskfile helpers |

> [!NOTE]
>
> **Windows users** – use a *Git Bash* or *WSL2* environment for best results. PowerShell functions are also included where possible.

---

## Quick‑start

### Local Usage

```bash
# Clone the repo and spin up everything (tools + cluster + add‑ons)
$ git clone https://github.com/datum-cloud/test-infra.git && cd test-infra
$ task cluster-up

# Use the kubectl wrapper to connect to the test-infra cluster
$ task kubectl -- get nodes  # Automatically uses correct kubeconfig
$ task k9s                    # Launch k9s terminal UI for cluster browsing

# Tear everything down when finished
$ task cluster-down
```

### Remote Usage (Include in Your Project)

You can include this test infrastructure in any project without cloning. See the [Using from Other Repositories](#using-from-other-repositories) section for complete setup instructions.

## Parallels on Windows
If you prefer PowerShell:

```powershell
PS> task ensure-tools         # idempotent – only installs what is missing
PS> task create-kind          # create the KIND cluster only
PS> task install-components   # deploy cert‑manager, Flux & Kyverno via kustomize
```

## How it Works

- `task ensure-tools` – installs or upgrades **kind**, **kubectl**, **kustomize**, and **flux** binaries using system package managers or direct downloads.
- `task create-kind` – boots a single-node **kind** cluster using `cluster/kind-config.yaml` and writes kubeconfig to `./kubeconfig`.
- `task install-components` – applies `cluster/kustomization.yaml`; that file, in turn, references **all** `components/*` Kustomizations. Each component is pinned to a specific, well-tested upstream release.
- **GitOps (optional)** – once Flux is running you can point it at your service repositories to sync manifests or Helm charts exactly as in production.

> [!NOTE]
> The cluster kubeconfig location depends on context:
> - When running in the test-infra repo: `./kubeconfig`
> - When running from another repo: `.test-infra/kubeconfig`
>
> This file is gitignored and should not be committed. The Taskfile automatically uses the correct path.


## Adding New Components

1. Create a new directory under `components/NAME`.
2. Add a `kustomization.yaml` that references either remote manifests, Helm charts, or local patches.
3. (Optional) add a Task target:

   ```yaml
   install-NAME:
     desc: "Install NAME component"
     cmds:
       - echo "Installing NAME…"
       - kustomize build components/NAME | kubectl apply -f -
    ```
4. Append the component to the cluster overlay or just run the new Task target:
    ```yaml
    # cluster/kustomization.yaml
    resources:
      - components/NAME
    ```
The modular layout keeps the bootstrap lean while letting teams layer in extra infrastructure as needed.


## Task Targets

### Core Targets

`task cluster-up` - Full happy-path: ensures tooling, creates cluster, installs add-ons

`task cluster-down` - Tears down the cluster and removes all resources

`task ensure-tools` - Installs or upgrades the required tools (kind, kubectl, kustomize, flux)

`task create-kind` - Creates a KIND cluster using the configuration in `cluster/kind-config.yaml`

`task kubectl -- <args>` - Run kubectl commands against the test-infra cluster without setting KUBECONFIG (e.g., `task kubectl -- get pods`)

`task k9s` - Launch k9s to interactively browse the test-infra cluster

`task install-components` - Applies the `kustomization.yaml` in the `cluster/` directory, which installs cert-manager, Flux, and Kyverno

`task install-cert-manager`, `task install-flux`, `task install-kyverno`, `task install-envoy-gateway-operator` - Install individual components directly

### Optional Components

`task install-observability` - Deploy complete telemetry stack (Victoria Metrics, Loki, Tempo, Grafana with Promtail, and Prometheus CRDs)

Run `task help` to see all available targets and their descriptions.

## Optional Components

The test infrastructure provides optional components that can be deployed after the core cluster is running:

### Observability Stack

Deploy a comprehensive telemetry system for monitoring, logging, and distributed tracing:

```bash
task cluster-up                   # Deploy core infrastructure first
task install-observability        # Add telemetry stack
```

**What's included:**
- **Victoria Metrics** - Time-series metrics collection and storage
- **Loki** - Log aggregation with container log collection via Promtail
- **Tempo** - Distributed tracing storage
- **Grafana** - Unified dashboard (accessible at http://localhost:30000, admin/datum123)
- **Prometheus CRDs** - Custom resources for advanced metrics scraping and alerting (servicemonitors, podmonitors, etc.)

The observability stack is designed for development and testing environments with appropriate resource limits and simplified configurations.

## Using from Other Repositories

This test infrastructure can be included and reused across multiple projects without requiring a full clone. The taskfile automatically handles repository management when used externally.

### Basic Setup

1. **Add to your project's `Taskfile.yml`:**
   ```yaml
   version: '3'

   includes:
     test-infra:
       taskfile: https://raw.githubusercontent.com/datum-cloud/test-infra/main/Taskfile.yml
   ```

2. **Enable experimental remote taskfiles:**
   ```bash
   # One-time setup
   export TASK_X_REMOTE_TASKFILES=1

   # Or add to .env file in your project
   echo "TASK_X_REMOTE_TASKFILES=1" >> .env
   ```

### Advanced Configuration

Override default settings by passing variables:

```yaml
# Your project's Taskfile.yml
version: '3'

includes:
  test-infra:
    taskfile: https://raw.githubusercontent.com/datum-cloud/test-infra/main/Taskfile.yml
    vars:
      CLUSTER_NAME: my-project-test     # Custom cluster name
      K8S_VERSION: v1.32.0              # Specific Kubernetes version
      REPO_REF: feature-branch          # Use specific branch/tag
      WAIT_TIMEOUT: 600s                # Longer timeout for slower environments
```

### Available Tasks

All tasks are prefixed with your include name:

```bash
task test-infra:help                       # Show all available commands
task test-infra:cluster-up                 # Deploy full infrastructure
task test-infra:cluster-down               # Destroy cluster
task test-infra:cluster-status             # Check cluster health
task test-infra:install-observability      # Add telemetry stack

# Use the wrapper commands (recommended - automatic path detection):
task test-infra:kubectl -- get nodes    # Run kubectl commands
task test-infra:k9s                     # Launch k9s terminal UI
```

## Federation topology (Karmada hub + members)

Beyond the single-cluster environment above, this repo can stand up a **Karmada
federation topology** — a hub cluster running the Karmada control plane plus any
number of member clusters, joined and labeled — that any Datum service e2e can
consume. Compute is the first consumer; NSO-style multi-cluster labs are the
next. The clusters are throwaway, throughput-tuned kind clusters; Karmada is
installed imperatively via Helm (federation clusters run **no** Flux).

> [!NOTE]
> The federation tasks use `for:` over `task:` calls, which requires **Task
> ≥ 3.44**. The single-cluster tasks are unaffected.

### Quick start

```bash
# Hub + two members, each labeled with a city code.
task federation-up FEDERATION_MEMBERS="pop-dfw=dfw pop-ord=ord"

task federation-status        # hub + member health, registered clusters
task federation-down FEDERATION_MEMBERS="pop-dfw=dfw pop-ord=ord"   # tear down
```

### Member-spec syntax

`FEDERATION_MEMBERS` is a space-separated list of `<kind-cluster-name>[=<label-value>]`
entries:

- A member's Karmada cluster name **is** its kind cluster name.
- `=<label-value>` is optional; when present it is applied under
  `FEDERATION_MEMBER_LABEL_KEY` (default `topology.datum.net/city-code`). Omit it
  for no label.
- Names must not contain `=`, and `karmada` is reserved (it collides with a
  kubeconfig filename).

### Variables

| Var | Default | Meaning |
|-----|---------|---------|
| `FEDERATION_HUB_CLUSTER` | `federation-hub` | Kind cluster hosting Karmada |
| `FEDERATION_MEMBERS` | _(required)_ | `name[=label]` entries, space-separated |
| `FEDERATION_MEMBER_LABEL_KEY` | `topology.datum.net/city-code` | Label key for each member's value |
| `KARMADA_VERSION` | `v1.16.0` | Karmada Helm chart version |
| `KARMADACTL_VERSION` | `{{KARMADA_VERSION}}` | karmadactl CLI version |
| `KARMADA_API_NODEPORT` | `32443` | NodePort + host port for the Karmada apiserver |
| `KARMADA_WAIT_TIMEOUT` | `10m` | Karmada install/converge waits |
| `FEDERATION_KUBECONFIG_DIR` | `<repo>/kubeconfigs/federation` | Where the kubeconfig bundle lands |
| `FEDERATION_HUB_KIND_CFG` / `FEDERATION_MEMBER_KIND_CFG` | `cluster/federation/kind-{hub,member}.yaml` | Kind config overrides |

### Kubeconfig bundle

All under `FEDERATION_KUBECONFIG_DIR`:

| File | Contents |
|------|----------|
| `<hub>.yaml` | Host-side kubeconfig for the hub cluster |
| `<member>.yaml` | Host-side kubeconfig per member |
| `<member>-internal.yaml` | Docker-bridge-IP variant (what Karmada stores for the member) |
| `karmada.yaml` | Karmada apiserver, server `https://127.0.0.1:<nodeport>` |
| `karmada-internal.yaml` | Karmada apiserver, server `https://<hub-docker-ip>:<nodeport>` (for in-docker-network reachers) |
| `federation.env` | Shell-sourceable facts (hub docker IP, NodePort, members) |

### Using from another repository

```yaml
# Your project's Taskfile.yml
version: '3'

includes:
  infra:
    taskfile: https://raw.githubusercontent.com/datum-cloud/test-infra/v0.7.0/Taskfile.yml
    vars:
      REPO_REF: v0.7.0                      # match the include ref for the self-clone
      FEDERATION_HUB_CLUSTER: my-hub
      FEDERATION_MEMBERS: my-pop-a=dfw my-pop-b=ord
```

```bash
export TASK_X_REMOTE_TASKFILES=1
task --yes infra:federation-up            # --yes accepts the remote-taskfile trust prompt
```

CI consumers can instead use the
[`federation-bootstrap`](.github/actions/federation-bootstrap/action.yaml)
composite action, which stands up the topology and emits the kubeconfig paths as
step outputs.

## Troubleshooting

**Versions** – run `task ensure-tools` regularly; it will upgrade outdated binaries.

**Docker conflicts** – if port collisions occur, delete the cluster and recreate with a different name: `task cluster-up CLUSTER_NAME=my‑test`.

**Permissions** – tools are installed to system directories and may require sudo privileges.

**Cluster interaction** – use the wrapper commands for automatic kubeconfig handling:
- `task kubectl -- <args>` / `task test-infra:kubectl -- <args>` - Run kubectl commands
- `task k9s` / `task test-infra:k9s` - Launch k9s terminal UI for interactive cluster browsing

These automatically use the correct kubeconfig path without manual setup.
