"""Enclave configuration constants and utilities."""
import os

# For local dev we point at the simulator; in a real CVM, dstack-sdk defaults
# to /var/run/dstack.sock inside the container.
#
# dstack-sdk checks `"DSTACK_SIMULATOR_ENDPOINT" in os.environ` — presence,
# not truthiness. An env var set to "" counts as present and makes the SDK
# try to connect to "" (EINVAL). Drop it if it's empty so the SDK falls
# through to /var/run/dstack.sock. A non-empty value means "I really do
# want the simulator" and stays put.
if os.environ.get("DSTACK_SIMULATOR_ENDPOINT", "") == "":
    os.environ.pop("DSTACK_SIMULATOR_ENDPOINT", None)

ENCLAVE_PORT = int(os.environ.get("FEEDLING_ENCLAVE_PORT", 5003))

# Phase 3: in-enclave TLS. When true, bootstrap() derives an ECDSA P-256
# keypair from dstack-KMS, issues a self-signed cert for it, binds
# sha256(cert-DER) into REPORT_DATA, and serves the enclave ASGI app over
# HTTPS on ENCLAVE_PORT. Clients verify by matching the presented cert's DER
# hash against the attested fingerprint — not by PKI chain, since the
# cert is self-signed on purpose (key material is bound to compose_hash
# via dstack-KMS, which is stronger than LE trust).
#
# Off by default so the local dstack simulator + curl/httpx stay HTTP.
# docker-compose.phala.yaml sets this true on real deployments.
ENCLAVE_TLS = os.environ.get("FEEDLING_ENCLAVE_TLS", "false").lower() == "true"

# Internal HTTPS (or HTTP in dev) to the non-TEE backend. This is the
# only network dependency the enclave has after boot. Requests carry the
# caller's api_key so the backend's require_user resolves to the right user's
# ciphertext. The enclave never sees users.json directly. (Env var name keeps
# the historical FLASK_URL spelling — it is baked into the compose contract.)
FLASK_URL = os.environ.get("FEEDLING_FLASK_URL", "http://127.0.0.1:5001")

# Shared runtime-token HMAC secret (same value the backend verifies + the
# supervisor mints with — all three live in the same TDX domain). When present,
# the enclave verifies a caller's runtime token LOCALLY and skips the
# /v1/users/whoami reentrant backend round-trip. Empty → fall back to the
# round-trip (unchanged behavior). Read once at import; it is deploy-time env.
RUNTIME_TOKEN_SECRET = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").encode("utf-8")

# Screen VLM (caption route) — reads at startup; runtime re-reads os.environ
# so a secret rotation takes effect without a restart.
SCREEN_VLM_API_KEY = os.environ.get("FEEDLING_SCREEN_VLM_API_KEY", "")
SCREEN_VLM_MODEL = os.environ.get("FEEDLING_SCREEN_VLM_MODEL", "qwen/qwen3-vl-8b-instruct")
SCREEN_VLM_BASE_URL = os.environ.get("FEEDLING_SCREEN_VLM_BASE_URL", "https://openrouter.ai/api/v1")

# Release metadata — normally injected via build-time env or read from a
# sidecar file baked into the image. For Phase 1 we accept env values with
# obvious placeholders so it's clear this isn't fabricated content.
RELEASE = {
    "git_commit": os.environ.get("FEEDLING_GIT_COMMIT", "dev"),
    "image_digest": os.environ.get("FEEDLING_IMAGE_DIGEST", "sha256:dev"),
    "built_at": os.environ.get("FEEDLING_BUILT_AT", "dev"),
    "compose_yaml_url": os.environ.get(
        "FEEDLING_COMPOSE_YAML_URL",
        "https://github.com/teleport-computer/feedling-mcp/raw/main/deploy/docker-compose.yaml",
    ),
    "build_recipe_url": os.environ.get(
        "FEEDLING_BUILD_RECIPE_URL",
        "https://github.com/teleport-computer/feedling-mcp/blob/main/deploy/BUILD.md",
    ),
}

# Phase 1 testnet deployment (Ethereum Sepolia, chain 11155111). Will be
# redeployed to Base Sepolia (chain 84532) before Phase 2, then to Base
# mainnet (chain 8453) before Phase 5. The default is the live Phase 1
# testnet contract; env vars override when we bring up new chains.
APP_AUTH = {
    "contract": os.environ.get(
        "FEEDLING_APP_AUTH_CONTRACT",
        "0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F",
    ),
    "chain_id": int(os.environ.get("FEEDLING_APP_AUTH_CHAIN_ID", 11155111)),
    "deploy_tx": os.environ.get(
        "FEEDLING_APP_AUTH_DEPLOY_TX",
        "0x752f213ae95f6759a86750dab9545c79c6841ad7838082ddf6ad5271d117915f",
    ),
    "explorer_base_url": os.environ.get(
        "FEEDLING_APP_AUTH_EXPLORER",
        "https://sepolia.etherscan.io",
    ),
}


def env_flag_enabled(name: str, default: str = "false") -> bool:
    return str(os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


# Number of worker threads in the single gunicorn worker. The enclave's
# concurrency profile is I/O-bound: every decrypt-and-serve request calls
# back into the backend over httpx and parks the thread on that round-trip,
# so a generously sized thread pool (not CPU count) is what keeps the pool
# from starving. The whoami short-TTL cache + singleflight (see top of file)
# already collapse the history-import auth storm, so 32 is ample headroom.
# Now is decryption thread pool capacity (see spec §4).
ENCLAVE_THREADS = int(os.environ.get("FEEDLING_ENCLAVE_THREADS", 32))


def enclave_worker_count() -> int:
    """gunicorn worker (process) count. Default 1 preserves the historical
    single-worker model where the process-local whoami/content-sk caches stay
    coherent. On a multi-vCPU CVM (prod = 8 vCPU) the decrypt crypto is GIL-bound,
    so once the enclave→backend I/O reentrancy is out of the way, additional
    worker PROCESSES parallelize decrypts across cores. Read from env so a deploy
    can flip it without a code change; clamped to ≥1."""
    # ``or "1"`` guards the empty string CI injects for an unset var (int("")
    # would crash enclave boot).
    return max(1, int((os.environ.get("FEEDLING_ENCLAVE_WORKERS") or "").strip() or "1"))
