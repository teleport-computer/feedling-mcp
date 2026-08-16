from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from model_api_runtime.v2 import profile_store


MODULE_PATH = Path(__file__).parent.parent / "tools" / "backfill_v2_profiles.py"
SPEC = importlib.util.spec_from_file_location("backfill_v2_profiles", MODULE_PATH)
backfill = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backfill)


def _seal(_user_id: str, text: str) -> dict:
    return {
        "body_ct": f"cipher-{len(text)}",
        "nonce": "fixed-nonce",
        "aad": {"purpose": "profile-test"},
    }


def _profile(
    *,
    state: str,
    disposition: str = "",
    disabled: bool = False,
) -> dict:
    family = {
        "provider_config": "provider_config",
        "terminal": "terminal",
        "scheduled": "shape",
        "source_change": "source",
    }.get(disposition, "")
    return profile_store.build_profile_document(
        "u",
        state=state,
        source={
            "card_count": 3,
            "max_updated_at": "2026-08-16T00:00:00Z",
            "generated_at": "2026-08-16T00:00:00Z" if state == "ok" else "",
        },
        last_attempt={
            "at": "2026-08-16T00:00:00Z",
            "reject_code": "reply_not_json" if disposition else "",
            "attempts": 2,
            "retry_disposition": disposition,
            "retry_family": family,
            "retry_attempts": 2 if disposition else 0,
            "retry_not_before": 999.0 if disposition == "scheduled" else 0,
        },
        memory_text="memory" if state == "ok" else None,
        style_text="style" if state == "ok" else None,
        disabled=disabled,
        seal_text=_seal,
    )


def test_selection_predicate_covers_the_closed_outcome_set_from_constants():
    stuck = sorted(profile_store.PROFILE_STUCK_RETRY_DISPOSITIONS)
    assert stuck == ["provider_config", "terminal"]
    docs = {
        "u-provider": _profile(state="pending", disposition=stuck[0]),
        "u-terminal": _profile(state="degraded", disposition=stuck[1]),
        "u-empty": _profile(state="empty"),
        "u-fresh": _profile(state="ok"),
        "u-disabled": _profile(state="ok", disabled=True),
        "u-invalid": {"v": -1},
    }

    rows = backfill.run_batch(
        docs,
        execute=False,
        force_all=False,
        read_profile=lambda uid, _kind: docs[uid],
    )

    assert {row["outcome"] for row in rows} == backfill.OUTCOME_SET
    assert {row["user_id"]: row["outcome"] for row in rows} == {
        "u-provider": "rescued_provider_config",
        "u-terminal": "rescued_terminal",
        "u-empty": "enqueued_empty",
        "u-fresh": "skipped_fresh",
        "u-disabled": "skipped_disabled",
        "u-invalid": "failed",
    }
    assert set(rows[0]) == {"user_id", "outcome", "state", "disposition"}


@pytest.mark.parametrize(
    "disposition", sorted(profile_store.PROFILE_STUCK_RETRY_DISPOSITIONS)
)
def test_rescue_cas_preserves_both_envelopes_byte_for_byte(disposition):
    raw = _profile(state="degraded", disposition=disposition)
    # Use distinct, nontrivial envelopes so a reseal or side swap is observable.
    raw["memory"] = {
        "envelope": {"body_ct": "memory-cipher", "nonce": "m", "aad": {"z": 1}},
        "chars": 6,
    }
    raw["style"] = {
        "envelope": {"body_ct": "style-cipher", "nonce": "s", "aad": {"a": 2}},
        "chars": 5,
    }
    state = {"doc": deepcopy(raw)}
    before_memory = json.dumps(raw["memory"]["envelope"], separators=(",", ":"))
    before_style = json.dumps(raw["style"]["envelope"], separators=(",", ":"))

    def _read(_uid, _kind):
        return deepcopy(state["doc"])

    def _cas(_uid, _kind, expected, candidate):
        assert expected == state["doc"]
        state["doc"] = deepcopy(candidate)
        return True

    outcome = backfill.rescue_stuck_profile(
        "u", read_profile=_read, compare_and_swap=_cas
    )

    assert outcome == f"rescued_{disposition}"
    assert json.dumps(
        state["doc"]["memory"]["envelope"], separators=(",", ":")
    ) == before_memory
    assert json.dumps(
        state["doc"]["style"]["envelope"], separators=(",", ":")
    ) == before_style
    assert state["doc"]["last_attempt"]["retry_disposition"] == ""
    assert state["doc"]["last_attempt"]["retry_not_before"] == 0.0


def test_rescue_does_not_clear_a_non_stuck_retry_after_plan_race():
    raw = _profile(state="degraded", disposition="scheduled")
    cas_calls = 0

    def _cas(*_args, **_kwargs):
        nonlocal cas_calls
        cas_calls += 1
        return True

    outcome = backfill.rescue_stuck_profile(
        "u",
        read_profile=lambda _uid, _kind: deepcopy(raw),
        compare_and_swap=_cas,
    )

    assert outcome == "skipped_fresh"
    assert cas_calls == 0


def test_execute_is_idempotent_and_second_run_has_zero_actions():
    docs = {
        "u-rescue": _profile(state="pending", disposition="terminal"),
        "u-empty": _profile(state="empty"),
    }
    action_counts = {"rescue": 0, "enqueue": 0}
    active_profile_jobs: set[str] = set()

    def _read(uid, _kind):
        return deepcopy(docs[uid])

    def _cas(uid, _kind, expected, candidate):
        if docs[uid] != expected:
            return False
        docs[uid] = deepcopy(candidate)
        action_counts["rescue"] += 1
        return True

    def _rescue(uid):
        return backfill.rescue_stuck_profile(
            uid, read_profile=_read, compare_and_swap=_cas
        )

    def _enqueue(uid):
        if uid in active_profile_jobs:
            return False
        active_profile_jobs.add(uid)
        action_counts["enqueue"] += 1
        return True

    first = backfill.run_batch(
        docs,
        execute=True,
        force_all=False,
        read_profile=_read,
        rescue=_rescue,
        enqueue=_enqueue,
    )
    counts_after_first = dict(action_counts)
    second = backfill.run_batch(
        docs,
        execute=True,
        force_all=False,
        read_profile=_read,
        rescue=_rescue,
        enqueue=_enqueue,
    )

    assert {row["outcome"] for row in first} == {
        "rescued_terminal",
        "enqueued_empty",
    }
    assert [row["outcome"] for row in second] == [
        "skipped_fresh",
        "skipped_fresh",
    ]
    assert action_counts == counts_after_first == {"rescue": 1, "enqueue": 1}


def test_one_user_failure_does_not_abort_the_batch():
    fresh = _profile(state="ok")

    def _read(uid, _kind):
        if uid == "u-bad":
            raise RuntimeError("simulated database failure")
        return fresh

    rows = backfill.run_batch(
        ["u-good", "u-bad"],
        execute=True,
        force_all=False,
        read_profile=_read,
    )

    assert {row["user_id"]: row["outcome"] for row in rows} == {
        "u-bad": "failed",
        "u-good": "skipped_fresh",
    }


def test_dry_run_is_default_and_never_calls_mutators():
    parser = backfill._parser()
    args = parser.parse_args(["--env", "test"])
    assert args.execute is False
    assert parser.parse_args(["--env", "test", "--dry-run"]).execute is False
    assert parser.parse_args(["--env", "test", "--execute"]).execute is True

    rows = backfill.run_batch(
        ["u"],
        execute=False,
        force_all=True,
        read_profile=lambda *_args: _profile(state="ok"),
        rescue=lambda _uid: pytest.fail("dry-run must not rescue"),
        enqueue=lambda _uid: pytest.fail("dry-run must not enqueue"),
    )
    assert rows[0]["outcome"] == "enqueued_empty"


def test_prod_execute_requires_explicit_seven_approval():
    with pytest.raises(SystemExit, match="Seven approval"):
        backfill._validate_execution_gate(
            argparse.Namespace(
                env="prod", execute=True, confirm_prod_seven_approved=False
            )
        )

    backfill._validate_execution_gate(
        argparse.Namespace(
            env="prod", execute=True, confirm_prod_seven_approved=True
        )
    )


def test_manual_workflow_keeps_dsns_secret_and_prod_double_gated():
    workflow = (
        Path(__file__).parent.parent
        / ".github"
        / "workflows"
        / "backfill-v2-profiles.yml"
    ).read_text()

    assert "TEST_DATABASE_URL: ${{ secrets.TEST_DATABASE_URL }}" in workflow
    assert "PROD_DATABASE_URL: ${{ secrets.DATABASE_URL }}" in workflow
    assert 'default: "DRY_RUN"' in workflow
    assert '!= "SEVEN_APPROVED"' in workflow
    assert "args+=(--confirm-prod-seven-approved)" in workflow


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="plain PostgreSQL is unavailable"
)
def test_selects_only_v2_users_with_completed_genesis_from_plain_postgres():
    users = {
        "u-backfill-eligible": ("v2", "done"),
        "u-backfill-resident": ("resident", "done"),
        "u-backfill-incomplete": ("v2", "processing"),
    }
    with db.get_pool().connection() as conn:
        for user_id, (runtime_state, genesis_state) in users.items():
            conftest.seed_user(user_id)
            conn.execute(
                "INSERT INTO v2_runtime_state "
                "(user_id,hosted_runtime_state,runtime_generation) "
                "VALUES (%s,%s,1) ON CONFLICT (user_id) DO UPDATE SET "
                "hosted_runtime_state=EXCLUDED.hosted_runtime_state",
                (user_id, runtime_state),
            )
            conn.execute(
                "INSERT INTO genesis_import_jobs (user_id,job_id,status) "
                "VALUES (%s,%s,%s)",
                (user_id, f"job-{user_id}", genesis_state),
            )

    assert backfill._select_user_ids(
        os.environ["DATABASE_URL"], users
    ) == ["u-backfill-eligible"]
