#!/usr/bin/env bash
set -euo pipefail

# Infrastructure smoke only: Kubernetes Deployment -> HTTPRoute -> Envoy Gateway.
# The Milo project API and native Workload path need their own A1-T suite.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
namespace=apps-a1t-smoke
context="${GATEWAY_SMOKE_CONTEXT:?Set the isolated kind context explicitly}"
image="${GATEWAY_SMOKE_IMAGE:?Set an image reference pinned to a sha256 digest}"
http_port="${GATEWAY_SMOKE_HTTP_PORT:-30080}"
timeout_seconds="${GATEWAY_SMOKE_TIMEOUT_SECONDS:-240}"
diagnostics="${GATEWAY_SMOKE_DIAG_DIR:-${TMPDIR:-/tmp}/apps-a1t-gateway-diagnostics}"

[[ -n "${KUBECONFIG:-}" && -f "$KUBECONFIG" ]] || {
  echo "A private KUBECONFIG file is required" >&2
  exit 1
}
[[ "$image" =~ ^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$ ]] || {
  echo "GATEWAY_SMOKE_IMAGE must be a sha256-pinned image reference" >&2
  exit 1
}
[[ "$http_port" =~ ^[0-9]+$ && "$timeout_seconds" =~ ^[0-9]+$ ]] &&
    (( 10#$http_port > 0 && 10#$http_port <= 65535 && 10#$timeout_seconds > 0 )) || {
  echo "Gateway smoke port and timeout must be positive integers" >&2
  exit 1
}
[[ "$(kubectl config current-context)" == "$context" ]] || {
  echo "KUBECONFIG current context is not the requested isolated context: $context" >&2
  exit 1
}
k() { kubectl --context="$context" --request-timeout=10s "$@"; }
kw() { kubectl --context="$context" --request-timeout="$(remaining)" "$@"; }
kc() { kubectl --context="$context" --request-timeout=70s "$@"; }
mkdir -p "$diagnostics"
deadline=$(( $(date +%s) + timeout_seconds ))
namespace_uid=""
render_dir=""

remaining() {
  local seconds=$(( deadline - $(date +%s) ))
  (( seconds > 0 )) || return 1
  printf '%ss' "$seconds"
}

collect_diagnostics() {
  k get nodes,pods -A -o wide > "$diagnostics/cluster.txt" 2>&1 || true
  k get gitrepositories,ocirepositories,kustomizations,helmreleases -A -o yaml > "$diagnostics/flux.yaml" 2>&1 || true
  k -n envoy-gateway-system get gateway default-gateway -o yaml > "$diagnostics/gateway.yaml" 2>&1 || true
  k -n "$namespace" get deployment,pods,service,endpoints,httproute -o yaml > "$diagnostics/fixture.yaml" 2>&1 || true
  k get events -A --sort-by=.lastTimestamp > "$diagnostics/events.txt" 2>&1 || true
}

delete_owned_namespace() {
  local proxy_dir socket proxy_pid body
  proxy_dir="$(mktemp -d "${TMPDIR:-/tmp}/apps-a1t-proxy.XXXXXX")"
  socket="$proxy_dir/api.sock"
  k proxy --unix-socket="$socket" > "$diagnostics/proxy.log" 2>&1 &
  proxy_pid=$!
  for _ in $(seq 1 20); do
    [[ -S "$socket" ]] && break
    sleep 0.1
  done
  if [[ ! -S "$socket" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    rm -rf "$proxy_dir"
    echo "Could not start a local API proxy for UID-safe namespace deletion" >&2
    return 1
  fi
  body="$(printf '{"apiVersion":"v1","kind":"DeleteOptions","preconditions":{"uid":"%s"}}' "$namespace_uid")"
  if ! curl --fail --silent --show-error --max-time 10 --unix-socket "$socket" \
      -X DELETE -H 'Content-Type: application/json' --data-binary "$body" \
      "http://localhost/api/v1/namespaces/$namespace" > "$diagnostics/cleanup.json"; then
    kill "$proxy_pid" 2>/dev/null || true
    rm -rf "$proxy_dir"
    echo "Namespace cleanup failed UID precondition; no replacement was deleted" >&2
    return 1
  fi
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  rm -rf "$proxy_dir"
  kc wait --for=delete "namespace/$namespace" --timeout=60s
}

finish() {
  local result=$?
  trap - EXIT
  collect_diagnostics
  if [[ -n "$namespace_uid" ]]; then
    delete_owned_namespace || result=1
  fi
  [[ -z "$render_dir" ]] || rm -rf "$render_dir"
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Refuse an existing namespace. The UID recorded below is required for cleanup.
namespace_uid="$(k create namespace "$namespace" -o jsonpath='{.metadata.uid}')"
[[ -n "$namespace_uid" ]] || { echo "Created namespace has no UID" >&2; exit 1; }

render_dir="$(mktemp -d "${TMPDIR:-/tmp}/apps-a1t-render.XXXXXX")"
cp "$repo_root"/fixtures/gateway-smoke/* "$render_dir/"
(
  cd "$render_dir"
  kustomize edit set image "gateway-smoke-image=$image"
  kustomize build .
) | k apply -f -

kw -n envoy-gateway-system wait gateway/default-gateway --for=condition=Programmed --timeout="$(remaining)"
kw -n "$namespace" rollout status deployment/gateway-smoke --timeout="$(remaining)"

while remaining > /dev/null; do
  generation="$(k -n "$namespace" get httproute gateway-smoke -o jsonpath='{.metadata.generation}')"
  parent="$(k -n "$namespace" get httproute gateway-smoke -o jsonpath='{.status.parents[0].parentRef.name}')"
  conditions="$(k -n "$namespace" get httproute gateway-smoke -o jsonpath='{range .status.parents[0].conditions[*]}{.type}={.status}@{.observedGeneration}{"\n"}{end}')"
  if [[ "$parent" == default-gateway ]] &&
      grep -qx "Accepted=True@$generation" <<< "$conditions" &&
      grep -qx "ResolvedRefs=True@$generation" <<< "$conditions"; then
    response_file="$diagnostics/response.txt"
    status="$(curl --silent --show-error --max-time 5 -o "$response_file" -w '%{http_code}' \
      -H 'Host: apps-smoke.test' "http://127.0.0.1:${http_port}/" 2>/dev/null || true)"
    if [[ "$status" == 200 && "$(cat "$response_file" 2>/dev/null || true)" == datum-a1t-gateway-ok ]]; then
      echo "Gateway route accepted and resolved at generation $generation; HTTP 200 returned the fixture response"
      exit 0
    fi
  fi
  sleep 2
done

echo "Gateway route did not become accepted, resolved, and reachable within ${timeout_seconds}s" >&2
exit 1
