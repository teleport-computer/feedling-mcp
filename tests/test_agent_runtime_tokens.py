"""Pure-unit tests for backend/agent_runtime/tokens.py runtime tokens."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import tokens

SECRET = b"supervisor-secret-key-for-tests"


def test_mint_then_verify_roundtrip():
    tok = tokens.mint(SECRET, user_id="u_1", runtime_instance_id="ri_1",
                      scope=["chat:write"], now=1000.0, ttl=600.0)
    claims = tokens.verify(SECRET, tok, now=1100.0)
    assert claims["user_id"] == "u_1"
    assert claims["sub"] == "ri_1"
    assert claims["scope"] == ["chat:write"]
    assert claims["exp"] == 1600.0


def test_verify_rejects_expired():
    tok = tokens.mint(SECRET, user_id="u_1", runtime_instance_id="ri_1",
                      scope=["chat:write"], now=1000.0, ttl=600.0)
    with pytest.raises(tokens.TokenError):
        tokens.verify(SECRET, tok, now=2000.0)  # past exp 1600


def test_verify_rejects_tampered_signature():
    tok = tokens.mint(SECRET, user_id="u_1", runtime_instance_id="ri_1",
                      scope=["chat:write"], now=1000.0, ttl=600.0)
    tampered = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    with pytest.raises(tokens.TokenError):
        tokens.verify(SECRET, tampered, now=1100.0)


def test_verify_rejects_wrong_secret():
    tok = tokens.mint(SECRET, user_id="u_1", runtime_instance_id="ri_1",
                      scope=["chat:write"], now=1000.0, ttl=600.0)
    with pytest.raises(tokens.TokenError):
        tokens.verify(b"other-secret", tok, now=1100.0)


def test_authorize_user_accepts_matching_user():
    claims = {"user_id": "u_1", "scope": ["chat:write"]}
    # Should not raise.
    tokens.authorize(claims, user_id="u_1", scope="chat:write")


def test_authorize_user_rejects_cross_user_access():
    claims = {"user_id": "u_1", "scope": ["chat:write"]}
    with pytest.raises(tokens.TokenError):
        tokens.authorize(claims, user_id="u_2", scope="chat:write")


def test_authorize_rejects_missing_scope():
    claims = {"user_id": "u_1", "scope": ["screen:read"]}
    with pytest.raises(tokens.TokenError):
        tokens.authorize(claims, user_id="u_1", scope="chat:write")


# ---------------------------------------------------------------------------
# Batch 4: the hosted-consumer runtime token must carry the "web" scope so its
# io_cli can reach /v1/agent/web/{search,fetch} (backend require_scope("web")).
# ---------------------------------------------------------------------------


def test_hosted_consumer_token_scopes_include_web():
    from agent_runtime import supervisor

    scopes = supervisor._CONSUMER_TOKEN_SCOPES
    assert "web" in scopes
    # A credential change is a whole family: the originals must survive intact.
    for base in ("chat", "memory", "identity", "perception", "envelope_decrypt"):
        assert base in scopes


def test_token_minted_with_consumer_scopes_authorizes_web():
    from agent_runtime import supervisor

    tok = tokens.mint(SECRET, user_id="u_1", runtime_instance_id="ri_1",
                      scope=list(supervisor._CONSUMER_TOKEN_SCOPES),
                      now=1000.0, ttl=600.0)
    claims = tokens.verify(SECRET, tok, now=1100.0)
    tokens.authorize(claims, user_id="u_1", scope="web")  # must not raise
    # Not a blanket grant — a scope that was never minted is still refused.
    with pytest.raises(tokens.TokenError):
        tokens.authorize(claims, user_id="u_1", scope="genesis")
