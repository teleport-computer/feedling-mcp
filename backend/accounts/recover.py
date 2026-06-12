"""Keypair proof-of-possession account recovery state."""

import threading

# ---------------------------------------------------------------------------
# Keypair proof-of-possession account recovery
#
# A device that still holds the content X25519 keypair (it syncs via iCloud
# Keychain) but lost its device-local api_key must recover its EXISTING account
# rather than registering a new one — otherwise it orphans the account (the
# register-orphan bug). The device proves possession of the private key by
# decrypting a challenge sealed to the account's public_key; the server then
# issues a fresh api_key for that existing account. No new account is minted.
# ---------------------------------------------------------------------------

RECOVER_CHALLENGE_TTL_SEC = 300
_recover_challenges: dict[str, dict] = {}
_recover_challenges_lock = threading.Lock()


def _prune_recover_challenges_locked(now: float) -> None:
    expired = [cid for cid, c in _recover_challenges.items() if c.get("expires_at", 0) < now]
    for cid in expired:
        _recover_challenges.pop(cid, None)
