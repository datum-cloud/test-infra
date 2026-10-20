#!/usr/bin/env bash
set -euo pipefail

# Pin and reconcile Milo's test-infra control plane, then probe the real API.
# This is a Milo prerequisite check, not the native Workload/Apps A1-T suite.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
context="${MILO_FLUX_CONTEXT:?Set the isolated kind context explicitly}"
https_port="${MILO_FLUX_HTTPS_PORT:-30443}"
diagnostics="${MILO_FLUX_DIAG_DIR:-${TMPDIR:-/tmp}/apps-a1t-milo-diagnostics}"
milo_sha=ba4b06e8dde99dd70e9a2f34e011d635e4408a76
metrics_sha=0bb44b7ed38b37bf5266596f135a50947baf03e1
image=ghcr.io/milo-os/milo@sha256:3a89b1edb93eb3d4bb74808cc031897055e6130bf36aea56d978fcf252464afd

[[ -n "${KUBECONFIG:-}" && -f "$KUBECONFIG" ]] || {
  echo "A private KUBECONFIG file is required" >&2
  exit 1
}
[[ "$https_port" =~ ^[0-9]+$ ]] &&
    (( 10#$https_port > 0 && 10#$https_port <= 65535 )) || {
  echo "MILO_FLUX_HTTPS_PORT must be a valid port" >&2
  exit 1
}
[[ "$(kubectl config current-context)" == "$context" ]] || {
  echo "KUBECONFIG current context is not the requested isolated context: $context" >&2
  exit 1
}
k() { kubectl --context="$context" --request-timeout=10s "$@"; }
mkdir -p "$diagnostics"

capture() {
  k get gitrepositories,kustomizations,helmreleases -A -o yaml > "$diagnostics/flux.yaml" 2>&1 || true
  k get pods -A -o wide > "$diagnostics/pods.txt" 2>&1 || true
  k -n milo-system get httproute milo-apiserver -o yaml > "$diagnostics/route.yaml" 2>&1 || true
  k get events -A --sort-by=.lastTimestamp > "$diagnostics/events.txt" 2>&1 || true
}
trap capture EXIT

# The upstream Milo overlay creates milo-system. Apply its root first: a
# single bulk apply fails on a new cluster because the child Flux objects
# themselves live in that namespace.
k apply -f "$repo_root/components/milo-core/source.yaml"
k apply -f "$repo_root/components/milo-core/resource-metrics-source.yaml"
k apply -f "$repo_root/components/milo-core/core.yaml"

flux reconcile source git milo -n flux-system --timeout=180s
flux reconcile source git milo-resource-metrics -n flux-system --timeout=180s
[[ "$(k -n flux-system get gitrepository milo -o jsonpath='{.status.artifact.revision}')" == "sha1:$milo_sha" ]]
[[ "$(k -n flux-system get gitrepository milo-resource-metrics -o jsonpath='{.status.artifact.revision}')" == "sha1:$metrics_sha" ]]

flux reconcile kustomization milo-core -n flux-system --timeout=360s
k get namespace milo-system >/dev/null
k apply -k "$repo_root/components/milo-core"
flux reconcile kustomization milo-infra-crds -n flux-system --timeout=180s
flux reconcile kustomization milo-api-resource-metrics-crd -n milo-system --timeout=180s
flux reconcile kustomization milo-api-test-users -n milo-system --timeout=180s
flux reconcile kustomization milo-api-webhooks -n milo-system --timeout=180s
flux reconcile kustomization milo-api-services -n milo-system --timeout=180s

for deployment in milo-apiserver milo-controller-manager; do
  actual="$(k -n milo-system get deployment "$deployment" -o jsonpath='{.spec.template.spec.containers[0].image}')"
  [[ "$actual" == "$image" ]] || {
    echo "$deployment image is $actual, expected $image" >&2
    exit 1
  }
done
k -n milo-system rollout status deployment/milo-apiserver --timeout=120s
k -n milo-system rollout status deployment/milo-controller-manager --timeout=120s
k wait --for=condition=Established crd/projectcontrolplanes.infrastructure.miloapis.com --timeout=60s

inventory="$(k -n milo-system get kustomization milo-api-test-users -o jsonpath='{range .status.inventory.entries[*]}{.id}{"\n"}{end}')"
grep -qx '_admin_iam.miloapis.com_User' <<< "$inventory"
grep -qx '_test-user_iam.miloapis.com_User' <<< "$inventory"
[[ "$(wc -l <<< "$inventory" | tr -d ' ')" == 2 ]]

route_deadline=$(( $(date +%s) + 120 ))
status=""
while (( $(date +%s) < route_deadline )); do
  generation="$(k -n milo-system get httproute milo-apiserver -o jsonpath='{.metadata.generation}')"
  conditions="$(k -n milo-system get httproute milo-apiserver -o jsonpath='{range .status.parents[0].conditions[*]}{.type}={.status}@{.observedGeneration}{"\n"}{end}')"
  if grep -qx "Accepted=True@$generation" <<< "$conditions" &&
      grep -qx "ResolvedRefs=True@$generation" <<< "$conditions"; then
    status="$(curl --insecure --silent --show-error --max-time 10 \
      -H 'Authorization: Bearer test-admin-token' \
      -o "$diagnostics/api-v1.json" -w '%{http_code}' \
      "https://127.0.0.1:${https_port}/api/v1" 2>/dev/null || true)"
    [[ "$status" == 200 ]] && break
  fi
  sleep 2
done
[[ "$status" == 200 ]] || { echo "Milo API route was not ready or returned HTTP $status" >&2; exit 1; }
python3 - "$diagnostics/api-v1.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    document = json.load(stream)
if document.get("kind") != "APIResourceList":
    raise SystemExit("Milo API did not return an APIResourceList")
PY
echo "Milo Flux sources, control plane, API services, two test users, and Gateway API request passed"
