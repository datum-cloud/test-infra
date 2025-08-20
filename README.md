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

> **Windows note** – use a *Git Bash* or *WSL2* environment for best results. PowerShell functions are also included where possible.

---

## Quick‑start

### Local Usage

```bash
# Clone the repo and spin up everything (tools + cluster + add‑ons)
$ git clone https://github.com/datum-cloud/test-infra.git && cd test-infra
$ task cluster-up

# Tear everything down when finished
$ task cluster-down
```

### Remote Usage (Include in Your Project)

You can include this test infrastructure in any project without cloning. See the [Using from Other Repositories](#using-from-other-repositories) section for complete setup instructions.

## Parallels on Windows
If you prefer PowerShell:

```powershell
PS> task ensure-tools  # idempotent – only installs what is missing
PS> task create-kind # create the KIND cluster only
PS> task install-components # deploy cert‑manager, Flux & Kyverno via kustomize
```

## How it Works

- `task ensure-tools` – installs or upgrades **kind**, **kubectl**, **kustomize**, and **flux** binaries using system package managers or direct downloads.
- `task create-kind` – boots a single-node **kind** cluster using `cluster/kind-config.yaml`.
- `task install-components` – applies `cluster/kustomization.yaml`; that file, in turn, references **all** `components/*` Kustomizations. Each component is pinned to a specific, well-tested upstream release.
- **GitOps (optional)** – once Flux is running you can point it at your service repositories to sync manifests or Helm charts exactly as in production.


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

`task install-components` - Applies the `kustomization.yaml` in the `cluster/` directory, which installs cert-manager, Flux, and Kyverno

`task install-cert-manager`, `task install-flux`, `task install-kyverno`, `task install-envoy-gateway-operator` - Install individual components directly

### Optional Components

`task install-observability` - Deploy complete telemetry stack (Victoria Metrics, Loki, Tempo, Grafana with Promtail)

Run `task help` to see all available targets and their descriptions.

## Optional Components

The test infrastructure provides optional components that can be deployed after the core cluster is running:

### Observability Stack

Deploy a comprehensive telemetry system for monitoring, logging, and distributed tracing:

```bash
task cluster-up                    # Deploy core infrastructure first
task install-observability        # Add telemetry stack
```

**What's included:**
- **Victoria Metrics** - Time-series metrics collection and storage
- **Loki** - Log aggregation with container log collection via Promtail
- **Tempo** - Distributed tracing storage
- **Grafana** - Unified dashboard (accessible at http://localhost:30000, admin/datum123)

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
```

## Troubleshooting

Versions – run task ensure-tools regularly; it will upgrade outdated binaries.

Docker conflicts – if port collisions occur, delete the cluster and recreate with a different name: task cluster-up CLUSTER_NAME=my‑test.

Permissions – tools are installed to system directories and may require sudo privileges.
