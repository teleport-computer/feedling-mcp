#!/usr/bin/env bash
set -euo pipefail
if [ -z "$DATABASE_URL" ]; then
  echo "::error::DATABASE_URL not set — runner has no lease store"; exit 1
fi
if [ -z "$FEEDLING_API_URL" ] || [ -z "$FEEDLING_ENCLAVE_URL" ]; then
  echo "::error::PROD_MAIN_API_URL / PROD_MAIN_ENCLAVE_URL not set — runner can't reach the main CVM"; exit 1
fi
case "$FEEDLING_PLAINTEXT_WRITES_ACCEPTED" in
  0|1) ;;
  *) echo "::error::PROD_FEEDLING_PLAINTEXT_WRITES_ACCEPTED must be exactly 0 or 1"; exit 1 ;;
esac
if [ "$FEEDLING_PLAINTEXT_WRITES_ACCEPTED" = "1" ] && [ "$FEEDLING_DATABASE_SCHEMA" != "tee" ]; then
  echo "::error::PROD plaintext writes require FEEDLING_DATABASE_SCHEMA=tee"
  exit 1
fi
printf '%s\n' "$IDS" | while IFS= read -r CVM_ID; do
  [ -z "$CVM_ID" ] && continue
  echo ">>> deploy prod runner CVM: $CVM_ID"
  phala deploy \
    --api-token "$PHALA_CLOUD_API_KEY" \
    --cvm-id "$CVM_ID" \
    -c deploy/docker-compose.phala.prod.runner.yaml \
    -e "FEEDLING_RUNNER_CVM_ID=$CVM_ID" \
    -e "FEEDLING_API_URL=$FEEDLING_API_URL" \
    -e "FEEDLING_ENCLAVE_URL=$FEEDLING_ENCLAVE_URL" \
    -e "AGENT_MAX_CHILDREN=$AGENT_MAX_CHILDREN" \
    -e "DATABASE_URL=$DATABASE_URL" \
    -e "FEEDLING_DATABASE_SCHEMA=$FEEDLING_DATABASE_SCHEMA" \
    -e "FEEDLING_PLAINTEXT_WRITES_ACCEPTED=$FEEDLING_PLAINTEXT_WRITES_ACCEPTED" \
    -e "R2_ENDPOINT=$R2_ENDPOINT" \
    -e "R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
    -e "R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
    -e "R2_FRAMES_BUCKET=$R2_FRAMES_BUCKET" \
    -e "FEEDLING_RUNTIME_TOKEN_SECRET=$FEEDLING_RUNTIME_TOKEN_SECRET" \
    -e "AGENT_RUNTIME_USERS=$AGENT_RUNTIME_USERS" \
    -e "AGENT_RUNTIME_AUTODISCOVER=$AGENT_RUNTIME_AUTODISCOVER" \
    -e "FEEDLING_HOST_ALL=$FEEDLING_HOST_ALL"
  # NOT --wait: the CLI's readiness wait is hard-capped at 300s and
  # the runner image pull + boot regularly exceeds it. Poll with our
  # own configurable deadline instead (deploy/wait-cvm-ready.sh).
  ./deploy/wait-cvm-ready.sh "$CVM_ID" "${CVM_READY_TIMEOUT_SEC:-900}"
done

