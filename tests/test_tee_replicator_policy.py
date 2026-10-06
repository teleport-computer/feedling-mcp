from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from accounts import registry  # noqa: E402
from conftest import seed_user  # noqa: E402
from tee_replicator import policy  # noqa: E402
from tee_replicator import transforms  # noqa: E402
from tee_replicator import worker  # noqa: E402


def _uid(label: str) -> str:
    return f"usr_policy_{label}_{uuid.uuid4().hex[:8]}"


def _sealed_chat_doc() -> dict:
    return {
        "id": "msg-policy",
        "role": "user",
        "ts": 1.0,
        "source": "app",
        "body_ct": "Y3Q=",
        "nonce": "bm9uY2U=",
        "K_user": "a3U=",
        "K_enclave": "a2U=",
        "visibility": "shared",
    }


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("on", "on"), ("off", "off"), (None, "off")],
)
def test_resolver_uses_authoritative_user_row(backend_env, stored, expected):
    user_id = _uid(expected)
    doc = {} if stored is None else {"content_encryption": stored}
    seed_user(user_id, **doc)

    assert policy.resolve_content_encryption(user_id) == expected


def test_resolver_distinguishes_missing_user_from_default_off(backend_env):
    assert policy.resolve_content_encryption(_uid("missing")) is None


def test_fresh_cli_process_does_not_treat_existing_unset_user_as_unknown(
    backend_env, monkeypatch
):
    user_id = _uid("fresh")
    seed_user(user_id)
    with registry._users_lock:
        registry._users[:] = []
    worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(
        worker,
        "_get_decrypt",
        lambda _user_id, **_kw: (
            lambda _envelope, *, purpose: b"plaintext"
        ),
    )

    out = worker._transform_with_retry(
        SimpleNamespace(transform=transforms.plaintext_chat_doc),
        _sealed_chat_doc(),
        user_id,
    )

    assert out["body"] == "plaintext"
    assert "body_ct" not in out


class _FailingPool:
    def connection(self):
        raise RuntimeError("postgresql://secret:password@example.invalid/prod")


def test_policy_source_probe_propagates_database_failure():
    with pytest.raises(RuntimeError):
        policy.probe_policy_source(pool=_FailingPool())
