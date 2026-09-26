"""T523 hybrid automatic recall: default-off byte identity, fusion, whole-turn fallback,
bounded encoding, vector authorization, and sweep/recall projection parity.

Run with the pinned memgarden (0.22.0):
    python -m pytest tests/test_memory_recall_hybrid.py -q
"""
from __future__ import annotations

import base64
import importlib.metadata
import json
import math
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

import recall_golden_cases as golden  # noqa: E402
from enclave import recall_hybrid  # noqa: E402
from enclave.routes import chat  # noqa: E402
from memory import card_shape, recall_metadata  # noqa: E402
from memory.embedding import fake as fake_embedding  # noqa: E402
from memory.embedding import projection, serve, sweep  # noqa: E402

GOLDEN = json.loads(golden.GOLDEN_PATH.read_text())
BASELINE_SHA = "834c154586b557afc84673253a3b50af22ea22c8"


@pytest.fixture(autouse=True)
def _recent_original():
    if not golden._ORIG_RECENT:
        golden._ORIG_RECENT.append(recall_metadata.recent_cards)


# --------------------------------------------------------------------------- #
# 1. flag off == pre-change behaviour, byte for byte
# --------------------------------------------------------------------------- #

def test_golden_provenance_is_the_pre_change_baseline():
    assert GOLDEN["generated_from"] == BASELINE_SHA
    assert GOLDEN["memgarden"] == importlib.metadata.version("memgarden") == "0.22.0"
    assert set(GOLDEN["cases"]) == {name for name, *_ in golden.SCENARIOS}


@pytest.mark.parametrize("flag", [None, "0", "false"])
@pytest.mark.parametrize("scenario", golden.SCENARIOS, ids=[s[0] for s in golden.SCENARIOS])
def test_flag_off_matches_pre_change_golden(monkeypatch, scenario, flag):
    if flag is None:
        monkeypatch.delenv(recall_hybrid.HYBRID_ENV, raising=False)
    else:
        monkeypatch.setenv(recall_hybrid.HYBRID_ENV, flag)
    name, window, args, ranker = scenario
    got = golden.run_scenario(chat, recall_metadata, monkeypatch, name, window, args, ranker)
    assert got == GOLDEN["cases"][name]
    assert "hybrid" not in got["context_memory_log"]
    assert ":hybrid" not in got["context_memory_log"]["mode"]


def test_flag_off_selection_never_asks_for_raw_bodies(monkeypatch):
    calls = []
    monkeypatch.setattr(chat.readside, "moments_to_cards",
                        lambda *a, **k: calls.append(k) or golden._garden())
    chat._build_context_memories([], golden.WINDOW_CN, {**golden.BASE_ARGS, "want_trace": True})
    assert calls == [{}]


def test_start_warmup_is_a_noop_while_off(monkeypatch):
    monkeypatch.delenv(recall_hybrid.HYBRID_ENV, raising=False)
    assert recall_hybrid.start_warmup() is False


# --------------------------------------------------------------------------- #
# 2. hybrid on (FakeEmbedder): fusion, and every failure falls back as a whole
# --------------------------------------------------------------------------- #

BODIES = {
    "sofa": {"summary": "The cat naps on the green sofa every afternoon", "content": "", "status": "active"},
    "ikea": {"summary": "The new bookshelf came from IKEA", "content": "", "status": "active"},
    "guitar": {"summary": "Practices guitar thirty minutes a day", "content": "", "status": "active"},
}
for _i in range(12):
    BODIES[f"f{_i}"] = {"summary": f"weekly report and meeting notes number {_i}", "content": "",
                        "status": "active"}
PARAPHRASE_WINDOW = [{"role": "user", "content": "Where does my feline like to nap?"}]
TWO_TURN_WINDOW = [{"role": "user", "content": "Did I buy anything for the living room lately?"},
                   {"role": "assistant", "content": "tell me more"},
                   {"role": "user", "content": "Where does my feline like to nap?"}]


def _cards():
    return [{"id": mid, "summary": b["summary"], "content": b["content"], "status": "active",
             "created_at": "2026-01-01T00:00:00+00:00"} for mid, b in BODIES.items()]


def _embedder():
    return fake_embedding.FakeEmbedder(dim=64, aliases={"feline": "cat"})


def _stored(embedder, bodies=BODIES):
    out = {}
    for mid, body in bodies.items():
        digest, text = projection.body_projection(body)
        out[mid] = (digest, embedder.encode_passages([text])[0])
    return out


def _state(embedder=None, **over):
    embedder = embedder or _embedder()
    state = {"deadline": time.monotonic() + 5.0, "started": time.monotonic(), "embedder": embedder,
             "model_id": embedder.model_id, "stored": _stored(embedder),
             "fallback_reason": None, "vectors_ms": 1.0, "vectors_rejected": 0}
    state.update(over)
    return state


def _patch_cards(monkeypatch):
    def fake(moments, uid, sk, inner_out=None):
        if inner_out is not None:
            inner_out.update({mid: dict(b) for mid, b in BODIES.items()})
        return _cards()
    monkeypatch.setattr(chat.readside, "moments_to_cards", fake)


def _run(monkeypatch, window, hybrid=None):
    _patch_cards(monkeypatch)
    args = {**golden.BASE_ARGS, "want_trace": True, "context_recent": False}
    if hybrid is not None:
        args["hybrid"] = hybrid
    return chat._build_context_memories([], window, args)


@pytest.fixture()
def hybrid_on(monkeypatch):
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    # FakeEmbedder shares one alias token between query and target: cosine ~0.13.
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "0.1")
    monkeypatch.delenv(chat.RECALL_RANKER_ENV, raising=False)


def test_hybrid_recalls_the_paraphrased_card_lexical_misses(monkeypatch, hybrid_on):
    lexical, _, lex_log = _run(monkeypatch, PARAPHRASE_WINDOW)
    assert "sofa" not in [c["id"] for c in lexical]
    picked, trace, log = _run(monkeypatch, PARAPHRASE_WINDOW,
                              _state())
    assert "sofa" in [c["id"] for c in picked]
    assert log["mode"].endswith(":hybrid")
    assert log["hybrid"]["status"] == "active" and log["hybrid"]["fallback_reason"] is None
    assert log["hybrid"]["with_vector"] == len(BODIES) and log["hybrid"]["hash_mismatch"] == 0
    assert log["hybrid"]["lanes"][0]["source"] == "current"
    # content-free record: no text, no vectors
    blob = json.dumps(log, ensure_ascii=False)
    assert "feline" not in blob and "sofa every" not in blob and "vector_b64" not in blob


def _assert_same_selection(a, b):
    (pa, ta, la), (pb, tb, lb) = a, b
    assert pa == pb and ta == tb
    for key in ("injected_ids", "counts", "by_bucket", "rejected_reasons", "query_fingerprint"):
        assert la.get(key) == lb.get(key)


@pytest.mark.parametrize("reason", ["embedder_loading", "embedder_unavailable", "vectors_unavailable",
                                    "deadline_exceeded", "vectors_model_mismatch", "legacy_ranker"])
def test_precomputed_fallback_reasons_give_the_exact_lexical_result(monkeypatch, hybrid_on, reason):
    lexical = _run(monkeypatch, TWO_TURN_WINDOW)
    got = _run(monkeypatch, TWO_TURN_WINDOW, _state(fallback_reason=reason))
    _assert_same_selection(got, lexical)
    assert got[2]["mode"] == lexical[2]["mode"] + ":hybrid-fallback"
    assert got[2]["hybrid"]["fallback_reason"] == reason and got[2]["hybrid"]["status"] == "fallback"


def test_expired_deadline_falls_back_before_encoding(monkeypatch, hybrid_on):
    lexical = _run(monkeypatch, TWO_TURN_WINDOW)
    got = _run(monkeypatch, TWO_TURN_WINDOW, _state(deadline=time.monotonic() - 1.0))
    _assert_same_selection(got, lexical)
    assert got[2]["hybrid"]["fallback_reason"] == "deadline_exceeded"


def test_encoder_failure_falls_back(monkeypatch, hybrid_on):
    def boom(*a, **k):
        raise recall_hybrid.Fallback("encode_failed")
    monkeypatch.setattr(recall_hybrid, "encode_queries", boom)
    lexical = _run(monkeypatch, TWO_TURN_WINDOW)
    got = _run(monkeypatch, TWO_TURN_WINDOW, _state())
    _assert_same_selection(got, lexical)
    assert got[2]["hybrid"]["fallback_reason"] == "encode_failed"


def test_failure_on_the_second_pass_discards_the_whole_turn(monkeypatch, hybrid_on):
    """current succeeds with vectors, combined raises: nothing hybrid may survive."""
    real = chat.mg_retrieval.select_context
    seen = []

    def flaky(query, cards, **kw):
        seen.append(("vector" if kw.get("query_vector") is not None else "lexical", query))
        if kw.get("query_vector") is not None and "\n" in query:
            from memgarden.scoring.hybrid import VectorContractError
            raise VectorContractError("dimension mismatch")
        return real(query, cards, **kw)

    lexical = _run(monkeypatch, TWO_TURN_WINDOW)
    monkeypatch.setattr(chat.mg_retrieval, "select_context", flaky)
    seen.clear()
    got = _run(monkeypatch, TWO_TURN_WINDOW, _state())
    assert ("vector", TWO_TURN_WINDOW[-1]["content"]) in seen  # the first pass did run hybrid
    _assert_same_selection(got, lexical)
    assert got[2]["hybrid"]["fallback_reason"] == "vector_contract_error"


def test_invalid_min_cosine_at_selection_time_falls_back(monkeypatch, hybrid_on):
    lexical = _run(monkeypatch, TWO_TURN_WINDOW)
    state = _state()
    monkeypatch.setattr(recall_hybrid, "min_cosine", lambda: 2.0)  # memgarden rejects it
    got = _run(monkeypatch, TWO_TURN_WINDOW, state)
    _assert_same_selection(got, lexical)
    assert got[2]["hybrid"]["fallback_reason"] == "vector_contract_error"


def test_hash_mismatch_drops_only_that_cards_vector(monkeypatch, hybrid_on):
    state = _state()
    digest, vector = state["stored"]["sofa"]
    state["stored"]["sofa"] = ("0" * 16, vector)
    picked, _, log = _run(monkeypatch, PARAPHRASE_WINDOW, state)
    assert log["hybrid"]["hash_mismatch"] == 1
    assert log["hybrid"]["with_vector"] == len(BODIES) - 1
    assert "sofa" not in [c["id"] for c in picked]  # no vector, and lexical never matched it


def test_vectors_for_cards_outside_the_candidate_set_are_ignored(monkeypatch, hybrid_on):
    state = _state()
    state["stored"]["someone_else"] = state["stored"]["sofa"]
    _, _, log = _run(monkeypatch, PARAPHRASE_WINDOW, state)
    assert log["hybrid"]["with_vector"] == len(BODIES)


def test_empty_query_skips_hybrid_and_stays_lexical(monkeypatch, hybrid_on):
    lexical = _run(monkeypatch, golden.WINDOW_EMPTY)
    got = _run(monkeypatch, golden.WINDOW_EMPTY, _state())
    _assert_same_selection(got, lexical)
    assert got[2]["hybrid"]["fallback_reason"] == "empty_query"


# --------------------------------------------------------------------------- #
# 3. begin(), bounded encoding, vector decoding
# --------------------------------------------------------------------------- #

def test_begin_reports_why_hybrid_cannot_run(monkeypatch):
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    monkeypatch.delenv(recall_hybrid.MIN_COSINE_ENV, raising=False)
    assert recall_hybrid.begin(True)["fallback_reason"] == "min_cosine_unset"
    assert recall_hybrid.begin(False)["fallback_reason"] == "legacy_ranker"
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "nan")
    assert recall_hybrid.begin(True)["fallback_reason"] == "min_cosine_unset"
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "0.8")
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "loading")
    state = recall_hybrid.begin(True)
    assert state["fallback_reason"] == "embedder_loading" and state["model_id"] is None
    embedder = _embedder()
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "ready")
    monkeypatch.setattr(recall_hybrid, "_embedder", embedder)
    state = recall_hybrid.begin(True)
    assert state["fallback_reason"] is None and state["model_id"] == embedder.model_id


class _SlowEmbedder:
    dim = 64

    def __init__(self, release: threading.Event):
        self.release = release
        self.inner = _embedder()

    def encode_query(self, text):
        self.release.wait(5)
        return self.inner.encode_query(text)


def test_encoding_is_bounded_by_deadline_and_in_flight_cap():
    release = threading.Event()
    slow = _SlowEmbedder(release)
    results = []

    def caller(text):
        try:
            recall_hybrid.encode_queries(slow, [text], time.monotonic() + 10)
            results.append("ok")
        except recall_hybrid.Fallback as exc:
            results.append(exc.reason)

    try:
        # a job that outlives its deadline keeps its permit while it runs
        with pytest.raises(recall_hybrid.Fallback) as first:
            recall_hybrid.encode_queries(slow, ["a"], time.monotonic() + 0.05)
        assert first.value.reason == "deadline_exceeded"
        waiter = threading.Thread(target=caller, args=("b",))  # takes the second permit, queued
        waiter.start()
        deadline = time.monotonic() + 2
        while recall_hybrid._in_flight._value > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert recall_hybrid._in_flight._value == 0
        with pytest.raises(recall_hybrid.Fallback) as busy:
            recall_hybrid.encode_queries(slow, ["c"], time.monotonic() + 1.0)
        assert busy.value.reason == "embedder_busy"
    finally:
        release.set()
    waiter.join(10)
    assert results == ["ok"]
    deadline = time.monotonic() + 5
    while recall_hybrid._in_flight._value < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recall_hybrid._in_flight._value == 2  # every permit came back
    vectors = recall_hybrid.encode_queries(_embedder(), ["ok"], time.monotonic() + 2)
    assert len(vectors) == 1 and len(vectors[0]) == 64


def _row(mid, vector, digest="h" * 16):
    return {"id": mid, "projection_hash": digest,
            "vector_b64": base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()}


def test_decode_vectors_rejects_foreign_model_and_bad_rows():
    unit = [1.0] + [0.0] * 63
    with pytest.raises(recall_hybrid.Fallback) as exc:
        recall_hybrid.decode_vectors({"model_id": "other", "vectors": []}, "m", 64)
    assert exc.value.reason == "vectors_model_mismatch"
    rows = [_row("ok", unit), _row("ok", unit), _row("short", unit[:10]),
            _row("nan", [math.nan] + [0.0] * 63), _row("not_unit", [2.0] + [0.0] * 63),
            {"id": "bad_b64", "projection_hash": "x", "vector_b64": "@@@"}]
    out, rejected = recall_hybrid.decode_vectors({"model_id": "m", "vectors": rows}, "m", 64)
    assert list(out) == ["ok"] and rejected == 5


# --------------------------------------------------------------------------- #
# 4. backend: the caller's own eligible, hash-current vectors only
# --------------------------------------------------------------------------- #

class _Store:
    user_id = "usr_a"


def _moment(mid, body, owner="usr_a", **extra):
    m = {"id": mid, "owner_user_id": owner, "visibility": "shared", "status": "active",
         "body": json.dumps(body)}
    m.update(extra)
    return m


def test_vectors_endpoint_serves_only_current_eligible_requested_rows(monkeypatch):
    unit = [1.0] + [0.0] * 3
    bodies = {"ok": {"summary": "a"}, "edited": {"summary": "b"}, "local": {"summary": "c"},
              "retired": {"summary": "d", "archived_at": "2026-01-01"},
              "encrypted": {"summary": "e"}, "foreign": {"summary": "f"}, "unasked": {"summary": "g"}}
    moments = [_moment("ok", bodies["ok"]), _moment("edited", bodies["edited"]),
               _moment("local", bodies["local"], visibility="local_only"),
               _moment("retired", bodies["retired"]),
               _moment("encrypted", bodies["encrypted"], body_ct="xx", K_enclave="kk"),
               _moment("foreign", bodies["foreign"], owner="usr_b"),
               _moment("unasked", bodies["unasked"])]
    stored = {mid: (projection.body_projection(b)[0], unit) for mid, b in bodies.items()}
    stored["edited"] = ("stalehash0000000", unit)
    stored["deleted"] = ("whatever00000000", unit)  # card gone, row not yet pruned
    loads = []
    monkeypatch.setattr(serve.service, "_load_moments", lambda store: moments)
    monkeypatch.setattr(serve.service, "_active_memory_moments", lambda ms: ms)
    monkeypatch.setattr(serve.db, "memory_vectors_load",
                        lambda uid, model: loads.append((uid, model)) or stored)
    asked = ["ok", "edited", "local", "retired", "encrypted", "foreign", "deleted", "ok"]
    body, status = serve.authorized_vectors(_Store(), {"model_id": "model-x", "ids": asked,
                                                       "user_id": "usr_b"})
    assert status == 200 and loads == [("usr_a", "model-x")]  # body user_id ignored
    assert [row["id"] for row in body["vectors"]] == ["ok"]
    assert body["dim"] == 4 and body["count"] == 1
    decoded = struct.unpack("<4f", base64.b64decode(body["vectors"][0]["vector_b64"]))
    assert list(decoded) == unit


def test_vectors_endpoint_validates_model_and_ids(monkeypatch):
    monkeypatch.setattr(serve.service, "_load_moments", lambda store: [])
    monkeypatch.setattr(serve.db, "memory_vectors_load", lambda uid, model: {})
    bad = [{}, {"model_id": "", "ids": []}, {"model_id": "m" * 301, "ids": []},
           {"model_id": "m"}, {"model_id": "m", "ids": "x"}, {"model_id": "m", "ids": [1]},
           {"model_id": "m", "ids": [""]}, {"model_id": "m", "ids": ["x" * 201]},
           {"model_id": "m", "ids": ["a"] * (serve.MAX_IDS + 1)}, None]
    for payload in bad:
        assert serve.authorized_vectors(_Store(), payload)[1] == 400, payload
    assert serve.authorized_vectors(_Store(), {"model_id": "m", "ids": []}) == (
        {"model_id": "m", "dim": 0, "count": 0, "vectors": []}, 200)


def test_candidates_beyond_any_global_cap_still_get_their_vectors(monkeypatch):
    """F2: the request names this turn's candidates, so a large garden cannot
    push them out (the old global sort-and-cap could)."""
    unit = [1.0, 0.0]
    moments = [_moment(f"c{i:04d}", {"summary": f"s{i}"}) for i in range(2500)]
    stored = {m["id"]: (projection.body_projection(json.loads(m["body"]))[0], unit) for m in moments}
    monkeypatch.setattr(serve.service, "_load_moments", lambda store: moments)
    monkeypatch.setattr(serve.service, "_active_memory_moments", lambda ms: ms)
    monkeypatch.setattr(serve.db, "memory_vectors_load", lambda uid, model: stored)
    wanted = ["c2499", "c2400", "c0001"]
    body, status = serve.authorized_vectors(_Store(), {"model_id": "m", "ids": wanted})
    assert status == 200 and sorted(r["id"] for r in body["vectors"]) == sorted(wanted)


def test_vectors_route_is_a_post_that_requires_auth():
    from asgi.deps import require_auth
    from memory import routes_asgi
    route = next(r for r in routes_asgi.router.routes if getattr(r, "path", "") == "/v1/memory/vectors")
    assert route.methods == {"POST"}
    assert require_auth in [d.call for d in route.dependant.dependencies]


# --------------------------------------------------------------------------- #
# 5. one projection for the sweep and for recall
# --------------------------------------------------------------------------- #

def _old_sweep_hash(body):
    garden = card_shape.to_garden_card(body)
    garden["retrieval_cues"] = body.get("retrieval_cues")
    return projection.projection_hash(garden), projection.card_projection_text(garden)


@pytest.mark.parametrize("body", [
    {"summary": "s", "content": "c", "bucket": "b", "threads": ["t1", "t2"]},
    {"summary": "s", "retrieval_cues": ["cue one", "cue two"], "title": "ignored?"},
    {"content": "only content", "retrieval_cues": "not-a-list"},
    {"summary": "  padded  ", "content": "padded", "threads": ["padded", "x"]},
    {"summary": "中文卡片", "content": "喜欢猫", "retrieval_cues": ["猫咪", "cat"]},
])
def test_shared_projection_equals_the_pre_refactor_sweep_formula(body):
    assert projection.body_projection(body) == _old_sweep_hash(body)


def test_sweep_uses_the_shared_projection(monkeypatch):
    moments = [_moment("x", {"summary": "hello", "retrieval_cues": ["greet"]})]
    monkeypatch.setattr(sweep.service, "_active_memory_moments", lambda ms: ms)
    eligible, _ = sweep._eligible(moments, "usr_a")
    assert eligible["x"] == projection.body_projection({"summary": "hello", "retrieval_cues": ["greet"]})


# --------------------------------------------------------------------------- #
# 6. route: the vector read happens only when on, never for probes
# --------------------------------------------------------------------------- #

@pytest.fixture()
def route_client(monkeypatch):
    from asgi_test_client import _AsgiTestClient
    from enclave import auth as enclave_auth
    from enclave import backend_client, keys
    from enclave import state as enclave_state
    from enclave.routes import build_app

    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    calls = []

    async def fake_backend_get(path, headers, params=None):
        calls.append((path, dict(headers), dict(params or {})))
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        if path == "/v1/memory/list":
            return {"moments": LIST_MOMENTS, "total": len(LIST_MOMENTS)}
        return {}

    async def fake_backend_post(path, headers, payload):
        calls.append((path, dict(headers), dict(payload or {})))
        if path == "/v1/memory/vectors":
            return {"model_id": payload["model_id"], "dim": 64, "count": 0, "vectors": []}
        return {}

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    monkeypatch.setattr(backend_client, "backend_post", fake_backend_post)

    async def fake_sk():
        return object()

    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    return _AsgiTestClient(build_app()), calls


LIST_MOMENTS = [
    {"id": "plain1", "owner_user_id": "usr_a", "visibility": "shared", "body": "{}"},
    {"id": "enc1", "owner_user_id": "usr_a", "visibility": "shared", "body_ct": "ct", "K_enclave": "ke"},
    {"id": "local1", "owner_user_id": "usr_a", "visibility": "local_only", "body": "{}"},
    {"id": "theirs1", "owner_user_id": "usr_b", "visibility": "shared", "body": "{}"},
]


def _paths(calls):
    return [path for path, _, _ in calls]


def test_route_flag_off_never_reads_vectors(monkeypatch, route_client):
    monkeypatch.delenv(recall_hybrid.HYBRID_ENV, raising=False)
    client, calls = route_client
    r = client.get("/v1/chat/history?limit=1&context_trace=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert "/v1/memory/list" in _paths(calls) and "/v1/memory/vectors" not in _paths(calls)
    assert "hybrid" not in (r.json.get("context_memory_log") or {})


def test_route_flag_on_reads_vectors_as_the_same_user_and_skips_probes(monkeypatch, route_client):
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "0.1")
    embedder = _embedder()
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "ready")
    monkeypatch.setattr(recall_hybrid, "_embedder", embedder)
    monkeypatch.setattr(recall_hybrid, "start_warmup", lambda: False)
    client, calls = route_client
    r = client.get("/v1/chat/history?limit=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    vector_calls = [c for c in calls if c[0] == "/v1/memory/vectors"]
    list_calls = [c for c in calls if c[0] == "/v1/memory/list"]
    assert len(vector_calls) == 1
    assert vector_calls[0][1] == list_calls[0][1]  # the same forwarded user credentials
    assert vector_calls[0][2]["model_id"] == embedder.model_id
    assert vector_calls[0][2]["ids"] == ["plain1"]  # never encrypted or local_only ids
    assert "user_id" not in vector_calls[0][2]
    log = r.json["context_memory_log"]
    assert log["hybrid"]["fallback_reason"] == "empty_query"  # no user message in the page
    calls.clear()
    r = client.get("/v1/chat/history?limit=1&probe=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200 and "/v1/memory/vectors" not in _paths(calls)


def test_route_flag_on_but_embedder_loading_does_not_read_vectors(monkeypatch, route_client):
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "0.1")
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "loading")
    monkeypatch.setattr(recall_hybrid, "start_warmup", lambda: False)
    client, calls = route_client
    r = client.get("/v1/chat/history?limit=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200 and "/v1/memory/vectors" not in _paths(calls)
    assert r.json["context_memory_log"]["hybrid"]["fallback_reason"] == "embedder_loading"


# --------------------------------------------------------------------------- #
# 7. V2 carries the hybrid record into memory.recall.completed only when present
# --------------------------------------------------------------------------- #

def _render(log):
    from model_api_runtime.v2 import memory_context
    return memory_context.render({"context_memories": [], "context_memory_log": log})


def test_v2_render_is_unchanged_without_a_hybrid_record():
    view = _render({"mode": "relevant:unified:x"})
    assert set(view) == {"block", "ids", "chars", "selected", "selection_status"}


def test_v2_recall_completed_gets_flat_hybrid_fields_only_when_present():
    import asyncio
    from types import SimpleNamespace
    from model_api_runtime.v2 import memory_recall

    @memory_recall.traced
    async def turn(**kwargs):
        return SimpleNamespace(stop_reason="final_text")

    plain, with_hybrid = [], []
    asyncio.run(turn(dispatch_tools=None, on_memory_recall_completed=plain.append,
                     memory_context_observation=_render({"mode": "relevant:unified:x"})))
    assert not any(k.startswith("hybrid_") or k == "selection_mode" for k in plain[0])
    record = {"status": "fallback", "fallback_reason": "deadline_exceeded", "encode_ms": 2001.5,
              "vectors_ms": 3.0, "vectors_received": 9, "vectors_rejected": 0, "with_vector": 9,
              "hash_mismatch": 0, "lanes": [{"source": "current"}]}
    view = _render({"mode": "relevant:unified:x:hybrid-fallback", "hybrid": record})
    asyncio.run(turn(dispatch_tools=None, on_memory_recall_completed=with_hybrid.append,
                     memory_context_observation=view))
    detail = with_hybrid[0]
    assert detail["selection_mode"] == "relevant:unified:x:hybrid-fallback"
    assert detail["hybrid_status"] == "fallback" and detail["hybrid_fallback_reason"] == "deadline_exceeded"
    assert detail["hybrid_encode_ms"] == 2001.5 and detail["hybrid_with_vector"] == 9
    assert all(not isinstance(v, (dict, list)) for k, v in detail.items() if k.startswith("hybrid_"))


# --------------------------------------------------------------------------- #
# 8. F1/F2: honest timings and requested-ids-only vectors
# --------------------------------------------------------------------------- #

def _clock(monkeypatch, start=100.0):
    now = [start]
    monkeypatch.setattr(recall_hybrid.time, "monotonic", lambda: now[0])
    return now


def test_vectors_ms_is_the_requests_own_round_trip(monkeypatch):
    import asyncio
    from enclave import backend_client
    now = _clock(monkeypatch)
    embedder = _embedder()
    unit = embedder.encode_query("x")

    async def slow_post(path, headers, payload):
        now[0] += 0.010  # the request itself takes 10 ms
        return {"model_id": embedder.model_id, "vectors": [_row("a", unit)]}

    monkeypatch.setattr(backend_client, "backend_post", slow_post)
    state = {"deadline": now[0] + 5, "model_id": embedder.model_id, "embedder": embedder,
             "fallback_reason": None, "stored": None, "vectors_rejected": 0}
    now[0] += 1.0  # a second spent elsewhere (history decrypt, list) before the read
    asyncio.run(recall_hybrid.fetch_vectors({}, state, ["a"]))
    now[0] += 1.0  # and a second after it before selection
    assert state["vectors_ms"] == 10.0
    assert state["vectors_requested"] == 1 and list(state["stored"]) == ["a"]


def test_vectors_for_ids_not_requested_are_rejected(monkeypatch):
    import asyncio
    from enclave import backend_client
    embedder = _embedder()
    unit = embedder.encode_query("x")

    async def post(path, headers, payload):
        return {"model_id": embedder.model_id, "vectors": [_row("a", unit), _row("smuggled", unit)]}

    monkeypatch.setattr(backend_client, "backend_post", post)
    state = {"deadline": time.monotonic() + 5, "model_id": embedder.model_id, "embedder": embedder,
             "fallback_reason": None, "stored": None, "vectors_rejected": 0}
    asyncio.run(recall_hybrid.fetch_vectors({}, state, ["a"]))
    assert list(state["stored"]) == ["a"] and state["vectors_rejected"] == 1


def test_fetch_failure_and_deadline_are_recorded_not_raised(monkeypatch):
    import asyncio
    from enclave import backend_client

    async def boom(path, headers, payload):
        raise RuntimeError("backend down")

    monkeypatch.setattr(backend_client, "backend_post", boom)
    embedder = _embedder()
    base = {"model_id": embedder.model_id, "embedder": embedder, "fallback_reason": None,
            "stored": None, "vectors_rejected": 0}
    state = {**base, "deadline": time.monotonic() + 5}
    asyncio.run(recall_hybrid.fetch_vectors({}, state, ["a"]))
    assert state["fallback_reason"] == "vectors_unavailable" and state["vectors_ms"] is not None
    state = {**base, "deadline": time.monotonic() - 1}
    asyncio.run(recall_hybrid.fetch_vectors({}, state, ["a"]))
    assert state["fallback_reason"] == "deadline_exceeded"


def test_plaintext_candidate_ids_skip_encrypted_local_only_and_foreign():
    me = {"owner_user_id": "usr_a"}
    moments = [{"id": "p", "body": "{}", **me}, {"id": "b64", "body_b64": "e30=", **me},
               {"id": "ct", "body_ct": "x", **me}, {"id": "ke", "K_enclave": "k", "body": "{}", **me},
               {"id": "lo", "visibility": "local_only", "body": "{}", **me}, {"id": "nobody", **me},
               {"id": "theirs", "body": "{}", "owner_user_id": "usr_b"}, {"id": "ownerless", "body": "{}"},
               {"id": "p", "body": "{}", **me}, "junk", {"body": "{}", **me}]
    assert recall_hybrid.plaintext_candidate_ids(moments, "usr_a") == ["p", "b64"]


def test_encode_timing_splits_queue_and_compute():
    timing = {}
    vectors = recall_hybrid.encode_queries(_embedder(), ["hello"], time.monotonic() + 5, timing)
    assert len(vectors) == 1
    assert set(timing) == {"encode_queue_ms", "encode_compute_ms"}
    assert timing["encode_queue_ms"] >= 0 and timing["encode_compute_ms"] >= 0


def test_selection_record_carries_the_split_timings(monkeypatch, hybrid_on):
    _, _, log = _run(monkeypatch, PARAPHRASE_WINDOW, _state(vectors_requested=15))
    record = log["hybrid"]
    assert record["status"] == "active" and record["vectors_requested"] == 15
    assert {"encode_ms", "encode_queue_ms", "encode_compute_ms", "vectors_ms"} <= set(record)


# --------------------------------------------------------------------------- #
# 9. real envelopes: readside inner_out keeps every existing gate; the route
#    asks only for plaintext ids and drops a smuggled vector for an encrypted card
# --------------------------------------------------------------------------- #

def _real_moments(sk, owner="usr_a"):
    import nacl.public  # noqa: F401
    from test_enclave_envelope_core import _make_envelope
    pk = bytes(sk.public_key)
    plain = {"summary": "The cat naps on the green sofa every afternoon", "content": ""}
    secret = {"summary": "Encrypted diary line about the cat", "content": ""}
    enc = _make_envelope(owner, "enc", json.dumps(secret).encode(), pk)
    tampered = _make_envelope(owner, "tampered", json.dumps(secret).encode(), pk)
    tampered["body_ct"] = base64.b64encode(b"\x00" * 40).decode()
    return [
        {"id": "plain", "owner_user_id": owner, "visibility": "shared", "status": "active",
         "body": json.dumps(plain)},
        {**enc, "visibility": "shared", "status": "active"},
        {"id": "local", "owner_user_id": owner, "visibility": "local_only", "body": json.dumps(plain)},
        {"id": "foreign", "owner_user_id": "usr_b", "visibility": "shared", "body": json.dumps(plain)},
        {**tampered, "visibility": "shared"},
    ], plain, secret


def test_real_readside_inner_out_only_holds_cards_that_passed_the_gates():
    import nacl.public
    from enclave import readside
    sk = nacl.public.PrivateKey.generate()
    moments, plain, secret = _real_moments(sk)
    baseline = readside.moments_to_cards(moments, "usr_a", sk)
    inner: dict = {}
    cards = readside.moments_to_cards(moments, "usr_a", sk, inner_out=inner)
    assert cards == baseline                          # the new parameter changes no output
    assert [c["id"] for c in cards] == ["plain", "enc"]  # local_only, foreign, tampered dropped
    assert set(inner) == {"plain", "enc"}
    assert inner["plain"]["summary"] == plain["summary"]
    assert recall_hybrid.plaintext_candidate_ids(moments, "usr_a") == ["plain"]


def test_route_real_crypto_requests_plaintext_ids_and_rejects_encrypted_vector(monkeypatch):
    import nacl.public
    from asgi_test_client import _AsgiTestClient
    from enclave import auth as enclave_auth
    from enclave import backend_client, keys
    from enclave import state as enclave_state
    from enclave.routes import build_app

    sk = nacl.public.PrivateKey.generate()
    moments, plain, secret = _real_moments(sk)
    embedder = _embedder()
    vec = {mid: embedder.encode_passages([projection.body_projection(body)[1]])[0]
           for mid, body in (("plain", plain), ("enc", secret))}
    posts = []

    async def fake_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [{"id": "m1", "owner_user_id": "usr_a", "role": "user",
                                  "ts": 1.0, "v": 1, "body": "Where does my feline like to nap?"}],
                    "total": 1}
        if path == "/v1/memory/list":
            return {"moments": moments, "total": len(moments)}
        return {}

    async def fake_post(path, headers, payload):
        posts.append((path, payload))
        # A misbehaving backend also sends a matching vector for the encrypted card.
        return {"model_id": payload["model_id"], "vectors": [
            _row("plain", vec["plain"], projection.body_projection(plain)[0]),
            _row("enc", vec["enc"], projection.body_projection(secret)[0])]}

    async def fake_sk():
        return sk

    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    monkeypatch.setattr(backend_client, "backend_get", fake_get)
    monkeypatch.setattr(backend_client, "backend_post", fake_post)
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "0.1")
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "ready")
    monkeypatch.setattr(recall_hybrid, "_embedder", embedder)
    monkeypatch.setattr(recall_hybrid, "start_warmup", lambda: False)
    client = _AsgiTestClient(build_app())
    r = client.get("/v1/chat/history?limit=5&context_trace=1", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert posts == [("/v1/memory/vectors", {"model_id": embedder.model_id, "ids": ["plain"]})]
    log = r.json["context_memory_log"]
    assert log["hybrid"]["status"] == "active"
    assert log["hybrid"]["vectors_rejected"] == 1 and log["hybrid"]["with_vector"] == 1
    assert "plain" in log["injected_ids"]
    blob = json.dumps(log, ensure_ascii=False)
    assert "green sofa" not in blob and "Encrypted diary" not in blob and "feline" not in blob


# --------------------------------------------------------------------------- #
# 10. /healthz deploy-state evidence (T739): flag values, model state, RSS
# --------------------------------------------------------------------------- #

def test_healthz_reports_flag_off_and_model_not_loaded(monkeypatch):
    from enclave.routes import health
    monkeypatch.delenv(recall_hybrid.HYBRID_ENV, raising=False)
    monkeypatch.delenv(recall_hybrid.MIN_COSINE_ENV, raising=False)
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "idle")
    monkeypatch.setattr(recall_hybrid, "_embedder", None)
    body = health._health_body()
    assert body["recall_hybrid"] == {"enabled": False, "min_cosine": None,
                                     "embedder_state": "not_loaded", "failure_reason": None,
                                     "model_id": None}
    assert body["rss_kb"] is None or (isinstance(body["rss_kb"], int) and body["rss_kb"] > 0)


@pytest.mark.parametrize("state,reason,label,expect_reason", [
    ("loading", "loading", "loading", None),
    ("unavailable", "model_directory_missing", "failed", "model_directory_missing"),
])
def test_healthz_embedder_state_labels(monkeypatch, state, reason, label, expect_reason):
    from enclave.routes import health
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_hybrid.MIN_COSINE_ENV, "0.80")
    monkeypatch.setattr(recall_hybrid, "_embedder_state", state)
    monkeypatch.setattr(recall_hybrid, "_embedder_reason", reason)
    monkeypatch.setattr(recall_hybrid, "_embedder", None)
    snap = health._health_body()["recall_hybrid"]
    assert snap["enabled"] is True and snap["min_cosine"] == 0.80
    assert snap["embedder_state"] == label and snap["failure_reason"] == expect_reason


def test_healthz_loaded_model_id_is_a_bounded_prefix_without_paths(monkeypatch):
    from enclave.routes import health
    monkeypatch.setenv(recall_hybrid.HYBRID_ENV, "1")
    embedder = _embedder()
    monkeypatch.setattr(recall_hybrid, "_embedder_state", "ready")
    monkeypatch.setattr(recall_hybrid, "_embedder", embedder)
    snap = health._health_body()["recall_hybrid"]
    assert snap["embedder_state"] == "loaded"
    assert snap["model_id"] == embedder.model_id[:48] and len(snap["model_id"]) <= 48
    assert "/opt" not in json.dumps(snap) and "models/" not in json.dumps(snap)


def test_rss_kb_reads_vmrss(monkeypatch, tmp_path):
    from enclave.routes import health
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmPeak:\t 999 kB\nVmRSS:\t  123456 kB\n")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(status if p == "/proc/self/status" else p, *a, **k))
    assert health._rss_kb() == 123456
