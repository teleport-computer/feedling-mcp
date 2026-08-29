"""Guard: teardown must attempt EVERY throwaway database, and say so when it fails.

The bug this pins: ``pytest_unconfigure`` used to wrap the whole ``for`` loop in
one ``try/except Exception: pass``. When the first ``DROP DATABASE`` was refused
(``ObjectInUse`` — a subprocess backend outlived the session), the exception left
the loop entirely, so the *second* database was never attempted at all, the
refusal printed nothing, and the admin connection was never closed. A failed
teardown and a successful one were byte-identical on the terminal.

Pure: no DB, no network. The fake admin connection records statements instead of
executing them.
"""

import sys
from pathlib import Path

import pytest

# Self-contained sys.path bootstrap. This file imports nothing from backend/,
# but conftest's autouse ``_disable_setup_auto_vision_probe`` does
# (``from hosted import setup_core``), and conftest only adds backend/ to
# sys.path inside its DB-provisioning try-block — which is exactly the branch
# that does not run on the no-Postgres machine this file is written for.
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_CONFTEST_PATH = str(Path(__file__).with_name("conftest.py"))


def _live_conftest():
    """The conftest module pytest already loaded — never re-import it.

    Importing ``tests/conftest.py`` a second time would re-run its module-level
    provisioning block and create two more throwaway databases.
    """
    for module in list(sys.modules.values()):
        if getattr(module, "__file__", None) == _CONFTEST_PATH:
            return module
    raise AssertionError(f"conftest module not loaded from {_CONFTEST_PATH}")


class _FakeAdmin:
    """Records every statement; raises on the DROP of ``fail_on``."""

    def __init__(self, fail_on=None, exc=None):
        self.statements = []
        self.closed = False
        self._fail_on = fail_on
        self._exc = exc or RuntimeError("database is being accessed by other users")

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if self._fail_on and sql.startswith("DROP DATABASE") and self._fail_on in sql:
            raise self._exc

    def close(self):
        self.closed = True


def _dropped(admin):
    return [sql for sql, _ in admin.statements if sql.startswith("DROP DATABASE")]


def _terminated(admin):
    return [params[0] for sql, params in admin.statements if "pg_terminate_backend" in sql]


def test_second_database_is_dropped_after_the_first_one_is_refused(capsys):
    conftest = _live_conftest()
    admin = _FakeAdmin(fail_on="db_one")

    conftest._drop_throwaway_databases(admin, ("db_one", "db_two"))

    # The load-bearing assertion: the refusal on db_one must not consume db_two.
    assert _terminated(admin) == ["db_one", "db_two"]
    assert [s for s in _dropped(admin) if "db_two" in s], (
        "db_two was never attempted — the failure on db_one escaped its iteration"
    )

    err = capsys.readouterr().err
    assert "db_one" in err
    assert "RuntimeError" in err
    assert "database is being accessed by other users" in err
    assert "db_two" not in err, "db_two succeeded; it must not be reported as leaked"


def test_every_failure_is_reported_not_swallowed(capsys):
    conftest = _live_conftest()
    admin = _FakeAdmin(fail_on="db_")  # both names contain it -> both refused

    conftest._drop_throwaway_databases(admin, ("db_one", "db_two"))

    assert _terminated(admin) == ["db_one", "db_two"]
    err = capsys.readouterr().err
    assert err.count("could not drop") == 2
    assert "db_one" in err and "db_two" in err


def test_successful_teardown_stays_silent(capsys):
    conftest = _live_conftest()
    admin = _FakeAdmin()

    conftest._drop_throwaway_databases(admin, ("db_one", "db_two"))

    assert len(_dropped(admin)) == 2
    assert capsys.readouterr().err == ""


def test_unconfigure_closes_the_connection_even_when_a_drop_fails(monkeypatch, capsys):
    """``pytest_unconfigure`` wiring: both names attempted, connection closed."""
    conftest = _live_conftest()
    psycopg = pytest.importorskip("psycopg")
    admin = _FakeAdmin(fail_on="fake_test_db")

    monkeypatch.setattr(conftest, "_provisioned", True)
    monkeypatch.setattr(conftest, "_TEST_DB", "fake_test_db")
    monkeypatch.setattr(conftest, "_TEE_DB", "fake_tee_db")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: admin)

    conftest.pytest_unconfigure(config=None)

    assert _terminated(admin) == ["fake_test_db", "fake_tee_db"]
    assert [s for s in _dropped(admin) if "fake_tee_db" in s]
    assert admin.closed, "admin connection leaked on the failure path"
    assert "fake_test_db" in capsys.readouterr().err


def test_unconfigure_reports_an_unreachable_admin_connection(monkeypatch, capsys):
    conftest = _live_conftest()
    psycopg = pytest.importorskip("psycopg")

    def _refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(conftest, "_provisioned", True)
    monkeypatch.setattr(conftest, "_TEST_DB", "fake_test_db")
    monkeypatch.setattr(conftest, "_TEE_DB", "fake_tee_db")
    monkeypatch.setattr(psycopg, "connect", _refuse)

    conftest.pytest_unconfigure(config=None)

    err = capsys.readouterr().err
    assert "OSError" in err
    assert "fake_test_db" in err and "fake_tee_db" in err
    # The admin URL carries a password — it must not reach the terminal.
    assert "postgres:" not in err


def test_unconfigure_is_a_noop_when_provisioning_never_succeeded(monkeypatch, capsys):
    conftest = _live_conftest()
    psycopg = pytest.importorskip("psycopg")

    def _explode(*args, **kwargs):
        raise AssertionError("must not connect when provisioning failed")

    monkeypatch.setattr(conftest, "_provisioned", False)
    monkeypatch.setattr(psycopg, "connect", _explode)

    conftest.pytest_unconfigure(config=None)

    assert capsys.readouterr().err == ""
