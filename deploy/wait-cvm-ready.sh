#!/usr/bin/env bash
# Wait until a Phala CVM finishes its update and is ready.
#
# `phala deploy --wait` uses a HARD-CODED 300s ceiling (dist/index.js:
# `Xl(e,t=3e5)`) with no flag or env override; runner CVMs routinely need
# longer (big agent-runner image pull), so CI deploys were
# failing on the CLI's timeout while the update itself completed fine.
# Deploy WITHOUT --wait, then poll readiness here with a real deadline.
#
# Usage: wait-cvm-ready.sh <cvm-id> [timeout-sec]
#   cvm-id       UUID, app_id, or name (anything `phala cvms get` accepts)
#   timeout-sec  default $CVM_READY_TIMEOUT_SEC or 900
#
# Auth: pass PHALA_CLOUD_API_KEY in the environment (forwarded as
# --api-token when set; otherwise the CLI's stored login is used).
#
# Ready == status == "running" AND progress empty — mirrors the CLI's own
# `status==="running" && !in_progress` check with the fields `cvms get`
# actually returns. Transient API errors are retried until the deadline.
set -euo pipefail

cvm_id="${1:?usage: wait-cvm-ready.sh <cvm-id> [timeout-sec]}"
timeout_sec="${2:-${CVM_READY_TIMEOUT_SEC:-900}}"
poll_sec="${CVM_READY_POLL_SEC:-10}"

token_args=()
if [ -n "${PHALA_CLOUD_API_KEY:-}" ]; then
  token_args=(--api-token "$PHALA_CLOUD_API_KEY")
fi

start=$(date +%s)
echo "waiting up to ${timeout_sec}s for CVM ${cvm_id} to be ready..."
while :; do
  elapsed=$(( $(date +%s) - start ))
  # ${arr[@]+...} keeps `set -u` happy on bash 3.2 when the array is empty.
  if out=$(phala cvms get "$cvm_id" --json ${token_args[@]+"${token_args[@]}"} 2>&1); then
    status=$(jq -r '.status // empty' <<<"$out" 2>/dev/null || true)
    # progress is null when idle and an object/string while an update runs.
    progress=$(jq -r '.progress // empty | if type == "object" then tojson else tostring end' <<<"$out" 2>/dev/null || true)
    echo "  [${elapsed}s] status=${status:-?} progress=${progress:-—}"
    if [ "$status" = "running" ] && [ -z "$progress" ]; then
      echo "CVM ${cvm_id} is ready (took ${elapsed}s)"
      exit 0
    fi
  else
    echo "  [${elapsed}s] cvms get failed (transient?): $(head -c 160 <<<"$out")"
  fi
  if [ "$elapsed" -ge "$timeout_sec" ]; then
    echo "::error::CVM ${cvm_id} not ready after ${timeout_sec}s"
    exit 1
  fi
  sleep "$poll_sec"
done
