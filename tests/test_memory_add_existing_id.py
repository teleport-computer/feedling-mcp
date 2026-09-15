"""memory.add never overwrites a stored card; card timestamps are one UTC format.

Before: a memory.add (or POST /v1/memory/add) carrying an envelope id that was
already stored replaced that card — including a superseded card, whose
superseded_by chain vanished so it came back active next to its successor.
Updated_at was naive local time while created_at was UTC "Z".

After: the exact same sealed card again (a client retry after a lost response)
succeeds without writing; any other card under that id is refused with the
content-free ``memory_id_conflict`` (409). New cards carry one UTC "Z" instant in
created_at and updated_at; mutations stamp updated_at the same way.

Real Postgres; the only double is the resident consumer's HTTP transport, routed
into the in-process ASGI app so V1's own client code handles the real receipts.
"""
from __future__ import annotations

import base64
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_memory_add_existing_id_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - mirrors the consumer suites
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402
from memgarden import timestamps as memory_timestamps  # noqa: E402
from memory import memory_core  # noqa: E402
import tools.chat_resident_consumer as crc  # noqa: E402


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(b"\x11" * 32).decode(), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


@pytest.fixture()
def non_utc_process(monkeypatch):
    # CI and the CVMs run UTC, which would hide a naive-local stamp.
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _envelope(uid: str, mid: str, *, body: str = "ciphertext-a", nonce: str = "nonce-a") -> dict:
    return {
        "id": mid,
        "type": "fact",
        "body_ct": body,
        "nonce": nonce,
        "K_user": "ku",
        "K_enclave": "ke",
        "visibility": "shared",
        "owner_user_id": uid,
        "occurred_at": "2026-06-20T10:00:00Z",
        "source": "live_conversation",
    }


def _act(uid: str, *actions: dict) -> tuple[dict, int]:
    return memory_core.actions(core_store.get_store(uid), None, {"actions": list(actions)})


def _add(uid: str, envelope: dict) -> dict:
    return {"type": "memory.add", "envelope": envelope}


def _row(uid: str, mid: str) -> dict | None:
    return next((r for r in db.memory_load_strict(uid) if r.get("id") == mid), None)


def _change_count(uid: str) -> int:
    with db.get_pool().connection() as conn:
        return int(conn.execute(
            "SELECT count(*) FROM user_logs WHERE user_id=%s AND stream='memory_changes'",
            (uid,),
        ).fetchone()[0])


# --------------------------------------------------------------------------- #
# D4 — /v1/memory/actions memory.add
# --------------------------------------------------------------------------- #

def test_identical_replay_succeeds_without_rewriting_logging_or_effects(user):
    uid, _key = user
    first, status = _act(uid, _add(uid, _envelope(uid, "mom_replay")))
    assert status == 200 and first["results"][0]["http_status"] == 201
    stored = _row(uid, "mom_replay")
    changes = _change_count(uid)
    time.sleep(1.05)  # card stamps have second precision

    again, status = _act(uid, _add(uid, _envelope(uid, "mom_replay")))

    assert status == 200
    item = again["results"][0]
    assert item["http_status"] == 200 and item["status"] == "ok" and item["replayed"] is True
    assert item["memory"]["id"] == "mom_replay"
    assert again["effects"] == [] and again["applied_count"] == 1
    assert _row(uid, "mom_replay") == stored
    assert _change_count(uid) == changes
    assert [r["id"] for r in db.memory_load_strict(uid)] == ["mom_replay"]


def test_different_card_under_a_stored_id_is_refused_and_leaves_it_untouched(user):
    uid, _key = user
    _act(uid, _add(uid, _envelope(uid, "mom_taken")))
    stored = _row(uid, "mom_taken")

    body, status = _act(uid, _add(uid, _envelope(uid, "mom_taken", body="ciphertext-b",
                                                 nonce="nonce-b")))

    assert status == 400  # nothing applied, one failure → promoted
    item = body["results"][0]
    assert item == {"status": "error", "error": "memory_id_conflict", "action": "memory.add",
                    "http_status": 409}
    assert "ciphertext" not in repr(body)
    assert _row(uid, "mom_taken") == stored


def test_add_cannot_resurrect_a_superseded_card(user):
    uid, _key = user
    _act(uid, _add(uid, _envelope(uid, "mom_old")))
    retired, status = _act(uid, {"type": "memory.supersede", "supersedes": ["mom_old"],
                                 "envelope": _envelope(uid, "mom_new", body="ct-new",
                                                       nonce="n-new")})
    assert status == 200 and retired["results"][0]["http_status"] == 201
    before = _row(uid, "mom_old")
    assert before["superseded_by"] == "mom_new"

    clobber, _ = _act(uid, _add(uid, _envelope(uid, "mom_old", body="ct-clobber",
                                               nonce="n-clobber")))
    assert clobber["results"][0]["error"] == "memory_id_conflict"
    # A late retry of the ORIGINAL add is a replay: success, and the card stays retired.
    replay, _ = _act(uid, _add(uid, _envelope(uid, "mom_old")))
    assert replay["results"][0]["replayed"] is True
    assert replay["results"][0]["memory"]["status"] == "superseded"

    assert _row(uid, "mom_old") == before
    active = [r["id"] for r in db.memory_load_strict(uid)
              if str(r.get("status") or "active") == "active" and not r.get("is_archived")]
    assert active == ["mom_new"]


def test_plain_add_route_replays_and_refuses_the_same_way(user):
    uid, _key = user
    store = core_store.get_store(uid)
    created, status = memory_core.add(store, {"envelope": _envelope(uid, "mom_route")})
    assert status == 201
    stored = _row(uid, "mom_route")

    replay, status = memory_core.add(store, {"envelope": _envelope(uid, "mom_route")})
    assert status == 200 and replay["replayed"] is True
    assert replay["moment"]["id"] == "mom_route"

    conflict, status = memory_core.add(
        store, {"envelope": _envelope(uid, "mom_route", body="other", nonce="other")})
    assert (conflict, status) == ({"error": "memory_id_conflict"}, 409)
    assert _row(uid, "mom_route") == stored


# --------------------------------------------------------------------------- #
# D4 — V1 resident consumer (hosted runner and self-hosted VPS) over the real route
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, res):
        self.status_code = res.status_code
        self._body = res.get_json()
        self.text = res.get_data(as_text=True)

    def json(self):
        return self._body


@pytest.fixture()
def consumer_http(user, monkeypatch):
    uid, key = user
    client = make_client()

    class _Http:
        @staticmethod
        def post(url, *, json, headers, timeout):
            path = urlsplit(url).path  # whatever FEEDLING_API_URL an earlier suite set
            return _Resp(client.post(path, json=json, headers={"X-API-Key": key}))

    monkeypatch.setattr(crc, "_HTTP", _Http)
    return uid


def test_consumer_retry_of_a_batch_the_server_already_applied_counts_as_applied(consumer_http):
    """A resident write whose response was lost is re-sent byte for byte (the
    actions list is built once). Old and new consumers both send that same payload,
    so both now get success instead of overwriting the stored cards."""
    uid = consumer_http
    actions = [
        {**_add(uid, _envelope(uid, "mom_v1_a", body="a", nonce="na")),
         "capture_mode": "memory_capture", "source_chat_message_ids": ["m1"]},
        {**_add(uid, _envelope(uid, "mom_v1_b", body="b", nonce="nb")),
         "capture_mode": "memory_capture", "source_chat_message_ids": ["m1"]},
    ]
    first = crc.execute_memory_actions(actions)
    stored = {mid: _row(uid, mid) for mid in ("mom_v1_a", "mom_v1_b")}
    time.sleep(1.05)

    retried = crc.execute_memory_actions(actions)

    observation = crc._memory_batch_observation(actions, retried)
    assert observation["status"] == "ok"
    assert observation["applied"]["added"] == 2 and observation["failed_count"] == 0
    assert crc._capture_semantic_retry_reasons([], retried) == []
    assert crc._memory_batch_observation(actions, first)["status"] == "ok"
    assert {mid: _row(uid, mid) for mid in stored} == stored


def test_consumer_sees_a_conflict_as_a_failed_item_not_a_success(consumer_http):
    uid = consumer_http
    crc.execute_memory_actions([_add(uid, _envelope(uid, "mom_v1_taken"))])
    clobber = [_add(uid, _envelope(uid, "mom_v1_taken", body="x", nonce="y"))]

    with pytest.raises(crc.ActionsHTTPError) as raised:
        crc.execute_memory_actions(clobber)

    body = raised.value.body
    observation = crc._memory_batch_observation(clobber, body)
    assert observation["status"] == "failed"
    assert observation["failed"]["by_error"] == {"memory_id_conflict": 1}


# --------------------------------------------------------------------------- #
# D6 — updated_at is UTC "Z" like created_at; mixed old rows still order
# --------------------------------------------------------------------------- #

def _is_utc_z(value: str) -> bool:
    return isinstance(value, str) and value.endswith("Z") and memory_timestamps.parse_ts(value)


def test_new_card_stamps_one_utc_instant_in_created_and_updated(user, non_utc_process):
    uid, _key = user
    _act(uid, _add(uid, _envelope(uid, "mom_clock")))
    row = _row(uid, "mom_clock")
    assert _is_utc_z(row["created_at"]) and row["updated_at"] == row["created_at"]
    drift = abs(memory_timestamps.parse_ts(row["updated_at"]) - datetime.now(timezone.utc))
    assert drift < timedelta(minutes=1)


def test_supersede_retype_and_archive_stamp_updated_at_in_utc(user, non_utc_process):
    uid, _key = user
    store = core_store.get_store(uid)
    for mid in ("mom_t_old", "mom_t_retype", "mom_t_archive"):
        _act(uid, _add(uid, _envelope(uid, mid, body=f"ct-{mid}", nonce=f"n-{mid}")))
    _act(uid, {"type": "memory.supersede", "supersedes": ["mom_t_old"],
               "envelope": _envelope(uid, "mom_t_new", body="ct-new", nonce="n-new")})
    body, _ = _act(uid, {"type": "memory.retype", "id": "mom_t_retype", "new_type": "event"})
    assert body["results"][0]["http_status"] < 400, body
    assert hosted_turn._archive_model_api_memory_cards(
        store, ["mom_t_archive"], reason="repair", job_id="job-1") == 1

    now = datetime.now(timezone.utc)
    for mid, fields in (("mom_t_old", ("updated_at", "archived_at")),
                        ("mom_t_retype", ("updated_at", "retyped_at")),
                        ("mom_t_archive", ("updated_at", "archived_at"))):
        row = _row(uid, mid)
        for field in fields:
            assert _is_utc_z(row[field]), (mid, field, row[field])
            assert abs(memory_timestamps.parse_ts(row[field]) - now) < timedelta(minutes=1)


def test_profile_witness_moves_past_old_naive_rows_after_a_utc_write(user):
    """Existing rows keep their naive stamps (written on UTC hosts). The profile
    refresh witness is a string max over updated_at: a later UTC "Z" write must
    still become the max, or edits after this change would never refresh the profile."""
    uid, _key = user
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    legacy = {**_envelope(uid, "mom_legacy"), "v": 1, "status": "active",
              "created_at": past.isoformat(), "updated_at": past.isoformat()}
    assert db.memory_upsert(uid, "mom_legacy", legacy["occurred_at"], legacy)
    _count, witness_before = db.memory_profile_source_stats(uid)
    assert witness_before == past.isoformat()

    _act(uid, _add(uid, _envelope(uid, "mom_after", body="after", nonce="after")))

    count, witness_after = db.memory_profile_source_stats(uid)
    assert count == 2 and witness_after == _row(uid, "mom_after")["updated_at"]
    assert witness_after != witness_before

    # A write that leaves the card count unchanged (archive) must move it as well.
    time.sleep(1.05)
    assert hosted_turn._archive_model_api_memory_cards(
        core_store.get_store(uid), ["mom_legacy"], reason="repair", job_id="job-w") == 1
    count, witness_archived = db.memory_profile_source_stats(uid)
    assert count == 2 and witness_archived == _row(uid, "mom_legacy")["updated_at"]
    assert witness_archived != witness_after
    # Readers that compare instants order the mixed shapes correctly.
    newest_first = sorted([legacy["updated_at"], witness_after],
                          key=memory_timestamps.sort_key, reverse=True)
    assert newest_first == [witness_after, legacy["updated_at"]]
