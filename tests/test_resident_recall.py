"""T779 step 3 (T788): per-turn card selection for plaintext resident accounts
in the backend, lexical only (Seven 2026-09-30).

1. Frozen same-input equivalence: the backend's lexical pick on the resident
   window equals the enclave selector run lexically on the same input, down to
   what the consumer stashes and renders; the vector path is never touched
   even when the deployment switch for hybrid is on.
2. The V2 window is unchanged by the parameterisation.
3. Shadow verdicts: exact / hybrid-fallback blanked (normalized) / hybrid
   active (policy difference, never counted as agreement).
4. The turn contract (off / encrypted / on / fallback / shadow / 422 / 503).
5. The consumer: only an account the whoami cache names ``off`` uses the
   route; 404/409 fall back to the enclave once; anything else is unknown.
6. The route bounds and validates its body before any work.
"""
from __future__ import annotations

import sys
import asyncio
import json
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from chat import resident_recall_core as core  # noqa: E402
from enclave.routes import chat as enclave_chat  # noqa: E402
from memory import plaintext_recall, recall_select  # noqa: E402
from memory.embedding import query_service, recall_policy  # noqa: E402
from test_plaintext_recall import (  # noqa: E402
    UID, WINDOWS, _embedder, _moments, _rows, _stored_payload, _strip_timing)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (recall_policy.HYBRID_ENV, recall_policy.MIN_COSINE_ENV, plaintext_recall.MODE_ENV,
                 recall_select.RECALL_RANKER_ENV, query_service.PORT_ENV, query_service.TOKEN_ENV,
                 query_service.OWNER_ENV, core.MODE_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def hybrid_on(monkeypatch):
    """The deployment switch for hybrid is on (as on test); the resident path
    must still stay lexical."""
    monkeypatch.setenv(recall_policy.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_policy.MIN_COSINE_ENV, "0.1")


class _Refuse:
    """An encoder that fails the test if the lexical path ever calls it."""
    def encode(self, texts, deadline):
        raise AssertionError("resident lexical path called the encoder")


def _lexical_deps(rows, moments=None):
    return plaintext_recall.Deps(
        effective_mode=lambda _u: "off",
        history_page=lambda _u, _s: [dict(r) for r in rows],
        list_moments=lambda _u, _l: [dict(m) for m in (_moments() if moments is None else moments)],
        stored_vectors=lambda *_a: pytest.fail("resident lexical path read vectors"),
        encoder=_Refuse())


def _enclave_resident(rows, moments, *, hybrid=None, fp=False):
    decrypted, _ = enclave_chat._decrypt_history_items([dict(r) for r in rows], UID, None)
    args = {**plaintext_recall.RESIDENT_QUERY_ARGS, "authorized_user_id": UID, "content_sk": None}
    if hybrid is not None:
        args["hybrid"] = hybrid
    if fp:
        args["input_fp_out"] = {}
    picked, trace, log = enclave_chat._build_context_memories(
        [dict(m) for m in moments], decrypted, args)
    return picked, trace, log, args.get("input_fp_out")


def _consumer(monkeypatch):
    monkeypatch.setenv("FEEDLING_API_URL", "http://localhost:5001")
    monkeypatch.setenv("FEEDLING_API_KEY", "test_key_00000000")
    from tools import chat_resident_consumer as consumer
    return consumer


# --------------------------------------------------------------------------- #
# 1. frozen same-input equivalence (lexical), through stash and render
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("window", sorted(WINDOWS))
def test_resident_pick_equals_the_enclave_lexical_selector(hybrid_on, window, monkeypatch):
    rows = _rows(WINDOWS[window])
    moments = _moments()
    e_picked, e_trace, e_log, _ = _enclave_resident(rows, moments)
    local = plaintext_recall.select(UID, len(rows), _lexical_deps(rows, moments),
                                    query_args=plaintext_recall.RESIDENT_QUERY_ARGS, hybrid=False)
    assert local.payload["context_memories"] == e_picked
    assert local.payload["context_memory_trace"] == e_trace
    assert _strip_timing(local.payload["context_memory_log"]) == _strip_timing(e_log)
    assert "hybrid" not in local.payload["context_memory_log"]
    consumer = _consumer(monkeypatch)
    ours = consumer._stash_auto_memories(local.payload["context_memories"],
                                         local.payload["context_memory_trace"])
    theirs = consumer._stash_auto_memories(e_picked, e_trace)
    assert ours == theirs
    assert consumer._auto_memory_render(ours, []) == consumer._auto_memory_render(theirs, [])


def test_the_resident_window_is_not_the_v2_window():
    rows = _rows(WINDOWS["two"])
    resident = plaintext_recall.select(UID, 3, _lexical_deps(rows),
                                       query_args=plaintext_recall.RESIDENT_QUERY_ARGS,
                                       hybrid=False)
    v2 = plaintext_recall.select(UID, 3, _lexical_deps(rows), hybrid=False)
    assert resident.input_fingerprint["flags"] != v2.input_fingerprint["flags"]
    assert plaintext_recall.RESIDENT_QUERY_ARGS == {
        "context_mode": "", "context_recent": False, "want_trace": True}


# --------------------------------------------------------------------------- #
# 2. V2 unchanged
# --------------------------------------------------------------------------- #

def test_v2_defaults_follow_the_deployment_switch_as_before(hybrid_on):
    embedder = _embedder()

    class _Client:
        calls = 0

        def encode(self, texts, deadline):
            _Client.calls += 1
            from memory.embedding import query_client
            return query_client.Reply(embedder.model_id, embedder.dim,
                                      [embedder.encode_query(t) for t in texts], {})
    rows = _rows(WINDOWS["two"])
    deps = plaintext_recall.Deps(
        effective_mode=lambda _u: "off", history_page=lambda _u, _s: [dict(r) for r in rows],
        list_moments=lambda _u, _l: _moments(),
        stored_vectors=lambda _u, model_id, ids: _stored_payload(embedder, model_id, ids),
        encoder=_Client())
    v2 = plaintext_recall.select(UID, 3, deps)            # no kwargs: V2 window, env switch
    assert _Client.calls == 1
    assert v2.payload["context_memory_log"]["hybrid"]["status"] == "active"
    assert v2.input_fingerprint["flags"] == recall_select.input_fingerprint(
        [], [], {**plaintext_recall.V2_QUERY_ARGS}, evidence={})["flags"]


# --------------------------------------------------------------------------- #
# 3. shadow verdicts
# --------------------------------------------------------------------------- #

def _local(rows):
    return plaintext_recall.select(UID, len(rows), _lexical_deps(rows),
                                   query_args=plaintext_recall.RESIDENT_QUERY_ARGS, hybrid=False)


def test_shadow_exact_when_the_enclave_is_lexical_too():
    rows = _rows(WINDOWS["cn"])
    _, _, _, diag = _enclave_resident(rows, _moments(), fp=True)
    verdict = core.compare(_local(rows), diag)
    assert verdict == {"verdict": "comparable", "basis": "exact", "same": True, "diff": []}


@pytest.mark.parametrize("reason", ["embedder_unavailable", "deadline_exceeded"])
def test_shadow_normalizes_only_the_hybrid_fields_of_a_fallen_back_enclave(hybrid_on, reason):
    rows = _rows(WINDOWS["two"])
    state = {"deadline": time.monotonic() + 5, "started": time.monotonic(), "embedder": None,
             "model_id": None, "stored": None, "fallback_reason": reason, "vectors_ms": None,
             "vectors_requested": 0, "vectors_rejected": 0}
    _, _, e_log, diag = _enclave_resident(rows, _moments(), hybrid=state, fp=True)
    assert diag["input_fingerprint"]["hybrid"].startswith("fallback")
    verdict = core.compare(_local(rows), diag)
    assert verdict["verdict"] == "normalized"
    assert verdict["basis"] == "hybrid_fallback_blanked"
    assert verdict["same"] is True, verdict   # the fallback really is the lexical rule


def test_shadow_marks_an_active_hybrid_enclave_as_a_policy_difference(hybrid_on):
    embedder = _embedder()
    rows = _rows(WINDOWS["two"])
    moments = _moments()
    ids = recall_policy.plaintext_candidate_ids(moments, UID)
    stored, _ = recall_policy.decode_vectors(
        _stored_payload(embedder, embedder.model_id, ids), embedder.model_id, embedder.dim)
    state = {"deadline": time.monotonic() + 5, "started": time.monotonic(), "embedder": embedder,
             "model_id": embedder.model_id, "stored": stored, "fallback_reason": None,
             "vectors_ms": 1.0, "vectors_requested": len(ids), "vectors_rejected": 0}
    _, _, _, diag = _enclave_resident(rows, moments, hybrid=state, fp=True)
    assert core.compare(_local(rows), diag) == {"verdict": "incomparable",
                                                "reason": "hybrid_policy"}


def test_shadow_does_not_blank_a_different_input():
    rows = _rows(WINDOWS["cn"])
    _, _, _, diag = _enclave_resident(_rows(WINDOWS["two"]), _moments(), fp=True)
    verdict = core.compare(_local(rows), diag)
    assert verdict["verdict"] == "incomparable" and "history" in verdict["reason"]


def test_shadow_without_diagnostics_is_unmeasured():
    assert core.compare(_local(_rows(WINDOWS["cn"])), None)["verdict"] == "unmeasured"


# --------------------------------------------------------------------------- #
# 4. the turn contract
# --------------------------------------------------------------------------- #

def _page(rows):
    return [{"id": r["id"], "role": r["role"], "seq": r["seq"], "body": r["body"]} for r in rows]


@pytest.fixture()
def turn(monkeypatch):
    rows = _rows(WINDOWS["two"])
    calls = {"enclave": 0, "traces": []}
    monkeypatch.setattr(core.accounts_registry, "effective_content_encryption", lambda _u: "off")
    monkeypatch.setattr(core, "_history_page", lambda _u, _s: [dict(r) for r in rows])
    monkeypatch.setattr(core, "_list_moments", lambda _u, _l: _moments())

    def enclave(user_id, seq, input_fp=False):
        calls["enclave"] += 1
        if calls.get("enclave_fails"):
            raise RuntimeError("enclave_read_failed")
        picked, trace, log, diag = _enclave_resident(rows, _moments(), fp=input_fp)
        return {"user_id": user_id, "messages": _page(rows), "context_memories": picked,
                "context_memory_trace": trace, "context_memory_log": log,
                "context_input_diagnostics": diag}
    monkeypatch.setattr(core, "enclave_select", enclave)
    monkeypatch.setattr(core.debug_trace, "trace_event",
                        lambda store, **kw: calls["traces"].append(kw))
    store = types.SimpleNamespace(user_id=UID)
    return types.SimpleNamespace(rows=rows, calls=calls, store=store,
                                 request={"message_id": rows[-1]["id"], "seq": rows[-1]["seq"]})


def test_mode_off_is_409_and_never_calls_the_enclave(turn):
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert (status, body["error"]) == (409, "not_served")
    assert turn.calls["enclave"] == 0


def test_an_encrypted_account_is_409_and_never_calls_the_enclave(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "on")
    monkeypatch.setattr(core.accounts_registry, "effective_content_encryption", lambda _u: "on")
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert (status, body["detail"]) == (409, "account_encrypted")
    assert turn.calls["enclave"] == 0


def test_mode_on_serves_the_local_lexical_pick_with_its_page(turn, monkeypatch, hybrid_on):
    monkeypatch.setenv(core.MODE_ENV, "on")
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert status == 200 and body["source"] == "local"
    assert turn.calls["enclave"] == 0
    assert body["messages"] == [{"id": r["id"], "role": r["role"], "seq": r["seq"]}
                                for r in turn.rows]            # no message content
    e_picked, e_trace, _, _ = _enclave_resident(turn.rows, _moments())
    assert body["context_memories"] == e_picked and body["context_memory_trace"] == e_trace
    # Explicitly lexical even with the deployment's hybrid switch on: no hybrid
    # step was attempted at all (not merely one that fell back).
    assert "hybrid" not in body["context_memory_log"]
    assert turn.calls["traces"][-1]["detail"]["source"] == "local"


def test_mode_on_falls_back_to_the_enclave_exactly_once(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "on")
    monkeypatch.setattr(core, "local_select", lambda *_a, **_k: (_ for _ in ()).throw(
        plaintext_recall.NotServedHere("sealed_history")))
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert status == 200 and body["source"] == "enclave_fallback"
    assert turn.calls["enclave"] == 1
    assert turn.calls["traces"][-1]["detail"]["reason"] == "sealed_history"


def test_mode_on_is_503_when_local_and_enclave_both_fail(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "on")
    monkeypatch.setattr(core, "local_select", lambda *_a, **_k: 1 / 0)
    turn.calls["enclave_fails"] = True
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert (status, body["error"]) == (503, "recall_unavailable")
    assert turn.calls["enclave"] == 1


def test_mode_on_past_the_budget_does_not_wait_for_a_slow_local_pick(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "on")
    monkeypatch.setattr(core, "local_select", lambda *_a, **_k: time.sleep(1.0))
    started = time.monotonic()
    body, status = core.select_for_turn(turn.store, turn.request, 0.2)
    assert time.monotonic() - started < 0.9
    assert status == 503                                   # budget spent: no half fallback


def test_a_message_outside_the_page_is_422(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "on")
    body, status = core.select_for_turn(turn.store, {"message_id": "nope", "seq": 3}, 3.0)
    assert (status, body["error"]) == (422, "message_not_in_window")


def test_mode_shadow_returns_the_enclave_answer_and_records_a_comparison(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "shadow")
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert status == 200 and body["source"] == "enclave_shadow"
    assert turn.calls["enclave"] == 1
    for _ in range(200):
        shadow = [t for t in turn.calls["traces"] if t["type"] == "memory.resident_recall.shadow"]
        if shadow:
            break
        time.sleep(0.01)
    assert shadow[0]["detail"]["verdict"] == "comparable"
    assert shadow[0]["detail"]["same"] is True


def test_a_failing_shadow_keeps_the_enclave_answer(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "shadow")
    monkeypatch.setattr(core, "local_select", lambda *_a, **_k: 1 / 0)
    body, status = core.select_for_turn(turn.store, turn.request, 3.0)
    assert status == 200 and body["source"] == "enclave_shadow"


def test_the_deadline_is_clamped_to_server_bounds():
    assert core.deadline_seconds("999999") == pytest.approx(core.MAX_DEADLINE_MS / 1000 - 0.15)
    assert core.deadline_seconds("1") == 0  # never manufacture more caller budget
    assert core.deadline_seconds(None) == pytest.approx(core.DEFAULT_DEADLINE_MS / 1000 - 0.15)
    assert core.deadline_seconds("nan") == pytest.approx(core.DEFAULT_DEADLINE_MS / 1000 - 0.15)


@pytest.mark.parametrize("body,error", [
    ([], "invalid_body"), ({"seq": 3}, "invalid_message_id"),
    ({"message_id": "m", "seq": 0}, "invalid_seq"), ({"message_id": "m", "seq": True}, "invalid_seq"),
    ({"message_id": "x" * 129, "seq": 3}, "invalid_message_id")])
def test_request_parsing_rejects_bad_input(body, error):
    assert core.parse_request(body) == (None, error)


def test_the_lexical_deps_never_read_vectors():
    with pytest.raises(AssertionError):
        core.deps().stored_vectors(UID, "m", ["a"])
    assert core.deps().encoder is None


# --------------------------------------------------------------------------- #
# 5. the consumer
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture()
def consumer_env(monkeypatch):
    consumer = _consumer(monkeypatch)
    rows = _rows(WINDOWS["two"])
    picked, trace, log, _ = _enclave_resident(rows, _moments())
    answer = {"source": "local", "messages": _page(rows), "context_memories": picked,
              "context_memory_trace": trace, "context_memory_log": log}
    calls = {"post": [], "get": []}
    state = {"post": lambda: _Resp(200, answer), "get": lambda: _Resp(200, answer)}

    def post(url, **kw):
        calls["post"].append((url, kw))
        result = state["post"]()
        if isinstance(result, Exception):
            raise result
        return result

    def get(url, **kw):
        calls["get"].append((url, kw))
        return state["get"]()
    monkeypatch.setattr(consumer, "_HTTP", types.SimpleNamespace(post=post))
    monkeypatch.setattr(consumer, "_ENCLAVE_CLIENT", types.SimpleNamespace(get=get))
    monkeypatch.setattr(consumer, "FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setitem(consumer._whoami_cache, "content_encryption_effective", "off")
    msg = {"id": rows[-1]["id"], "seq": rows[-1]["seq"]}
    return types.SimpleNamespace(consumer=consumer, calls=calls, state=state, msg=msg,
                                 answer=answer)


def test_a_plaintext_account_uses_the_backend_and_not_the_enclave(consumer_env):
    c = consumer_env
    result = c.consumer._auto_memory_fetch_for_turn(c.msg)
    assert result is not None and result["selected"] == len(c.answer["context_memories"])
    assert len(c.calls["post"]) == 1 and c.calls["get"] == []
    url, kw = c.calls["post"][0]
    assert url.endswith("/v1/memory/turn-selection")
    assert kw["json"] == {"message_id": c.msg["id"], "seq": c.msg["seq"]}
    assert kw["headers"]["X-Recall-Deadline-Ms"]


@pytest.mark.parametrize("effective", ["on", "", "weird"])
def test_encrypted_or_unknown_accounts_never_call_the_backend(consumer_env, monkeypatch, effective):
    c = consumer_env
    monkeypatch.setitem(c.consumer._whoami_cache, "content_encryption_effective", effective)
    c.state["post"] = lambda: pytest.fail("the backend route was called")
    assert c.consumer._auto_memory_fetch_for_turn(c.msg) is not None
    assert len(c.calls["get"]) == 1


@pytest.mark.parametrize("status", [404, 409])
def test_404_and_409_fall_back_to_the_enclave_once(consumer_env, status):
    c = consumer_env
    c.state["post"] = lambda: _Resp(status, {"error": "not_served"})
    assert c.consumer._auto_memory_fetch_for_turn(c.msg) is not None
    assert len(c.calls["get"]) == 1


@pytest.mark.parametrize("outcome", ["503", "500", "401", "timeout"])
def test_other_failures_are_unknown_and_never_retry_the_enclave(consumer_env, outcome):
    c = consumer_env
    c.state["post"] = ((lambda: TimeoutError("read timeout")) if outcome == "timeout"
                       else (lambda: _Resp(int(outcome), {"error": "x"})))
    assert c.consumer._auto_memory_fetch_for_turn(c.msg) is None
    assert c.calls["get"] == []


def test_the_page_check_still_applies_to_the_backend_answer(consumer_env):
    c = consumer_env
    mismatched = {**c.answer, "messages": c.answer["messages"][:-1]}
    c.state["post"] = lambda: _Resp(200, mismatched)
    assert c.consumer._auto_memory_fetch_for_turn(c.msg) is None      # unknown, not 0
    failed = {**c.answer, "context_memory_log": {**c.answer["context_memory_log"], "mode": "failed"}}
    c.state["post"] = lambda: _Resp(200, failed)
    assert c.consumer._auto_memory_fetch_for_turn(c.msg) is None


def test_a_healthy_empty_pick_is_zero_not_unknown(consumer_env):
    c = consumer_env
    empty = {**c.answer, "context_memories": [], "context_memory_trace": {"selected": []}}
    c.state["post"] = lambda: _Resp(200, empty)
    result = c.consumer._auto_memory_fetch_for_turn(c.msg)
    assert result is not None and result["selected"] == 0


# --------------------------------------------------------------------------- #
# 6. the route
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from asgi.deps import require_auth
    from chat import routes_asgi

    app = FastAPI()
    app.include_router(routes_asgi.router)
    app.dependency_overrides[require_auth] = lambda: types.SimpleNamespace(
        store=types.SimpleNamespace(user_id=UID))
    seen = []
    monkeypatch.setattr(core, "select_for_turn",
                        lambda store, request, deadline: seen.append((request, deadline))
                        or ({"source": "local"}, 200))
    return TestClient(app), seen


def test_the_route_refuses_an_oversized_body_before_any_work(client):
    c, seen = client
    r = c.post("/v1/memory/turn-selection", content=b"{" + b" " * 2000 + b"}",
               headers={"content-type": "application/json"})
    assert (r.status_code, r.json()["error"]) == (413, "request_too_large")
    assert seen == []


def test_the_route_rejects_invalid_bodies_with_a_stable_slug(client):
    c, seen = client
    r = c.post("/v1/memory/turn-selection", content=b"not json")
    assert (r.status_code, r.json()) == (400, {"error": "request_invalid", "detail": "invalid_body"})
    r = c.post("/v1/memory/turn-selection", json={"message_id": "m1"})
    assert r.json()["detail"] == "invalid_seq"
    assert seen == []


def test_the_route_passes_a_clamped_deadline(client):
    c, seen = client
    r = c.post("/v1/memory/turn-selection", json={"message_id": "m1", "seq": 2},
               headers={"X-Recall-Deadline-Ms": "999999"})
    assert r.status_code == 200
    assert seen == [({"message_id": "m1", "seq": 2},
                     pytest.approx(core.MAX_DEADLINE_MS / 1000 - 0.15, abs=0.02))]


def test_chunked_body_stops_reading_as_soon_as_it_exceeds_the_limit():
    from starlette.requests import Request
    from chat import routes_asgi
    chunks = []

    async def receive():
        chunks.append(1)
        return {"type": "http.request", "body": b"x" * 1025,
                "more_body": len(chunks) < 3}

    request = Request({"type": "http", "headers": []}, receive=receive)
    response = asyncio.run(routes_asgi.resident_turn_selection(
        request, types.SimpleNamespace(store=types.SimpleNamespace(user_id=UID))))
    assert response.status_code == 413
    assert len(chunks) == 1


def test_zero_budget_never_starts_work():
    calls = []
    status, _ = plaintext_recall.run_bounded(lambda: calls.append(1), 0,
                                             threading.BoundedSemaphore(1))
    assert status == "timeout" and calls == []
    assert core.deadline_seconds("0") == 0


def test_thread_start_failure_does_not_leak_a_permit(monkeypatch):
    permit = threading.BoundedSemaphore(1)
    monkeypatch.setattr(threading.Thread, "start", lambda _: (_ for _ in ()).throw(
        RuntimeError("cannot start thread")))
    status, error = plaintext_recall.run_bounded(lambda: None, 1, permit)
    assert status == "error" and isinstance(error, RuntimeError)
    assert permit.acquire(blocking=False)
    permit.release()


@pytest.mark.parametrize("target", ["account", "trace"])
def test_account_checks_and_diagnostics_cannot_overrun_the_response_budget(turn, monkeypatch, target):
    monkeypatch.setenv(core.MODE_ENV, "on")
    release = threading.Event()
    entered = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(0.6)
        return "off"

    if target == "account":
        monkeypatch.setattr(core.accounts_registry, "effective_content_encryption", slow)
    else:
        monkeypatch.setattr(core.debug_trace, "trace_event", slow)
    try:
        started = time.monotonic()
        _, status = core.select_for_turn(turn.store, turn.request, 0.08)
        assert time.monotonic() - started < 0.4
        assert status == (503 if target == "account" else 200)
        assert entered.wait(0.2)
    finally:
        release.set()


def test_shadow_thread_start_failure_keeps_the_enclave_result(turn, monkeypatch):
    monkeypatch.setenv(core.MODE_ENV, "shadow")
    original = threading.Thread.start

    def start(thread):
        if thread.name == "resident-recall-shadow":
            raise RuntimeError("cannot start shadow")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    body, status = core.select_for_turn(turn.store, turn.request, 1)
    assert status == 200 and body["source"] == "enclave_shadow"
    assert core._SHADOW_PERMIT.acquire(blocking=False)
    core._SHADOW_PERMIT.release()


@pytest.mark.parametrize("hybrid_fallback", [False, True])
def test_shadow_uses_the_frozen_normalized_sealed_card_diagnostics(hybrid_fallback):
    import nacl.public
    from test_plaintext_recall import _sealed_moment
    sk = nacl.public.PrivateKey.generate()
    rows = _rows(WINDOWS["cn"])
    moments = _moments() + [_sealed_moment(sk)]
    decrypted, _ = enclave_chat._decrypt_history_items(rows, UID, sk)
    args = {**plaintext_recall.RESIDENT_QUERY_ARGS, "authorized_user_id": UID,
            "content_sk": sk, "input_fp_out": {}}
    if hybrid_fallback:
        args["hybrid"] = {"deadline": time.monotonic() + 5, "started": time.monotonic(),
                          "embedder": None, "model_id": None, "stored": None,
                          "fallback_reason": "embedder_unavailable", "vectors_ms": None,
                          "vectors_requested": 0, "vectors_rejected": 0}
    full, _, _ = enclave_chat._build_context_memories(moments, decrypted, args)
    assert "sealed_cat" in [c["id"] for c in full]
    local = plaintext_recall.select(UID, 1, _lexical_deps(rows, moments),
                                    query_args=plaintext_recall.RESIDENT_QUERY_ARGS, hybrid=False)
    assert local.sealed_cards == 1
    verdict = core.compare(local, args["input_fp_out"])
    assert verdict["verdict"] == "normalized" and verdict["same"] is True
    assert verdict["full_same"] is False


@pytest.mark.parametrize("candidates", ["none", "retired", "local_only"])
def test_no_eligible_cards_matches_the_enclave_stash_and_render(candidates, monkeypatch):
    rows = _rows(WINDOWS["cn"])
    moments = [] if candidates == "none" else _moments()
    for moment in moments:
        if candidates == "retired":
            moment["status"] = "archived"  # an actual retired lifecycle state in card_shape
            moment["body"] = json.dumps({**json.loads(moment["body"]), "status": "archived"})
        else:
            moment["visibility"] = "local_only"
    picked, trace, _, _ = _enclave_resident(rows, moments)
    local = plaintext_recall.select(UID, 1, _lexical_deps(rows, moments),
                                    query_args=plaintext_recall.RESIDENT_QUERY_ARGS, hybrid=False)
    assert picked == local.payload["context_memories"] == []
    consumer = _consumer(monkeypatch)
    theirs = consumer._stash_auto_memories(picked, trace)
    ours = consumer._stash_auto_memories(local.payload["context_memories"],
                                         local.payload["context_memory_trace"])
    assert ours == theirs
    assert consumer._auto_memory_render(ours, []) == consumer._auto_memory_render(theirs, [])


def test_backend_enclave_call_is_bound_to_the_authenticated_user(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret")
    calls = []

    def get(path, api_key, *, params, runtime_token):
        claims = core.runtime_token.verify(b"test-secret", runtime_token)
        calls.append((path, api_key, params, claims))
        return {"user_id": "someone_else"}, ""

    monkeypatch.setattr(core.core_enclave, "_enclave_get_json_for_gate", get)
    with pytest.raises(RuntimeError, match="enclave_user_mismatch"):
        core.enclave_select(UID, 17)
    path, api_key, params, claims = calls[0]
    assert path == "/v1/chat/history" and api_key is None
    assert params == {"before_seq": 18, "limit": 4, "include_image_body": "false", "context_trace": "1"}
    assert claims["user_id"] == UID and claims["scope"] == ["envelope_decrypt"]


def test_unknown_hybrid_status_cannot_be_blanked_into_agreement():
    local = _local(_rows(WINDOWS["cn"]))
    diag = {"input_fingerprint": {**local.input_fingerprint, "hybrid": "unknown"},
            "summary": local.summary}
    assert core.compare(local, diag)["verdict"] == "unmeasured"


def test_slow_body_read_spends_the_same_deadline(client):
    from starlette.requests import Request
    from chat import routes_asgi
    _, seen = client

    async def receive():
        await asyncio.sleep(0.3)
        return {"type": "http.request", "body": b'{"message_id":"m1","seq":2}',
                "more_body": False}

    request = Request({"type": "http", "headers": [(b"x-recall-deadline-ms", b"200")]}, receive)
    started = time.monotonic()
    response = asyncio.run(routes_asgi.resident_turn_selection(
        request, types.SimpleNamespace(store=types.SimpleNamespace(user_id=UID))))
    assert response.status_code == 503 and seen == []
    assert time.monotonic() - started < 0.25


def test_expired_threadpool_queue_never_starts_selection(client):
    import anyio.to_thread
    from starlette.requests import Request
    from chat import routes_asgi
    _, seen = client

    async def check():
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 1
        await limiter.acquire()

        async def receive():
            return {"type": "http.request", "body": b'{"message_id":"m1","seq":2}',
                    "more_body": False}

        request = Request({"type": "http", "headers": [(b"x-recall-deadline-ms", b"200")]}, receive)
        task = asyncio.create_task(routes_asgi.resident_turn_selection(
            request, types.SimpleNamespace(store=types.SimpleNamespace(user_id=UID))))
        try:
            await asyncio.sleep(0.15)
            assert task.done(), "the route is still waiting for threadpool capacity"
            response = await task
            assert response.status_code == 503 and seen == []
        finally:
            limiter.release()
            await task
        await asyncio.sleep(0.03)
        assert seen == [], "expired queued work ran after capacity became available"

    asyncio.run(check())


def test_timed_out_work_keeps_capacity_until_it_really_finishes():
    permit = threading.BoundedSemaphore(1)
    release = threading.Event()
    entered = threading.Event()

    def work():
        entered.set()
        release.wait(1)

    try:
        assert plaintext_recall.run_bounded(work, 0.02, permit)[0] == "timeout"
        assert entered.is_set()
        calls = []
        assert plaintext_recall.run_bounded(lambda: calls.append(1), 1, permit)[0] == "busy"
        assert calls == []
    finally:
        release.set()
    assert permit.acquire(timeout=0.5)
    permit.release()


def test_a_worker_scheduled_after_the_deadline_does_not_start_io(monkeypatch):
    original = threading.Thread.start
    calls = []

    def delayed_start(thread):
        time.sleep(0.04)
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", delayed_start)
    status, _ = plaintext_recall.run_bounded(lambda: calls.append(1), 0.01,
                                             threading.BoundedSemaphore(1))
    assert status == "timeout" and calls == []


def test_shadow_limits_the_entire_background_job_including_trace(turn, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    starts = []
    original = threading.Thread.start

    def start(thread):
        starts.append(thread.name)
        return original(thread)

    def trace(*args, **kwargs):
        entered.set()
        release.wait(1)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(core.debug_trace, "trace_event", trace)
    try:
        core._submit_shadow(turn.store, UID, turn.request["seq"], None)
        assert entered.wait(0.5)
        for _ in range(20):
            core._submit_shadow(turn.store, UID, turn.request["seq"], None)
        assert starts == ["resident-recall-shadow"]
    finally:
        release.set()
    assert core._SHADOW_PERMIT.acquire(timeout=0.5)
    core._SHADOW_PERMIT.release()


@pytest.mark.parametrize("body,error", [
    ({"message_id": 1, "seq": 2}, "invalid_message_id"),
    ({"message_id": "m1", "seq": 2**63 - 1}, "invalid_seq"),
    ({"message_id": "m1", "seq": 2, "user_id": "someone_else"}, "invalid_body"),
])
def test_request_cannot_change_user_or_overflow_the_page_cursor(body, error):
    assert core.parse_request(body) == (None, error)
