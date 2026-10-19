#!/usr/bin/env bash
# publish-compose-hash.sh — compute the current deployment's compose_hash
# and publish it on-chain via the FeedlingAppAuth contract.
#
# Usage:
#   ./deploy/publish-compose-hash.sh <chain>
#   <chain>: eth_sepolia | base_sepolia | base
#
# The compose_hash we publish MUST match the one dstack itself will
# compute at CVM boot and encode in RTMR3 (via event log) and
# mr_config_id. dstack's canonical form is:
#
#   canonical = json.dumps(app_compose, separators=(",", ":"), sort_keys=True)
#   compose_hash = sha256(canonical.encode()).hexdigest()
#
# Where `app_compose` is the dstack wrapper manifest (app-compose.json)
# that embeds the docker-compose yaml as a string field along with
# dstack-specific settings (kms_enabled, allowed_envs, runner, etc.).
#
# If you provide just a docker-compose.yaml we wrap it in a minimal
# app-compose.json on the fly so the hash matches what dstack will
# compute. For a real deployment you'd generate the app-compose.json
# once at build time (via `phala deploy` or equivalent) and pass it
# here with FEEDLING_APP_COMPOSE=<path>.
#
# References:
#   - dstack-tutorial/01-attestation-and-reference-values/verify.py
#   - dstack-tutorial/dstack_audit/phases/attestation.py

set -euo pipefail

CHAIN="${1:-}"
if [ -z "$CHAIN" ]; then
  echo "usage: $0 <eth_sepolia|base_sepolia|base>" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${FEEDLING_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.yaml}"
APP_COMPOSE_FILE="${FEEDLING_APP_COMPOSE:-}"

if [ -n "$APP_COMPOSE_FILE" ] && [ ! -f "$APP_COMPOSE_FILE" ]; then
  echo "FEEDLING_APP_COMPOSE pointed at $APP_COMPOSE_FILE but file doesn't exist" >&2
  exit 1
fi
if [ -z "$APP_COMPOSE_FILE" ] && [ ! -f "$COMPOSE_FILE" ]; then
  echo "compose file not found at $COMPOSE_FILE" >&2
  exit 1
fi

# In synthesis mode we hash the local file, so uncommitted changes would
# publish a hash that points at a commit without the actual content.
# In live-cvm mode the hash comes from dstack, so local file state is
# irrelevant (we only use the compose path for yaml_url metadata).
if [ -z "${FEEDLING_CVM_ID:-}" ]; then
  CHECK_FILE="${APP_COMPOSE_FILE:-$COMPOSE_FILE}"
  if ! git -C "$REPO_ROOT" diff --quiet HEAD -- "$CHECK_FILE"; then
    echo "ERROR: $CHECK_FILE has uncommitted changes." >&2
    echo "       Commit it first, then re-run this script." >&2
    exit 1
  fi
fi

# Compute compose_hash. Primary mode: read the live hash from an already-
# deployed CVM (dstack is the ground truth — it injects `features`,
# updates `pre_launch_script` version, etc. at deploy time, so local
# synthesis always drifts). Fallback: local synthesis for offline dev.
#
# Primary — live CVM mode:   FEEDLING_CVM_ID=<uuid> ./publish-compose-hash.sh …
# Fallback — offline synth:  plain `./publish-compose-hash.sh …`
#
# Tier-2 CI must deploy the CVM FIRST, then call this with FEEDLING_CVM_ID
# set — otherwise we're authorizing a hash dstack never computes.
if [ -n "${FEEDLING_CVM_ID:-}" ]; then
  if [ -z "${PHALA_CLOUD_API_KEY:-}" ]; then
    echo "ERROR: FEEDLING_CVM_ID set but PHALA_CLOUD_API_KEY is not." >&2
    echo "       Can't query the live CVM for its compose_hash." >&2
    exit 1
  fi
  if ! command -v phala >/dev/null 2>&1; then
    echo "ERROR: phala CLI not installed — install via 'npm install -g phala'." >&2
    exit 1
  fi
  echo ">>> reading compose_hash from live CVM: $FEEDLING_CVM_ID"
  LIVE_HASH="$(phala cvms get "$FEEDLING_CVM_ID" -j --api-key "$PHALA_CLOUD_API_KEY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["compose_hash"])' \
    2>/dev/null || true)"
  if [ -z "$LIVE_HASH" ] || [ "${#LIVE_HASH}" -ne 64 ]; then
    echo "ERROR: couldn't read compose_hash from CVM $FEEDLING_CVM_ID (got: '$LIVE_HASH')" >&2
    exit 1
  fi
  COMPOSE_HASH="0x${LIVE_HASH}"
  HASH_SOURCE="live-cvm:$FEEDLING_CVM_ID"
else
  COMPOSE_HASH=$(
python3 - "$APP_COMPOSE_FILE" "$COMPOSE_FILE" <<'PY'
import hashlib, json, pathlib, yaml, sys
app_compose_path, compose_yaml_path = sys.argv[1:]

if app_compose_path:
    app_compose = json.loads(pathlib.Path(app_compose_path).read_text())
else:
    # Synthesize a minimal app-compose.json wrapping the docker-compose.yaml.
    # NOTE: dstack at deploy time adds fields like "features" and rewrites
    # "pre_launch_script" to its current version, so this synthesis will
    # typically NOT match the live CVM's hash. Use FEEDLING_CVM_ID mode
    # (above) for any real deploy; synthesis stays only as an offline
    # dev aid.
    docker_yaml = pathlib.Path(compose_yaml_path).read_text()
    app_compose = {
        "manifest_version": 2,
        "name": "feedling-mcp",
        "runner": "docker-compose",
        "docker_compose_file": docker_yaml,
        "docker_config": {},
        "kms_enabled": True,
        "tproxy_enabled": True,
        "public_logs": False,
        "public_sysinfo": False,
        "public_tcbinfo": False,
        "local_key_provider_enabled": False,
        "allowed_envs": [],
        "no_instance_id": False,
    }

canonical = json.dumps(app_compose, separators=(",", ":"), sort_keys=True)
print(hashlib.sha256(canonical.encode()).hexdigest())
PY
)
  COMPOSE_HASH="0x$COMPOSE_HASH"
  HASH_SOURCE="synthesized from $COMPOSE_FILE (offline mode)"
fi

GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
REMOTE_URL="$(git -C "$REPO_ROOT" config --get remote.origin.url | \
  sed -e 's|\.git$||' -e 's|git@github.com:|https://github.com/|')"
REL_COMPOSE="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$COMPOSE_FILE" "$REPO_ROOT")"
YAML_URL="${REMOTE_URL}/raw/${GIT_COMMIT}/${REL_COMPOSE}"

echo ">>> compose_file  : $COMPOSE_FILE"
echo ">>> hash_source   : $HASH_SOURCE"
echo ">>> compose_hash  : $COMPOSE_HASH"
echo ">>> git_commit    : $GIT_COMMIT"
echo ">>> yaml_url      : $YAML_URL"
echo ">>> chain         : $CHAIN"
echo

# Publisher validates chain/owner/hash, estimates with a hard cap, journals the
# exact signed intent, broadcasts once and verifies receipt plus authorization.
if [ -z "${FEEDLING_APP_AUTH_CONTRACT:-}" ] && [ -f "$REPO_ROOT/contracts/.env" ]; then
  set -a; . "$REPO_ROOT/contracts/.env"; set +a
fi
export FEEDLING_PUBLISH_CHAIN="$CHAIN"
export FEEDLING_COMPOSE_HASH="$COMPOSE_HASH"
export FEEDLING_GIT_COMMIT="$GIT_COMMIT"
export FEEDLING_COMPOSE_YAML_URL="$YAML_URL"
export FEEDLING_PUBLISH_EVIDENCE_DIR="${FEEDLING_PUBLISH_EVIDENCE_DIR:-$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-path compose-publish-evidence)}"
exec python3 "$REPO_ROOT/deploy/compose_publisher.py"
