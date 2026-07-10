import os, psycopg
from tee_shadow import mirror


def _tee(sql):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql).fetchall()


def test_mirror_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    mirror.execute("INSERT INTO server_config (key, value) VALUES (%s, %s)", ("k1", b"v"))
    assert _tee("SELECT count(*) FROM server_config WHERE key='k1'")[0][0] == 0


def test_mirror_writes_when_enabled(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    mirror.execute(
        "INSERT INTO server_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k2", b"v"))
    assert _tee("SELECT count(*) FROM server_config WHERE key='k2'")[0][0] == 1


def test_mirror_swallows_failure_and_counts(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    before = mirror.failure_count()
    mirror.execute("INSERT INTO no_such_table VALUES (1)")  # 必须不 raise
    assert mirror.failure_count() == before + 1


def test_mirror_execute_many_atomic_and_counts_failure(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")

    # Group applied atomically when enabled.
    mirror.execute_many([
        ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k3", b"v")),
        ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k4", b"v")),
    ])
    assert _tee("SELECT count(*) FROM server_config WHERE key IN ('k3', 'k4')")[0][0] == 2

    # A failing group leaves no partial writes and increments failure_count by 1.
    before = mirror.failure_count()
    mirror.execute_many([
        ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k5", b"v")),
        ("INSERT INTO no_such_table VALUES (1)", ()),
    ])
    assert mirror.failure_count() == before + 1
    assert _tee("SELECT count(*) FROM server_config WHERE key='k5'")[0][0] == 0
