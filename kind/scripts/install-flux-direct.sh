#!/bin/bash

set -euo pipefail

echo "🔧 Installing Flux with CI-optimized configuration"
echo "=================================================="

# Install Flux with minimal controllers for CI/CD
echo "Installing Flux controllers (source, kustomize, helm only)..."
flux install \
    --components=source-controller,kustomize-controller,helm-controller \
    --timeout=10m \
    --verbose

echo "⏳ Waiting for Flux system to be ready..."
kubectl wait --for=condition=Ready pods -l app.kubernetes.io/part-of=flux-system -n flux-system --timeout=300s

echo "📦 Applying cert-manager configuration via kustomize..."
if ! kustomize build /kind/config/ | kubectl apply -f -; then
    echo "❌ Failed to apply cert-manager configuration"
    exit 1
fi

echo "⏳ Waiting for cert-manager to be ready..."
# Wait for cert-manager namespace to exist
if ! timeout 120 bash -c 'until kubectl get namespace cert-manager 2>/dev/null; do sleep 2; done'; then
    echo "❌ Timeout waiting for cert-manager namespace"
    exit 1
fi

# Wait for cert-manager pods to be ready
if ! kubectl wait --for=condition=Ready pods -l app.kubernetes.io/instance=cert-manager -n cert-manager --timeout=300s; then
    echo "❌ cert-manager pods failed to become ready"
    kubectl get pods -n cert-manager
    exit 1
fi

echo "🔑 Verifying ClusterIssuers..."
if ! kubectl wait --for=condition=Ready clusterissuers --all --timeout=60s; then
    echo "⚠️  ClusterIssuers not ready yet, but continuing..."
    kubectl get clusterissuers
fi

echo ""
echo "✅ Flux and cert-manager installation completed!"
echo "==============================================="
echo ""

# Display status
echo "📊 Installation Summary:"
echo "Flux Controllers:"
kubectl get pods -n flux-system -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.conditions[?(@.type==\"Ready\")].status --no-headers | sed 's/^/  /'

echo ""
echo "cert-manager:"
kubectl get pods -n cert-manager -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.conditions[?(@.type==\"Ready\")].status --no-headers | sed 's/^/  /'

echo ""
echo "ClusterIssuers:"
kubectl get clusterissuers -o custom-columns=NAME:.metadata.name,READY:.status.conditions[?(@.type==\"Ready\")].status,AGE:.metadata.creationTimestamp --no-headers | sed 's/^/  /'

echo ""
echo "🚀 Ready for CI/CD testing!"
