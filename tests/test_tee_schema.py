"""TEE 明文库独立 Alembic 链验证：表集齐全 + 版本表隔离（P2T1 / spec §4）。"""

import json
import os
from dataclasses import dataclass

import psycopg
import pytest


def _constraint_sql(value: str) -> str:
    """Normalize formatting only; constraint names are deliberately not identity."""
    normalized = " ".join(value.split())
    return normalized.replace("( ", "(").replace(" )", ")")


@dataclass(frozen=True)
class _KnownConstraintDifference:
    source_name: str
    cause: str
    remediation: str
    removal_condition: str


_DifferenceKey = tuple[str, str, str]  # table, kind, normalized definition

# Transition ledger, not a blanket allowlist: every entry is explicit, a resolved
# entry fails as stale, and review may only remove entries rather than grow this list.


def _difference_key(table: str, kind: str, definition: str) -> _DifferenceKey:
    return (table, kind, _constraint_sql(definition))


def _known(
    source_name: str,
    *,
    cause: str,
    remediation: str,
    removal_condition: str,
) -> _KnownConstraintDifference:
    return _KnownConstraintDifference(
        source_name=source_name,
        cause=cause,
        remediation=remediation,
        removal_condition=removal_condition,
    )


def _derived_check_debt(source_name: str, revision: str) -> _KnownConstraintDifference:
    return _known(
        source_name,
        cause=(
            f"T149/A: RDS {revision}; scripts/tee/derive_tee_ddl.py emits columns, "
            "PKs and selected user FKs but no CHECK constraints"
        ),
        remediation="teach the TEE derivation/migration path to carry this CHECK after data preflight",
        removal_condition="TEE has the same CHECK and this entry has become stale",
    )


def _lane_check_debt(source_name: str) -> _KnownConstraintDifference:
    return _known(
        source_name,
        cause="T149/external: RDS 0091 vs TEE 0023; owned by claude2's lane-rollup repair",
        remediation="land the separately reviewed idempotent TEE lane-rollup CHECK migration",
        removal_condition="the claude2 migration lands and this entry has become stale",
    )


_KNOWN_MISSING_TEE_DEBT: dict[_DifferenceKey, _KnownConstraintDifference] = {
    _difference_key("chat_r2_cleanup", "c", "CHECK (attempt_count >= 0)"):
        _derived_check_debt("chat_r2_cleanup_attempt_count_check", "0036_chat_r2_lifecycle"),
    _difference_key("chat_r2_cleanup", "c", "CHECK (body_key <> ''::text)"):
        _derived_check_debt("chat_r2_cleanup_body_key_check", "0036_chat_r2_lifecycle"),
    _difference_key("chat_r2_lifecycle", "c", "CHECK (generation >= 0)"):
        _derived_check_debt("chat_r2_lifecycle_generation_check", "0036_chat_r2_lifecycle"),
    _difference_key("chat_r2_lifecycle", "c", "CHECK (inventory_attempt_count >= 0)"):
        _derived_check_debt(
            "chat_r2_lifecycle_inventory_attempt_count_check", "0036_chat_r2_lifecycle"
        ),
    _difference_key(
        "dau_daily_snapshot", "c",
        "CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text)",
    ): _derived_check_debt("dau_daily_snapshot_day_format", "0017_dau_daily_snapshot"),
    _difference_key(
        "retention_cohort_snapshot", "c",
        "CHECK (cohort_week ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text)",
    ): _derived_check_debt("retention_cohort_week_format", "0021_growth_retention"),
    _difference_key("retention_cohort_snapshot", "c", "CHECK (period_index >= 0)"):
        _derived_check_debt("retention_period_nonneg", "0021_growth_retention"),
    _difference_key(
        "user_growth_daily_snapshot", "c",
        "CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text)",
    ): _derived_check_debt(
        "user_growth_daily_snapshot_day_format", "0021_growth_retention"
    ),
    _difference_key(
        "v2_effect_sink_applied", "c",
        "CHECK (claim_state = ANY (ARRAY['claimed'::text, 'completed'::text]))",
    ): _derived_check_debt(
        "v2_effect_sink_applied_claim_state_check", "0033_v2_seq_cursor_and_effect_order"
    ),
    _difference_key("v2_runtime_control", "c", "CHECK (id = 1)"):
        _derived_check_debt("v2_runtime_control_id_check", "0030_v2_runtime_control"),
    _difference_key("v2_trajectory_events", "c", "CHECK (event_index >= 0)"):
        _derived_check_debt("ck_v2_trajectory_event_index", "0043_v2_encrypted_trajectories"),
    _difference_key(
        "v2_trajectory_events", "c",
        "CHECK (event_kind ~ '^[a-z][a-z0-9_]{0,63}$'::text)",
    ): _derived_check_debt("ck_v2_trajectory_event_kind", "0043_v2_encrypted_trajectories"),
    _difference_key(
        "v2_trajectory_events", "c",
        "CHECK (length(idempotency_key) >= 1 AND length(idempotency_key) <= 96)",
    ): _derived_check_debt("ck_v2_trajectory_idempotency", "0043_v2_encrypted_trajectories"),
    _difference_key(
        "v2_trajectory_events", "c",
        "CHECK (payload_bytes >= 1 AND payload_bytes <= 1048576)",
    ): _derived_check_debt("ck_v2_trajectory_payload_bytes", "0043_v2_encrypted_trajectories"),
    _difference_key(
        "v2_trajectory_events",
        "c",
        """
        CHECK (jsonb_typeof(payload_envelope) = 'object'::text
          AND payload_envelope ? 'owner_user_id'::text
          AND payload_envelope ? 'id'::text
          AND payload_envelope ? 'visibility'::text
          AND jsonb_typeof(payload_envelope -> 'owner_user_id'::text) = 'string'::text
          AND jsonb_typeof(payload_envelope -> 'id'::text) = 'string'::text
          AND jsonb_typeof(payload_envelope -> 'visibility'::text) = 'string'::text
          AND (payload_envelope ->> 'owner_user_id'::text) = user_id
          AND (payload_envelope ->> 'visibility'::text) = 'shared'::text
          AND length(payload_envelope ->> 'id'::text) > 0
          AND (
            payload_envelope ? 'body_ct'::text
            AND payload_envelope ? 'nonce'::text
            AND payload_envelope ? 'K_user'::text
            AND payload_envelope ? 'K_enclave'::text
            AND payload_envelope ? 'v'::text
            AND jsonb_typeof(payload_envelope -> 'body_ct'::text) = 'string'::text
            AND jsonb_typeof(payload_envelope -> 'nonce'::text) = 'string'::text
            AND jsonb_typeof(payload_envelope -> 'K_user'::text) = 'string'::text
            AND jsonb_typeof(payload_envelope -> 'K_enclave'::text) = 'string'::text
            AND jsonb_typeof(payload_envelope -> 'v'::text) = 'number'::text
            AND length(payload_envelope ->> 'body_ct'::text) > 0
            AND length(payload_envelope ->> 'nonce'::text) > 0
            AND length(payload_envelope ->> 'K_user'::text) > 0
            AND length(payload_envelope ->> 'K_enclave'::text) > 0
            AND (payload_envelope - ARRAY['v'::text, 'id'::text, 'owner_user_id'::text,
              'visibility'::text, 'body_ct'::text, 'nonce'::text, 'K_user'::text,
              'K_enclave'::text, 'enclave_pk_fpr'::text, 'content_pk_fpr'::text]) = '{}'::jsonb
            OR payload_envelope ? 'body'::text
            AND jsonb_typeof(payload_envelope -> 'body'::text) = 'string'::text
            AND NOT payload_envelope ? 'body_ct'::text
            AND (payload_envelope - ARRAY['id'::text, 'owner_user_id'::text,
              'visibility'::text, 'body'::text]) = '{}'::jsonb
          ))
        """,
    ): _known(
        "ck_v2_trajectory_envelope",
        cause=(
            "T149/A-17: RDS 0043 was ciphertext-only; 0072_relax_v2_envelope_shape "
            "added the exact plaintext body shape emitted by the TEE replicator"
        ),
        remediation=(
            "ensure the target RDS chain includes 0072, then add its final two-branch "
            "CHECK to TEE after live-row preflight"
        ),
        removal_condition="TEE has the final two-branch CHECK and this entry has become stale",
    ),
    _difference_key(
        "v2_user_allowlist", "c",
        "CHECK (desired = ANY (ARRAY['v2'::text, 'resident'::text]))",
    ): _derived_check_debt("v2_user_allowlist_desired_check", "0052_dual_runtime_coexistence"),
    _difference_key("v2_worker_heartbeats", "c", "CHECK (capacity >= 0)"):
        _derived_check_debt("v2_worker_heartbeats_capacity_check", "0024_v2_worker_capacity"),
    _difference_key(
        "lane_daily_rollup", "c",
        "CHECK (completed >= 0 AND failed >= 0 AND expired >= 0 AND superseded >= 0)",
    ): _lane_check_debt("lane_daily_rollup_counts_nonneg"),
    _difference_key(
        "lane_daily_rollup", "c",
        "CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text)",
    ): _lane_check_debt("lane_daily_rollup_day_format"),
    _difference_key(
        "lane_rollup_watermark", "c",
        "CHECK (backfill_from ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text)",
    ): _lane_check_debt("lane_rollup_watermark_from_format"),
    _difference_key(
        "lane_rollup_watermark", "c",
        "CHECK (through_day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'::text)",
    ): _lane_check_debt("lane_rollup_watermark_through_format"),
}


_INTENTIONAL_MISSING_TEE_DIFFERENCES: dict[
    _DifferenceKey, _KnownConstraintDifference
] = {
    _difference_key(
        "agent_action_queue", "f",
        "FOREIGN KEY (job_id) REFERENCES agent_jobs(id) ON DELETE CASCADE",
    ): _known(
        "agent_action_queue_job_id_fkey",
        cause=(
            "T149/B: alembic_tee 0004 section 2 omitted cross-table FKs because "
            "independently refreshed tables can arrive or prune out of order"
        ),
        remediation="re-evaluate now that TEST is TEE-primary; do not copy blindly",
        removal_condition="Seven decides the post-primary FK policy and the schema follows it",
    ),
    _difference_key(
        "v2_trajectory_events", "f",
        "FOREIGN KEY (job_id, user_id) REFERENCES "
        "v2_trajectory_streams(job_id, user_id) ON DELETE CASCADE",
    ): _known(
        "fk_v2_trajectory_event_stream",
        cause=(
            "T149/B: worker.py v2_trajectory_events deliberately converges orphans with "
            "reflow/prune instead of a cross-table FK during shadow refresh"
        ),
        remediation="re-evaluate now that TEST is TEE-primary; preserve prune correctness",
        removal_condition="Seven decides the post-primary FK policy and the schema follows it",
    ),
}


_KNOWN_EXTRA_TEE_DEBT: dict[_DifferenceKey, _KnownConstraintDifference] = {
    _difference_key(
        "world_book_entries", "f",
        "FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE",
    ): _known(
        "world_book_entries_user_id_fkey",
        cause=(
            "T149/reverse: TEE 0004 is stricter than RDS despite its own source-parity rule; "
            "zero current orphans does not make the stronger replica contract safe"
        ),
        remediation="remove the TEE-only FK after a live orphan-count preflight",
        removal_condition="TEE no longer has the extra FK and this entry has become stale",
    ),
}


def _tee_conn():
    return psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True)


def test_tee_schema_has_all_tables():
    # agent_runtime_instances / agent_runtime_supervisor_heartbeats: 0002_drop_retired_
    # hosted_supervisor 曾按"V1 supervisor 已退役"把这两张表从这里的 want 集合里撤下
    # （当时还专门加了 "not in" 断言防止它们被误建回来）。0004_full_table_alignment
    # 按 2026-07-27 用户的决定把它们重新建回——实测 V1 在 RDS 侧仍然活着（prod 220 行
    # agent_runtime_instances + 1 行心跳，backend db.py:335 / agent_runtime/leases.py:51
    # 仍在写），TEE 全量对齐目标下不能留这个缺口。所以它们现在应当存在，"not in" 断言
    # 已删除，改为并入 want。
    want = {"server_config","global_blobs","users","user_blobs","user_logs",
            "perception_items","perception_daily","copytext_strings","copytext_meta",
            "genesis_import_jobs","genesis_import_outputs","chat_messages","memory_moments",
            "world_book_entries","frames","frame_envelopes","genesis_import_chunks",
            "voice_turn_results","voice_turn_streams","tee_replication_cursors",
            "tee_pending_device_migration","notify_relay_configs","notify_relay_logs",
            "agent_runtime_instances","agent_runtime_supervisor_heartbeats"}
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
    assert want <= {r[0] for r in rows}


def test_tee_version_table_is_isolated():
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'alembic%'").fetchall()
    assert {r[0] for r in rows} == {"alembic_tee_version"}


def test_tee_primary_startup_refuses_unprepared_shadow(monkeypatch):
    """A schema head alone is not proof that the frozen prepare completed."""
    import db

    with _tee_conn() as c:
        before = {
            r[0]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        }

    monkeypatch.setenv("DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    with pytest.raises(RuntimeError, match="frozen Phase-4 prepare"):
        db.init_schema()

    with _tee_conn() as c:
        after = {
            r[0]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        }
    assert after == before
    assert "alembic_version" not in after


def test_tee_primary_disables_stale_shadow_configuration(monkeypatch):
    from tee_shadow import mirror

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    monkeypatch.setenv("TEE_DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    assert mirror.enabled() is False


def test_tee_primary_shared_table_columns_match_runtime_schema():
    """A shadow may omit scratch values; a promoted primary may not.

    This guard caught the Phase-4 gaps in chat storage generations, Genesis
    claim ownership, and Runtime V2's effective compaction batch cap.
    """
    query = """
        SELECT table_name, column_name, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public'
        ORDER BY table_name, ordinal_position
    """

    def columns(url: str) -> dict[str, dict[str, tuple[str, str]]]:
        with psycopg.connect(url) as conn:
            rows = conn.execute(query).fetchall()
        result: dict[str, dict[str, tuple[str, str]]] = {}
        for table, column, udt_name, nullable in rows:
            result.setdefault(table, {})[column] = (udt_name, nullable)
        return result

    rds = columns(os.environ["DATABASE_URL"])
    tee = columns(os.environ["TEE_DATABASE_URL"])
    shared = set(rds) & set(tee)
    mismatches = {
        table: {"rds": rds[table], "tee": tee[table]}
        for table in sorted(shared)
        if rds[table] != tee[table]
    }
    assert mismatches == {}


def test_tee_primary_shared_unique_contracts_match_runtime_schema():
    """A promoted primary must satisfy every runtime ON CONFLICT target.

    The old shadow deliberately omitted unique indexes because RDS serialized
    writes.  That is unsafe once TEE becomes DATABASE_URL and was first exposed
    by the whole-turn metric upsert failing on ``ON CONFLICT (job_id)``.
    """
    query = """
        SELECT table_name, is_primary, is_unique, key_columns, predicate
        FROM (
          SELECT tbl.relname AS table_name,
                 idx.indisprimary AS is_primary,
                 idx.indisunique AS is_unique,
                 ARRAY(
                   SELECT pg_get_indexdef(idx.indexrelid, key_position, true)
                   FROM generate_series(1, idx.indnkeyatts) AS key_position
                   ORDER BY key_position
                 ) AS key_columns,
                 COALESCE(pg_get_expr(idx.indpred, idx.indrelid), '') AS predicate
          FROM pg_index AS idx
          JOIN pg_class AS tbl ON tbl.oid = idx.indrelid
          JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
          WHERE ns.nspname = 'public'
            AND (idx.indisprimary OR idx.indisunique)
        ) AS contracts
        ORDER BY table_name, is_primary, key_columns, predicate
    """

    def contracts(url: str) -> dict[str, set[tuple]]:
        with psycopg.connect(url) as conn:
            rows = conn.execute(query).fetchall()
        result: dict[str, set[tuple]] = {}
        for table, primary, unique, columns, predicate in rows:
            result.setdefault(table, set()).add(
                (bool(primary), bool(unique), tuple(columns), predicate)
            )
        return result

    rds = contracts(os.environ["DATABASE_URL"])
    tee = contracts(os.environ["TEE_DATABASE_URL"])
    shared = set(rds) & set(tee)
    missing = {
        table: sorted(rds[table] - tee[table], key=repr)
        for table in sorted(shared)
        if rds[table] - tee[table]
    }
    assert missing == {}


def test_tee_primary_shared_checks_and_foreign_keys_match_runtime_schema():
    """Compare every shared table; explicit transition entries must only shrink."""
    query = """
        SELECT tbl.relname, contract.conname, contract.contype,
               CASE WHEN contract.oid IS NULL THEN NULL
                    ELSE pg_get_constraintdef(contract.oid, true)
               END
        FROM pg_class AS tbl
        JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
        LEFT JOIN pg_constraint AS contract
          ON contract.conrelid = tbl.oid
         AND contract.contype IN ('c', 'f', 'x')
        WHERE ns.nspname = 'public'
          AND tbl.relkind IN ('r', 'p')
        ORDER BY tbl.relname, contract.conname, contract.contype,
                 CASE WHEN contract.oid IS NULL THEN NULL
                      ELSE pg_get_constraintdef(contract.oid, true)
                 END
    """

    def constraints(url: str) -> dict[str, dict[tuple[str, str], str]]:
        with psycopg.connect(url) as conn:
            rows = conn.execute(query).fetchall()
        result: dict[str, dict[tuple[str, str], str]] = {}
        for table, name, kind, definition in rows:
            result.setdefault(table, {})
            if kind is not None:
                result[table][(kind, _constraint_sql(definition))] = name
        return result

    rds = constraints(os.environ["DATABASE_URL"])
    tee = constraints(os.environ["TEE_DATABASE_URL"])
    shared = set(rds) & set(tee)
    missing_in_tee = {
        _difference_key(table, kind, definition): rds[table][(kind, definition)]
        for table in sorted(shared)
        for kind, definition in set(rds[table]) - set(tee[table])
    }
    extra_in_tee = {
        _difference_key(table, kind, definition): tee[table][(kind, definition)]
        for table in sorted(shared)
        for kind, definition in set(tee[table]) - set(rds[table])
    }

    intentional = set(_INTENTIONAL_MISSING_TEE_DIFFERENCES)
    debt_missing = set(_KNOWN_MISSING_TEE_DEBT)
    debt_extra = set(_KNOWN_EXTRA_TEE_DEBT)
    assert not intentional & debt_missing, "one difference cannot be debt and intentional"

    def details(
        keys: set[_DifferenceKey],
        observed: dict[_DifferenceKey, str],
        known: dict[_DifferenceKey, _KnownConstraintDifference],
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for table, kind, definition in sorted(keys):
            metadata = known.get((table, kind, definition))
            rows.append(
                {
                    "table": table,
                    "observed_name": observed.get((table, kind, definition), ""),
                    "kind": kind,
                    "definition": definition,
                    "cause": metadata.cause if metadata else "",
                    "remediation": metadata.remediation if metadata else "",
                    "removal_condition": metadata.removal_condition if metadata else "",
                }
            )
        return rows

    actual_missing = set(missing_in_tee)
    actual_extra = set(extra_in_tee)
    report = {
        "unexpected_missing_in_tee": details(
            actual_missing - debt_missing - intentional, missing_in_tee, {}
        ),
        "unexpected_extra_in_tee": details(actual_extra - debt_extra, extra_in_tee, {}),
        "stale_known_missing": details(
            debt_missing - actual_missing, {}, _KNOWN_MISSING_TEE_DEBT
        ),
        "stale_intentional_missing": details(
            intentional - actual_missing, {}, _INTENTIONAL_MISSING_TEE_DIFFERENCES
        ),
        "stale_known_extra": details(
            debt_extra - actual_extra, {}, _KNOWN_EXTRA_TEE_DEBT
        ),
    }
    assert not any(report.values()), json.dumps(report, ensure_ascii=False, indent=2)


def test_tee_primary_shared_runtime_indexes_match_runtime_schema():
    """Do not promote a schema that drops the runtime's query plan contracts."""
    query = """
        SELECT tbl.relname, idx.indisprimary, idx.indisunique,
               idx.indnkeyatts, idx.indnatts,
               ARRAY(
                 SELECT pg_get_indexdef(idx.indexrelid, position, true)
                 FROM generate_series(1, idx.indnatts) AS position
                 ORDER BY position
               ),
               COALESCE(pg_get_expr(idx.indpred, idx.indrelid), '')
        FROM pg_index AS idx
        JOIN pg_class AS tbl ON tbl.oid = idx.indrelid
        JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
        WHERE ns.nspname = 'public'
        ORDER BY tbl.relname, idx.indisprimary, idx.indisunique
    """

    def indexes(url: str) -> dict[str, set[tuple]]:
        with psycopg.connect(url) as conn:
            rows = conn.execute(query).fetchall()
        result: dict[str, set[tuple]] = {}
        for table, primary, unique, key_count, attr_count, columns, predicate in rows:
            result.setdefault(table, set()).add(
                (
                    bool(primary), bool(unique), int(key_count), int(attr_count),
                    tuple(columns), predicate,
                )
            )
        return result

    rds = indexes(os.environ["DATABASE_URL"])
    tee = indexes(os.environ["TEE_DATABASE_URL"])
    shared = set(rds) & set(tee)
    missing = {
        table: sorted(rds[table] - tee[table], key=repr)
        for table in sorted(shared)
        if rds[table] - tee[table]
    }
    assert missing == {}
