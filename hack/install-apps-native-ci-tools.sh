#!/usr/bin/env bash
# Exact Linux amd64 tools for the isolated native Apps authoring CI job.
# Digests are from the official release assets / Kubernetes kubectl.sha256:
# https://github.com/kubernetes-sigs/kind/releases/tag/v0.30.0
# https://github.com/kubernetes-sigs/kustomize/releases/tag/kustomize/v5.8.1
# https://github.com/fluxcd/flux2/releases/tag/v2.8.2
# https://dl.k8s.io/release/v1.32.0/bin/linux/amd64/kubectl.sha256
set -euo pipefail
umask 077

[[ $# == 1 && "$1" == /* && "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo 'Expected one absolute destination on Linux amd64' >&2
  exit 1
}
destination="$1"
[[ ! -e "$destination" && ! -L "$destination" ]] || {
  echo 'Refusing existing native CI tool directory' >&2
  exit 1
}
mkdir -m 700 -- "$destination"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/apps-native-ci-tools.XXXXXXXX")"
trap 'rm -rf -- "$scratch"' EXIT

download_checked() {
  local url="$1" digest="$2" output="$3"
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --fail --location --silent --show-error \
    --retry 3 --connect-timeout 10 --max-time 120 --output "$output" "$url"
  printf '%s  %s\n' "$digest" "$output" | sha256sum --check --status || {
    echo "Official native CI tool digest mismatch: $url" >&2
    exit 1
  }
}

download_checked \
  https://github.com/kubernetes-sigs/kind/releases/download/v0.30.0/kind-linux-amd64 \
  517ab7fc89ddeed5fa65abf71530d90648d9638ef0c4cde22c2c11f8097b8889 \
  "$scratch/kind"
install -m 755 "$scratch/kind" "$destination/kind"

download_checked \
  https://dl.k8s.io/release/v1.32.0/bin/linux/amd64/kubectl \
  646d58f6d98ee670a71d9cdffbf6625aeea2849d567f214bc43a35f8ccb7bf70 \
  "$scratch/kubectl"
install -m 755 "$scratch/kubectl" "$destination/kubectl"

download_checked \
  https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v5.8.1/kustomize_v5.8.1_linux_amd64.tar.gz \
  029a7f0f4e1932c52a0476cf02a0fd855c0bb85694b82c338fc648dcb53a819d \
  "$scratch/kustomize.tar.gz"
mkdir -m 700 "$scratch/kustomize-extract"
tar -xzf "$scratch/kustomize.tar.gz" -C "$scratch/kustomize-extract"
[[ -f "$scratch/kustomize-extract/kustomize" &&
   ! -L "$scratch/kustomize-extract/kustomize" ]] || {
  echo 'Official Kustomize archive has unexpected layout' >&2
  exit 1
}
install -m 755 "$scratch/kustomize-extract/kustomize" "$destination/kustomize"

download_checked \
  https://github.com/fluxcd/flux2/releases/download/v2.8.2/flux_2.8.2_linux_amd64.tar.gz \
  711a0ac3d1ea15e957c2775af11ddc48a505b6adaa8fa5db5a65355ee6f49cde \
  "$scratch/flux.tar.gz"
mkdir -m 700 "$scratch/flux-extract"
tar -xzf "$scratch/flux.tar.gz" -C "$scratch/flux-extract"
[[ -f "$scratch/flux-extract/flux" && ! -L "$scratch/flux-extract/flux" ]] || {
  echo 'Official Flux archive has unexpected layout' >&2
  exit 1
}
install -m 755 "$scratch/flux-extract/flux" "$destination/flux"

[[ "$("$destination/kind" --version)" == 'kind version 0.30.0' ]]
[[ "$("$destination/kubectl" version --client -o json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')" == v1.32.0 ]]
[[ "$("$destination/kustomize" version)" == v5.8.1 ]]
[[ "$("$destination/flux" --version)" == 'flux version 2.8.2' ]]
printf '%s\n' 'Verified Kind 0.30.0, kubectl 1.32.0, Kustomize 5.8.1, Flux 2.8.2'
