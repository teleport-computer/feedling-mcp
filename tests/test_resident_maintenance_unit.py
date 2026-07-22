from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import resident_maintenance  # noqa: E402


def _fake_store():
    return SimpleNamespace(user_id="usr_unit", consumer_state_lock=threading.RLock())


def _patch_state(monkeypatch, state_box: dict) -> None:
    def mutate_state(_store, mutate):
        state = copy.deepcopy(state_box.get("state") or {})
        result = mutate(state)
        state_box["state"] = copy.deepcopy(state)
        return state, result

    monkeypatch.setattr(
        resident_maintenance.chat_consumer,
        "_mutate_consumer_state",
        mutate_state,
    )


def test_state_cas_exhaustion_fails_closed_before_message_append(monkeypatch):
    store = _fake_store()
    monkeypatch.setattr(
        resident_maintenance.onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        resident_maintenance.chat_consumer,
        "_mutate_consumer_state",
        lambda _store, _mutate: None,
    )
    appended: list[str] = []
    monkeypatch.setattr(
        resident_maintenance,
        "_append_maintenance_message",
        lambda *_args, **_kwargs: appended.append("message"),
    )

    result = resident_maintenance._maybe_handle_poll(
        store,
        {"official": True, "consumer_id": "vps-resident-c1"},
    )

    assert result == {"triggered": False, "reason": "state_update_conflict"}
    assert appended == []


def test_fallback_db_check_is_throttled(monkeypatch):
    store = _fake_store()
    state_box: dict = {
        "state": {
            "decrypt_status": "ok",
            "decrypt_checked_at_epoch": "1000000",
        }
    }
    _patch_state(monkeypatch, state_box)
    now = {"t": 1_000_000.0}
    calls: list[tuple[str, int]] = []

    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("FEEDLING_RESIDENT_FALLBACK_CHECK_SEC", "300")
    monkeypatch.setattr(resident_maintenance, "_now", lambda: now["t"])
    monkeypatch.setattr(resident_maintenance.onboarding, "_load_onboarding_route", lambda _store: "resident")

    def oldest(user_id: str, older_than_sec: int):
        calls.append((user_id, older_than_sec))
        return None

    monkeypatch.setattr(resident_maintenance, "_oldest_unclaimed_resident_job", oldest)
    info = {
        "official": True,
        "consumer_id": "vps-resident-c1",
        "consumer_commit": "abcdef1234567890",
    }

    assert resident_maintenance._maybe_handle_poll(store, info)["reason"] == "not_stale"
    assert calls == [("usr_unit", 15 * 60)]

    now["t"] += 299
    assert resident_maintenance._maybe_handle_poll(store, info)["reason"] == "fallback_check_skipped"
    assert calls == [("usr_unit", 15 * 60)]

    now["t"] += 1
    assert resident_maintenance._maybe_handle_poll(store, info)["reason"] == "not_stale"
    assert calls == [("usr_unit", 15 * 60), ("usr_unit", 15 * 60)]


def test_prompt_uses_deployment_repo_description_not_internal_clone_name(monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    prompt = resident_maintenance._prompt_for(
        {
            "reason": "missing_consumer_commit",
            "expected_commit": "abcdef1234567890",
            "actual_commit": "",
        },
        {"consumer_id": "vps-resident-c1"},
    )

    assert "feedling-mcp-test" not in prompt
    assert "仓库目录(包含 tools/chat_resident_consumer.py)" in prompt
    assert "tools/chat_resident_requirements.txt" in prompt
    assert "FEEDLING_AUTO_UPDATE" in prompt
    assert prompt.startswith("【Feedling 维护通知】(来自 Feedling 服务端,非用户本人发送)")
    assert prompt.endswith("expected_commit: abcdef1234567890")


def test_decrypt_prompt_uses_only_decrypt_source_steps(monkeypatch):
    prompt = resident_maintenance._prompt_for(
        {
            "reason": "decrypt_source_unavailable",
            "decrypt_status": "unreachable",
            "decrypt_reason": "decrypt_source_unreachable",
            "checked_at_epoch": 123.0,
        },
        {"consumer_id": "vps-resident-c1"},
    )

    assert "不需要带 API key" in prompt
    assert "curl -k -o /dev/null" in prompt
    assert "Authorization" not in prompt
    assert "python -m pip install" not in prompt
    assert "FEEDLING_AUTO_UPDATE" not in prompt
    assert prompt.endswith("decrypt_checked_at_epoch: 123.0")


def test_reason_key_excludes_heartbeat_and_deploy_commit_diagnostics():
    mismatch_a = {
        "reason": "consumer_commit_mismatch",
        "expected_commit": "deploy-a",
        "actual_commit": "old-a",
    }
    mismatch_b = {
        "reason": "consumer_commit_mismatch",
        "expected_commit": "deploy-b",
        "actual_commit": "old-b",
    }
    degraded_a = {
        "reason": "decrypt_source_unavailable",
        "decrypt_status": "degraded",
        "decrypt_reason": "decrypt_source_degraded",
        "checked_at_epoch": 100.0,
    }
    degraded_b = {**degraded_a, "checked_at_epoch": 200.0}

    assert resident_maintenance._reason_key(mismatch_a) == resident_maintenance._reason_key(mismatch_b)
    assert resident_maintenance._reason_key(degraded_a) == resident_maintenance._reason_key(degraded_b)


def test_decrypt_health_failure_warns_immediately_with_diagnostics(monkeypatch):
    store = _fake_store()
    state_box = {
        "state": {
            "decrypt_status": "unconfigured",
            "decrypt_checked_at_epoch": "1000000",
        }
    }
    _patch_state(monkeypatch, state_box)
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    monkeypatch.setattr(resident_maintenance, "_now", lambda: 1_000_001.0)
    monkeypatch.setattr(
        resident_maintenance.onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(resident_maintenance, "_fallback_reason", lambda _store: None)
    monkeypatch.setattr(
        resident_maintenance,
        "_append_maintenance_message",
        lambda *_args, **_kwargs: {"id": "decrypt-warning"},
    )
    emitted: dict = {}
    monkeypatch.setattr(
        resident_maintenance,
        "_emit_notice",
        lambda _store, **kwargs: emitted.update(kwargs),
    )
    info = {
        "official": True,
        "consumer_id": "vps-resident-c1",
        "consumer_commit": "abcdef1234567890",
        "decrypt_status": "unconfigured",
        "decrypt_checked_at_epoch": "1000000",
    }

    result = resident_maintenance._maybe_handle_poll(store, info)

    assert result["triggered"] is True
    assert result["reason"] == "decrypt_source_unavailable"
    assert emitted["reason"]["decrypt_status"] == "unconfigured"
    assert emitted["reason"]["checked_at_epoch"] == 1_000_000.0
    assert "FEEDLING_ENCLAVE_URL" in emitted["prompt"]
    assert "decrypt_status: unconfigured" in emitted["prompt"]


def test_decrypt_health_recovery_resolves_its_own_notice(monkeypatch):
    store = _fake_store()
    state_box = {
        "state": {
            "decrypt_status": "ok",
            "decrypt_checked_at_epoch": "1000000",
            "resident_maintenance": {"decrypt_health_active": True},
        }
    }
    _patch_state(monkeypatch, state_box)
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    monkeypatch.setattr(resident_maintenance, "_now", lambda: 1_000_001.0)
    monkeypatch.setattr(
        resident_maintenance.onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(resident_maintenance, "_fallback_reason", lambda _store: None)
    resolved: list[str] = []
    monkeypatch.setattr(
        resident_maintenance.notices_core,
        "resolve",
        lambda _store, key: resolved.append(key),
    )
    info = {
        "official": True,
        "consumer_id": "vps-resident-c1",
        "consumer_commit": "abcdef1234567890",
        "decrypt_status": "ok",
        "decrypt_checked_at_epoch": "1000000",
    }

    result = resident_maintenance._maybe_handle_poll(store, info)

    assert result == {"triggered": False, "reason": "decrypt_resolved"}
    assert resolved == [resident_maintenance.DECRYPT_DEDUPE_KEY]
    maintenance = state_box["state"]["resident_maintenance"]
    assert "decrypt_health_active" not in maintenance


def test_commit_recovery_resolves_stale_notice_while_decrypt_alert_continues(monkeypatch):
    store = _fake_store()
    state_box = {
        "state": {
            "decrypt_status": "unconfigured",
            "decrypt_checked_at_epoch": "1000000",
            "resident_maintenance": {
                "active_reason": "decrypt_source_unavailable:unconfigured",
                "commit_notice_active": True,
                "commit_mismatch_key": "abcdef1234567890:old1234567890",
                "commit_mismatch_since_epoch": 900_000.0,
            },
        }
    }
    _patch_state(monkeypatch, state_box)
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "abcdef1234567890")
    monkeypatch.setattr(resident_maintenance, "_now", lambda: 1_000_001.0)
    monkeypatch.setattr(
        resident_maintenance.onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(resident_maintenance, "_fallback_reason", lambda _store: None)
    monkeypatch.setattr(
        resident_maintenance,
        "_append_maintenance_message",
        lambda *_args, **_kwargs: {"id": "decrypt-warning"},
    )
    emitted: list[str] = []
    monkeypatch.setattr(
        resident_maintenance,
        "_emit_notice",
        lambda _store, **kwargs: emitted.append(str(kwargs["reason"]["reason"])),
    )
    resolved: list[str] = []
    monkeypatch.setattr(
        resident_maintenance.notices_core,
        "resolve",
        lambda _store, key: resolved.append(key),
    )
    info = {
        "official": True,
        "consumer_id": "vps-resident-c1",
        "consumer_commit": "abcdef1234567890",
    }

    result = resident_maintenance._maybe_handle_poll(store, info)

    assert result["triggered"] is True
    assert result["reason"] == "decrypt_source_unavailable"
    assert emitted == ["decrypt_source_unavailable"]
    assert resolved == [resident_maintenance.DEDUPE_KEY]
    maintenance = state_box["state"]["resident_maintenance"]
    assert "commit_notice_active" not in maintenance
