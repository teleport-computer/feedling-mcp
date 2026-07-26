"""Runtime V1 reference decisions used by Runtime V2 parity tests."""
from __future__ import annotations

import os


def legacy_consumer():
    os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
    os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
    os.environ.setdefault("AGENT_MODE", "http")
    os.environ.setdefault("AGENT_HTTP_URL", "http://localhost:8080/chat")
    os.environ.setdefault(
        "CHECKPOINT_FILE",
        "/tmp/feedling_test_v2_incident_guard_parity_checkpoint.json",
    )
    from tools import chat_resident_consumer

    return chat_resident_consumer


def legacy_wake_should_publish(
    monkeypatch,
    *,
    lane: str,
    message: dict,
    now: float,
) -> bool:
    legacy = legacy_consumer()
    monkeypatch.setattr(legacy, "PROACTIVE_CHAT_COLLISION_WINDOW_SEC", 90.0)
    monkeypatch.setattr(
        legacy,
        "get_decrypted_history",
        lambda **_kwargs: [message],
    )
    collision = legacy._proactive_chat_collision(now=now)
    return lane == "scheduled" or not collision
