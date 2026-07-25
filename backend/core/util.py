"""Small dependency-free helpers shared across domains."""

import json
import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ENV_TRUTHY = {"1", "true", "yes", "y", "on"}


def _env_flag_enabled(name: str, default: str = "false") -> bool:
    return str(os.environ.get(name, default) or "").strip().lower() in _ENV_TRUTHY


RUNTIME_V2_DEFAULT_ON_ENV = "FEEDLING_RUNTIME_V2_DEFAULT_ON"


def runtime_v2_default_on() -> bool:
    """Baseline default for the perception/resident V2 rollout flags.

    ⚠️ READ THIS BEFORE ASSUMING "prod is off": ALL THREE main composes
    (``deploy/docker-compose.phala{,.test,.pre}.yaml``) inject
    ``FEEDLING_RUNTIME_V2_DEFAULT_ON=true``, so prod/test/pre are all ON. Only a
    process without the env (local runs, plain pytest) is OFF. The old wording
    here ("OFF by default so prod keeps the dormant legacy path") described an
    intent that deployment never matched, and believing it is what made PR #107's
    "perception ingress follows the chat fence" change look like a no-op — it
    silently moved every prod user off the decrypting ingress (2026-07-24
    null-perception incident). Pinned by
    ``tests/test_hosted_runtime_policy.py::test_every_main_compose_turns_the_runtime_v2_baseline_on``.

    An explicit per-user blob value still overrides this baseline (operator
    opt-in/opt-out wins).
    """
    return _env_flag_enabled(RUNTIME_V2_DEFAULT_ON_ENV)


# io-onboarding docs branch this code serves skill_url from. Defaults to "main"
# so a merge to main just works with NO code edit (the old hard-coded constant
# had to be hand-flipped every cutover — that footgun is gone). The per-deploy
# difference now lives in env: the test deploy (which serves test-api) injects
# FEEDLING_IO_ONBOARDING_BRANCH=test to point skill docs at the test branch.
IO_ONBOARDING_BRANCH_ENV = "FEEDLING_IO_ONBOARDING_BRANCH"


def io_onboarding_branch() -> str:
    """io-onboarding docs branch matching this deploy (default ``main``)."""
    return (os.environ.get(IO_ONBOARDING_BRANCH_ENV, "") or "").strip() or "main"


def io_onboarding_skill_url(filename: str) -> str:
    """Raw URL for an io-onboarding skill doc on the branch matching this deploy."""
    base = (
        "https://raw.githubusercontent.com/teleport-computer/io-onboarding/"
        f"{io_onboarding_branch()}"
    )
    return f"{base}/{filename}"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _strip_json_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _json_from_model_text(text: str):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    try:
        return json.loads(raw)
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
            return obj
        except Exception:
            continue
    raise ValueError("no json object found")


def _to_epoch(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _epoch_to_iso(epoch: float) -> str:
    try:
        if epoch and epoch > 0:
            return datetime.fromtimestamp(float(epoch)).isoformat()
    except Exception:
        pass
    return ""
