#!/usr/bin/env python3
"""Non-persistent feasibility probe for narrow daily call dimensions."""

from __future__ import annotations

from decimal import Decimal
import re

from scripts.perf.admin_usage_ranked_flags_temp_probe import (
    FLAG_COLUMN_NAMES,
    _ranked_rows_and_outputs,
)


NARROW_IDENTITY_COLUMNS = (
    "local_day",
    "user_id",
    "cohort_lane",
    "requested_provider",
    "requested_model",
    "resolved_provider",
    "resolved_model",
    "effective_usage_known",
)
MAX_NARROW_TOTAL_BYTES = 700_000_000
MAX_MEMBERSHIP_RATIO = Decimal("0.25")
_RELATION = re.compile(r"^[a-z_][a-z0-9_]*$")

__all__ = (
    "FLAG_COLUMN_NAMES",
    "NARROW_IDENTITY_COLUMNS",
    "_narrow_storage_passed",
    "_narrow_table_ddl",
    "_ranked_rows_and_outputs",
)


def _narrow_storage_passed(
    stats: dict[str, int], *, membership_total_bytes: int
) -> bool:
    total = int(stats["total_bytes"])
    return (
        total <= MAX_NARROW_TOTAL_BYTES
        and Decimal(total)
        < Decimal(membership_total_bytes) * MAX_MEMBERSHIP_RATIO
    )


def _narrow_table_ddl(*, relation: str) -> tuple[str, ...]:
    if not _RELATION.fullmatch(relation):
        raise ValueError(f"unsafe diagnostic relation name: {relation!r}")

    identity_definitions = (
        "local_day DATE NOT NULL",
        "user_id TEXT NOT NULL",
        "cohort_lane TEXT NOT NULL",
        "requested_provider TEXT NOT NULL",
        "requested_model TEXT NOT NULL",
        "resolved_provider TEXT NOT NULL",
        "resolved_model TEXT NOT NULL",
        "effective_usage_known BOOLEAN NOT NULL",
    )
    flag_definitions = tuple(
        f"{name} BIGINT NOT NULL DEFAULT 0 CHECK ({name} >= 0)"
        for name in FLAG_COLUMN_NAMES
    )
    grain = ",".join(NARROW_IDENTITY_COLUMNS)
    return (
        "CREATE TEMP TABLE "
        + relation
        + " ("
        + ",".join((*identity_definitions, *flag_definitions))
        + ") ON COMMIT PRESERVE ROWS",
        f"CREATE UNIQUE INDEX {relation}_grain_idx ON {relation} ({grain})",
        f"CREATE INDEX {relation}_user_day_idx ON {relation} "
        "(user_id,local_day)",
        f"CREATE INDEX {relation}_resolved_day_idx ON {relation} "
        "(local_day,resolved_provider,resolved_model,user_id,cohort_lane) "
        "INCLUDE (requested_provider,requested_model,effective_usage_known)",
    )
