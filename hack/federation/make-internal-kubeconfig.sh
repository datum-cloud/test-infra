#!/usr/bin/env bash
# make-internal-kubeconfig.sh <input-kubeconfig> <output-kubeconfig> <kind-cluster-name> [<port>]
#
# Produces a kubeconfig variant that uses a Kind node's Docker container IP
# instead of localhost. Two callers use it:
#   - member internals: the variant stored in Karmada so the controller manager
#     (running inside Docker) can reach member cluster API servers across the
#     kind bridge network — port defaults to 6443 (the in-container API port).
#   - karmada-internal.yaml: the hub apiserver reached from inside the docker
#     network — input is karmada.yaml, cluster name is the hub, and the port is
#     the Karmada API NodePort.
#
# Background: Kind maps each cluster's API server to a random localhost port on
# the developer machine. Inside Docker containers, "localhost" refers to the
# container's own loopback — not the host. We therefore swap the server address
# to the Kind control-plane container's Docker bridge IP (e.g. 172.18.0.x) and
# set insecure-skip-tls-verify because the node certificate does not include the
# Docker bridge IP in its SANs.
#
# Usage:
#   hack/federation/make-internal-kubeconfig.sh \
#     kubeconfigs/federation/compute-pop-dfw.yaml \
#     kubeconfigs/federation/compute-pop-dfw-internal.yaml \
#     compute-pop-dfw
#
#   hack/federation/make-internal-kubeconfig.sh \
#     kubeconfigs/federation/karmada.yaml \
#     kubeconfigs/federation/karmada-internal.yaml \
#     federation-hub \
#     32443

set -euo pipefail

INPUT="${1:?usage: $0 <input-kubeconfig> <output-kubeconfig> <kind-cluster-name> [<port>]}"
OUTPUT="${2:?usage: $0 <input-kubeconfig> <output-kubeconfig> <kind-cluster-name> [<port>]}"
CLUSTER_NAME="${3:?usage: $0 <input-kubeconfig> <output-kubeconfig> <kind-cluster-name> [<port>]}"
# Kind API server always listens on port 6443 inside the container; the hub's
# NodePort variant overrides this.
PORT="${4:-6443}"

CONTAINER_NAME="${CLUSTER_NAME}-control-plane"

# Resolve the container's Docker bridge IP. This docker-inspect Go-template must
# live in a shell script, never inline in the Taskfile — its {{...}} collides
# with Task's own templating.
DOCKER_IP=$(docker inspect \
  -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  "${CONTAINER_NAME}" 2>/dev/null || true)

if [ -z "${DOCKER_IP}" ]; then
  echo "ERROR: Could not resolve Docker IP for container '${CONTAINER_NAME}'." >&2
  echo "       Is the Kind cluster '${CLUSTER_NAME}' running?" >&2
  exit 1
fi

echo "  ${CLUSTER_NAME}: Docker IP ${DOCKER_IP}:${PORT} → ${OUTPUT}"

# Start from a copy of the input, then rewrite its server in place with kubectl
# config (no PyYAML dependency). The cluster's entry name is read from the
# kubeconfig itself so this works for both kind-exported configs (kind-<name>)
# and the Karmada secret's config (karmada). The certificate-authority-data must
# be unset BEFORE enabling insecure-skip-tls-verify — kubectl rejects a config
# that carries both a CA and the insecure flag.
cp "${INPUT}" "${OUTPUT}"
ENTRY=$(kubectl --kubeconfig="${OUTPUT}" config view -o jsonpath='{.clusters[0].name}')
kubectl --kubeconfig="${OUTPUT}" config unset "clusters.${ENTRY}.certificate-authority-data" >/dev/null
kubectl --kubeconfig="${OUTPUT}" config set-cluster "${ENTRY}" \
  --server="https://${DOCKER_IP}:${PORT}" \
  --insecure-skip-tls-verify=true >/dev/null
