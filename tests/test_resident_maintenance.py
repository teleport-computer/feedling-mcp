from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import resident_maintenance  # noqa: E402


_HEADERS = {
    "X-Feedling-Consumer": "feedling-chat-resident",
    "X-Feedling-Consumer-Id": "vps-resident-c1",
    "X-Feedling-Consumer-Version": "resident-v1",
}


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={
            "public_key": base64.b64encode(b"\x71" * 32).decode("ascii"),
            "archive_language": "zh",
        },
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str, *, consumer_id: str = "vps-resident-c1", commit: str | None = None):
    out = {"X-API-Key": api_key, **_HEADERS, "X-Feedling-Consumer-Id": consumer_id}
    if commit is not None:
        out["X-Feedling-Consumer-Commit"] = commit
    return out


def _fake_shared_envelope(monkeypatch, captured: dict):
    def build(store, plaintext: bytes, *, item_id: str | None = None):
        captured["plaintext"] = plaintext.decode("utf-8")
        return {
            "v": 1,
            "id": item_id or "resident_maintenance_test",
            "body_ct": base64.b64encode(plaintext).decode("ascii"),
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "enclave-fpr",
            "content_pk_fpr": "user-fpr",
        }, ""

    monkeypatch.setattr(resident_maintenance.core_envelope, "_build_shared_envelope_for_store", build)


def _env(user_id: str, msg_id: str) -> dict:
    return {
        "v": 1,
        "id": msg_id,
        "body_ct": base64.b64encode(f"{user_id}:{msg_id}".encode("utf-8")).decode("ascii"),
        "nonce": "nonce",
        "K_user": "ku",
        "K_enclave": "ke",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def test_missing_consumer_commit_injects_claimable_shared_maintenance_message(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "abcdef1234567890")
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)

    first = client.get("/v1/chat/poll?timeout=0", headers=_headers(api_key))
    assert first.status_code == 200, first.get_data(as_text=True)
    assert first.get_json()["messages"] == []

    now["t"] += 15 * 60 + 1
    second = client.get("/v1/chat/poll?timeout=0", headers=_headers(api_key))
    assert second.status_code == 200, second.get_data(as_text=True)
    messages = second.get_json()["messages"]
    assert len(messages) == 1
    msg = messages[0]
    assert msg["role"] == "user"
    assert msg["source"] == resident_maintenance.SOURCE
    assert msg["content"] == captured["plaintext"]
    assert msg["visibility"] == "shared"
    assert msg["K_enclave"] == "ke"
    assert msg["reply_claimed_by"] == "vps-resident-c1"
    assert "tools/chat_resident_requirements.txt" in captured["plaintext"]
    assert "FEEDLING_AUTO_UPDATE" in captured["plaintext"]

    notices = client.get("/v1/notices", headers={"X-API-Key": api_key}).get_json()["notices"]
    notice = next(n for n in notices if n["dedupe_key"] == resident_maintenance.DEDUPE_KEY)
    assert notice["error_class"] == "resident_consumer_stale"
    assert notice["blame"] == "user_environment"
    assert notice["severity"] == "warning"
    assert notice["copyable_prompt"] == captured["plaintext"]

    rows = [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]
    assert len(rows) == 1
    assert rows[0]["content"] == captured["plaintext"]


def test_consumer_commit_mismatch_waits_for_floor_before_prompting(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("FEEDLING_RESIDENT_COMMIT_MISMATCH_GRACE_SEC", "1")
    now = {"t": 2_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    headers = _headers(api_key, commit="1111111222222222")

    first = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert first.status_code == 200
    assert first.get_json()["messages"] == []

    now["t"] += 3599
    early = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert early.status_code == 200
    assert early.get_json()["messages"] == []

    now["t"] += 2
    late = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert late.status_code == 200
    messages = late.get_json()["messages"]
    assert [m["source"] for m in messages] == [resident_maintenance.SOURCE]
    assert "consumer_commit_mismatch" in captured["plaintext"]
    assert len([m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]) == 1


def test_hosted_agent_runner_poll_is_exempt_from_resident_maintenance(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "abcdef1234567890")
    now = {"t": 3_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)

    db.set_blob(user_id, "consumer_state", {
        "resident_maintenance": {"missing_commit_since_epoch": now["t"] - 3600}
    })
    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, consumer_id=f"agent-runner:{user_id}"),
    )
    assert poll.status_code == 200
    assert poll.get_json()["messages"] == []
    assert "plaintext" not in captured
    assert not [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]


def test_unclaimed_resident_job_active_poll_warns_without_failing_job(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    now = {"t": 4_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    db.genesis_create_job(user_id, {
        "job_id": "genesis_waiting_for_old_consumer",
        "status": "awaiting_resident",
        "source_kind": "update_identity",
        "privacy_mode": "resident_sealed",
    })
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE genesis_import_jobs SET updated_at = now() - make_interval(secs => %s) "
            "WHERE user_id = %s AND job_id = %s",
            (1000, user_id, "genesis_waiting_for_old_consumer"),
        )

    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="abcdef1234567890"),
    )

    assert poll.status_code == 200, poll.get_data(as_text=True)
    messages = poll.get_json()["messages"]
    assert [m["source"] for m in messages] == [resident_maintenance.SOURCE]
    assert "awaiting_resident_unclaimed" in captured["plaintext"]
    assert db.genesis_get_job(user_id, "genesis_waiting_for_old_consumer")["status"] == "awaiting_resident"


def test_resident_maintenance_reminder_is_rate_limited(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "abcdef1234567890")
    now = {"t": 5_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    db.set_blob(user_id, "consumer_state", {
        "resident_maintenance": {"missing_commit_since_epoch": now["t"] - 3600}
    })

    first = client.get("/v1/chat/poll?timeout=0", headers=_headers(api_key))
    assert first.status_code == 200
    now["t"] += 60
    second = client.get("/v1/chat/poll?timeout=0", headers=_headers(api_key, consumer_id="vps-resident-c2"))
    assert second.status_code == 200

    rows = [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]
    assert len(rows) == 1
    notices = [n for n in db.log_read_all(user_id, "user_notices") if n["dedupe_key"] == resident_maintenance.DEDUPE_KEY]
    assert len(notices) == 1
    assert notices[0]["occurrences"] == 1


def test_resident_maintenance_reply_source_is_accepted_before_live_chat_gate(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "abcdef1234567890")
    now = {"t": 6_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    db.set_blob(user_id, "consumer_state", {
        "resident_maintenance": {"missing_commit_since_epoch": now["t"] - 3600}
    })
    poll = client.get("/v1/chat/poll?timeout=0", headers=_headers(api_key))
    maintenance_id = poll.get_json()["messages"][0]["id"]

    reply = client.post(
        "/v1/chat/response",
        headers=_headers(api_key),
        json={
            "envelope": _env(user_id, "resident_maintenance_reply_1"),
            "source": resident_maintenance.SOURCE,
            "reply_to_message_id": maintenance_id,
            "alert_body": "maintenance handled",
            "push_live_activity": True,
        },
    )

    assert reply.status_code == 200, reply.get_data(as_text=True)
    rows = {m["id"]: m for m in db.chat_load(user_id)}
    assert rows["resident_maintenance_reply_1"]["source"] == resident_maintenance.SOURCE
    assert rows[maintenance_id]["reply_status"] == "replied"
