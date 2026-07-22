from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import resident_maintenance  # noqa: E402
from core import store as core_store  # noqa: E402


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


def _headers(
    api_key: str,
    *,
    consumer_id: str = "vps-resident-c1",
    commit: str | None = None,
    compat_commit: str | None = None,
    decrypt_status: str | None = "ok",
):
    out = {
        "X-API-Key": api_key,
        **_HEADERS,
        "X-Feedling-Consumer-Id": consumer_id,
    }
    if decrypt_status is not None:
        out["X-Feedling-Decrypt-Status"] = decrypt_status
        out["X-Feedling-Decrypt-Checked-At"] = str(resident_maintenance._now())
    if commit is not None:
        out["X-Feedling-Consumer-Commit"] = commit
    if compat_commit is not None:
        out["X-Feedling-Consumer-Compat-Commit"] = compat_commit
    return out


@pytest.mark.parametrize("decrypt_status", ["unconfigured", "unreachable"])
def test_new_explicit_decrypt_failure_alerts_on_first_poll(
    backend_env,
    monkeypatch,
    decrypt_status,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    now = {"t": 5_000_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)

    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(
            api_key,
            commit="abcdef1234567890",
            decrypt_status=decrypt_status,
        ),
    )

    assert poll.status_code == 200, poll.get_data(as_text=True)
    messages = poll.get_json()["messages"]
    assert [m["source"] for m in messages] == [resident_maintenance.SOURCE]
    assert "decrypt_source_unavailable" in captured["plaintext"]
    assert f"decrypt_status: {decrypt_status}" in captured["plaintext"]
    assert "FEEDLING_ENCLAVE_URL" in captured["plaintext"]

    notices = client.get(
        "/v1/notices", headers={"X-API-Key": api_key}
    ).get_json()["notices"]
    notice = next(
        n for n in notices
        if n["dedupe_key"] == resident_maintenance.DECRYPT_DEDUPE_KEY
    )
    assert notice["error_class"] == resident_maintenance.DECRYPT_ERROR_CLASS
    assert f"status={decrypt_status}" in notice["detail"]
    assert len(
        [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]
    ) == 1


def test_established_unknown_is_notice_only_and_never_uses_decrypt_copy(
    backend_env,
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    now = {"t": 5_100_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    core_store.get_store(user_id).mark_first_chat_ok(at_iso="2026-07-01T00:00:00")

    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(
            api_key,
            commit="abcdef1234567890",
            decrypt_status=None,
        ),
    )

    assert poll.status_code == 200, poll.get_data(as_text=True)
    assert poll.get_json()["messages"] == []
    assert "plaintext" not in captured
    notices = client.get(
        "/v1/notices", headers={"X-API-Key": api_key}
    ).get_json()["notices"]
    notice = next(
        n
        for n in notices
        if n["dedupe_key"] == resident_maintenance.DECRYPT_UNKNOWN_DEDUPE_KEY
    )
    assert notice["error_class"] == resident_maintenance.DECRYPT_UNKNOWN_ERROR_CLASS
    assert "copyable_prompt" not in notice
    assert "解密源不可用" not in notice["user_text"]
    assert not [
        m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE
    ]


def test_established_explicit_failure_waits_30m_then_injects_once(
    backend_env,
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("FEEDLING_RESIDENT_DECRYPT_FAILURE_GRACE_SEC", "1800")
    now = {"t": 5_200_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    core_store.get_store(user_id).mark_first_chat_ok(at_iso="2026-07-01T00:00:00")

    first = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="abcdef1234567890", decrypt_status="degraded"),
    )
    assert first.status_code == 200
    assert first.get_json()["messages"] == []
    assert "plaintext" not in captured

    now["t"] += 1799
    early = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="abcdef1234567890", decrypt_status="degraded"),
    )
    assert early.status_code == 200
    assert early.get_json()["messages"] == []

    now["t"] += 2
    late = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="abcdef1234567890", decrypt_status="degraded"),
    )
    assert late.status_code == 200
    assert [m["source"] for m in late.get_json()["messages"]] == [resident_maintenance.SOURCE]
    assert "decrypt_status: degraded" in captured["plaintext"]
    assert "python -m pip install" not in captured["plaintext"]

    for _ in range(3):
        now["t"] += 120
        repeat = client.get(
            "/v1/chat/poll?timeout=0",
            headers=_headers(
                api_key,
                commit="abcdef1234567890",
                decrypt_status="degraded",
            ),
        )
        assert repeat.status_code == 200
    rows = [
        m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE
    ]
    assert len(rows) == 1
    state = db.get_blob(user_id, "consumer_state")
    assert state["resident_maintenance"]["last_reason_key"] == (
        "decrypt_source_unavailable:degraded:decrypt_source_degraded"
    )


def test_explicit_failure_recovery_resolves_notice_without_injection(
    backend_env,
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    now = {"t": 5_300_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    core_store.get_store(user_id).mark_first_chat_ok(at_iso="2026-07-01T00:00:00")

    failed = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="abcdef1234567890", decrypt_status="unreachable"),
    )
    assert failed.status_code == 200
    assert failed.get_json()["messages"] == []
    now["t"] += 10
    recovered = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="abcdef1234567890", decrypt_status="ok"),
    )
    assert recovered.status_code == 200
    assert recovered.get_json()["messages"] == []
    notices = client.get(
        "/v1/notices", headers={"X-API-Key": api_key}
    ).get_json()["notices"]
    notice = next(
        n for n in notices if n["dedupe_key"] == resident_maintenance.DECRYPT_DEDUPE_KEY
    )
    assert notice["resolved"] is True
    assert "plaintext" not in captured


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
    headers["X-Feedling-Decrypt-Checked-At"] = str(now["t"])
    early = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert early.status_code == 200
    assert early.get_json()["messages"] == []

    now["t"] += 2
    headers["X-Feedling-Decrypt-Checked-At"] = str(now["t"])
    late = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert late.status_code == 200
    messages = late.get_json()["messages"]
    assert [m["source"] for m in messages] == [resident_maintenance.SOURCE]
    assert "consumer_commit_mismatch" in captured["plaintext"]
    assert len([m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]) == 1


def test_matching_compat_commit_suppresses_mismatch_and_clears_old_state(
    backend_env,
    monkeypatch,
):
    expected = "abcdef1234567890"
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", expected)
    monkeypatch.setenv("FEEDLING_RESIDENT_COMMIT_MISMATCH_GRACE_SEC", "3600")
    now = {"t": 2_050_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    db.set_blob(
        user_id,
        "consumer_state",
        {
            "resident_maintenance": {
                "active_reason": "consumer_commit_mismatch",
                "commit_notice_active": True,
                "commit_mismatch_key": f"{expected}:oldoldold1234",
                "commit_mismatch_since_epoch": now["t"] - 7200,
            }
        },
    )

    for advance in (0, 3601):
        now["t"] += advance
        poll = client.get(
            "/v1/chat/poll?timeout=0",
            headers=_headers(
                api_key,
                commit="oldoldold1234",
                compat_commit="abcdef1",
            ),
        )
        assert poll.status_code == 200
        assert poll.get_json()["messages"] == []

    assert "plaintext" not in captured
    state = db.get_blob(user_id, "consumer_state")
    assert state["consumer_compat_commit"] == "abcdef1"
    maintenance = state["resident_maintenance"]
    assert "commit_mismatch_key" not in maintenance
    assert "commit_mismatch_since_epoch" not in maintenance
    assert "commit_notice_active" not in maintenance
    assert "active_reason" not in maintenance
    validation = resident_maintenance.chat_consumer._consumer_validation_state(
        core_store.get_store(user_id),
    )
    assert validation["consumer_compat_commit"] == "abcdef1"
    assert not [
        m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE
    ]


def test_stale_compat_commit_does_not_mask_new_expected_commit(
    backend_env,
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "targetaaaaaaa")
    monkeypatch.setenv("FEEDLING_RESIDENT_COMMIT_MISMATCH_GRACE_SEC", "3600")
    now = {"t": 2_075_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)

    compatible = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(
            api_key,
            commit="runningold111",
            compat_commit="targetaaaaaaa",
        ),
    )
    assert compatible.status_code == 200
    assert compatible.get_json()["messages"] == []

    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "targetbbbbbbb")
    starts_grace = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(
            api_key,
            commit="runningold111",
            compat_commit="targetaaaaaaa",
        ),
    )
    assert starts_grace.status_code == 200
    assert starts_grace.get_json()["messages"] == []

    now["t"] += 3601
    warned = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(
            api_key,
            commit="runningold111",
            compat_commit="targetaaaaaaa",
        ),
    )
    assert warned.status_code == 200
    assert [m["source"] for m in warned.get_json()["messages"]] == [
        resident_maintenance.SOURCE
    ]
    assert "consumer_commit_mismatch" in captured["plaintext"]
    assert len(
        [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]
    ) == 1


def test_compat_commit_cannot_mask_missing_running_commit(backend_env, monkeypatch):
    expected = "abcdef1234567890"
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", expected)
    now = {"t": 2_090_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)

    first = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, compat_commit=expected),
    )
    assert first.status_code == 200
    assert first.get_json()["messages"] == []

    now["t"] += 15 * 60 + 1
    warned = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, compat_commit=expected),
    )
    assert warned.status_code == 200
    assert [m["source"] for m in warned.get_json()["messages"]] == [
        resident_maintenance.SOURCE
    ]
    assert "missing_consumer_commit" in captured["plaintext"]
    assert len(
        [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]
    ) == 1


def test_two_backend_deploys_do_not_bypass_24h_reminder_interval(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "deployaaaaaaaa")
    monkeypatch.setenv("FEEDLING_RESIDENT_COMMIT_MISMATCH_GRACE_SEC", "3600")
    now = {"t": 2_100_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    headers = _headers(api_key, commit="residentold111")

    first = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert first.status_code == 200
    now["t"] += 3601
    headers["X-Feedling-Decrypt-Checked-At"] = str(now["t"])
    warned = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert warned.status_code == 200
    assert [m["source"] for m in warned.get_json()["messages"]] == [
        resident_maintenance.SOURCE
    ]

    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "deploybbbbbbbb")
    deploy_change = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert deploy_change.status_code == 200
    now["t"] += 3601
    headers["X-Feedling-Decrypt-Checked-At"] = str(now["t"])
    second_matured = client.get("/v1/chat/poll?timeout=0", headers=headers)
    assert second_matured.status_code == 200

    rows = [
        m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE
    ]
    assert len(rows) == 1
    state = db.get_blob(user_id, "consumer_state")["resident_maintenance"]
    assert state["last_reason_key"] == "consumer_commit_mismatch"


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
        headers=_headers(
            api_key,
            consumer_id=f"agent-runner:{user_id}",
            commit="oldoldold",
            compat_commit="abcdef1234567890",
        ),
    )
    assert poll.status_code == 200
    assert poll.get_json()["messages"] == []
    assert "plaintext" not in captured
    assert not [m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE]


def test_non_resident_route_is_exempt_from_resident_maintenance(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    now = {"t": 3_100_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    db.set_blob(user_id, "onboarding_route", {"route": "model_api"})

    poll = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(api_key, commit="oldoldold", decrypt_status="unreachable"),
    )

    assert poll.status_code == 200
    assert poll.get_json()["messages"] == []
    assert "plaintext" not in captured
    assert not [
        m for m in db.chat_load(user_id) if m.get("source") == resident_maintenance.SOURCE
    ]


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


def test_maintenance_append_is_idempotent_in_bucket_and_allows_next_bucket(
    backend_env,
    monkeypatch,
):
    client = make_client()
    user_id, _api_key = _register(client)
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    store = core_store.get_store(user_id)
    notifications: list[str] = []
    monkeypatch.setattr(
        store,
        "notify_chat_waiters",
        lambda: notifications.append("wake"),
    )
    prompt = "stable maintenance prompt"
    interval = 24 * 60 * 60
    first_now = 8_000_000.0
    first_id = resident_maintenance._message_id(store, prompt, now=first_now)

    first = resident_maintenance._append_maintenance_message(
        store,
        prompt=prompt,
        msg_id=first_id,
    )
    db.chat_update_metadata(
        user_id,
        first_id,
        {"reply_status": "replied", "reply_message_id": "maintenance-reply-1"},
    )
    duplicate = resident_maintenance._append_maintenance_message(
        store,
        prompt=prompt,
        msg_id=first_id,
    )

    assert duplicate["id"] == first["id"] == first_id
    assert duplicate["reply_status"] == "replied"
    assert duplicate["reply_message_id"] == "maintenance-reply-1"
    rows = [
        row
        for row in db.chat_load(user_id)
        if row.get("source") == resident_maintenance.SOURCE
    ]
    assert [row["id"] for row in rows] == [first_id]
    assert rows[0]["reply_status"] == "replied"
    assert notifications == ["wake"], "only the atomic insert winner may notify"

    next_id = resident_maintenance._message_id(
        store,
        prompt,
        now=first_now + interval + 1,
    )
    assert next_id != first_id
    resident_maintenance._append_maintenance_message(
        store,
        prompt=prompt,
        msg_id=next_id,
    )
    rows = [
        row
        for row in db.chat_load(user_id)
        if row.get("source") == resident_maintenance.SOURCE
    ]
    assert {row["id"] for row in rows} == {first_id, next_id}
    assert notifications == ["wake", "wake"]


def test_reason_flip_does_not_bypass_global_reminder_interval(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    now = {"t": 8_500_000.0}
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    captured: dict = {}
    _fake_shared_envelope(monkeypatch, captured)
    client = make_client()
    user_id, api_key = _register(client)
    db.set_blob(
        user_id,
        "consumer_state",
        {
            "decrypt_status": "ok",
            "decrypt_checked_at_epoch": str(now["t"]),
            "resident_maintenance": {
                "missing_commit_since_epoch": now["t"] - 3600,
            },
        },
    )

    first = client.get("/v1/chat/poll?timeout=0", headers=_headers(api_key))
    assert first.status_code == 200
    assert len(
        [
            row
            for row in db.chat_load(user_id)
            if row.get("source") == resident_maintenance.SOURCE
        ]
    ) == 1

    now["t"] += 60
    second = client.get(
        "/v1/chat/poll?timeout=0",
        headers=_headers(
            api_key,
            commit="abcdef1234567890",
            decrypt_status="unconfigured",
        ),
    )
    assert second.status_code == 200
    rows = [
        row
        for row in db.chat_load(user_id)
        if row.get("source") == resident_maintenance.SOURCE
    ]
    assert len(rows) == 1
    state = db.get_blob(user_id, "consumer_state")["resident_maintenance"]
    assert state["active_reason"].startswith("decrypt_source_unavailable")
    assert state["last_reminder_epoch"] == now["t"] - 60


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
