#!/bin/bash

set -euo pipefail

# CI/CD optimized installation - direct kustomize approach
echo "=== Installing Flux + cert-manager via direct kustomize (CI/CD optimized) ==="
echo "Using direct kustomize installation approach..."
echo "Installing Flux and applying GitOps manifests directly..."
/kind/scripts/install-flux-direct.sh

echo "=== Component installation completed! ==="

# Display final status
echo "=== Final Status ==="
kubectl get pods -n flux-system
kubectl get pods -n cert-manager 2>/dev/null || echo "cert-manager namespace not found"
kubectl get clusterissuers 2>/dev/null || echo "No ClusterIssuers found"
echo "====================="
