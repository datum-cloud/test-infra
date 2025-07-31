#!/usr/bin/env bash
# Wait until every Deployment in every namespace is Available.
#   $1 = timeout in seconds (accepts “300” or “300s”, default 300)

set -euo pipefail

# ---------- parameters ----------
RAW_TIMEOUT=${1:-300}
# strip any trailing non-digits, e.g. “300s” -> “300”
TIMEOUT_SEC=$(printf '%s\n' "$RAW_TIMEOUT" | sed 's/[^0-9]*$//')
INTERVAL=5

echo "⏳ Waiting for deployments to become Ready (timeout ${TIMEOUT_SEC}s) …"
start=$(date +%s)

while true; do
  # Any deployment whose availableReplicas < desiredReplicas
  notready=$(kubectl get deploy -A --no-headers \
              | awk '
                {
                  split($3, ready, "/");    # READY column = x/y
                  desired = ready[2];       # y
                  available = $5;           # AVAILABLE column
                  if (available < desired)
                    printf "  %s/%s (%s/%s)\n", $1, $2, available, desired
                }')

  if [ -z "$notready" ]; then
    echo "✅ All deployments are Ready!"
    break
  fi

  echo "🔄 Still waiting for:"
  echo "$notready"

  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -ge "$TIMEOUT_SEC" ]; then
    echo "❌ Timeout reached — some deployments never became Ready"
    exit 1
  fi

  sleep "$INTERVAL"
done

