#!/usr/bin/env bash
#
# install or verify CLI dependencies (kind, kubectl, kustomize, flux)
# supports macOS, Linux, Windows (w/ Git Bash + choco or winget)
set -euo pipefail

TOOLS=("$@")
OS=$(uname | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

# map arm64 → aarch64 for some downloads
[[ "$ARCH" == "arm64" ]] && ARCH="aarch64"

install_kind() {
  if ! command -v kind &>/dev/null; then
    echo "Installing kind..."
    case "$OS" in
      darwin|linux)
        curl -Lo ./kind "https://kind.sigs.k8s.io/dl/v0.23.0/kind-${OS}-${ARCH}" && \
          chmod +x ./kind && sudo mv ./kind /usr/local/bin/
        ;;
      msys*|mingw*|cygwin*)
        choco install kind   -y || winget install Kind
        ;;
    esac
  fi
}

install_kubectl() {
  if ! command -v kubectl &>/dev/null; then
    echo "Installing kubectl..."
    case "$OS" in
      darwin) brew install kubernetes-cli ;;
      linux)
        curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl"
        chmod +x kubectl && sudo mv kubectl /usr/local/bin/
        ;;
      msys*|mingw*|cygwin*) choco install kubernetes-cli -y || winget install Kubernetes kubectl ;;
    esac
  fi
}

install_kustomize() {
  if ! command -v kustomize &>/dev/null; then
    echo "Installing kustomize..."
    case "$OS" in
      darwin) brew install kustomize ;;
      linux)
        curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
        sudo mv kustomize /usr/local/bin/
        ;;
      msys*|mingw*|cygwin*) choco install kustomize -y || winget install kustomize ;;
    esac
  fi
}

install_flux() {
  if ! command -v flux &>/dev/null; then
    echo "Installing flux..."
    case "$OS" in
      darwin|linux) curl -s https://fluxcd.io/install.sh | sudo bash ;;
      msys*|mingw*|cygwin*) choco install fluxcd -y || winget install flux ;;
    esac
  fi
}

for tool in "${TOOLS[@]}"; do
  install_${tool} || true
done

echo "✅ Tools verified/installed: ${TOOLS[*]}"
