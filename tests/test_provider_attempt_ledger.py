from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_attempt_ledger  # noqa: E402
from diagnostics import diagnostics_core  # noqa: E402


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, tb):
        return False


def test_record_attempts_assigns_numbers_server_side(monkeypatch):
    stored = []

    def fake_append(user_id, stream, doc, *, number_field, ts, item_key):
        row = dict(doc)
        row[number_field] = len(stored) + 1
        stored.append((user_id, stream, item_key, row))
        return row

    monkeypatch.setattr(
        provider_attempt_ledger.db,
        "log_append_numbered",
        fake_append,
    )
    body, status = provider_attempt_ledger.record_attempts_payload(
        SimpleNamespace(user_id="usr_1"),
        {
            "provider_attempts": [
                {
                    "parent_message_id": "msg_1",
                    "attempt_n": 999,
                    "trigger": "first",
                    "provider_request_id": "req_1",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                    "outcome": "ok",
                    "ts": 10,
                },
                {
                    "parent_message_id": "msg_1",
                    "trigger": "stream_cut_retry",
                    "usage": {"input_tokens": None, "output_tokens": None},
                    "outcome": "stream cut",
                    "ts": 11,
                },
            ]
        },
    )

    assert status == 200
    assert body == {"status": "ok", "recorded": 2, "attempt_n": [1, 2]}
    assert [entry[3]["attempt_n"] for entry in stored] == [1, 2]
    assert stored[1][3]["outcome"] == "stream_cut"


def test_record_attempts_rejects_invalid_trigger(monkeypatch):
    monkeypatch.setattr(
        provider_attempt_ledger.db,
        "log_append_numbered",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    body, status = provider_attempt_ledger.record_attempts_payload(
        SimpleNamespace(user_id="usr_1"),
        {
            "provider_attempt": {
                "parent_message_id": "msg_1",
                "trigger": "automatic_retry",
                "outcome": "ok",
            }
        },
    )

    assert status == 400
    assert body == {"error": "invalid_trigger"}


def test_record_runtime_attempt_accepts_hosted_model_api_probe(monkeypatch):
    stored = []

    def fake_append(user_id, stream, doc, *, number_field, ts, item_key):
        stored.append((user_id, stream, item_key, dict(doc)))
        return {**doc, number_field: 1}

    monkeypatch.setattr(
        provider_attempt_ledger.db,
        "log_append_numbered",
        fake_append,
    )

    assert provider_attempt_ledger.record_runtime_attempt(
        "usr_setup",
        parent_key="model_api_probe:abc",
        trigger="model_api_probe",
        outcome="ok",
        provider="anthropic",
        model="claude-sonnet-4-5",
        lane="setup",
        runtime="hosted_setup",
        input_tokens=11,
        output_tokens=3,
        total_tokens=14,
        provider_request_id="req_1",
    ) is True

    assert len(stored) == 1
    user_id, stream, item_key, doc = stored[0]
    assert (user_id, stream, item_key) == (
        "usr_setup",
        "provider_attempts",
        "model_api_probe:abc",
    )
    assert doc["trigger"] == "model_api_probe"
    assert doc["runtime"] == "hosted_setup"
    assert doc["lane"] == "setup"
    assert doc["usage"] == {
        "input_tokens": 11,
        "output_tokens": 3,
        "total_tokens": 14,
    }
    assert doc["provider_request_id"] == "req_1"


def test_diagnostics_payload_routes_ledger_without_trace_ring(monkeypatch):
    monkeypatch.setattr(
        diagnostics_core.debug_trace,
        "trace_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider attempts must not enter the debug trace store")
        ),
    )
    monkeypatch.setattr(
        diagnostics_core.provider_attempt_ledger,
        "record_attempts_payload",
        lambda store, payload: ({"status": "ok", "recorded": 1}, 200),
    )

    body, status = diagnostics_core.emit_trace_event_payload(
        SimpleNamespace(user_id="usr_1"),
        {"provider_attempt": {"parent_message_id": "msg_1"}},
    )

    assert status == 200
    assert body == {"status": "ok", "recorded": 1}


def test_log_append_numbered_assigns_before_append_and_mirrors(monkeypatch):
    executed = []
    fetches = iter([(2,), (91,)])

    class Cursor:
        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchone(self):
            return next(fetches)

    class Connection:
        def transaction(self):
            return _Context(None)

        def cursor(self):
            return _Context(Cursor())

    class Pool:
        def connection(self):
            return _Context(Connection())

    mirrored = []
    monkeypatch.setattr(provider_attempt_ledger.db, "get_pool", lambda: Pool())
    from tee_shadow import mirror

    monkeypatch.setattr(
        mirror,
        "execute",
        lambda sql, params: mirrored.append((sql, params)),
    )

    stored = provider_attempt_ledger.db.log_append_numbered(
        "usr_1",
        "provider_attempts",
        {"parent_message_id": "msg_1"},
        number_field="attempt_n",
        ts=12.0,
        item_key="msg_1",
    )

    assert stored == {"parent_message_id": "msg_1", "attempt_n": 3}
    assert "pg_advisory_xact_lock" in executed[0][0]
    assert "COUNT(*)" in executed[1][0]
    assert "INSERT INTO user_logs" in executed[2][0]
    assert executed[2][1][4].obj["attempt_n"] == 3
    assert mirrored[0][1][2] == 91
    assert mirrored[0][1][5].obj["attempt_n"] == 3
