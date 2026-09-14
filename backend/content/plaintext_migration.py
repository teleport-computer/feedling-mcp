"""Single-user legacy content-envelope to plaintext migration.

The public entry point is deliberately small and fail closed.  Inventory is
read-only; apply requires an explicit per-user ``off`` preference and the CLI's
independent write gates.  Surface-specific inventory and CAS writers live in
this module so the command can be exercised without exposing content values.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import db


APPLY_ENV = "FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION"


@dataclass(frozen=True)
class Item:
    surface: str
    item_id: str
    classification: str


@dataclass(frozen=True)
class Result:
    apply: bool
    user_id: str
    counts: dict[str, int]
    failures: int = 0

    def public_dict(self) -> dict:
        """Return the intentionally content-free operator report."""
        return {
            "apply": self.apply,
            "counts": self.counts,
            "failures": self.failures,
            "user_id": self.user_id,
        }


def content_encryption_preference(user_id: str) -> str | None:
    """Read the stored three-state preference; absence is not explicit off."""
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc->>'content_encryption' FROM users WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    if row is None:
        return None
    value = str(row[0] or "").strip().lower()
    return value or None


def make_decrypt(user_id: str):
    """Create the existing user-scoped enclave decrypt callback lazily."""
    from tee_replicator.worker import _make_decrypt

    return _make_decrypt(user_id)


def inventory(user_id: str) -> Iterable[Item]:
    """Yield content-free inventory items (implemented in Task 2)."""
    del user_id
    return ()


def run(user_id: str, *, apply: bool = False) -> Result:
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("exact user_id is required")
    if apply and content_encryption_preference(user_id) != "off":
        raise PermissionError("content_encryption must be explicitly off")

    items = inventory(user_id)
    counts = Counter(item.classification for item in items)
    return Result(
        apply=bool(apply),
        user_id=user_id,
        counts=dict(sorted(counts.items())),
    )
