#!/usr/bin/env python3
"""Provision and clean up the private accounts used by API-key qualification.

This module is deliberately mechanical.  It is the credential boundary between
GitHub Actions secrets and the headless qualification agent: provider secrets
are consumed here and are never written to the private account manifest.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import ssl
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.provider_smoke import crypto  # noqa: E402
from tools.provider_smoke.client import Session, SmokeClient  # noqa: E402

try:
    from qa.orchestration_contract import MEMORY_CONTRACT_PROFILE_ID
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from orchestration_contract import MEMORY_CONTRACT_PROFILE_ID


ALLOWED_BASE_URL = "https://test-api.feedling.app"
ALLOWED_KONGBEIQIE_BASE_URL = "https://xn--vduyey89e.com/v1"
BASELINE_RUNTIME_REQUIREMENT = "deployed_current"
RUNTIME_V2_REQUIREMENT = "hosted_resident"
EXPECTED_RUNTIME_MODE = RUNTIME_V2_REQUIREMENT
RUNTIME_V2_VERSION = 2
# Backward-compatible names for existing fixtures and strict-V2 callers. They
# describe the observed legacy API label, not the default diagnostic requirement.
DIAGNOSTIC_RUNTIME_MODE = RUNTIME_V2_REQUIREMENT
DIAGNOSTIC_RUNTIME_VERSION = RUNTIME_V2_VERSION
QUALIFICATION_MODE_DIAGNOSTIC = "diagnostic"
EXPECTED_REASONING_EFFORT = "medium"
INVALID_PROVIDER_KEY = "io-e2e-agent-driven-test-intentionally-invalid"
MANIFEST_SCHEMA_VERSION = 1
PERSONA_MEMORY_POOL_MANIFEST_KIND = "persona_memory_account_pool"
MAX_PERSONA_MEMORY_POOL_COUNT = 24
SYNTHETIC_LABEL_PREFIX = "agent-e2e-"
SYNTHETIC_REAPER_PATH = "/v1/admin/qa/synthetic-account-reaper"
SYNTHETIC_REGISTRATION_PATH = "/v1/admin/qa/synthetic-accounts/register"
SYNTHETIC_ABSENCE_PATH = "/v1/admin/qa/synthetic-accounts/absence"
SYNTHETIC_CLEANUP_RUN_PATH = "/v1/admin/qa/synthetic-accounts/cleanup-run"
SYNTHETIC_CLEANUP_RUN_KIND = "qa_synthetic_run_cleanup"
MAX_SYNTHETIC_RUN_ID_LENGTH = 48
MAX_SYNTHETIC_TTL_SECONDS = 14_400
CLEANUP_EVIDENCE_ATTEMPTS = 8
CLEANUP_EVIDENCE_DELAY_SECONDS = 2.0
MAX_PRIVATE_MANIFEST_BYTES = 2 * 1024 * 1024
MANIFEST_CLEANUP_FAILURE_ID = "__manifest__"
_SYNTHETIC_LEASE_RE = re.compile(r"^lease_[0-9a-f]{32}$")
_SYNTHETIC_ABSENCE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_SYNTHETIC_RUN_ID_RE = re.compile(
    rf"^[A-Za-z0-9_.-]{{1,{MAX_SYNTHETIC_RUN_ID_LENGTH}}}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVISION_STATUS_READY = "ready"
PROVISION_STATUS_BLOCKED = "blocked"
PROVISION_FAILURE_NONE = "NONE"
PROVISION_FAILURE_INCOMPLETE = "PROVISIONING_INCOMPLETE"
OPERATIONAL_PROVISION_FAILURE_CODES = frozenset(
    {
        "FRESH_ACCOUNT_CHECK_FAILED",
        "REGISTRATION_VERIFICATION_FAILED",
        "ACCOUNT_NOT_FRESH",
        "INVALID_KEY_CHECK_FAILED",
        "INVALID_KEY_REJECTION_FAILED",
        "INVALID_KEY_ECHOED",
        "INVALID_KEY_ACCEPTED",
        "VALID_KEY_SETUP_FAILED",
        "VALID_KEY_REJECTED",
        "VALID_KEY_ECHOED",
        "VALID_KEY_ROUTE_MISMATCH",
        "TRACE_ENABLE_FAILED",
        "TRACE_UNAVAILABLE",
        "RUNTIME_MODE_SET_FAILED",
        "RUNTIME_MODE_VERIFICATION_FAILED",
    }
)


class ProvisionError(RuntimeError):
    """A sanitized provisioning failure safe to print in CI."""


class _ProfileProvisionFailure(RuntimeError):
    """A fixed-code operational failure isolated to one registered profile."""

    def __init__(self, code: str):
        if code not in OPERATIONAL_PROVISION_FAILURE_CODES:
            raise ValueError("unsupported profile provisioning failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProfileSpec:
    provider: str
    route_family: str
    model_family: str
    credential_env: str
    model_env: str
    allowed_model_regex: str
    expected_configured_base_url: str
    base_url_env: str = ""
    allowed_base_url: str = ""


PROFILE_SPECS: dict[str, ProfileSpec] = {
    "official-deepseek": ProfileSpec(
        provider="deepseek",
        route_family="official",
        model_family="deepseek",
        credential_env="QA_DEEPSEEK_API_KEY",
        model_env="QA_DEEPSEEK_MODEL",
        allowed_model_regex=r"^deepseek-[a-z0-9][a-z0-9._-]*$",
        expected_configured_base_url="https://api.deepseek.com",
    ),
    "official-anthropic": ProfileSpec(
        provider="anthropic",
        route_family="official",
        model_family="claude",
        credential_env="QA_ANTHROPIC_API_KEY",
        model_env="QA_ANTHROPIC_MODEL",
        allowed_model_regex=r"^claude-[a-z0-9][a-z0-9._-]*$",
        expected_configured_base_url="https://api.anthropic.com/v1",
    ),
    "official-openai": ProfileSpec(
        provider="openai",
        route_family="official",
        model_family="openai",
        credential_env="QA_OPENAI_PROVIDER_API_KEY",
        model_env="QA_OPENAI_MODEL",
        allowed_model_regex=r"^(?:gpt-[a-z0-9][a-z0-9._-]*|o[1-9][a-z0-9._-]*)$",
        expected_configured_base_url="https://api.openai.com/v1",
    ),
    "official-gemini": ProfileSpec(
        provider="gemini",
        route_family="official",
        model_family="gemini",
        credential_env="QA_GEMINI_API_KEY",
        model_env="QA_GEMINI_MODEL",
        allowed_model_regex=r"^gemini-(?:2\.5|3\.5)-[a-z0-9][a-z0-9._-]*$",
        expected_configured_base_url=(
            "https://generativelanguage.googleapis.com/v1beta"
        ),
    ),
    "openrouter-claude": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="claude",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_CLAUDE_MODEL",
        allowed_model_regex=r"^anthropic/claude-[a-z0-9][a-z0-9._:-]*$",
        expected_configured_base_url="https://openrouter.ai/api/v1",
    ),
    "openrouter-openai": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="openai",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_OPENAI_MODEL",
        allowed_model_regex=r"^openai/(?:gpt-[a-z0-9][a-z0-9._:-]*|o[a-z0-9._:-]*)$",
        expected_configured_base_url="https://openrouter.ai/api/v1",
    ),
    "openrouter-glm": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="glm",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_GLM_MODEL",
        allowed_model_regex=r"^(?:z-ai|thudm)/glm-[a-z0-9][a-z0-9._:-]*$",
        expected_configured_base_url="https://openrouter.ai/api/v1",
    ),
    "openrouter-kimi": ProfileSpec(
        provider="openrouter",
        route_family="openrouter",
        model_family="kimi",
        credential_env="QA_OPENROUTER_API_KEY",
        model_env="QA_OPENROUTER_KIMI_MODEL",
        allowed_model_regex=r"^moonshotai/kimi-[a-z0-9][a-z0-9._:-]*$",
        expected_configured_base_url="https://openrouter.ai/api/v1",
    ),
    "relay-kongbeiqie": ProfileSpec(
        provider="openai_compatible",
        route_family="relay",
        model_family="claude",
        credential_env="QA_KONGBEIQIE_API_KEY",
        model_env="QA_KONGBEIQIE_MODEL",
        allowed_model_regex=(
            r"^(?:\[[^\r\n\]|`]{1,32}\])?" r"claude-[a-z0-9][a-z0-9._-]*$"
        ),
        expected_configured_base_url=ALLOWED_KONGBEIQIE_BASE_URL,
        base_url_env="QA_KONGBEIQIE_BASE_URL",
        allowed_base_url=ALLOWED_KONGBEIQIE_BASE_URL,
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_synthetic_run_id(value: Any) -> str:
    raw = str(value or "local")
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw)
    if len(normalized) > MAX_SYNTHETIC_RUN_ID_LENGTH:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        prefix_length = MAX_SYNTHETIC_RUN_ID_LENGTH - len(digest) - 1
        normalized = f"{normalized[:prefix_length]}-{digest}"
    if _SYNTHETIC_RUN_ID_RE.fullmatch(normalized) is None:
        raise ProvisionError("synthetic cleanup run ID is invalid")
    return normalized


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise ProvisionError(f"missing required environment variable: {name}")
    return value


def _response_contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Mapping):
        return any(
            _response_contains_secret(key, secret)
            or _response_contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_response_contains_secret(item, secret) for item in value)
    return False


def validate_base_url(raw: str) -> str:
    """Return the one allowed test endpoint, rejecting redirect-like variants."""
    value = str(raw or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ProvisionError(
            "IO_E2E_BASE_URL is not the approved test endpoint"
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "test-api.feedling.app"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisionError("IO_E2E_BASE_URL is not the approved test endpoint")
    return ALLOWED_BASE_URL


def _load_coverage(path: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProvisionError(f"coverage lock not found: {path}") from None
    except (OSError, json.JSONDecodeError):
        raise ProvisionError(f"coverage lock is unreadable: {path}") from None

    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, list):
        raise ProvisionError("coverage lock must contain a profiles array")

    by_id: dict[str, dict[str, Any]] = {}
    for raw in profiles:
        if not isinstance(raw, dict):
            raise ProvisionError("coverage profile entries must be objects")
        profile_id = str(raw.get("profile_id") or raw.get("id") or "").strip()
        if not profile_id:
            raise ProvisionError("coverage profile is missing profile_id")
        if profile_id in by_id:
            raise ProvisionError(f"duplicate coverage profile: {profile_id}")
        normalized = dict(raw)
        normalized["profile_id"] = profile_id
        by_id[profile_id] = normalized

    expected = set(PROFILE_SPECS)
    actual = set(by_id)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        unexpected = ",".join(sorted(actual - expected)) or "none"
        raise ProvisionError(
            f"coverage profiles do not match the locked API-key matrix "
            f"(missing={missing}; unexpected={unexpected})"
        )

    ordered: list[dict[str, Any]] = []
    for profile_id in PROFILE_SPECS:
        profile = by_id[profile_id]
        spec = PROFILE_SPECS[profile_id]
        if str(profile.get("provider") or "").strip() != spec.provider:
            raise ProvisionError(
                f"provider mismatch for coverage profile: {profile_id}"
            )
        route_family = str(profile.get("route_family") or spec.route_family).strip()
        if route_family != spec.route_family:
            raise ProvisionError(
                f"route family mismatch for coverage profile: {profile_id}"
            )
        model_family = str(profile.get("model_family") or "").strip()
        if model_family != spec.model_family:
            raise ProvisionError(
                f"model family mismatch for coverage profile: {profile_id}"
            )
        credential_slot = str(
            profile.get("credential_slot")
            or profile.get("provider_key_env")
            or spec.credential_env
        ).strip()
        if credential_slot != spec.credential_env:
            raise ProvisionError(
                f"credential slot mismatch for coverage profile: {profile_id}"
            )
        model_env = str(profile.get("model_env") or "").strip()
        if model_env != spec.model_env:
            raise ProvisionError(
                f"model environment mismatch for profile: {profile_id}"
            )
        allowed_model_regex = profile.get("allowed_model_regex")
        if allowed_model_regex != spec.allowed_model_regex:
            raise ProvisionError(
                f"model constraint mismatch for coverage profile: {profile_id}"
            )
        base_url_env = str(profile.get("base_url_env") or "").strip()
        if base_url_env != spec.base_url_env:
            raise ProvisionError(
                f"base URL environment mismatch for coverage profile: {profile_id}"
            )
        allowed_base_url = str(profile.get("allowed_base_url") or "").strip()
        if allowed_base_url != spec.allowed_base_url:
            raise ProvisionError(
                f"base URL constraint mismatch for coverage profile: {profile_id}"
            )
        ordered.append(profile)
    return ordered


def _select_profiles(
    profiles: Sequence[dict[str, Any]],
    *,
    diagnostic: bool,
    profile_ids: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Select a locked diagnostic subset without weakening strict coverage."""
    if not diagnostic:
        if profile_ids is not None:
            raise ProvisionError("profile subsets require diagnostic mode")
        return list(profiles)
    if profile_ids is None:
        return list(profiles)
    if isinstance(profile_ids, (str, bytes)):
        raise ProvisionError("diagnostic profile selection must be a sequence")

    requested = [str(profile_id or "").strip() for profile_id in profile_ids]
    if not requested or any(not profile_id for profile_id in requested):
        raise ProvisionError("diagnostic profile selection must not be empty")
    if len(requested) != len(set(requested)):
        raise ProvisionError("diagnostic profile selection contains duplicates")
    unknown = sorted(set(requested) - set(PROFILE_SPECS))
    if unknown:
        raise ProvisionError(
            "diagnostic profile selection is outside the locked API-key matrix"
        )
    requested_set = set(requested)
    return [
        profile
        for profile in profiles
        if str(profile.get("profile_id") or "") in requested_set
    ]


def _model_for(
    profile: Mapping[str, Any], spec: ProfileSpec, env: Mapping[str, str]
) -> str:
    configured_env = str(profile.get("model_env") or spec.model_env).strip()
    if configured_env != spec.model_env:
        raise ProvisionError(
            f"model environment mismatch for profile: {profile['profile_id']}"
        )
    raw_model = str(env.get(spec.model_env) or "")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in raw_model
    ):
        raise ProvisionError(
            f"model configuration does not match the locked family for profile: "
            f"{profile['profile_id']}"
        )
    model = raw_model.strip()
    if not model:
        raise ProvisionError(f"missing required model configuration: {spec.model_env}")
    if len(model) > 160 or re.fullmatch(spec.allowed_model_regex, model) is None:
        raise ProvisionError(
            f"model configuration does not match the locked family for profile: "
            f"{profile['profile_id']}"
        )
    return model


def _provider_base_url_for(
    profile: Mapping[str, Any], spec: ProfileSpec, env: Mapping[str, str]
) -> str:
    """Return the locked request URL for a custom relay, or empty for defaults."""
    profile_id = str(profile.get("profile_id") or "unknown")
    if not spec.base_url_env:
        return ""
    value = _required_env(env, spec.base_url_env)
    if value != spec.allowed_base_url:
        raise ProvisionError(
            f"provider base URL does not match the locked endpoint for profile: "
            f"{profile_id}"
        )
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ProvisionError(
            f"provider base URL does not match the locked endpoint for profile: "
            f"{profile_id}"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisionError(
            f"provider base URL does not match the locked endpoint for profile: "
            f"{profile_id}"
        )
    return spec.allowed_base_url


def _reasoning_effort_for(profile: Mapping[str, Any]) -> str:
    profile_id = str(profile.get("profile_id") or "unknown")
    if profile.get("reasoning_expected") is not True:
        raise ProvisionError(
            f"reasoning must be required for coverage profile: {profile_id}"
        )
    effort = str(profile.get("reasoning_effort") or "").strip().lower()
    if effort != EXPECTED_REASONING_EFFORT:
        raise ProvisionError(
            f"reasoning effort must be {EXPECTED_REASONING_EFFORT} for coverage profile: {profile_id}"
        )
    return effort


def _atomic_write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(manifest, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate_new_private_manifest_path(path: Path) -> Path:
    """Fail before mutation unless a new manifest can live in a private dir."""
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.exists() or candidate.is_symlink():
        raise ProvisionError("pool manifest path must be a new absolute path")
    try:
        parent = candidate.parent.resolve(strict=True)
        metadata = parent.stat()
    except (OSError, RuntimeError):
        raise ProvisionError("pool manifest parent is unavailable") from None
    if (
        parent != candidate.parent
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProvisionError("pool manifest parent must be owner-controlled mode 0700")
    return candidate


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into a terminal HTTP response.

    ``urllib`` normally copies custom headers to a redirected request, including
    ``X-Admin-Token``.  Raising here prevents construction or dispatch of that
    second request, regardless of whether the target is same-origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "admin endpoint redirect rejected",
            headers,
            fp,
        )


class AdminClient:
    """Minimal admin transport kept separate from per-user SmokeClient auth."""

    def __init__(
        self, base_url: str, token: str, ssl_context: ssl.SSLContext | None = None
    ):
        self.base_url = validate_base_url(base_url)
        self._token = token
        self._ssl = ssl_context or ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl),
            _RejectRedirects(),
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        attempts: int = 5,
        timeout_seconds: float = 45,
    ) -> tuple[int, dict]:
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("attempts must be an integer between 1 and 5")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= 300
        ):
            raise ValueError("timeout_seconds must be between 1 and 300")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json", "X-Admin-Token": self._token},
            method=method,
        )
        for attempt in range(attempts):
            try:
                with self._opener.open(
                    request, timeout=float(timeout_seconds)
                ) as response:
                    return response.status, json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read() or b"{}")
                except Exception:
                    payload = {}
                return exc.code, payload
            except (
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
                ConnectionError,
                OSError,
            ):
                if attempt < attempts - 1:
                    time.sleep(2.0 * (attempt + 1))
        raise ProvisionError("admin endpoint was unreachable") from None

    def register_synthetic(
        self, label: str, *, run_id: str, ttl_seconds: int
    ) -> tuple[Session, dict[str, Any]]:
        """Create one server-marked account while keeping its private key local."""

        if (
            not isinstance(run_id, str)
            or _SYNTHETIC_RUN_ID_RE.fullmatch(run_id) is None
            or not label.startswith(f"{SYNTHETIC_LABEL_PREFIX}{run_id}-")
        ):
            raise ProvisionError("synthetic registration run binding is invalid")

        sk, pk = crypto.generate_keypair()
        try:
            status, body = self.request(
                "POST",
                SYNTHETIC_REGISTRATION_PATH,
                {
                    "public_key": base64.b64encode(pk).decode("ascii"),
                    "access_mode": "model_api",
                    "label": label,
                    "run_id": run_id,
                    "ttl_seconds": ttl_seconds,
                },
                # This endpoint is deliberately non-idempotent. A lost response
                # may mean the server already committed an account; retrying
                # would create a second inaccessible lease until the TTL reaper.
                attempts=1,
            )
        except Exception:
            raise ProvisionError("synthetic account registration failed") from None
        response_body = body if isinstance(body, Mapping) else {}
        user_id = response_body.get("user_id")
        api_key = response_body.get("api_key")
        lease_id = response_body.get("lease_id")
        absence_token = response_body.get("absence_token")
        expires_at = response_body.get("expires_at")
        expires_at_epoch = response_body.get("expires_at_epoch")
        if (
            status != 201
            or not isinstance(user_id, str)
            or not user_id
            or not isinstance(api_key, str)
            or not api_key
            or response_body.get("label") != label
            or not isinstance(lease_id, str)
            or _SYNTHETIC_LEASE_RE.fullmatch(lease_id) is None
            or not isinstance(absence_token, str)
            or _SYNTHETIC_ABSENCE_TOKEN_RE.fullmatch(absence_token) is None
            or not isinstance(expires_at, str)
            or not expires_at
            or type(expires_at_epoch) is not int
            or expires_at_epoch <= int(time.time())
        ):
            raise ProvisionError("synthetic account registration receipt is invalid")
        return (
            Session(user_id=user_id, api_key=api_key, sk=sk, pk=pk),
            {
                "registered": True,
                "lease_id": lease_id,
                "absence_token": absence_token,
                "expires_at": expires_at,
                "expires_at_epoch": expires_at_epoch,
                "ttl_seconds": ttl_seconds,
            },
        )

    def cleanup_synthetic_run(self, run_id: str) -> dict[str, Any]:
        """Sweep one normalized run without relying on a local manifest."""
        if (
            not isinstance(run_id, str)
            or _SYNTHETIC_RUN_ID_RE.fullmatch(run_id) is None
        ):
            raise ProvisionError("synthetic cleanup run ID is invalid")
        try:
            status, body = self.request(
                "POST",
                SYNTHETIC_CLEANUP_RUN_PATH,
                {"run_id": run_id},
                attempts=1,
                timeout_seconds=180,
            )
        except Exception:
            raise ProvisionError("synthetic run cleanup failed") from None
        receipt = body if isinstance(body, Mapping) else {}
        expected_keys = {
            "schema_version",
            "kind",
            "run_id_sha256",
            "label_prefix_sha256",
            "database_authoritative",
            "matched_count",
            "deleted_count",
            "already_absent_count",
            "operation_failure_count",
            "remaining_count",
            "complete",
        }
        count_keys = {
            "matched_count",
            "deleted_count",
            "already_absent_count",
            "operation_failure_count",
            "remaining_count",
        }
        expected_run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        expected_label_hash = hashlib.sha256(
            f"{SYNTHETIC_LABEL_PREFIX}{run_id}-".encode("utf-8")
        ).hexdigest()
        counts_valid = all(
            type(receipt.get(key)) is int and receipt[key] >= 0
            for key in count_keys
        )
        if (
            status != 200
            or set(receipt) != expected_keys
            or receipt.get("schema_version") != 1
            or receipt.get("kind") != SYNTHETIC_CLEANUP_RUN_KIND
            or receipt.get("run_id_sha256") != expected_run_hash
            or receipt.get("label_prefix_sha256") != expected_label_hash
            or receipt.get("database_authoritative") is not True
            or type(receipt.get("complete")) is not bool
            or not counts_valid
            or receipt.get("deleted_count", 0)
            + receipt.get("already_absent_count", 0)
            > receipt.get("matched_count", 0)
            or receipt.get("operation_failure_count", 0)
            > receipt.get("matched_count", 0)
            or receipt.get("matched_count", 0)
            != receipt.get("deleted_count", 0)
            + receipt.get("already_absent_count", 0)
            + receipt.get("operation_failure_count", 0)
            or (
                receipt.get("complete") is True
                and (
                    receipt.get("operation_failure_count") != 0
                    or receipt.get("remaining_count") != 0
                )
            )
        ):
            raise ProvisionError("synthetic run cleanup receipt is invalid")
        return dict(receipt)


def _manifest_entry(
    profile: Mapping[str, Any],
    spec: ProfileSpec,
    model: str,
    configured_base_url: str,
    reasoning_effort: str,
    session: Session,
    label: str,
    *,
    diagnostic: bool = False,
    runtime_requirement: str = RUNTIME_V2_REQUIREMENT,
    synthetic_lease: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "profile_id": profile["profile_id"],
        "label": label,
        "provider": spec.provider,
        "route_family": spec.route_family,
        "configured_model": model,
        "configured_base_url": configured_base_url,
        "reasoning_effort": reasoning_effort,
        "user_id": session.user_id,
        "api_key": session.api_key,
        "secret_key_b64": base64.b64encode(session.sk).decode("ascii"),
        "public_key_b64": base64.b64encode(session.pk).decode("ascii"),
        "trace_enabled": False,
        "runtime_mode": "",
        "runtime_version": 0,
        # Current Runtime V2 auto-migrates every configured model-API profile.
        # Qualification proves the resulting user-scoped readback instead of
        # depending on a test-only mutation endpoint that production does not use.
        "runtime_mode_set_required": False,
        "runtime_readback_receipt": None,
        "registration_verified": False,
        "fresh_state_verified": False,
        "invalid_key_rejected": False,
        "invalid_key_receipt": None,
        "valid_key_configured": False,
        "valid_key_receipt": None,
        "runtime_mode_set_verified": False,
        "runtime_mode_readback_verified": False,
        "provision_status": PROVISION_STATUS_BLOCKED,
        "provision_failure_code": PROVISION_FAILURE_INCOMPLETE,
        "synthetic_account_lease": (
            None if synthetic_lease is None else dict(synthetic_lease)
        ),
    }
    if diagnostic:
        entry.update(
            {
                "qualification_mode": QUALIFICATION_MODE_DIAGNOSTIC,
            }
        )
    return entry


def _memory_contract_entry(
    session: Session, label: str, synthetic_lease: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "profile_id": MEMORY_CONTRACT_PROFILE_ID,
        "purpose": "deterministic_memory_contract",
        "label": label,
        "user_id": session.user_id,
        "api_key": session.api_key,
        "secret_key_b64": base64.b64encode(session.sk).decode("ascii"),
        "public_key_b64": base64.b64encode(session.pk).decode("ascii"),
        "provision_status": PROVISION_STATUS_READY,
        "provision_failure_code": PROVISION_FAILURE_NONE,
        "synthetic_account_lease": dict(synthetic_lease),
    }


def _verify_synthetic_reaper(admin_client: AdminClient) -> dict[str, Any]:
    try:
        status, body = admin_client.request("GET", SYNTHETIC_REAPER_PATH)
    except Exception:
        raise ProvisionError("synthetic-account reaper preflight failed") from None
    if (
        status != 200
        or not isinstance(body, dict)
        or body.get("enabled") is not True
        or body.get("ready") is not True
        or body.get("heartbeat_fresh") is not True
        or body.get("label_prefix") != SYNTHETIC_LABEL_PREFIX
        or not isinstance(body.get("max_ttl_seconds"), int)
        or isinstance(body.get("max_ttl_seconds"), bool)
        or not 1 <= body["max_ttl_seconds"] <= MAX_SYNTHETIC_TTL_SECONDS
    ):
        raise ProvisionError("synthetic-account reaper is not safely configured")
    return {
        "enabled": True,
        "ready": True,
        "heartbeat_fresh": True,
        "label_prefix": SYNTHETIC_LABEL_PREFIX,
        "max_ttl_seconds": body["max_ttl_seconds"],
    }


def _check_fresh_account(
    client: SmokeClient, session: Session, entry: dict[str, Any]
) -> None:
    try:
        who_status, who_body = client._req(
            "GET", "/v1/users/whoami", api_key=session.api_key
        )
        chat_status, chat_body = client._req(
            "GET", "/v1/chat/history?limit=1", api_key=session.api_key
        )
        memory_status, memory_body = client._req(
            "GET", "/v1/memory/list?limit=1", api_key=session.api_key
        )
    except Exception:
        raise _ProfileProvisionFailure("FRESH_ACCOUNT_CHECK_FAILED") from None
    if (
        who_status != 200
        or not isinstance(who_body, Mapping)
        or who_body.get("user_id") != session.user_id
    ):
        raise _ProfileProvisionFailure("REGISTRATION_VERIFICATION_FAILED")
    entry["registration_verified"] = True
    if (
        chat_status != 200
        or memory_status != 200
        or not isinstance(chat_body, Mapping)
        or not isinstance(memory_body, Mapping)
        or (chat_body.get("messages") or [])
        or (memory_body.get("moments") or [])
    ):
        raise _ProfileProvisionFailure("ACCOUNT_NOT_FRESH")
    entry["fresh_state_verified"] = True


def _check_invalid_key(
    client: SmokeClient,
    session: Session,
    spec: ProfileSpec,
    model: str,
    provider_base_url: str,
    reasoning_effort: str,
    entry: dict[str, Any],
) -> None:
    try:
        invalid_status, invalid_body = client.setup_raw(
            session,
            spec.provider,
            model,
            provider_base_url,
            INVALID_PROVIDER_KEY,
            reasoning_effort=reasoning_effort,
        )
    except Exception:
        raise _ProfileProvisionFailure("INVALID_KEY_CHECK_FAILED") from None
    if not isinstance(invalid_body, Mapping):
        raise _ProfileProvisionFailure("INVALID_KEY_REJECTION_FAILED")
    if _response_contains_secret(invalid_body, INVALID_PROVIDER_KEY):
        raise _ProfileProvisionFailure("INVALID_KEY_ECHOED")
    provider_status = invalid_body.get("status_code")
    if (
        invalid_status != 400
        or invalid_body.get("error") != "provider_test_failed"
        or provider_status not in (400, 401, 403)
    ):
        if invalid_status == 200:
            raise _ProfileProvisionFailure("INVALID_KEY_ACCEPTED")
        raise _ProfileProvisionFailure("INVALID_KEY_REJECTION_FAILED")
    entry["invalid_key_rejected"] = True
    entry["invalid_key_receipt"] = {
        "http_status": invalid_status,
        "error": "provider_test_failed",
        "provider_status_code": provider_status,
    }


def _configure_valid_key(
    client: SmokeClient,
    session: Session,
    spec: ProfileSpec,
    model: str,
    provider_base_url: str,
    expected_configured_base_url: str,
    reasoning_effort: str,
    provider_key: str,
    entry: dict[str, Any],
) -> None:
    try:
        valid_status, valid_body = client.setup_raw(
            session,
            spec.provider,
            model,
            provider_base_url,
            provider_key,
            reasoning_effort=reasoning_effort,
        )
    except Exception:
        raise _ProfileProvisionFailure("VALID_KEY_SETUP_FAILED") from None
    if _response_contains_secret(valid_body, provider_key):
        raise _ProfileProvisionFailure("VALID_KEY_ECHOED")
    if isinstance(valid_body, Mapping) and (
        valid_status in (400, 401, 403)
        or valid_body.get("error") == "provider_test_failed"
    ):
        raise _ProfileProvisionFailure("VALID_KEY_REJECTED")
    configured = valid_body.get("config") if isinstance(valid_body, Mapping) else None
    if (
        valid_status != 200
        or not isinstance(valid_body, Mapping)
        or valid_body.get("status") != "configured"
        or not isinstance(configured, Mapping)
        or configured.get("provider") != spec.provider
        or configured.get("model") != model
        or configured.get("base_url") != expected_configured_base_url
        or configured.get("reasoning_effort") != reasoning_effort
    ):
        raise _ProfileProvisionFailure("VALID_KEY_ROUTE_MISMATCH")
    entry["valid_key_configured"] = True
    entry["valid_key_receipt"] = {
        "status": "configured",
        "provider": configured["provider"],
        "model": configured["model"],
        "base_url": configured["base_url"],
        "reasoning_effort": configured["reasoning_effort"],
    }


def _enable_trace(client: SmokeClient, session: Session, entry: dict[str, Any]) -> None:
    try:
        trace_status, trace_body = client._req(
            "POST",
            "/v1/debug/trace/enable",
            api_key=session.api_key,
            body={"enabled": True},
        )
    except Exception:
        raise _ProfileProvisionFailure("TRACE_ENABLE_FAILED") from None
    if (
        trace_status != 200
        or not isinstance(trace_body, Mapping)
        or trace_body.get("enabled") is not True
        or trace_body.get("deploy_enabled") is not True
    ):
        raise _ProfileProvisionFailure("TRACE_UNAVAILABLE")
    entry["trace_enabled"] = True


def _verify_diagnostic_runtime(
    client: SmokeClient,
    session: Session,
    entry: dict[str, Any],
    *,
    runtime_requirement: str,
) -> None:
    try:
        body = client.runtime_status(session)
    except Exception:
        raise _ProfileProvisionFailure("RUNTIME_MODE_VERIFICATION_FAILED") from None
    runtime_mode = body.get("runtime_mode") if isinstance(body, Mapping) else None
    runtime_version = body.get("runtime_version") if isinstance(body, Mapping) else None
    generic_readback_invalid = (
        not isinstance(body, Mapping)
        or body.get("configured") is not True
        or not isinstance(runtime_mode, str)
        or not runtime_mode
        or type(runtime_version) is not int
        or runtime_version < 1
    )
    runtime_v2_mismatch = runtime_requirement == RUNTIME_V2_REQUIREMENT and (
        runtime_mode != RUNTIME_V2_REQUIREMENT or runtime_version != RUNTIME_V2_VERSION
    )
    if generic_readback_invalid or runtime_v2_mismatch:
        raise _ProfileProvisionFailure("RUNTIME_MODE_VERIFICATION_FAILED")
    entry["runtime_mode"] = runtime_mode
    entry["runtime_version"] = runtime_version
    entry["runtime_mode_readback_verified"] = True
    entry["runtime_readback_receipt"] = {
        "configured": True,
        "runtime_mode": runtime_mode,
        "runtime_version": runtime_version,
    }


def _complete_diagnostic_manifest(manifest: Mapping[str, Any]) -> bool:
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list):
        return False
    ids = [
        row.get("profile_id") if isinstance(row, Mapping) else None for row in profiles
    ]
    if manifest.get("qualification_mode") == QUALIFICATION_MODE_DIAGNOSTIC:
        selected = manifest.get("selected_profile_ids")
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(profile_id, str) for profile_id in selected)
            or selected
            != [profile_id for profile_id in PROFILE_SPECS if profile_id in selected]
            or manifest.get("runtime_requirement")
            not in {BASELINE_RUNTIME_REQUIREMENT, RUNTIME_V2_REQUIREMENT}
        ):
            return False
        expected_ids = selected
    else:
        expected_ids = list(PROFILE_SPECS)
    if ids != expected_ids:
        return False
    for row in profiles:
        profile_id = str(row.get("profile_id") or "")
        spec = PROFILE_SPECS.get(profile_id)
        if (
            spec is None
            or row.get("configured_base_url") != spec.expected_configured_base_url
        ):
            return False
        status = row.get("provision_status")
        failure_code = row.get("provision_failure_code")
        if status == PROVISION_STATUS_READY:
            if failure_code != PROVISION_FAILURE_NONE:
                return False
        elif status == PROVISION_STATUS_BLOCKED:
            if failure_code not in OPERATIONAL_PROVISION_FAILURE_CODES:
                return False
        else:
            return False
        if not all(
            isinstance(row.get(field), str) and bool(row.get(field))
            for field in ("user_id", "api_key", "secret_key_b64", "public_key_b64")
        ):
            return False
        if manifest.get(
            "qualification_mode"
        ) != QUALIFICATION_MODE_DIAGNOSTIC and not _synthetic_absence_attestation_valid(
            row
        ):
            return False
    auxiliary = manifest.get("auxiliary_accounts")
    if manifest.get("qualification_mode") == QUALIFICATION_MODE_DIAGNOSTIC:
        return auxiliary == []
    if not isinstance(auxiliary, list) or len(auxiliary) != 1:
        return False
    memory = auxiliary[0]
    return (
        isinstance(memory, Mapping)
        and memory.get("profile_id") == MEMORY_CONTRACT_PROFILE_ID
        and memory.get("purpose") == "deterministic_memory_contract"
        and memory.get("provision_status") == PROVISION_STATUS_READY
        and memory.get("provision_failure_code") == PROVISION_FAILURE_NONE
        and all(
            isinstance(memory.get(field), str) and bool(memory.get(field))
            for field in (
                "label",
                "user_id",
                "api_key",
                "secret_key_b64",
                "public_key_b64",
            )
        )
        and _synthetic_absence_attestation_valid(memory)
    )


def _synthetic_absence_attestation_valid(entry: Mapping[str, Any]) -> bool:
    lease = entry.get("synthetic_account_lease")
    return bool(
        isinstance(lease, Mapping)
        and lease.get("registered") is True
        and isinstance(lease.get("lease_id"), str)
        and _SYNTHETIC_LEASE_RE.fullmatch(lease["lease_id"]) is not None
        and isinstance(lease.get("absence_token"), str)
        and _SYNTHETIC_ABSENCE_TOKEN_RE.fullmatch(lease["absence_token"]) is not None
    )


def _admin_confirms_user_absent(
    admin_client: AdminClient | None, entry: Mapping[str, Any]
) -> bool:
    """Require a lease-attested, strict database absence response."""
    if admin_client is None:
        return False
    user_id = str(entry.get("user_id") or "")
    lease = entry.get("synthetic_account_lease")
    if not isinstance(lease, Mapping):
        return False
    lease_id = str(lease.get("lease_id") or "")
    absence_token = str(lease.get("absence_token") or "")
    if (
        not user_id
        or _SYNTHETIC_LEASE_RE.fullmatch(lease_id) is None
        or _SYNTHETIC_ABSENCE_TOKEN_RE.fullmatch(absence_token) is None
    ):
        return False
    try:
        status, body = admin_client.request(
            "POST",
            SYNTHETIC_ABSENCE_PATH,
            {
                "user_id": user_id,
                "lease_id": lease_id,
                "absence_token": absence_token,
            },
        )
    except Exception:
        return False
    return bool(
        status == 200
        and isinstance(body, Mapping)
        and body.get("schema_version") == 1
        and body.get("status") == "absent"
        and body.get("user_id") == user_id
        and body.get("lease_id") == lease_id
        and body.get("lease_attested") is True
        and body.get("database_authoritative") is True
    )


def _complete_pool_manifest(manifest: Mapping[str, Any]) -> bool:
    """Return whether a strict same-route account pool is ready for mutation."""
    profile_id = manifest.get("pool_profile_id")
    count = manifest.get("pool_count")
    profiles = manifest.get("profiles")
    reaper = manifest.get("synthetic_account_reaper")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("manifest_kind") != PERSONA_MEMORY_POOL_MANIFEST_KIND
        or not isinstance(profile_id, str)
        or profile_id not in PROFILE_SPECS
        or type(count) is not int
        or not 1 <= count <= MAX_PERSONA_MEMORY_POOL_COUNT
        or not isinstance(profiles, list)
        or len(profiles) != count
        or manifest.get("auxiliary_accounts") != []
        or not isinstance(reaper, Mapping)
        or reaper.get("enabled") is not True
        or reaper.get("ready") is not True
        or reaper.get("heartbeat_fresh") is not True
        or reaper.get("label_prefix") != SYNTHETIC_LABEL_PREFIX
        or type(reaper.get("max_ttl_seconds")) is not int
        or not 1 <= reaper["max_ttl_seconds"] <= MAX_SYNTHETIC_TTL_SECONDS
    ):
        return False

    spec = PROFILE_SPECS[profile_id]
    requirement = manifest.get("runtime_mode")
    if requirement not in {BASELINE_RUNTIME_REQUIREMENT, RUNTIME_V2_REQUIREMENT}:
        return False

    labels: set[str] = set()
    user_ids: set[str] = set()
    api_keys: set[str] = set()
    lease_ids: set[str] = set()
    routes: set[tuple[Any, ...]] = set()
    for index, row in enumerate(profiles, start=1):
        if not isinstance(row, Mapping):
            return False
        lease = row.get("synthetic_account_lease")
        runtime_version = row.get("runtime_version")
        if (
            row.get("profile_id") != profile_id
            or row.get("pool_index") != index
            or row.get("provider") != spec.provider
            or row.get("route_family") != spec.route_family
            or row.get("configured_base_url") != spec.expected_configured_base_url
            or row.get("reasoning_effort") != EXPECTED_REASONING_EFFORT
            or row.get("registration_verified") is not True
            or row.get("fresh_state_verified") is not True
            or row.get("invalid_key_rejected") is not True
            or row.get("valid_key_configured") is not True
            or row.get("trace_enabled") is not True
            or row.get("runtime_mode_readback_verified") is not True
            or not isinstance(row.get("runtime_mode"), str)
            or not row["runtime_mode"]
            or type(runtime_version) is not int
            or runtime_version < 1
            or row.get("provision_status") != PROVISION_STATUS_READY
            or row.get("provision_failure_code") != PROVISION_FAILURE_NONE
            or not all(
                isinstance(row.get(field), str) and bool(row.get(field))
                for field in (
                    "label",
                    "configured_model",
                    "user_id",
                    "api_key",
                    "secret_key_b64",
                    "public_key_b64",
                )
            )
            or not isinstance(lease, Mapping)
            or lease.get("registered") is not True
            or not isinstance(lease.get("lease_id"), str)
            or _SYNTHETIC_LEASE_RE.fullmatch(lease["lease_id"]) is None
            or not _synthetic_absence_attestation_valid(row)
            or lease.get("ttl_seconds") != reaper["max_ttl_seconds"]
        ):
            return False
        if requirement == RUNTIME_V2_REQUIREMENT:
            if (
                row.get("runtime_mode") != RUNTIME_V2_REQUIREMENT
                or runtime_version != RUNTIME_V2_VERSION
                or row.get("runtime_mode_set_required") is not False
                or row.get("runtime_mode_set_verified") is not False
            ):
                return False
        elif (
            row.get("runtime_mode_set_required") is not False
            or row.get("runtime_mode_set_verified") is not False
        ):
            return False

        label = str(row["label"])
        user_id = str(row["user_id"])
        api_key = str(row["api_key"])
        lease_id = str(lease["lease_id"])
        if (
            label in labels
            or user_id in user_ids
            or api_key in api_keys
            or lease_id in lease_ids
        ):
            return False
        labels.add(label)
        user_ids.add(user_id)
        api_keys.add(api_key)
        lease_ids.add(lease_id)
        routes.add(
            (
                row.get("provider"),
                row.get("configured_model"),
                row.get("configured_base_url"),
                row.get("runtime_mode"),
                runtime_version,
                row.get("reasoning_effort"),
                row.get("trace_enabled"),
            )
        )
    return len(routes) == 1


def _cleanup_safe_pool_manifest(manifest: Mapping[str, Any]) -> bool:
    """Return whether every checkpointed pool account is safe to delete.

    Unlike ``_complete_pool_manifest``, this deliberately accepts an
    all-or-nothing provisioning checkpoint that stopped before the requested
    pool size was reached.  Cleanup needs only the account credentials and the
    signed synthetic-account absence attestations; it must not strand the
    accounts that were created before provisioning failed.
    """
    profile_id = manifest.get("pool_profile_id")
    declared_count = manifest.get("pool_count")
    profiles = manifest.get("profiles")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("manifest_kind") != PERSONA_MEMORY_POOL_MANIFEST_KIND
        or not isinstance(profile_id, str)
        or profile_id not in PROFILE_SPECS
        or type(declared_count) is not int
        or not 1 <= declared_count <= MAX_PERSONA_MEMORY_POOL_COUNT
        or not isinstance(profiles, list)
        or not 1 <= len(profiles) <= declared_count
        or manifest.get("auxiliary_accounts") != []
    ):
        return False

    user_ids: set[str] = set()
    api_keys: set[str] = set()
    lease_ids: set[str] = set()
    for expected_index, row in enumerate(profiles, start=1):
        if not isinstance(row, Mapping):
            return False
        user_id = row.get("user_id")
        api_key = row.get("api_key")
        lease = row.get("synthetic_account_lease")
        if (
            row.get("profile_id") != profile_id
            or row.get("pool_index") != expected_index
            or not isinstance(user_id, str)
            or not user_id
            or not isinstance(api_key, str)
            or not api_key
            or not isinstance(lease, Mapping)
            or not _synthetic_absence_attestation_valid(row)
        ):
            return False
        lease_id = str(lease.get("lease_id") or "")
        if user_id in user_ids or api_key in api_keys or lease_id in lease_ids:
            return False
        user_ids.add(user_id)
        api_keys.add(api_key)
        lease_ids.add(lease_id)
    return True


def _reset_one(
    client: SmokeClient,
    entry: Mapping[str, Any],
    admin_client: AdminClient | None = None,
) -> bool:
    api_key = str(entry.get("api_key") or "")
    if not api_key:
        return False
    try:
        status, body = client._req(
            "POST",
            "/v1/account/reset",
            api_key=api_key,
            body={"confirm": "delete-all-data"},
        )
        if status == 200 and body.get("deleted") is True:
            return True
        # A retry after a successful-but-lost reset response returns 401 because
        # the key is revoked.  A wrong key also returns 401, so it is safe to
        # accept only after the admin lookup independently proves the synthetic
        # user no longer exists.
        if status == 401:
            return _admin_confirms_user_absent(admin_client, entry)
        return False
    except Exception:
        return False


def _request_account_reset(client: SmokeClient, entry: Mapping[str, Any]) -> bool:
    """Issue one reset request without treating the response as final evidence."""
    api_key = str(entry.get("api_key") or "")
    if not api_key:
        return False
    try:
        status, body = client._req(
            "POST",
            "/v1/account/reset",
            api_key=api_key,
            body={"confirm": "delete-all-data"},
        )
    except Exception:
        return False
    return (status == 200 and body.get("deleted") is True) or status == 401


def _old_credential_is_rejected(client: SmokeClient, api_key: str) -> bool:
    if not api_key:
        return False
    try:
        status, _body = client._req("GET", "/v1/users/whoami", api_key=api_key)
    except Exception:
        return False
    return status == 401


def _delete_provider_config_with_evidence(
    client: SmokeClient, entry: Mapping[str, Any]
) -> tuple[bool, bool, bool, bool]:
    """Delete one configured route and prove both projection and envelope absence."""
    api_key = str(entry.get("api_key") or "")
    if not api_key:
        return False, False, False, False
    valid_receipt = entry.get("valid_key_receipt")
    preexisted = (
        entry.get("valid_key_configured") is True
        and isinstance(valid_receipt, Mapping)
        and valid_receipt.get("status") == "configured"
    )
    live_predelete_observed = False
    for attempt in range(3):
        try:
            before_status, before = client._req(
                "GET", "/v1/model_api/get", api_key=api_key
            )
            before_config = (
                before.get("config") if isinstance(before, Mapping) else None
            )
            if before_status == 401:
                return preexisted, False, False, False
            if (
                before_status == 200
                and isinstance(before_config, Mapping)
                and before_config.get("configured") is True
            ):
                live_predelete_observed = True
            delete_status, _deleted = client._req(
                "DELETE", "/v1/model_api/delete", api_key=api_key
            )
            post_status, after = client._req(
                "GET", "/v1/model_api/get", api_key=api_key
            )
            post_config = after.get("config") if isinstance(after, Mapping) else None
            projection_absent = (
                post_status == 200
                and isinstance(post_config, Mapping)
                and post_config.get("configured") is False
            )
            envelope_status, _envelope = client._req(
                "GET", "/v1/model_api/key_envelope", api_key=api_key
            )
            envelope_absent = envelope_status == 404
            if delete_status == 200 and projection_absent and envelope_absent:
                return preexisted, live_predelete_observed, True, True
        except Exception:
            pass
        if attempt < 2:
            time.sleep(float(attempt + 1))
    return preexisted, live_predelete_observed, False, False


def _cleanup_evidence_rows(
    client: SmokeClient,
    admin_client: AdminClient | None,
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reset all accounts, then prove deletion after one shared retry window.

    The public rows contain only locked profile IDs, optional pool indexes,
    one-way user fingerprints, and booleans. Account IDs, account keys, and
    response bodies remain confined to the private manifest.
    """
    work: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for entry in entries:
        profile_id = str(entry.get("profile_id") or "unknown")
        if profile_id == MEMORY_CONTRACT_PROFILE_ID:
            config_preexisted = False
            config_deleted = True
            envelope_deleted = True
            config_deletion_source = "not_applicable"
        else:
            (
                config_preexisted,
                config_live_predelete_observed,
                config_deleted,
                envelope_deleted,
            ) = _delete_provider_config_with_evidence(client, entry)
            config_deletion_source = "explicit_api" if config_deleted else "unknown"
        if profile_id == MEMORY_CONTRACT_PROFILE_ID:
            config_live_predelete_observed = False
        accepted = _request_account_reset(client, entry)
        row = {
            "profile_id": profile_id,
            "attempted": True,
            "reset_response_accepted": accepted,
            "provider_config_preexisted": config_preexisted,
            "provider_config_live_predelete_observed": (config_live_predelete_observed),
            "provider_config_deleted": config_deleted,
            "key_envelope_deleted": envelope_deleted,
            "provider_config_deletion_source": config_deletion_source,
            "account_reset": False,
            "old_credential_rejected": False,
            "user_absence_verified": False,
            "status": "FAIL",
        }
        pool_index = entry.get("pool_index")
        user_id = str(entry.get("user_id") or "")
        if type(pool_index) is int and pool_index > 0 and user_id:
            row["pool_index"] = pool_index
            row["account_fingerprint"] = hashlib.sha256(
                user_id.encode("utf-8")
            ).hexdigest()
        work.append((entry, row))

    for attempt in range(CLEANUP_EVIDENCE_ATTEMPTS):
        pending = False
        for entry, row in work:
            if row["status"] == "PASS":
                continue
            if not row["reset_response_accepted"]:
                row["reset_response_accepted"] = _request_account_reset(client, entry)
                if not row["reset_response_accepted"]:
                    pending = True
                    continue
            api_key = str(entry.get("api_key") or "")
            user_absent = _admin_confirms_user_absent(admin_client, entry)
            credential_rejected = _old_credential_is_rejected(client, api_key)
            row["user_absence_verified"] = user_absent
            row["old_credential_rejected"] = credential_rejected
            row["account_reset"] = user_absent
            if (
                user_absent
                and row["profile_id"] != MEMORY_CONTRACT_PROFILE_ID
                and row["provider_config_deleted"] is not True
            ):
                # Account deletion is an authoritative FK-cascade boundary for
                # provider credentials/routes. This also verifies runs where a
                # profile worker reset first and the parent sees only 401.
                row["provider_config_deleted"] = True
                row["key_envelope_deleted"] = True
                row["provider_config_deletion_source"] = "account_cascade"
            provider_proof_valid = (
                row["provider_config_deleted"] is True
                and row["key_envelope_deleted"] is True
            )
            if user_absent and credential_rejected and provider_proof_valid:
                row["status"] = "PASS"
            else:
                pending = True
        if not pending:
            break
        if attempt + 1 < CLEANUP_EVIDENCE_ATTEMPTS:
            time.sleep(CLEANUP_EVIDENCE_DELAY_SECONDS)
    return [row for _entry, row in work]


def _validate_receipted_cleanup_manifest(
    profiles: Sequence[Mapping[str, Any]],
    auxiliary: Sequence[Mapping[str, Any]],
) -> None:
    profile_ids = [str(row.get("profile_id") or "") for row in profiles]
    auxiliary_ids = [str(row.get("profile_id") or "") for row in auxiliary]
    if (
        any(profile_id not in PROFILE_SPECS for profile_id in profile_ids)
        or len(set(profile_ids)) != len(profile_ids)
        or profile_ids
        != [profile_id for profile_id in PROFILE_SPECS if profile_id in profile_ids]
    ):
        raise ProvisionError("cleanup receipt profile rows are invalid")
    if auxiliary_ids not in ([], [MEMORY_CONTRACT_PROFILE_ID]):
        raise ProvisionError("cleanup receipt auxiliary rows are invalid")
    entries = [*profiles, *auxiliary]
    if any(
        not isinstance(row, Mapping) or not _synthetic_absence_attestation_valid(row)
        for row in entries
    ):
        raise ProvisionError("cleanup receipt requires synthetic absence attestations")
    user_ids = [str(row.get("user_id") or "") for row in entries]
    api_keys = [str(row.get("api_key") or "") for row in entries]
    if (
        not all(user_ids)
        or len(set(user_ids)) != len(user_ids)
        or not all(api_keys)
        or len(set(api_keys)) != len(api_keys)
    ):
        raise ProvisionError("cleanup receipt requires unique account credentials")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _owner_regular_file(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
    )


def _manifest_read_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_cleanup_manifest_snapshot(
    manifest_path: Path,
) -> tuple[dict[str, Any], tuple[int, int]] | None:
    """Read one owner-held regular file without following its final symlink."""
    try:
        before = os.lstat(manifest_path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ProvisionError("private manifest is unreadable") from None
    if not _owner_regular_file(before):
        raise ProvisionError("private manifest must be an owner-owned regular file")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    descriptor = -1
    try:
        descriptor = os.open(manifest_path, flags)
        opened = os.fstat(descriptor)
        if (
            not _owner_regular_file(opened)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > MAX_PRIVATE_MANIFEST_BYTES
        ):
            raise ProvisionError("private manifest is unreadable")

        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, MAX_PRIVATE_MANIFEST_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_PRIVATE_MANIFEST_BYTES:
                raise ProvisionError("private manifest is unreadable")

        after = os.fstat(descriptor)
        current = os.lstat(manifest_path)
        if (
            _manifest_read_signature(after) != _manifest_read_signature(opened)
            or not _owner_regular_file(current)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ProvisionError("private manifest changed while reading")
        try:
            document = json.loads(
                b"".join(chunks).decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise ProvisionError("private manifest is unreadable") from None
        if not isinstance(document, dict):
            raise ProvisionError("private manifest is unreadable")
        return document, (opened.st_dev, opened.st_ino)
    except ProvisionError:
        raise
    except OSError:
        raise ProvisionError("private manifest is unreadable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def unlink_manifest_snapshot(
    manifest_path: Path, manifest_identity: tuple[int, int]
) -> str | None:
    """Unlink only when the path still names the verified owner-held inode."""
    try:
        candidate = os.lstat(manifest_path)
    except FileNotFoundError:
        return "manifest_path_missing"
    except OSError:
        return "manifest_path_unreadable"
    if not _owner_regular_file(candidate):
        return "manifest_path_not_owner_regular"
    if (candidate.st_dev, candidate.st_ino) != manifest_identity:
        return "manifest_path_identity_changed"

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = -1
    try:
        descriptor = os.open(manifest_path, flags)
        opened = os.fstat(descriptor)
        current = os.lstat(manifest_path)
        if (
            not _owner_regular_file(opened)
            or not _owner_regular_file(current)
            or (opened.st_dev, opened.st_ino) != manifest_identity
            or (current.st_dev, current.st_ino) != manifest_identity
        ):
            return "manifest_path_identity_changed"
        os.unlink(manifest_path)
        return None
    except FileNotFoundError:
        return "manifest_path_missing"
    except OSError:
        return "manifest_unlink_failed"
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _cleanup_pool_manifest_snapshot(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    manifest_identity: tuple[int, int],
    *,
    client: SmokeClient,
    admin_client: AdminClient | None,
    retain_manifest: bool,
    delete_manifest: bool,
) -> dict[str, Any]:
    """Delete every checkpointed pool account and return sanitized evidence."""
    if not _cleanup_safe_pool_manifest(manifest):
        raise ProvisionError("persona-memory pool cleanup manifest is invalid")
    profiles = manifest["profiles"]
    evidence = _cleanup_evidence_rows(client, admin_client, profiles)
    evidence.sort(key=lambda row: int(row.get("pool_index") or 0))
    failed_rows = [row for row in evidence if row.get("status") != "PASS"]
    failed = [
        f"{row['profile_id']}#{int(row['pool_index']):03d}"
        for row in failed_rows
    ]
    cleaned = len(evidence) - len(failed_rows)

    deleted = False
    delete_failure: str | None = None
    if not failed and delete_manifest and not retain_manifest:
        delete_failure = unlink_manifest_snapshot(manifest_path, manifest_identity)
        deleted = delete_failure is None
        if delete_failure is not None:
            failed.append(MANIFEST_CLEANUP_FAILURE_ID)

    result: dict[str, Any] = {
        "attempted": len(evidence),
        "cleaned": cleaned,
        "failed_profile_ids": failed,
        "manifest_deleted": deleted,
        "manifest_missing": False,
        "cleanup_accounts": evidence,
    }
    if (not delete_manifest or retain_manifest) and not failed:
        result["manifest_retained"] = True
    if delete_failure is not None:
        result["manifest_delete_failure"] = delete_failure
    return result


def cleanup_manifest_snapshot(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    manifest_identity: tuple[int, int],
    *,
    env: Mapping[str, str] | None = None,
    client: SmokeClient | None = None,
    admin_client: AdminClient | None = None,
    receipt_path: Path | None = None,
    run_id: str | None = None,
    retain_manifest: bool = False,
    delete_manifest: bool = True,
) -> dict[str, Any]:
    """Reset accounts from a verified snapshot without rereading its contents.

    ``manifest_identity`` must be the ``(st_dev, st_ino)`` captured when the
    mapping was read. A successful account reset never authorizes deletion of
    a different object subsequently installed at ``manifest_path``.
    """
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(manifest_identity, tuple)
        or len(manifest_identity) != 2
        or any(type(value) is not int or value < 0 for value in manifest_identity)
    ):
        raise ProvisionError("private manifest snapshot identity is invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ProvisionError("private manifest schema version is unsupported")
    base_url = validate_base_url(str(manifest.get("base_url") or ""))
    profiles = manifest.get("profiles")
    auxiliary = manifest.get("auxiliary_accounts", [])
    if not isinstance(profiles, list) or not isinstance(auxiliary, list):
        raise ProvisionError("private manifest has no profiles array")
    entries = [
        dict(raw) if isinstance(raw, Mapping) else {}
        for raw in [*profiles, *auxiliary]
    ]
    active_client = client or SmokeClient(base_url)
    active_env = os.environ if env is None else env
    verification_admin = admin_client
    if verification_admin is None:
        admin_token = str(active_env.get("IO_E2E_ADMIN_TOKEN") or "").strip()
        if admin_token:
            verification_admin = AdminClient(
                base_url,
                admin_token,
                getattr(active_client, "_ssl", None),
            )

    if manifest.get("manifest_kind") == PERSONA_MEMORY_POOL_MANIFEST_KIND:
        if receipt_path is not None:
            raise ProvisionError(
                "persona-memory pool receipt must be finalized by its cleanup command"
            )
        return _cleanup_pool_manifest_snapshot(
            manifest,
            manifest_path,
            manifest_identity,
            client=active_client,
            admin_client=verification_admin,
            retain_manifest=retain_manifest,
            delete_manifest=delete_manifest,
        )

    if receipt_path is not None:
        if not retain_manifest:
            raise ProvisionError("cleanup receipt requires retained manifest scanning")
        safe_run_id = str(run_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", safe_run_id):
            raise ProvisionError("cleanup receipt run ID is invalid")
        _validate_receipted_cleanup_manifest(profiles, auxiliary)
        evidence = _cleanup_evidence_rows(
            active_client,
            verification_admin,
            [row for row in entries if isinstance(row, Mapping)],
        )
        profile_rows = evidence[: len(profiles)]
        auxiliary_rows = evidence[len(profiles) :]
        failed = [
            str(row["profile_id"])
            for row in evidence
            if row.get("status") != "PASS"
        ]
        cleaned = len(evidence) - len(failed)
        receipt = {
            "schema_version": 1,
            "kind": "deterministic_cleanup_receipt",
            "run_id": safe_run_id,
            "generated_at": _utc_now(),
            "attempted": len(evidence),
            "cleaned": cleaned,
            "failed_profile_ids": failed,
            "manifest_deleted": False,
            "manifest_retained_for_scan": True,
            "profiles": profile_rows,
            "auxiliary_accounts": auxiliary_rows,
        }
        _atomic_write_manifest(receipt_path, receipt)
        return {
            "attempted": receipt["attempted"],
            "cleaned": cleaned,
            "failed_profile_ids": failed,
            "manifest_deleted": False,
            "manifest_missing": False,
            "receipt_written": True,
        }

    cleaned = 0
    seen_users: set[str] = set()
    pending: list[tuple[str, Mapping[str, Any]]] = []
    for entry in entries:
        profile_id = str(entry.get("profile_id") or "unknown")
        user_id = str(entry.get("user_id") or "")
        if user_id and user_id in seen_users:
            continue
        if user_id:
            seen_users.add(user_id)
        pending.append((profile_id, entry))

    # A deployment rollover can transiently reject every reset in one round.
    # Retry only accounts that have not already produced authoritative success;
    # this keeps cleanup bounded and avoids issuing duplicate destructive calls.
    attempts = max(1, int(CLEANUP_EVIDENCE_ATTEMPTS))
    for attempt in range(attempts):
        still_pending: list[tuple[str, Mapping[str, Any]]] = []
        for profile_id, entry in pending:
            if _reset_one(active_client, entry, verification_admin):
                cleaned += 1
            else:
                still_pending.append((profile_id, entry))
        pending = still_pending
        if not pending:
            break
        if attempt + 1 < attempts:
            time.sleep(CLEANUP_EVIDENCE_DELAY_SECONDS)

    failed = [profile_id for profile_id, _entry in pending]

    deleted = False
    delete_failure: str | None = None
    if not failed and delete_manifest and not retain_manifest:
        delete_failure = unlink_manifest_snapshot(manifest_path, manifest_identity)
        deleted = delete_failure is None
        if delete_failure is not None:
            failed.append(MANIFEST_CLEANUP_FAILURE_ID)
    result = {
        "attempted": len(seen_users) if seen_users else len(entries),
        "cleaned": cleaned,
        "failed_profile_ids": failed,
        "manifest_deleted": deleted,
        "manifest_missing": False,
    }
    if (not delete_manifest or retain_manifest) and not failed:
        result["manifest_retained"] = True
    if delete_failure is not None:
        result["manifest_delete_failure"] = delete_failure
    return result


def cleanup(
    manifest_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    client: SmokeClient | None = None,
    admin_client: AdminClient | None = None,
    receipt_path: Path | None = None,
    run_id: str | None = None,
    retain_manifest: bool = False,
    delete_manifest: bool = True,
) -> dict[str, Any]:
    """Securely load a private manifest, then clean its verified snapshot."""
    snapshot = _read_cleanup_manifest_snapshot(manifest_path)
    if snapshot is None:
        return {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        }
    manifest, manifest_identity = snapshot
    return cleanup_manifest_snapshot(
        manifest,
        manifest_path=manifest_path,
        manifest_identity=manifest_identity,
        env=env,
        client=client,
        admin_client=admin_client,
        receipt_path=receipt_path,
        run_id=run_id,
        retain_manifest=retain_manifest,
        delete_manifest=delete_manifest,
    )


def cleanup_run(
    run_id: str,
    receipt_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    admin_client: AdminClient | None = None,
) -> dict[str, Any]:
    """Authoritatively clean one run without reading an account manifest."""
    normalized_run_id = normalize_synthetic_run_id(run_id)
    if normalized_run_id != run_id and len(str(run_id)) <= MAX_SYNTHETIC_RUN_ID_LENGTH:
        raise ProvisionError("synthetic cleanup run ID must already be normalized")
    active_env = os.environ if env is None else env
    base_url = validate_base_url(_required_env(active_env, "IO_E2E_BASE_URL"))
    admin_token = _required_env(active_env, "IO_E2E_ADMIN_TOKEN")
    receipt_path = _validate_new_private_manifest_path(receipt_path)
    active_admin = admin_client or AdminClient(base_url, admin_token)
    receipt = active_admin.cleanup_synthetic_run(normalized_run_id)
    _atomic_write_manifest(receipt_path, receipt)
    return receipt


def provision(
    coverage_path: Path,
    manifest_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    client: SmokeClient | None = None,
    admin_client: AdminClient | None = None,
    diagnostic: bool = False,
    profile_ids: Sequence[str] | None = None,
    runtime_requirement: str | None = None,
) -> dict[str, Any]:
    """Create the locked matrix, isolating operational failures by profile."""
    active_env = os.environ if env is None else env
    requirement = runtime_requirement or (
        BASELINE_RUNTIME_REQUIREMENT if diagnostic else RUNTIME_V2_REQUIREMENT
    )
    if requirement not in {BASELINE_RUNTIME_REQUIREMENT, RUNTIME_V2_REQUIREMENT}:
        raise ProvisionError("runtime requirement is invalid")
    base_url = validate_base_url(_required_env(active_env, "IO_E2E_BASE_URL"))
    admin_token = "" if diagnostic else _required_env(active_env, "IO_E2E_ADMIN_TOKEN")
    profiles = _select_profiles(
        _load_coverage(coverage_path),
        diagnostic=diagnostic,
        profile_ids=profile_ids,
    )
    # Validate every static input before the reaper preflight or registration.
    # A missing credential is a broken run contract; an expired credential is a
    # per-profile diagnostic discovered later by the valid-key probe.
    models = {
        str(profile["profile_id"]): _model_for(
            profile, PROFILE_SPECS[str(profile["profile_id"])], active_env
        )
        for profile in profiles
    }
    provider_keys = {
        str(profile["profile_id"]): _required_env(
            active_env, PROFILE_SPECS[str(profile["profile_id"])].credential_env
        )
        for profile in profiles
    }
    provider_base_urls = {
        str(profile["profile_id"]): _provider_base_url_for(
            profile, PROFILE_SPECS[str(profile["profile_id"])], active_env
        )
        for profile in profiles
    }
    reasoning_efforts = {
        str(profile["profile_id"]): _reasoning_effort_for(profile)
        for profile in profiles
    }
    active_client = client or SmokeClient(base_url)
    active_admin: AdminClient | None
    if diagnostic:
        active_admin = None
        reaper_receipt = {
            "required": False,
            "verified": False,
            "reason": "adminless_diagnostic",
        }
    else:
        active_admin = admin_client or AdminClient(
            base_url, admin_token, active_client._ssl
        )
        reaper_receipt = _verify_synthetic_reaper(active_admin)
    run_id = normalize_synthetic_run_id(active_env.get("QA_RUN_ID") or "local")
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "base_url": base_url,
        "runtime_mode": requirement,
        "synthetic_account_reaper": reaper_receipt,
        "profiles": [],
        "auxiliary_accounts": [],
    }
    if diagnostic:
        manifest.update(
            {
                "qualification_mode": QUALIFICATION_MODE_DIAGNOSTIC,
                "runtime_requirement": requirement,
                "selected_profile_ids": [
                    str(profile["profile_id"]) for profile in profiles
                ],
            }
        )

    try:
        for profile in profiles:
            profile_id = str(profile["profile_id"])
            spec = PROFILE_SPECS[profile_id]
            provider_key = provider_keys[profile_id]
            model = models[profile_id]
            provider_base_url = provider_base_urls[profile_id]
            expected_configured_base_url = spec.expected_configured_base_url
            reasoning_effort = reasoning_efforts[profile_id]
            label = f"{SYNTHETIC_LABEL_PREFIX}{run_id}-{profile_id}"

            try:
                if diagnostic:
                    session = active_client.register(label)
                    synthetic_lease = None
                else:
                    if active_admin is None:  # pragma: no cover - defensive guard
                        raise ProvisionError(
                            "strict provisioning requires admin client"
                        )
                    session, synthetic_lease = active_admin.register_synthetic(
                        label,
                        run_id=run_id,
                        ttl_seconds=int(reaper_receipt["max_ttl_seconds"]),
                    )
            except Exception:
                raise ProvisionError(
                    f"account registration failed for profile: {profile_id}"
                ) from None
            try:
                entry = _manifest_entry(
                    profile,
                    spec,
                    model,
                    expected_configured_base_url,
                    reasoning_effort,
                    session,
                    label,
                    diagnostic=diagnostic,
                    runtime_requirement=requirement,
                    synthetic_lease=synthetic_lease,
                )
            except Exception:
                _reset_one(
                    active_client,
                    {
                        "profile_id": profile_id,
                        "user_id": str(getattr(session, "user_id", "")),
                        "api_key": str(getattr(session, "api_key", "")),
                    },
                    active_admin,
                )
                raise ProvisionError(
                    f"account registration failed for profile: {profile_id}"
                ) from None
            manifest["profiles"].append(entry)
            _atomic_write_manifest(manifest_path, manifest)

            try:
                _check_fresh_account(active_client, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                _check_invalid_key(
                    active_client,
                    session,
                    spec,
                    model,
                    provider_base_url,
                    reasoning_effort,
                    entry,
                )
                _atomic_write_manifest(manifest_path, manifest)
                _configure_valid_key(
                    active_client,
                    session,
                    spec,
                    model,
                    provider_base_url,
                    expected_configured_base_url,
                    reasoning_effort,
                    provider_key,
                    entry,
                )
                _atomic_write_manifest(manifest_path, manifest)
                _enable_trace(active_client, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                _verify_diagnostic_runtime(
                    active_client,
                    session,
                    entry,
                    runtime_requirement=requirement,
                )
                entry["provision_status"] = PROVISION_STATUS_READY
                entry["provision_failure_code"] = PROVISION_FAILURE_NONE
                _atomic_write_manifest(manifest_path, manifest)
            except _ProfileProvisionFailure as failure:
                entry["provision_status"] = PROVISION_STATUS_BLOCKED
                entry["provision_failure_code"] = failure.code
                _atomic_write_manifest(manifest_path, manifest)
                continue
        if not diagnostic:
            if active_admin is None:  # pragma: no cover - defensive guard
                raise ProvisionError("strict provisioning requires admin client")
            memory_label = (
                f"{SYNTHETIC_LABEL_PREFIX}{run_id}-{MEMORY_CONTRACT_PROFILE_ID}"
            )
            try:
                memory_session, memory_lease = active_admin.register_synthetic(
                    memory_label,
                    run_id=run_id,
                    ttl_seconds=int(reaper_receipt["max_ttl_seconds"]),
                )
            except Exception:
                raise ProvisionError(
                    "memory contract account registration failed"
                ) from None
            memory_entry = _memory_contract_entry(
                memory_session, memory_label, memory_lease
            )
            manifest["auxiliary_accounts"].append(memory_entry)
            try:
                _atomic_write_manifest(manifest_path, manifest)
            except Exception:
                _reset_one(active_client, memory_entry, active_admin)
                raise
        if not _complete_diagnostic_manifest(manifest):
            raise ProvisionError(
                "provisioning did not produce a complete diagnostic manifest"
            )
    except Exception:
        # Best effort prevents failed setup attempts from accumulating accounts.
        # If any reset fails, cleanup deliberately leaves the 0600 manifest so
        # the workflow's `if: always()` cleanup step can retry.
        try:
            result = cleanup(
                manifest_path,
                env=active_env,
                client=active_client,
                admin_client=active_admin,
            )
            if result["manifest_missing"]:
                for entry in [
                    *manifest["profiles"],
                    *manifest["auxiliary_accounts"],
                ]:
                    _reset_one(active_client, entry, active_admin)
        except Exception:
            # The in-memory checkpoint remains sufficient to attempt cleanup
            # even if the on-disk manifest itself became unreadable.
            for entry in [
                *manifest["profiles"],
                *manifest["auxiliary_accounts"],
            ]:
                _reset_one(active_client, entry, active_admin)
        raise

    return manifest


def provision_pool(
    coverage_path: Path,
    manifest_path: Path,
    *,
    profile_id: str,
    count: int,
    env: Mapping[str, str] | None = None,
    client: SmokeClient | None = None,
    admin_client: AdminClient | None = None,
    runtime_requirement: str | None = None,
) -> dict[str, Any]:
    """Create an all-or-nothing strict pool on one locked provider route."""
    if type(count) is not int or not 1 <= count <= MAX_PERSONA_MEMORY_POOL_COUNT:
        raise ProvisionError(
            f"pool count must be between 1 and {MAX_PERSONA_MEMORY_POOL_COUNT}"
        )
    selected_profile_id = str(profile_id or "").strip()
    if selected_profile_id not in PROFILE_SPECS:
        raise ProvisionError("pool profile is outside the locked API-key matrix")
    requirement = runtime_requirement or RUNTIME_V2_REQUIREMENT
    if requirement not in {BASELINE_RUNTIME_REQUIREMENT, RUNTIME_V2_REQUIREMENT}:
        raise ProvisionError("runtime requirement is invalid")
    manifest_path = _validate_new_private_manifest_path(manifest_path)

    profiles = _load_coverage(coverage_path)
    profile = next(
        row
        for row in profiles
        if str(row.get("profile_id") or "") == selected_profile_id
    )
    spec = PROFILE_SPECS[selected_profile_id]
    active_env = os.environ if env is None else env
    base_url = validate_base_url(_required_env(active_env, "IO_E2E_BASE_URL"))
    admin_token = _required_env(active_env, "IO_E2E_ADMIN_TOKEN")

    # Validate every selected-route input before checking the reaper or creating
    # external state. Unrelated provider credentials are deliberately not read.
    model = _model_for(profile, spec, active_env)
    provider_key = _required_env(active_env, spec.credential_env)
    provider_base_url = _provider_base_url_for(profile, spec, active_env)
    reasoning_effort = _reasoning_effort_for(profile)
    active_client = client or SmokeClient(base_url)
    active_admin = admin_client or AdminClient(
        base_url, admin_token, active_client._ssl
    )
    reaper_receipt = _verify_synthetic_reaper(active_admin)
    run_id = normalize_synthetic_run_id(active_env.get("QA_RUN_ID") or "local")
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_kind": PERSONA_MEMORY_POOL_MANIFEST_KIND,
        "generated_at": _utc_now(),
        "base_url": base_url,
        "runtime_mode": requirement,
        "pool_profile_id": selected_profile_id,
        "pool_count": count,
        "synthetic_account_reaper": reaper_receipt,
        "profiles": [],
        "auxiliary_accounts": [],
    }
    persisted_profile_count = 0

    try:
        for pool_index in range(1, count + 1):
            label = (
                f"{SYNTHETIC_LABEL_PREFIX}{run_id}-{selected_profile_id}-"
                f"{pool_index:03d}"
            )
            try:
                session, synthetic_lease = active_admin.register_synthetic(
                    label,
                    run_id=run_id,
                    ttl_seconds=int(reaper_receipt["max_ttl_seconds"]),
                )
            except Exception:
                raise ProvisionError(
                    f"pool account registration failed at index: {pool_index}"
                ) from None
            try:
                entry = _manifest_entry(
                    profile,
                    spec,
                    model,
                    spec.expected_configured_base_url,
                    reasoning_effort,
                    session,
                    label,
                    runtime_requirement=requirement,
                    synthetic_lease=synthetic_lease,
                )
                entry["pool_index"] = pool_index
            except Exception:
                _reset_one(
                    active_client,
                    {
                        "profile_id": selected_profile_id,
                        "user_id": str(getattr(session, "user_id", "")),
                        "api_key": str(getattr(session, "api_key", "")),
                    },
                    active_admin,
                )
                raise ProvisionError(
                    f"pool account registration failed at index: {pool_index}"
                ) from None
            manifest["profiles"].append(entry)
            _atomic_write_manifest(manifest_path, manifest)
            persisted_profile_count = len(manifest["profiles"])

            try:
                _check_fresh_account(active_client, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                _check_invalid_key(
                    active_client,
                    session,
                    spec,
                    model,
                    provider_base_url,
                    reasoning_effort,
                    entry,
                )
                _atomic_write_manifest(manifest_path, manifest)
                _configure_valid_key(
                    active_client,
                    session,
                    spec,
                    model,
                    provider_base_url,
                    spec.expected_configured_base_url,
                    reasoning_effort,
                    provider_key,
                    entry,
                )
                _atomic_write_manifest(manifest_path, manifest)
                _enable_trace(active_client, session, entry)
                _atomic_write_manifest(manifest_path, manifest)
                # Runtime V2 auto-migrates configured model-API users.  Pools
                # qualify the same production user path as the locked matrix;
                # they must never rely on an admin-only mode mutation.
                _verify_diagnostic_runtime(
                    active_client,
                    session,
                    entry,
                    runtime_requirement=requirement,
                )
                entry["provision_status"] = PROVISION_STATUS_READY
                entry["provision_failure_code"] = PROVISION_FAILURE_NONE
                _atomic_write_manifest(manifest_path, manifest)
            except _ProfileProvisionFailure as failure:
                entry["provision_status"] = PROVISION_STATUS_BLOCKED
                entry["provision_failure_code"] = failure.code
                _atomic_write_manifest(manifest_path, manifest)
                raise ProvisionError(
                    f"pool account provisioning blocked: {failure.code}"
                ) from None

        if not _complete_pool_manifest(manifest):
            raise ProvisionError("provisioning did not produce a complete account pool")
    except Exception:
        # The aggregate manifest is the cleanup authority. A lost response from
        # the non-idempotent registration endpoint remains covered by the
        # server-signed expiry lease even when no credentials reached this process.
        recovery_entries = list(manifest["profiles"][persisted_profile_count:])
        try:
            result = cleanup(
                manifest_path,
                env=active_env,
                client=active_client,
                admin_client=active_admin,
            )
            if result["manifest_missing"]:
                recovery_entries = list(manifest["profiles"])
        except Exception:
            recovery_entries = list(manifest["profiles"])
        unrecovered = [
            entry
            for entry in recovery_entries
            if not _reset_one(active_client, entry, active_admin)
        ]
        if unrecovered:
            # Preserve every credential we know, including an account whose
            # first checkpoint failed, so `cleanup --manifest` can retry. The
            # server-side signed lease remains the final fallback if storage is
            # unavailable and this recovery checkpoint also fails.
            try:
                _atomic_write_manifest(manifest_path, manifest)
            except Exception:
                pass
        raise

    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("provision", help="create the locked API-key profiles")
    create.add_argument("--coverage", type=Path, default=Path("qa/coverage-lock.json"))
    create.add_argument("--manifest", type=Path, required=True)
    create.add_argument(
        "--diagnostic",
        action="store_true",
        help="use user-scoped runtime readback without admin-only contracts",
    )
    create.add_argument(
        "--profile",
        action="append",
        dest="profile_ids",
        help="locked profile id to provision (repeatable; diagnostic mode only)",
    )
    create.add_argument(
        "--require-runtime-v2",
        action="store_true",
        help="require hosted_resident runtime version 2 instead of baseline readback",
    )
    create.add_argument(
        "--baseline-runtime",
        action="store_true",
        help="qualify the currently deployed runtime without selecting Runtime V2",
    )
    pool = commands.add_parser(
        "provision-pool",
        help="create a strict same-route persona-memory account pool",
    )
    pool.add_argument("--coverage", type=Path, default=Path("qa/coverage-lock.json"))
    pool.add_argument("--manifest", type=Path, required=True)
    pool.add_argument("--profile", required=True)
    pool.add_argument("--count", type=int, required=True)
    pool.add_argument(
        "--require-runtime-v2",
        action="store_true",
        help="require hosted_resident runtime version 2 (the default)",
    )
    pool.add_argument(
        "--baseline-runtime",
        action="store_true",
        help="qualify the currently deployed runtime without selecting Runtime V2",
    )
    remove = commands.add_parser(
        "cleanup", help="reset all accounts in a private manifest"
    )
    remove.add_argument("--manifest", type=Path, required=True)
    remove.add_argument("--receipt", type=Path)
    remove.add_argument("--run-id")
    remove.add_argument("--retain-manifest", action="store_true")
    remove_run = commands.add_parser(
        "cleanup-run",
        help="authoritatively reset signed accounts for one normalized run ID",
    )
    remove_run.add_argument("--run-id", required=True)
    remove_run.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"provision", "provision-pool"}:
            if args.require_runtime_v2 and args.baseline_runtime:
                raise ProvisionError("runtime requirement flags are mutually exclusive")
            requirement = (
                BASELINE_RUNTIME_REQUIREMENT
                if args.baseline_runtime
                or (
                    args.command == "provision"
                    and args.diagnostic
                    and not args.require_runtime_v2
                )
                else RUNTIME_V2_REQUIREMENT
            )
            if args.command == "provision-pool":
                result = provision_pool(
                    args.coverage,
                    args.manifest,
                    profile_id=args.profile,
                    count=args.count,
                    runtime_requirement=requirement,
                )
                if not _complete_pool_manifest(result):
                    raise ProvisionError(
                        "provisioning did not produce a complete account pool"
                    )
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "manifest_kind": PERSONA_MEMORY_POOL_MANIFEST_KIND,
                            "pool_profile_id": result["pool_profile_id"],
                            "pool_count": result["pool_count"],
                            "manifest": str(args.manifest),
                        }
                    )
                )
                return 0
            if args.diagnostic:
                result = provision(
                    args.coverage,
                    args.manifest,
                    diagnostic=True,
                    profile_ids=args.profile_ids,
                    runtime_requirement=requirement,
                )
            else:
                if args.profile_ids is not None:
                    raise ProvisionError("profile subsets require diagnostic mode")
                result = provision(
                    args.coverage,
                    args.manifest,
                    runtime_requirement=requirement,
                )
            if not _complete_diagnostic_manifest(result):
                raise ProvisionError(
                    "provisioning did not produce a complete diagnostic manifest"
                )
            ready = [
                row["profile_id"]
                for row in result["profiles"]
                if row["provision_status"] == PROVISION_STATUS_READY
            ]
            blocked = [
                row["profile_id"]
                for row in result["profiles"]
                if row["provision_status"] == PROVISION_STATUS_BLOCKED
            ]
            summary = {
                "ok": True,
                "profile_count": len(result["profiles"]),
                "ready_profile_count": len(ready),
                "blocked_profile_count": len(blocked),
                "blocked_profile_ids": blocked,
                "manifest": str(args.manifest),
            }
            if args.diagnostic:
                summary["qualification_mode"] = QUALIFICATION_MODE_DIAGNOSTIC
            print(json.dumps(summary))
            return 0
        if args.command == "cleanup-run":
            result = cleanup_run(args.run_id, args.receipt)
            ok = bool(
                result["complete"]
                and result["operation_failure_count"] == 0
                and result["remaining_count"] == 0
            )
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "matched_count": result["matched_count"],
                        "deleted_count": result["deleted_count"],
                        "operation_failure_count": result[
                            "operation_failure_count"
                        ],
                        "remaining_count": result["remaining_count"],
                        "receipt": str(args.receipt),
                    }
                )
            )
            return 0 if ok else 1
        result = cleanup(
            args.manifest,
            receipt_path=args.receipt,
            run_id=args.run_id,
            retain_manifest=args.retain_manifest,
        )
        print(
            json.dumps(
                {
                    "ok": not result["failed_profile_ids"],
                    "attempted": result["attempted"],
                    "cleaned": result["cleaned"],
                    "failed_profile_ids": result["failed_profile_ids"],
                    "manifest_deleted": result["manifest_deleted"],
                }
            )
        )
        return 0 if not result["failed_profile_ids"] else 1
    except ProvisionError as exc:
        print(f"provisioning error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("provisioning error: internal failure", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
