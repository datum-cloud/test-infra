#!/usr/bin/env bash
#
# install or verify CLI dependencies (kind, kubectl, kustomize, flux)
# supports macOS, Linux, Windows (w/ Git Bash + choco or winget)
set -euo pipefail

TOOLS=("$@")

KIND_VERSION="${KIND_VERSION:-v0.29.0}"

# Detect OS/arch once, normalise
OS=$(uname | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
[[ "$ARCH" == "x86_64" ]] && ARCH=amd64
[[ "$ARCH" == "aarch64" ]] && ARCH=arm64


install_kind() {
  wanted="${KIND_VERSION#v}"                 # drop leading “v” if present

  current=""
  if command -v kind >/dev/null 2>&1; then
    # kind --version prints:  kind version 0.29.0
    current="$(kind --version 2>/dev/null | awk '{print $3}')"
  fi

  if [ "$current" = "$wanted" ]; then
    echo "✅ kind $current already installed"
    return 0
  fi

  echo "Installing kind v$wanted …"
  case "$OS" in
    darwin|linux)
      url="https://github.com/kubernetes-sigs/kind/releases/download/v${wanted}/kind-${OS}-${ARCH}"
      curl -fsSL -o kind "$url"
      chmod +x kind
      sudo mv kind /usr/local/bin/kind
      ;;
    msys*|mingw*|cygwin*)
      choco install kind -y || winget install Kind
      ;;
  esac
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
