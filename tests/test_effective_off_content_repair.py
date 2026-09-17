from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from content import plaintext_repair  # noqa: E402
import migrate_effective_off_content_to_plaintext as cli  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return _Rows(self.rows)


class _Pool:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def connection(self):
        return _Connection(self.rows, self.calls)


def test_eligible_users_are_ordered_resumable_and_exclude_explicit_on():
    calls = []
    pool = _Pool([("usr_b",), ("usr_c",)], calls)

    assert plaintext_repair.eligible_user_ids(
        start_after="usr_a", user_limit=2, pool=pool
    ) == ["usr_b", "usr_c"]

    sql, params = calls[0]
    assert "ORDER BY user_id" in sql
    assert "content_encryption" in sql and "<> 'on'" in sql
    assert params == ("usr_a", 2)


def test_dry_run_is_deterministic_and_does_not_probe_health(monkeypatch):
    monkeypatch.setattr(
        plaintext_repair, "eligible_user_ids", lambda **_kwargs: ["usr_a", "usr_b"]
    )
    calls = []

    def migrate(user_id, **kwargs):
        calls.append((user_id, kwargs))
        return SimpleNamespace(counts={"migratable_shared": 2}, failures=0)

    monkeypatch.setattr(plaintext_repair.plaintext_migration, "run", migrate)

    result = plaintext_repair.run(
        health_probe=lambda: pytest.fail("dry-run must not probe enclave")
    )

    assert calls == [
        ("usr_a", {"apply": False, "limit": 0, "rate": 1.0}),
        ("usr_b", {"apply": False, "limit": 0, "rate": 1.0}),
    ]
    assert result.public_dict() == {
        "apply": False,
        "failures": 0,
        "item_counts": {"migratable_shared": 4},
        "last_completed_user_id": "usr_b",
        "users_completed": 2,
        "users_selected": 2,
    }


def test_apply_waits_for_consecutive_healthy_probes(monkeypatch):
    monkeypatch.setattr(
        plaintext_repair, "eligible_user_ids", lambda **_kwargs: ["usr_a"]
    )
    probes = iter([False, True, True])
    sleeps = []
    monkeypatch.setattr(
        plaintext_repair.plaintext_migration,
        "run",
        lambda user_id, **kwargs: SimpleNamespace(counts={"migrated": 1}, failures=0),
    )

    result = plaintext_repair.run(
        apply=True,
        health_probe=lambda: next(probes),
        healthy_streak=2,
        health_poll_sec=0.25,
        max_pause_sec=5,
        sleep=sleeps.append,
    )

    assert sleeps == [0.25, 0.25]
    assert result.users_completed == 1
    assert result.failures == 0


def test_apply_stops_after_first_failed_user(monkeypatch):
    monkeypatch.setattr(
        plaintext_repair, "eligible_user_ids", lambda **_kwargs: ["usr_a", "usr_b"]
    )
    called = []

    def migrate(user_id, **_kwargs):
        called.append(user_id)
        return SimpleNamespace(
            counts={"failed_transform_or_storage": 1}, failures=1
        )

    monkeypatch.setattr(plaintext_repair.plaintext_migration, "run", migrate)

    result = plaintext_repair.run(
        apply=True,
        health_probe=lambda: True,
        healthy_streak=1,
    )

    assert called == ["usr_a"]
    assert result.failures == 1
    assert result.users_completed == 0
    assert result.last_completed_user_id == ""


def test_apply_partial_user_stops_without_advancing_resume_cursor(monkeypatch):
    monkeypatch.setattr(
        plaintext_repair,
        "eligible_user_ids",
        lambda **_kwargs: ["usr_a", "usr_b", "usr_c"],
    )
    called = []

    def migrate(user_id, **_kwargs):
        called.append(user_id)
        if user_id == "usr_a":
            return SimpleNamespace(counts={"migrated": 2}, failures=0)
        if user_id == "usr_b":
            return SimpleNamespace(
                counts={"migrated": 20, "not_attempted_limit": 80},
                failures=0,
            )
        return SimpleNamespace(counts={"migrated": 3}, failures=0)

    monkeypatch.setattr(plaintext_repair.plaintext_migration, "run", migrate)

    result = plaintext_repair.run(
        apply=True,
        row_limit=20,
        health_probe=lambda: True,
        healthy_streak=1,
    )

    assert called == ["usr_a", "usr_b"]
    assert result.public_dict() == {
        "apply": True,
        "failures": 0,
        "item_counts": {"migrated": 22, "not_attempted_limit": 80},
        "last_completed_user_id": "usr_a",
        "users_completed": 1,
        "users_selected": 3,
    }


def test_apply_stops_when_health_does_not_recover(monkeypatch):
    monkeypatch.setattr(
        plaintext_repair, "eligible_user_ids", lambda **_kwargs: ["usr_a"]
    )
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(plaintext_repair.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        plaintext_repair.plaintext_migration,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unhealthy gate must block migration"),
    )

    result = plaintext_repair.run(
        apply=True,
        health_probe=lambda: False,
        healthy_streak=1,
        health_poll_sec=0.1,
        max_pause_sec=1.0,
        sleep=lambda _seconds: None,
    )

    assert result.failures == 1
    assert result.item_counts == {"failed_health_gate": 1}


@pytest.mark.parametrize(
    ("extra", "enabled"),
    [
        ([], True),
        (["--allow-plaintext-rewrite"], True),
        (
            [
                "--allow-plaintext-rewrite",
                "--confirm-all-effective-off",
                "ALL-EFFECTIVE-OFF",
            ],
            False,
        ),
    ],
)
def test_cli_apply_requires_all_independent_gates(
    monkeypatch, capsys, extra, enabled
):
    if enabled:
        monkeypatch.setenv(plaintext_repair.APPLY_ENV, "1")
    else:
        monkeypatch.delenv(plaintext_repair.APPLY_ENV, raising=False)
    monkeypatch.setattr(
        plaintext_repair,
        "run",
        lambda **_kwargs: pytest.fail("gate must precede repair"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["--apply", *extra])

    assert exc.value.code == 2
    assert "requires" in capsys.readouterr().err


def test_cli_dry_run_emits_content_free_json(monkeypatch, capsys):
    report = {
        "apply": False,
        "failures": 0,
        "item_counts": {"migratable_shared": 8},
        "last_completed_user_id": "usr_b",
        "users_completed": 2,
        "users_selected": 2,
    }
    monkeypatch.setattr(
        plaintext_repair,
        "run",
        lambda **_kwargs: SimpleNamespace(public_dict=lambda: report, failures=0),
    )

    assert cli.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
