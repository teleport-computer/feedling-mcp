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
    def load(_store):
        return copy.deepcopy(state_box.get("state") or {})

    def save(_store, state):
        state_box["state"] = copy.deepcopy(state)

    monkeypatch.setattr(resident_maintenance.chat_consumer, "_load_consumer_state", load)
    monkeypatch.setattr(resident_maintenance.chat_consumer, "_save_consumer_state", save)


def test_fallback_db_check_is_throttled(monkeypatch):
    store = _fake_store()
    state_box: dict = {"state": {}}
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
    assert "包含 tools/chat_resident_consumer.py 的仓库目录" in prompt
    assert "tools/chat_resident_requirements.txt" in prompt
    assert "FEEDLING_AUTO_UPDATE" in prompt
