"""Resident onboarding/gate integration for decrypt-health policy."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from bootstrap import gates as boot_gates  # noqa: E402
from chat import consumer as chat_consumer  # noqa: E402
from hosted import onboarding_validation  # noqa: E402


class _Store:
    user_id = "usr_onboarding_decrypt"

    def __init__(self, *, established: bool = False):
        self.chat_lock = threading.RLock()
        self.chat_messages: list[dict] = []
        self._established = established

    def first_chat_ok_at(self) -> str:
        return "2026-07-20T00:00:00" if self._established else ""


def _health(status: str, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if status == "unknown":
        return chat_consumer._decrypt_health_from_state(
            {"decrypt_health_unknown_since_epoch": now}, now_epoch=now
        )
    return chat_consumer._decrypt_health_from_state(
        {
            "decrypt_status": status,
            "decrypt_checked_at_epoch": str(now),
        },
        now_epoch=now,
    )


def _resident_state(health: dict) -> dict:
    return {
        "passing": True,
        "official": True,
        "consumer_name": "feedling-chat-resident",
        "consumer_id": "resident-test",
        "consumer_version": "resident-v1",
        "consumer_commit": "test",
        "last_poll_at": "2026-07-21T00:00:00",
        "last_response_at": "",
        "age_sec": 1.0,
        "recent_window_sec": 180,
        "decrypt_health": health,
        "required": "",
    }


def _install_validation_harness(monkeypatch, resident: dict) -> None:
    monkeypatch.setattr(
        onboarding_validation.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        onboarding_validation.boot_gates,
        "_bootstrap_state",
        lambda _store: {"memory_count": 0, "memory_floor": 2},
    )
    identity = {
        "relationship_started_at": "2026-07-01",
        "relationship_anchor_evidence": "real transcript",
    }
    monkeypatch.setattr(
        onboarding_validation.identity_service, "_load_identity", lambda _store: identity
    )
    monkeypatch.setattr(
        onboarding_validation.identity_service,
        "_live_days_with_user",
        lambda *_args, **_kwargs: 20,
    )
    monkeypatch.setattr(
        onboarding_validation, "_latest_onboarding_genesis_job", lambda _store: None
    )
    monkeypatch.setattr(
        onboarding_validation.chat_consumer,
        "_consumer_validation_state",
        lambda _store: resident,
    )
    monkeypatch.setattr(
        onboarding_validation.boot_gates,
        "_chat_loop_verified_by_server",
        lambda _store: True,
    )
    monkeypatch.setattr(
        onboarding_validation, "_visible_agent_message_count", lambda _store: 1
    )
    monkeypatch.setattr(
        onboarding_validation,
        "_real_user_agent_exchange_verified",
        lambda _store: True,
    )


def test_new_onboarding_unknown_decrypt_health_blocks_before_live_loop(monkeypatch):
    resident = _resident_state(_health("unknown"))
    _install_validation_harness(monkeypatch, resident)

    body = onboarding_validation._onboarding_validation_payload(_Store())
    ids = [step["id"] for step in body["steps"]]
    step = next(step for step in body["steps"] if step["id"] == "decrypt_source")

    assert ids.index("resident_consumer") < ids.index("decrypt_source") < ids.index("live_loop")
    assert body["passing"] is False
    assert body["stage"] == "decrypt_source"
    assert step["passing"] is False
    assert step["blocking"] is True
    assert step["status"] == "unknown"
    assert step["reason"] == "decrypt_health_unknown"


def test_established_unknown_is_warning_only_during_grace(monkeypatch):
    resident = _resident_state(_health("unknown"))
    _install_validation_harness(monkeypatch, resident)

    body = onboarding_validation._onboarding_validation_payload(
        _Store(established=True)
    )
    step = next(step for step in body["steps"] if step["id"] == "decrypt_source")

    assert step["passing"] is True
    assert step["blocking"] is False
    assert step["warning"] is True
    assert step["required"] == ""
    assert body["passing"] is True


def test_established_explicit_failure_blocks_validation_not_ordinary_chat(monkeypatch):
    resident = _resident_state(_health("unconfigured"))
    _install_validation_harness(monkeypatch, resident)
    store = _Store(established=True)

    body = onboarding_validation._onboarding_validation_payload(store)
    step = next(step for step in body["steps"] if step["id"] == "decrypt_source")
    assert body["stage"] == "decrypt_source"
    assert step["reason"] == "decrypt_source_unconfigured"
    assert step["policy"]["blocks_onboarding"] is True
    assert step["policy"]["blocks_verify"] is True
    assert step["policy"]["blocks_chat"] is False


def test_new_chat_gate_reports_needs_decrypt_source(monkeypatch):
    resident = _resident_state(_health("unreachable"))
    store = _Store()
    monkeypatch.setattr(
        boot_gates, "_bootstrap_state", lambda _store: {
            "stage": "main_loop",
            "memory_count": 0,
            "memory_floor": 2,
            "identity_written": True,
        }
    )
    monkeypatch.setattr(
        boot_gates.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        boot_gates.chat_consumer,
        "_consumer_validation_state",
        lambda _store: resident,
    )
    monkeypatch.setattr(
        boot_gates, "_chat_loop_verified_by_server", lambda _store: True
    )

    body, status = boot_gates._gate_bootstrap_for_chat(store)
    assert status == 409
    assert body["stage"] == "needs_decrypt_source"
    assert body["decrypt_health"]["status"] == "unreachable"

    assert boot_gates._gate_bootstrap_for_chat(_Store(established=True)) is None
