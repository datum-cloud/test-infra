#!/usr/bin/env bash
set -euo pipefail

# Build a fresh, task-owned Milo host in the dedicated Colima VZ profile.
# Partial failures retain the private Kind cluster and diagnostics for review.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile=""
cluster=""
private=""
execute=false
port_base=32000
while (($#)); do
  case "$1" in
    --profile) profile="${2:?Missing profile}"; shift 2 ;;
    --cluster) cluster="${2:?Missing cluster}"; shift 2 ;;
    --private-dir) private="${2:?Missing private directory}"; shift 2 ;;
    --port-base) port_base="${2:?Missing port base}"; shift 2 ;;
    --execute) execute=true; shift ;;
    *) echo "Usage: $0 --profile apps-native-vz-NAME --cluster apps-a1t-ci-vz-NAME --private-dir /new/private/path [--port-base 32000] [--execute]" >&2; exit 2 ;;
  esac
done
[[ "$profile" =~ ^apps-native-vz-[a-z0-9-]+$ && "$cluster" =~ ^apps-a1t-ci-vz-[a-z0-9-]+$ ]] || {
  echo "Explicit dedicated Apps VZ profile and cluster names are required" >&2; exit 1;
}
[[ "$port_base" =~ ^[1-9][0-9]{3,4}$ ]] && ((port_base >= 1024 && port_base <= 65092)) || {
  echo "Port base must be 1024-65092" >&2; exit 1;
}
http_port=$((port_base + 80))
https_port=$((port_base + 443))
node_image=kindest/node@sha256:c48c62eac5da28cdadcf560d1d8616cfa6783b58f0d94cf63ad1bf49600cb027
[[ "$private" = /* && ! -e "$private" && ! -L "$private" ]] || {
  echo "Private artifact directory must be an absent absolute path" >&2
  exit 1
}
umask 077
export DOCKER_HOST="unix://${HOME}/.colima/${profile}/docker.sock"
unset DOCKER_CONTEXT
[[ -S "${DOCKER_HOST#unix://}" ]] || { echo "Dedicated Docker socket absent" >&2; exit 1; }
[[ "$(docker info --format '{{.Architecture}}')" = aarch64 ]] || {
  echo "Dedicated Docker daemon must be aarch64" >&2; exit 1;
}
[[ "$(docker info --format '{{.NCPU}}')" -ge 6 ]] || {
  echo "Dedicated Docker daemon has fewer than six CPUs" >&2; exit 1;
}
colima ssh --profile "$profile" -- test -c /dev/kvm || {
  echo "Nested virtualization device /dev/kvm is unavailable" >&2; exit 1;
}
existing_clusters="$(kind get clusters)"
[[ -z "$existing_clusters" ]] || {
  echo "VZ profile already has Kind clusters; refusing adoption" >&2; exit 1;
}
python3 - "$port_base" "$http_port" "$https_port" <<'PY'
import socket, sys
for port in map(int, sys.argv[1:]):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
PY

if [[ "$execute" != true ]]; then
  echo "Fresh dedicated VZ host preflight passed; rerun with --execute to create it"
  exit 0
fi
source_changes="$(git -C "$repo_root" status --porcelain)"
[[ -z "$source_changes" ]] || {
  echo "Execution requires a clean pinned test-infra checkout" >&2; exit 1;
}
git -C "$repo_root" verify-commit HEAD >/dev/null 2>&1 || {
  echo "Execution requires a verifiable signed source commit" >&2; exit 1;
}
mkdir -m 700 "$private"
export KUBECONFIG="${private}/host.kubeconfig"
python3 - "$private" "$profile" "$cluster" "$repo_root" <<'PYINTENT'
import json, os, pathlib, subprocess, sys
private, profile, cluster, repo = sys.argv[1:]
revision = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
with open(pathlib.Path(private) / "cluster-create-intent", "x") as f:
    os.fchmod(f.fileno(), 0o600)
    json.dump({"profile": profile, "cluster": cluster, "sourceRevision": revision}, f)
    f.flush()
    os.fsync(f.fileno())
fd = os.open(private, os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
PYINTENT

# Create an unpredictable private directory as the VM user. No privileged
# write or predictable shared /tmp file is used; existing paths are untouched.
audit_dir="$(python3 "$repo_root/hack/apps_native_vz_audit.py" "$profile" "$cluster")"
audit_in_vm="${audit_dir}/policy.yaml"
python3 - "$private" "$audit_dir" <<'PYAUDIT'
import os, pathlib, sys
private, directory = sys.argv[1:]
with open(pathlib.Path(private) / "vm-audit-directory", "x") as f:
    os.fchmod(f.fileno(), 0o600)
    f.write(directory + "\n")
    f.flush()
    os.fsync(f.fileno())
fd = os.open(private, os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
PYAUDIT
expected_audit="$(shasum -a 256 "$repo_root/cluster/audit-policy.yaml" | awk '{print $1}')"
colima ssh --profile "$profile" -- tee "$audit_in_vm" \
  < "$repo_root/cluster/audit-policy.yaml" > /dev/null
observed="$(colima ssh --profile "$profile" -- sha256sum "$audit_in_vm" | awk '{print $1}')"
[[ "$observed" = "$expected_audit" ]] || {
  echo "Audit policy copy failed SHA-256 verification" >&2; exit 1;
}

python3 - "$repo_root/cluster/kind-config.yaml" "${private}/kind-host.yaml" "$audit_in_vm" "$port_base" "$http_port" "$https_port" <<'PY'
from pathlib import Path
import sys
source, target, audit = map(Path, sys.argv[1:4])
grafana_port, http_port, https_port = map(int, sys.argv[4:])
body = source.read_text()
changes = {
    '${REPO_DIR}/cluster/audit-policy.yaml': str(audit),
    'hostPort: 30000': f'hostPort: {grafana_port}',
    'hostPort: 30080': f'hostPort: {http_port}',
    'hostPort: 30443': f'hostPort: {https_port}',
}
for old, new in changes.items():
    if body.count(old) != 1:
        raise SystemExit(f'Expected exactly one Kind source field {old!r}')
    body = body.replace(old, new)
if '${' in body:
    raise SystemExit('Unresolved Kind config variable')
target.write_text(body)
target.chmod(0o600)
PY

docker pull --platform linux/arm64 "$node_image"
docker image inspect "$node_image" --format '{{json .RepoDigests}}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "kindest/node@sha256:c48c62eac5da28cdadcf560d1d8616cfa6783b58f0d94cf63ad1bf49600cb027" in d'

kind create cluster --name "$cluster" --image "$node_image" \
  --config "${private}/kind-host.yaml" --kubeconfig "$KUBECONFIG" --retain
chmod 600 "$KUBECONFIG"
context="kind-${cluster}"
[[ "$(kubectl config current-context)" = "$context" ]] || {
  echo "Kind did not write the expected private context" >&2; exit 1;
}
node="${cluster}-control-plane"
[[ "$(kind get nodes --name "$cluster")" = "$node" ]] || {
  echo "Unexpected task Kind node identity" >&2; exit 1;
}
docker inspect --type container --format '{{.Id}}' "$node" > "${private}/cluster-node-id"
chmod 600 "${private}/cluster-node-id"
k() { kubectl --context "$context" --request-timeout=10s "$@"; }
k wait node/"$node" --for=condition=Ready --timeout=180s

# Preserve the full source-owned baseline: Flux, cert-manager, Kyverno and Gateway.
kustomize build "$repo_root/components/flux" | k apply -f -
k -n flux-system wait deployment/{source-controller,helm-controller,kustomize-controller,notification-controller} \
  --for=condition=Available --timeout=300s
kustomize build "$repo_root/components/cert-manager" | k apply -f -
k -n cert-manager wait helmrelease/{cert-manager,cert-manager-csi-driver} \
  --for=condition=Ready --timeout=600s
kustomize build "$repo_root/components/kyverno" | k apply --server-side \
  --field-manager=kyverno-installer --force-conflicts -f -
k -n kyverno wait deployment/kyverno-{admission-controller,background-controller,cleanup-controller,reports-controller} \
  --for=condition=Available --timeout=600s
kustomize build "$repo_root/components/envoy-gateway-operator" | k apply -f -
k -n flux-system wait helmrelease/envoy-gateway --for=condition=Ready --timeout=600s
kustomize build "$repo_root/components/envoy-gateway-operator/gateway-resources" | k apply -f -
k -n envoy-gateway-system wait gateway/default-gateway --for=condition=Programmed --timeout=300s

MILO_FLUX_CONTEXT="$context" MILO_FLUX_HTTPS_PORT="$https_port" \
MILO_FLUX_DIAG_DIR="${private}/milo-flux" \
  bash "$repo_root/hack/test-milo-flux.sh"
echo "Fresh dedicated VZ Milo host Ready; private evidence: $private"
