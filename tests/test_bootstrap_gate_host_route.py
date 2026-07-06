"""The chat bootstrap gate must not impose the resident-consumer requirement on
host (model_api) accounts.

Prod regression: a host/model_api account reached main_loop (identity written,
memory floor met) but had no live official `feedling-chat-resident` heartbeat —
host liveness is tracked separately (the agent-runner consumer claims proactive
jobs but does not stamp the chat heartbeat). The route-blind gate then 409'd with
`needs_resident_consumer` + the resident-consumer `required` text, even though the
host's canonical chat path (/v1/model_api/chat/send) is gate-free. The gate must
consult the onboarding route and exempt model_api accounts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from bootstrap import gates as boot_gates  # noqa: E402
from chat import consumer as chat_consumer  # noqa: E402
from accounts import onboarding as accounts_onboarding  # noqa: E402


_MAIN_LOOP_STATE = {
    "stage": "main_loop",
    "memory_count": 3,
    "memory_floor": 2,
    "counts": {"total": 3},
    "floors": {"total": 2},
    "missing_tabs": [],
    "identity_written": True,
}

_NOT_PASSING = {
    "passing": False,
    "official": False,
    "consumer_name": None,
    "age_sec": None,
    "required": (
        "Run the standard independent feedling-chat-resident / IO resident "
        "consumer with the current FEEDLING_API_KEY."
    ),
}


class _Store:
    user_id = "usr_host_gate"


def _patch(monkeypatch, route: str) -> None:
    monkeypatch.setattr(boot_gates, "_bootstrap_state", lambda store: dict(_MAIN_LOOP_STATE))
    monkeypatch.setattr(chat_consumer, "_consumer_validation_state", lambda store: dict(_NOT_PASSING))
    monkeypatch.setattr(boot_gates, "_chat_loop_verified_by_server", lambda store: False)
    monkeypatch.setattr(accounts_onboarding, "_load_onboarding_route", lambda store: route)


def test_host_account_not_blocked_by_resident_consumer_gate(monkeypatch):
    _patch(monkeypatch, "model_api")
    gated = boot_gates._gate_bootstrap_for_chat(_Store())
    assert gated is None, "host (model_api) account must pass the chat gate at main_loop"


def test_resident_account_still_blocked_by_resident_consumer_gate(monkeypatch):
    _patch(monkeypatch, "resident")
    gated = boot_gates._gate_bootstrap_for_chat(_Store())
    assert gated is not None, "resident account must still require a live resident consumer"
    body, status = gated
    assert status == 409
    assert body["stage"] == "needs_resident_consumer"
