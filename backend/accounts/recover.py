"""Keypair proof-of-possession account recovery state.

Cross-worker, atomic challenge store backed by PostgreSQL.

Production runs multiple gunicorn workers (FEEDLING_BACKEND_WORKERS defaults to
6 in deploy/docker-compose.phala.yaml). The previous process-local dict meant a
challenge created on worker A was invisible to a verify request hitting worker
B, surfacing as ``invalid_or_expired_challenge``. Every worker now reads/writes
the same Postgres table, and verify consumes the challenge with a single atomic
``DELETE ... RETURNING`` so exactly one worker can ever use it.

A device that still holds the content X25519 keypair (it syncs via iCloud
Keychain) but lost its device-local api_key must recover its EXISTING account
rather than registering a new one — otherwise it orphans the account (the
register-orphan bug). The device proves possession of the private key by
decrypting a challenge sealed to the account's public_key; the server then
issues a fresh api_key for that existing account. No new account is minted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from db import get_pool

RECOVER_CHALLENGE_TTL_SEC = 300

_TABLE = "account_recover_challenges"


@dataclass(frozen=True)
class StoredChallenge:
    user_id: str
    public_key: str
    answer_sha256: str
    expires_at: float


def answer_sha256(challenge: str) -> str:
    """Equivalent safe representation of the expected answer (never stores the
    plaintext challenge; compare with hmac.compare_digest at verify time)."""
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


def create_challenge(
    *,
    challenge_id: str,
    user_id: str,
    public_key: str,
    challenge: str,
    now: float,
) -> float:
    """Persist one challenge with a 300s TTL (shared across workers).

    Expired rows and any older challenge for the same user are removed so only
    the newest challenge is usable. Never stores the private key or an api key.
    """
    expires_at = now + RECOVER_CHALLENGE_TTL_SEC
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                f"DELETE FROM {_TABLE} WHERE expires_at < %s OR user_id = %s",
                (now, user_id),
            )
            conn.execute(
                f"INSERT INTO {_TABLE} "
                "(challenge_id, user_id, public_key, answer_sha256, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (challenge_id) DO NOTHING",
                (
                    challenge_id,
                    user_id,
                    public_key,
                    answer_sha256(challenge),
                    now,
                    expires_at,
                ),
            )
    return expires_at


def consume_challenge(challenge_id: str, now: float) -> StoredChallenge | None:
    """Atomically consume one challenge. Returns None when the id is absent or
    already expired (both map to ``invalid_or_expired_challenge``).

    The DELETE is the single consume point: concurrent verifies from different
    workers race on the same row, and only the winner receives the RETURNING
    row — the loser sees no row and the challenge cannot be double-spent.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                f"DELETE FROM {_TABLE} WHERE challenge_id = %s "
                "RETURNING user_id, public_key, answer_sha256, expires_at",
                (challenge_id,),
            ).fetchone()
    if row is None:
        return None
    user_id, public_key, answer_sha256, expires_at = row
    if float(expires_at) < now:
        return None
    return StoredChallenge(
        user_id=user_id,
        public_key=public_key,
        answer_sha256=answer_sha256,
        expires_at=float(expires_at),
    )
