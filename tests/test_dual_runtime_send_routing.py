"""Three-state hosted send routing matrix (dual-runtime, Task 6).

``/v1/model_api/chat/send`` is a per-user router keyed on ``(runtime_mode,
runtime_state)`` and the process ``FEEDLING_HOSTED_RUNTIME_POLICY``:

    policy     (mode, state)                     -> outcome
    v2_only    non-(db_action_v2, v2)            -> 503 runtime_policy_not_ready
    v2_only    (db_action_v2, v2)                -> V2 path
    dual       (*, draining)                     -> 503 runtime_switching
    dual       (db_action_v2, v2) + workers up   -> 202 processing
    dual       (db_action_v2, v2) + workers down -> 503 workers_unavailable
    dual       (resident_cli, resident) + sup up -> resident branch (202)
    dual       (resident_cli, resident) + sup dn -> 503 hosting_runtime_unavailable
    dual       any other combo                   -> 503 runtime_control_invalid

The error strings are an operational contract — asserted exactly. Drives
``chat_send_core.model_api_chat_send_core`` directly against real DB state
(mirrors ``test_chat_send_v2_enqueue.py``); tuples that return before any
persistence are monkeypatched onto ``get_hosted_runtime_control_strict``.
"""
import base64
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import chat_send_core, config_store as hosted_config_store  # noqa: E402

from conftest import configure_model_api_route  # noqa: E402

POLICY_ENV = hosted_config_store.HOSTED_RUNTIME_POLICY_ENV
V2 = hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
RESIDENT = hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT


def _seed(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    configure_model_api_route(
        uid, provider="anthropic", model="m", test_status="ok",
        envelope={"body_ct": "x", "nonce": "n", "K_user": "k"})


def _fake_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: (
            {"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )


def _pin_tuple(monkeypatch, mode: str, state: str, generation: int = 7) -> None:
    monkeypatch.setattr(
        hosted_config_store, "get_hosted_runtime_control_strict",
        lambda _store: (mode, state, generation),
    )


def _forbid_persist(monkeypatch, store) -> None:
    monkeypatch.setattr(
        store, "append_chat",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not persist before a gated 503")),
    )


def _send(store):
    return chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"})


# --------------------------------------------------------------------------
# v2_only policy (retirement-era contract; the pinned regression net)
# --------------------------------------------------------------------------

def test_v2_only_non_v2_tuple_is_runtime_policy_not_ready(monkeypatch):
    _seed("u_dr_v2only_resident")
    store = core_store.get_store("u_dr_v2only_resident")
    monkeypatch.setenv(POLICY_ENV, "v2_only")
    _pin_tuple(monkeypatch, RESIDENT, "resident")
    _forbid_persist(monkeypatch, store)

    body, status = _send(store)
    assert status == 503
    assert body == {"error": "runtime_policy_not_ready"}


def test_v2_only_v2_tuple_falls_into_v2_path(monkeypatch):
    # Proof it enters the V2 path (not a dispatch error): the workers-alive
    # liveness gate fires there.
    _seed("u_dr_v2only_v2")
    store = core_store.get_store("u_dr_v2only_v2")
    monkeypatch.setenv(POLICY_ENV, "v2_only")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    _fake_envelope(monkeypatch)
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: False)

    body, status = _send(store)
    assert status == 503
    assert body["error"] == "workers_unavailable"


# --------------------------------------------------------------------------
# dual policy — draining / invalid combos (pure dispatch, no persistence)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [V2, RESIDENT])
def test_dual_draining_is_runtime_switching(monkeypatch, mode):
    _seed(f"u_dr_drain_{mode}")
    store = core_store.get_store(f"u_dr_drain_{mode}")
    monkeypatch.setenv(POLICY_ENV, "dual")
    _pin_tuple(monkeypatch, mode, "draining")
    _forbid_persist(monkeypatch, store)

    body, status = _send(store)
    assert status == 503
    assert body == {"error": "runtime_switching"}


@pytest.mark.parametrize(
    ("mode", "state"),
    [(RESIDENT, "v2"), (V2, "resident")],
)
def test_dual_split_tuple_is_runtime_control_invalid(monkeypatch, mode, state):
    _seed(f"u_dr_invalid_{mode}_{state}")
    store = core_store.get_store(f"u_dr_invalid_{mode}_{state}")
    monkeypatch.setenv(POLICY_ENV, "dual")
    _pin_tuple(monkeypatch, mode, state)
    _forbid_persist(monkeypatch, store)

    body, status = _send(store)
    assert status == 503
    assert body == {"error": "runtime_control_invalid"}


# --------------------------------------------------------------------------
# dual policy — resident branch
# --------------------------------------------------------------------------

def test_dual_resident_tuple_with_live_supervisor_routes_to_resident(monkeypatch):
    _seed("u_dr_resident_live")
    store = core_store.get_store("u_dr_resident_live")
    monkeypatch.setenv(POLICY_ENV, "dual")
    _pin_tuple(monkeypatch, RESIDENT, "resident")
    _fake_envelope(monkeypatch)
    monkeypatch.setattr(
        hosted_config_store, "_load_runtime_provider_config",
        lambda *a, **k: types.SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(
        hosted_config_store, "_ensure_model_api_runtime_profile",
        lambda *a, **k: None)
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "check_supervisor_live",
        lambda **kw: (True, ""))
    called = {}

    def _fake_handle_send(s, row, driver, **kw):
        called["driver"] = driver
        return ({"status": "processing", "reply_ready": False,
                 "runtime": {"driver": driver}}, 202)

    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "handle_send", _fake_handle_send)

    body, status = _send(store)
    assert status == 202
    assert body["status"] == "processing"
    # handle_send is the resident-only tail — proves resident routing, not V2.
    assert called["driver"] == "claude"


@pytest.mark.parametrize("idempotent_send", [False, True])
def test_dual_resident_follow_main_image_pins_active_route_version(
    monkeypatch,
    idempotent_send,
):
    uid = f"u_dr_resident_image_pin_{idempotent_send}"
    _seed(uid)
    store = core_store.get_store(uid)
    monkeypatch.setenv(POLICY_ENV, "dual")
    _pin_tuple(monkeypatch, RESIDENT, "resident")
    _fake_envelope(monkeypatch)
    monkeypatch.setattr(
        hosted_config_store,
        "_load_runtime_provider_config",
        lambda *a, **k: types.SimpleNamespace(
            provider="anthropic", model="m", base_url=""
        ),
    )
    monkeypatch.setattr(
        hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover,
        "check_supervisor_live",
        lambda **kw: (True, ""),
    )
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover,
        "handle_send",
        lambda _store, row, driver: (
            {
                "status": "processing",
                "user_message": row,
                "runtime": {"driver": driver},
            },
            202,
        ),
    )

    payload = {
        "image_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
        "image_mime": "image/png",
    }
    if idempotent_send:
        payload["client_msg_id"] = "11111111-1111-4111-8111-111111111111"
    body, status = chat_send_core.model_api_chat_send_core(
        store,
        api_key="key",
        runtime_tok="",
        payload=payload,
    )

    assert status == 202
    row = body["user_message"]
    binding = db.model_api_active_route_version(uid)
    assert row["vision_main_route_id"] == binding["route_id"]
    assert (
        row["vision_main_route_updated_at"]
        == binding["updated_at_token"]
    )
    assert not row.get("vision_route_id")


def test_dual_resident_tuple_with_dead_supervisor_is_hosting_runtime_unavailable(monkeypatch):
    _seed("u_dr_resident_dead")
    store = core_store.get_store("u_dr_resident_dead")
    monkeypatch.setenv(POLICY_ENV, "dual")
    _pin_tuple(monkeypatch, RESIDENT, "resident")
    _fake_envelope(monkeypatch)
    monkeypatch.setattr(
        hosted_config_store, "_load_runtime_provider_config",
        lambda *a, **k: types.SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(
        hosted_config_store, "_ensure_model_api_runtime_profile",
        lambda *a, **k: None)
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "check_supervisor_live",
        lambda **kw: (False, "stale_supervisor_heartbeat_120s"))
    # Wedge fires BEFORE any append — no orphan user turn.
    monkeypatch.setattr(
        store, "append_chat",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("dead supervisor must refuse before persistence")),
    )

    body, status = _send(store)
    assert status == 503
    assert body == {
        "error": "hosting_runtime_unavailable",
        "reason": "stale_supervisor_heartbeat_120s",
    }


# --------------------------------------------------------------------------
# dual policy — V2 branch
# --------------------------------------------------------------------------

def test_dual_v2_tuple_with_dead_workers_is_workers_unavailable(monkeypatch):
    _seed("u_dr_v2_dead")
    store = core_store.get_store("u_dr_v2_dead")
    monkeypatch.setenv(POLICY_ENV, "dual")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    _fake_envelope(monkeypatch)
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: False)
    _forbid_persist(monkeypatch, store)

    body, status = _send(store)
    assert status == 503
    assert body["error"] == "workers_unavailable"


def test_dual_v2_tuple_with_live_workers_processes(monkeypatch):
    _seed("u_dr_v2_live")
    store = core_store.get_store("u_dr_v2_live")
    monkeypatch.setenv(POLICY_ENV, "dual")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    _fake_envelope(monkeypatch)
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(
        chat_send_core.core_wake_bus, "notify", lambda *a, **k: None)

    body, status = _send(store)
    assert status == 202
    assert body["status"] == "processing"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, status, reason FROM agent_jobs "
            "WHERE user_id=%s AND lane='chat'",
            ("u_dr_v2_live",),
        ).fetchall()
    assert rows == [("chat", "pending", "chat_send")]
