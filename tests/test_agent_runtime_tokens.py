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
