from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from content import plaintext_migration  # noqa: E402
import migrate_user_content_to_plaintext as cli  # noqa: E402


def test_cli_requires_an_exact_user_before_accessing_data(monkeypatch, capsys):
    monkeypatch.setattr(
        plaintext_migration,
        "run",
        lambda *_a, **_kw: pytest.fail("argument gate must precede data access"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2
    assert "--user" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra_args", "enable_env"),
    [
        ([], False),
        (["--allow-plaintext-rewrite"], False),
        ([], True),
    ],
)
def test_apply_requires_both_independent_write_gates(
    monkeypatch, capsys, extra_args, enable_env
):
    if enable_env:
        monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    else:
        monkeypatch.delenv(plaintext_migration.APPLY_ENV, raising=False)
    monkeypatch.setattr(
        plaintext_migration,
        "run",
        lambda *_a, **_kw: pytest.fail("write gate must precede data access"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["--user", "usr_target", "--apply", *extra_args])

    assert exc.value.code == 2
    assert "requires" in capsys.readouterr().err


def test_dry_run_does_not_construct_decryptor(monkeypatch, capsys):
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda user_id: [
            plaintext_migration.Item(
                surface="chat_live",
                item_id="msg-1",
                classification="migratable_shared",
            )
        ],
    )
    monkeypatch.setattr(
        plaintext_migration,
        "make_decrypt",
        lambda *_a, **_kw: pytest.fail("dry-run must not construct decryptor"),
    )

    assert cli.main(["--user", "usr_target", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "apply": False,
        "counts": {"migratable_shared": 1},
        "failures": 0,
        "user_id": "usr_target",
    }


@pytest.mark.parametrize("preference", [None, "on", ""])
def test_apply_rejects_any_preference_other_than_explicit_off(
    monkeypatch, capsys, preference
):
    monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    monkeypatch.setattr(
        plaintext_migration, "content_encryption_preference", lambda _uid: preference
    )
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda *_a, **_kw: pytest.fail("preference gate must precede inventory"),
    )

    assert cli.main(
        [
            "--user",
            "usr_target",
            "--apply",
            "--allow-plaintext-rewrite",
        ]
    ) == 2
    assert "explicitly off" in capsys.readouterr().err


def test_apply_gate_accepts_explicit_off_without_exposing_items(monkeypatch, capsys):
    monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    monkeypatch.setattr(
        plaintext_migration, "content_encryption_preference", lambda _uid: "off"
    )
    monkeypatch.setattr(plaintext_migration, "inventory", lambda _uid: [])

    assert cli.main(
        [
            "--user",
            "usr_target",
            "--apply",
            "--allow-plaintext-rewrite",
            "--json",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {}
    assert set(report) == {"apply", "counts", "failures", "user_id"}
