#!/bin/bash

set -euo pipefail

echo "🚀 Starting CI/CD Optimized Kind Node"
echo "====================================="

# Start the original Kind node setup in the background
echo "Starting Kubernetes node..."
/usr/local/bin/entrypoint &
KIND_PID=$!

# Function to check if Kubernetes API is ready
wait_for_kubernetes() {
    echo "⏳ Waiting for Kubernetes API to be ready..."
    local timeout=300
    local elapsed=0

    while ! kubectl --kubeconfig=/etc/kubernetes/admin.conf get nodes >/dev/null 2>&1; do
        if [ $elapsed -ge $timeout ]; then
            echo "❌ Timeout waiting for Kubernetes API"
            exit 1
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        echo "   Waiting... (${elapsed}s/${timeout}s)"
    done

    echo "✅ Kubernetes API is ready"
}

# Function to install components
install_components() {
    echo "🔧 Installing Flux and cert-manager..."
    export KUBECONFIG=/etc/kubernetes/admin.conf
    /kind/scripts/install-components.sh
}

# Wait for Kubernetes to be ready
wait_for_kubernetes

# Install components using the configured mode
install_components

echo ""
echo "🎉 CI/CD Optimized Kind Node Ready!"
echo "=================================="
echo "✅ Kubernetes: Ready"
echo "✅ Flux: Installed"
echo "✅ cert-manager: Ready via HelmRelease"
echo "✅ ClusterIssuers: Available"
echo ""

# Wait for the Kind process to complete
wait $KIND_PID
