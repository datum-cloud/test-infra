# Test Infrastructure

A comprehensive test infrastructure repository designed to support software
testing across an organization. This repository provides standardized Kubernetes
testing environments with pre-configured shared infrastructure components,
enabling consistent and efficient testing workflows for services that operate
within Kubernetes clusters.

## Overview

Modern software organizations need reliable, reproducible testing environments
that mirror production infrastructure. This repository delivers:

- **🚀 Fast Test Environment Provisioning**: Get a fully configured Kubernetes
  cluster with essential components in 2-3 minutes
- **🔄 Standardized Infrastructure**: Consistent shared components (FluxCD,
  cert-manager) across all testing environments
- **⚡ CI/CD Optimized**: Purpose-built for GitHub Actions with minimal resource
  overhead
- **🎯 Ephemeral by Design**: Perfect for short-lived test environments that can
  be created and destroyed on-demand
- **📦 GitOps Ready**: Pre-configured Flux installation supports declarative
  infrastructure management

## Architecture

### Core Components

**Shared Infrastructure Stack:**
- **Kubernetes**: Lightweight Kind clusters for container orchestration
- **FluxCD**: GitOps continuous delivery with source, kustomize, and helm
  controllers
- **cert-manager**: Automated TLS certificate management with pre-configured
  issuers
- **Container Runtime**: Docker-based Kind nodes optimized for CI/CD

**Missing Components (Available for Extension):**
- Monitoring/Observability (Prometheus, Grafana, Jaeger)
- Service Mesh (Istio, Linkerd)
- Ingress Controllers (though can be enabled via configuration)

## Quick Start

### For GitHub Actions (Recommended)

Add this to your GitHub workflow to get a fully configured test environment:

```yaml
name: Test with Shared Infrastructure
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
    - uses: actions/checkout@v4

    - name: Setup Test Infrastructure
      uses: datum-cloud/test-infra/actions/setup-cluster@main
      with:
        cluster-name: my-app-test
        # Flux + cert-manager ready automatically

    - name: Run Integration Tests
      run: |
        # Your tests here - cluster is ready with:
        kubectl get nodes                    # Kubernetes cluster
        kubectl get pods -n flux-system     # FluxCD controllers
        kubectl get pods -n cert-manager    # Certificate management
        kubectl get clusterissuers          # selfsigned-issuer, test-ca-issuer

        # Run your application tests
        make test-integration

    - name: Cleanup
      if: always()
      uses: datum-cloud/test-infra/actions/cleanup-cluster@main
      with:
        cluster-name: my-app-test
```

### For Local Development

```bash
# Build the custom test infrastructure image
git clone https://github.com/datum-cloud/test-infra.git
cd test-infra/kind/container
docker build -t test-infra:local .

# Create a test cluster with shared infrastructure
kind create cluster --image test-infra:local --name dev-test

# Verify shared components are ready
kubectl get pods -n flux-system    # FluxCD controllers
kubectl get pods -n cert-manager   # Certificate management
kubectl get clusterissuers         # Pre-configured CAs

# Your development work here...

# Cleanup when done
kind delete cluster --name dev-test
```

## Usage Patterns

### 1. Microservice Integration Testing

Perfect for testing services that need:
- TLS certificates for secure communication
- GitOps-style configuration management
- Kubernetes-native service discovery

```yaml
- name: Test Microservice Stack
  uses: datum-cloud/test-infra/actions/setup-cluster@main
  with:
    cluster-name: microservice-test

- name: Deploy Test Services
  run: |
    # Deploy your services using the pre-installed infrastructure
    kubectl apply -f test-manifests/
    # Services can immediately request certificates via cert-manager
    # Use Flux for GitOps-style deployments
```

### 2. End-to-End Application Testing

Test complete applications that require:
- Certificate authorities for internal TLS
- Declarative infrastructure configuration
- Container orchestration

### 3. Infrastructure Component Testing

Validate new infrastructure components by:
- Extending the base kustomization configurations
- Testing against the shared Flux and cert-manager setup
- Ensuring compatibility with organizational standards

## Configuration

### Cluster Setup Options

| Parameter | Description | Default | Use Case |
|-----------|-------------|---------|----------|
| `cluster-name` | Unique cluster identifier | `test-cluster` | Multiple parallel tests |
| `kubernetes-version` | K8s version to use | `v1.28.0` | Version compatibility testing |
| `node-count` | Number of worker nodes | `1` | Scale testing |
| `wait-timeout` | Setup timeout | `10m` | Complex deployments |

### Extending Infrastructure

Add new shared components by:

```bash
# 1. Create component configuration
mkdir -p kind/config/my-component/
cat > kind/config/my-component/kustomization.yaml << EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - helmrepository.yaml
  - helmrelease.yaml
EOF

# 2. Reference in main configuration
# Edit kind/config/kustomization.yaml to include:
# resources:
#   - cert-manager/
#   - my-component/

# 3. Test the configuration
kustomize build kind/config/
```

## Pre-Installed Components

### FluxCD (GitOps)
- **Controllers**: source-controller, kustomize-controller, helm-controller
- **Purpose**: Declarative configuration management and continuous delivery
- **Optimization**: Minimal controller set for CI/CD performance

### cert-manager (Certificate Management)
- **Version**: 1.13.x (auto-updating minor versions)
- **Issuers Available**:
  - `selfsigned-issuer`: For internal testing certificates
  - `test-ca-issuer`: For CA-signed certificates in test environments
- **Features**: Automatic certificate provisioning and renewal

### Container Tools
- **kubectl**: Kubernetes command-line tool
- **flux**: FluxCD CLI for GitOps operations
- **helm**: Package manager for Kubernetes applications
- **kustomize**: Configuration management tool

## Repository Structure

```
test-infra/
├── actions/                    # GitHub composite actions
│   ├── setup-cluster/         # Main cluster provisioning action
│   └── cleanup-cluster/       # Cluster teardown action
├── kind/                      # Kind cluster configuration
│   ├── container/             # Custom container image
│   │   ├── Dockerfile         # Multi-tool container build
│   │   ├── entrypoint.sh      # Startup orchestration
│   │   └── scripts/           # Installation automation
│   └── config/                # Kubernetes manifests
│       └── cert-manager/      # Certificate management setup
├── docs/                      # Comprehensive documentation
├── tests/                     # Validation and testing tools
└── examples/                  # Usage examples and templates
```

## Development Workflow

### Adding New Infrastructure Components

1. **Create Component Configuration**:
   ```bash
   mkdir -p kind/config/new-component/
   # Add HelmRepository, HelmRelease, or raw manifests
   ```

2. **Update Main Kustomization**:
   ```yaml
   # kind/config/kustomization.yaml
   resources:
     - cert-manager/
     - new-component/  # Add your component
   ```

3. **Test Configuration**:
   ```bash
   kustomize build kind/config/
   # Validate output before committing
   ```

4. **Update Documentation**:
   - Update this README with new component details
   - Add configuration examples
   - Update troubleshooting guide if needed

### Testing Changes

```bash
# Validate repository structure
./tests/validation/validate.sh

# Build and test container locally
cd kind/container/
docker build -t test-infra:dev .
kind create cluster --image test-infra:dev --name validation

# Test GitHub Actions locally (requires act)
act -j test-setup-action
```

## Organizational Benefits

### For Development Teams
- **Consistent Test Environments**: Same infrastructure components across all
  teams
- **Faster Setup**: No need to configure Flux, cert-manager, or Kubernetes from
  scratch
- **Reduced Maintenance**: Shared infrastructure updates benefit all teams
  automatically
- **GitOps Ready**: Built-in support for declarative configuration management

### For Platform Teams
- **Standardization**: Single source of truth for test infrastructure patterns
- **Version Management**: Centralized updates to shared components
- **Resource Optimization**: Lightweight environments reduce CI/CD costs
- **Extensibility**: Easy to add organization-specific components

### For DevOps/SRE Teams
- **Infrastructure as Code**: All configurations version-controlled and
  reviewable
- **Automated Provisioning**: Reduces manual environment setup overhead
- **Troubleshooting**: Consistent environments simplify debugging
- **Compliance**: Standardized certificate management and security practices

## Documentation

- **[Configuration Guide](docs/CONFIGURATION.md)**: Detailed setup and
  customization options
- **[Contributing Guide](docs/CONTRIBUTING.md)**: How to extend and improve the
  infrastructure
- **[Troubleshooting Guide](docs/TROUBLESHOOTING.md)**: Common issues and
  solutions
- **[CLAUDE.md](CLAUDE.md)**: AI assistant guidance for working with this
  repository

## Support

- **Issues**: Report problems or request features via GitHub Issues
- **Documentation**: Comprehensive guides in the `docs/` directory
- **Examples**: Working examples in the `examples/` directory
- **Community**: Contribute improvements via pull requests

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Ready to standardize your organization's test infrastructure?** Start with the
Quick Start guide above or explore the examples directory for specific use
cases.
