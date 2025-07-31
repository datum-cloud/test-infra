# Test Infrastructure

A comprehensive test‑infrastructure repository designed to support software testing across an organization. This repository provides **standardised Kubernetes test environments** with pre‑configured shared infrastructure components, enabling consistent and efficient testing workflows for services that operate within Kubernetes clusters.

## Overview

- 🚀 **Fast Test Environment Provisioning** – get a fully configured Kubernetes cluster in **2–3 minutes**.
- 🔄 **Standardised Infrastructure** – cert‑manager, Flux CD and Kyverno installed and version‑pinned out‑of‑the‑box.
- ⚡ **CI/CD Optimised** – purpose‑built for GitHub Actions with minimal resource overhead.
- 🎯 **Ephemeral by Design** – perfect for short‑lived test environments that can be created and destroyed on‑demand.
- 📦 **GitOps Ready** – pre‑configured Flux installation supports declarative infrastructure management.

---

## Prerequisites

| Requirement | Version | Why |
|-------------|---------|-----|
| Docker      | ≥ 20.10 | KIND creates Docker containers that act as Kubernetes nodes |
| GNU Make    | ≥ 4.3   | Simple cross‑platform task runner |
| Bash (or PowerShell) | n/a | Scripts & Makefile helpers |

> **Windows note** – use a *Git Bash* or *WSL2* environment for best results. PowerShell functions are also included where possible.

---

## Quick‑start

```bash
# Clone the repo and spin up everything (tools + cluster + add‑ons)
$ git clone https://github.com/datum-cloud/test-infra.git && cd test-infra
$ make cluster-up

# Tear everything down when finished
$ make cluster-down
```

## Parallels on Windows
If you prefer PowerShell:

```powershell
PS> make install-tools  # idempotent – only installs what is missing
PS> make cluster-create # create the KIND cluster only
PS> make install-addons # deploy cert‑manager, Flux & Kyverno via kustomize
```

## How it Works

- `make install-tools` – installs or upgrades **kind**, **kubectl**, **kustomize**, and **flux** binaries into `./bin` (then adds that directory to `PATH`).
- `make cluster-create` – boots a single-node **kind** cluster using `cluster/kind-config.yaml`.
- `make install-addons` – applies `cluster/kustomization.yaml`; that file, in turn, references **all** `components/*` Kustomizations. Each component is pinned to a specific, well-tested upstream release.
- **GitOps (optional)** – once Flux is running you can point it at your service repositories to sync manifests or Helm charts exactly as in production.


## Adding New Components

1. Create a new directory under `components/NAME`.
2. Add a `kustomization.yaml` that references either remote manifests, Helm charts, or local patches.
3. (Optional) add a Make target:

   ```make
   install-NAME:
   	@echo "Installing NAME…"
   	kustomize build components/NAME | kubectl apply -f -
    ```
4. Append the component to the cluster overlay or just run the new Make target:
    ```yaml
    # cluster/kustomization.yaml
    resources:
      - components/NAME
    ```
The modular layout keeps the bootstrap lean while letting teams layer in extra infrastructure as needed.


## Make Targets

`make cluster-up` - Full happy-path: ensures tooling, creates cluster, installs add-ons

`make cluster-down` - Tears down the cluster and removes all resources

`make install-tools` - Installs or upgrades the required tools (kind, kubectl, kustomize, flux)

`make cluster-create` - Creates a KIND cluster using the configuration in `cluster/kind-config.yaml`

`make install-addons` - Applies the `kustomization.yaml` in the `cluster/` directory, which installs cert-manager, Flux, and Kyverno

`make install-cert-manager`, `make install-flux`, `make install-kyverno` - Install individual components directly

Run `make help` to see all available targets and their descriptions.

## Troubleshooting

Versions – run make install-tools regularly; it will upgrade outdated binaries.

Docker conflicts – if port collisions occur, delete the cluster and recreate with a different name: make CLUSTER_NAME=my‑test cluster-up.

Permissions – on Linux you may need to sudo chown -R $USER:$GROUP bin after the first tool install.