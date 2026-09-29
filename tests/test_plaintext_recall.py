"""T779 step 2b: plaintext accounts select context cards next to the data.

Pins: the local path reads exactly what the enclave reads and selects the same
cards (BM25 and hybrid); every sealed piece keeps the enclave path; the shadow
comparison uses the enclave's real input fingerprint (and its normalized
baseline when sealed cards are present); the parent encoder gives queries
priority over sweep segments, bounds its queue, requires the token and never
truncates; slot processes cannot build the model; mode off is unchanged.
"""
from __future__ import annotations

import base64
import json
import multiprocessing as mp
import os
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from core import history_view  # noqa: E402
from enclave import recall_hybrid  # noqa: E402
from enclave.routes import chat  # noqa: E402
from memory import plaintext_recall, recall_select  # noqa: E402
from memory.embedding import fake as fake_embedding  # noqa: E402
from memory.embedding import projection, query_client, query_service, recall_policy  # noqa: E402

UID = "usr_pt"
NOW_ISO = "2026-01-05T08:00:00+00:00"


def _moment(mid, body, **extra):
    row = {"id": mid, "owner_user_id": UID, "visibility": "shared", "status": "active",
           "body": json.dumps(body, ensure_ascii=False), "created_at": NOW_ISO}
    row.update(extra)
    return row


BODIES = {
    "sofa": {"summary": "The cat naps on the green sofa every afternoon", "content": ""},
    "ikea": {"summary": "The new bookshelf came from IKEA", "content": "Assembled it"},
    "guitar": {"summary": "Practices guitar thirty minutes a day", "content": ""},
    "cat_cn": {"summary": "咪咪是只三岁的橘猫，最近不太吃饭", "content": "兽医说先观察饮水"},
    "old": {"summary": "猫咪 旧卡", "content": "", "status": "retired"},
}
for _i in range(10):
    BODIES[f"f{_i}"] = {"summary": f"weekly report and meeting notes number {_i}", "content": ""}


def _moments():
    return [_moment(mid, body) for mid, body in BODIES.items()] + [
        _moment("local", BODIES["sofa"], visibility="local_only")]


def _rows(texts):
    rows = []
    for i, (role, text) in enumerate(texts, start=1):
        rows.append({"id": f"m{i}", "seq": i, "role": role, "ts": 1790000000.0 + i, "v": 1,
                     "source": "chat", "owner_user_id": UID, "body": text})
    return rows


WINDOWS = {
    "cn": [("user", "担心咪咪最近不吃饭")],
    "two": [("user", "Did I buy anything for the living room lately?"), ("agent", "tell me more"),
            ("user", "Where does my feline like to nap?")],
    "empty": [("agent", "你好呀")],
}


def _embedder():
    return fake_embedding.FakeEmbedder(dim=64, aliases={"feline": "cat"})


def _stored_payload(embedder, model_id, ids):
    rows = []
    for mid in ids:
        body = BODIES.get(mid)
        if body is None:
            continue
        digest, text = projection.body_projection(body)
        vec = embedder.encode_passages([text])[0]
        rows.append({"id": mid, "projection_hash": digest,
                     "vector_b64": base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode()})
    return {"model_id": model_id, "dim": embedder.dim, "count": len(rows), "vectors": rows}


class _FakeClient:
    def __init__(self, embedder, fail=None):
        self.embedder, self.fail, self.calls = embedder, fail, 0

    def encode(self, texts, deadline):
        self.calls += 1
        if self.fail:
            raise recall_policy.Fallback(self.fail)
        return query_client.Reply(self.embedder.model_id, self.embedder.dim,
                                  [self.embedder.encode_query(t) for t in texts],
                                  {"encode_queue_ms": 0.0, "encode_compute_ms": 1.0})


def _deps(rows, *, moments=None, encoder=None, embedder=None, mode="off"):
    embedder = embedder or _embedder()
    return plaintext_recall.Deps(
        effective_mode=lambda _u: mode,
        history_page=lambda _u, _s: [dict(r) for r in rows],
        list_moments=lambda _u, _l: [dict(m) for m in (moments if moments is not None else _moments())],
        stored_vectors=lambda _u, model_id, ids: _stored_payload(embedder, model_id, ids),
        encoder=encoder,
    )


def _enclave(rows, moments, *, hybrid=None, fp=False):
    decrypted, _ = chat._decrypt_history_items(rows, UID, None)
    args = {**plaintext_recall.V2_QUERY_ARGS, "authorized_user_id": UID, "content_sk": None}
    if hybrid is not None:
        args["hybrid"] = hybrid
    if fp:
        args["input_fp_out"] = {}
    picked, trace, log = chat._build_context_memories(moments, decrypted, args)
    return picked, trace, log, args.get("input_fp_out")


_TIMING = {"dur_ms", "encode_ms", "encode_queue_ms", "encode_compute_ms", "vectors_ms"}


def _strip_timing(value):
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items() if k not in _TIMING}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (recall_policy.HYBRID_ENV, recall_policy.MIN_COSINE_ENV, plaintext_recall.MODE_ENV,
                 recall_select.RECALL_RANKER_ENV, query_service.PORT_ENV, query_service.TOKEN_ENV,
                 query_service.OWNER_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    query_service.stop()      # the service is a process singleton; tests must not leak it


# --------------------------------------------------------------------------- #
# 1. same reads as the enclave
# --------------------------------------------------------------------------- #

def _plain_rows_every_reader_branch():
    good = base64.b64encode(b"\x89PNG....").decode()
    return [
        {"id": "a", "seq": 1, "role": "user", "ts": 1.0, "owner_user_id": UID, "body": "text"},
        {"id": "b", "seq": 2, "role": "user", "ts": 2.0, "owner_user_id": UID,
         "body": "both", "body_b64": base64.b64encode(b"ignored").decode()},
        {"id": "c", "seq": 3, "role": "user", "ts": 3.0, "owner_user_id": UID, "content_type": "image",
         "body_b64": "not-base64!!", "caption_body": "cap"},
        {"id": "d", "seq": 4, "role": "user", "ts": 4.0, "owner_user_id": UID, "body": 7},
        {"id": "e", "seq": 5, "role": "user", "ts": 5.0, "body": "no owner"},
        {"id": "f", "seq": 6, "role": "user", "ts": 6.0, "owner_user_id": "usr_other", "body": "foreign"},
        {"id": "g", "seq": 7, "role": "user", "ts": 7.0, "owner_user_id": UID, "content_type": "image",
         "body_b64": good, "caption_body": "plain caption"},
        {"id": "h", "seq": 8, "role": "user", "ts": 8.0, "owner_user_id": UID, "visibility": "local_only",
         "body": "local"},
        {"id": "i", "seq": 9, "role": "user", "ts": 9.0, "owner_user_id": UID, "content_type": "file",
         "body_omitted": True, "file_name": "x.txt", "caption_body": "omitted caption"},
    ]


def test_local_reader_view_equals_the_enclave_view_for_plaintext_rows():
    rows = _plain_rows_every_reader_branch()
    assert plaintext_recall.page_is_plaintext(rows)
    enclave = chat._decrypt_history_items([dict(r) for r in rows], UID, None)
    local = history_view.history_items([dict(r) for r in rows], plaintext_recall._row_reader(UID),
                                       plaintext_recall.PlaintextReadFailure)
    assert local == enclave
    # the branches really ran: two read failures with the enclave's own wording
    reasons = [e["reason"] for e in enclave[1]]
    assert "plaintext body must be a string" in reasons
    assert any(r.startswith("owner mismatch") for r in reasons)


@pytest.mark.parametrize("row,expected", [
    ({"content_type": "image", "body_b64": "eA==", "caption_body_ct": "c", "caption_nonce": "n"}, False),
    ({"content_type": "image", "body_b64": "eA==", "caption_body": "x", "caption_K_enclave": "k"}, False),
    ({"content_type": "image", "body_omitted": True, "caption_body": "x"}, True),
    ({"content_type": "file", "body_omitted": True, "caption_body_ct": "c"}, False),
    ({"body_omitted": True}, True),
    ({"visibility": "local_only", "body_ct": "c", "K_enclave": "k"}, True),
    ({"body_ct": "c", "K_enclave": "k"}, False),
    ({"body": "x", "K_enclave": "k"}, False),
    ({}, False),
])
def test_page_is_plaintext_uses_the_shared_sealed_test_per_piece(row, expected):
    base = {"id": "m1", "seq": 1, "role": "user", "ts": 1.0, "owner_user_id": UID}
    assert plaintext_recall.page_is_plaintext([{**base, **row}]) is expected


def test_plaintext_cards_equal_the_enclave_reader_and_count_sealed_rows():
    from enclave import readside
    moments = _moments() + [{"id": "sealed", "owner_user_id": UID, "visibility": "shared",
                             "body_ct": "Y3Q=", "nonce": "bg==", "K_enclave": "aw=="}]
    plain_only = [m for m in moments if m["id"] != "sealed"]
    enclave_cards = readside.moments_to_cards(plain_only, UID, None)
    inner: dict = {}
    cards, sealed = plaintext_recall.plaintext_cards(moments, UID, inner_out=inner)
    assert cards == enclave_cards
    assert sealed == 1
    assert "local" not in inner and set(inner) == {c["id"] for c in cards}


# --------------------------------------------------------------------------- #
# 2. same selection (frozen inputs, both paths)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("window", sorted(WINDOWS))
def test_bm25_local_selection_equals_the_enclave(window):
    rows = _rows(WINDOWS[window])
    moments = _moments()
    e_picked, e_trace, e_log, _ = _enclave([dict(r) for r in rows], [dict(m) for m in moments])
    local = plaintext_recall.select(UID, 3, _deps(rows, moments=moments))
    assert local.payload["context_memories"] == e_picked
    assert local.payload["context_memory_trace"] == e_trace
    assert _strip_timing(local.payload["context_memory_log"]) == _strip_timing(e_log)
    assert local.summary == recall_select.decision_summary(e_picked, e_log)


@pytest.fixture()
def hybrid_env(monkeypatch):
    monkeypatch.setenv(recall_policy.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_policy.MIN_COSINE_ENV, "0.1")


@pytest.mark.parametrize("window", ["two", "cn"])
def test_hybrid_local_selection_equals_the_enclave(hybrid_env, window):
    embedder = _embedder()
    rows = _rows(WINDOWS[window])
    moments = _moments()
    ids = recall_policy.plaintext_candidate_ids(moments, UID)
    stored, _ = recall_policy.decode_vectors(
        _stored_payload(embedder, embedder.model_id, ids), embedder.model_id, embedder.dim)
    state = {"deadline": time.monotonic() + 5.0, "started": time.monotonic(), "embedder": embedder,
             "model_id": embedder.model_id, "stored": stored, "fallback_reason": None,
             "vectors_ms": 1.0, "vectors_requested": len(ids), "vectors_rejected": 0}
    e_picked, e_trace, e_log, _ = _enclave([dict(r) for r in rows], [dict(m) for m in moments],
                                           hybrid=state)
    client = _FakeClient(embedder)
    local = plaintext_recall.select(UID, 3, _deps(rows, moments=moments, encoder=client,
                                                  embedder=embedder))
    assert client.calls == 1  # one encode request, no metadata round trip
    assert e_log["hybrid"]["status"] == "active"
    assert local.payload["context_memory_log"]["hybrid"]["status"] == "active"
    assert local.payload["context_memories"] == e_picked
    assert _strip_timing(local.payload["context_memory_trace"]) == _strip_timing(e_trace)
    assert local.summary == recall_select.decision_summary(e_picked, e_log)


@pytest.mark.parametrize("fail", ["encoder_service_unavailable", "deadline_exceeded",
                                  "encoder_busy", "encode_failed", "query_too_large"])
def test_encoder_failure_keeps_the_exact_lexical_result(hybrid_env, fail):
    rows = _rows(WINDOWS["two"])
    lexical = plaintext_recall.select(UID, 3, _deps(rows))  # hybrid on, but no encoder
    failed = plaintext_recall.select(UID, 3, _deps(rows, encoder=_FakeClient(_embedder(), fail=fail)))
    assert failed.payload["context_memories"] == lexical.payload["context_memories"]
    assert failed.payload["context_memory_log"]["hybrid"]["fallback_reason"] == fail
    assert failed.payload["context_memory_log"]["mode"].endswith(":hybrid-fallback:recent7d")


def test_model_id_mismatch_in_stored_vectors_falls_back_as_a_whole(hybrid_env):
    rows = _rows(WINDOWS["two"])
    embedder = _embedder()
    deps = _deps(rows, encoder=_FakeClient(embedder), embedder=embedder)
    deps.stored_vectors = lambda _u, _m, ids: _stored_payload(embedder, "another-model", ids)
    local = plaintext_recall.select(UID, 3, deps)
    assert local.payload["context_memory_log"]["hybrid"]["fallback_reason"] == "vectors_model_mismatch"


# --------------------------------------------------------------------------- #
# 3. what keeps the enclave path
# --------------------------------------------------------------------------- #

def test_encrypted_account_and_sealed_history_are_not_served_here():
    rows = _rows(WINDOWS["cn"])
    with pytest.raises(plaintext_recall.NotServedHere) as enc:
        plaintext_recall.select(UID, 1, _deps(rows, mode="on"))
    assert enc.value.reason == "account_encrypted"
    sealed = rows + [{"id": "s", "seq": 9, "role": "user", "ts": 9.0, "owner_user_id": UID,
                      "body_ct": "Y3Q=", "K_enclave": "aw=="}]
    with pytest.raises(plaintext_recall.NotServedHere) as hist:
        plaintext_recall.select(UID, 9, _deps(sealed))
    assert hist.value.reason == "sealed_history"


# --------------------------------------------------------------------------- #
# 4. shadow evidence: the enclave's real fingerprint and normalized baseline
# --------------------------------------------------------------------------- #

def test_shadow_is_comparable_when_both_read_the_same_inputs():
    rows = _rows(WINDOWS["cn"])
    _, _, _, diag = _enclave([dict(r) for r in rows], _moments(), fp=True)
    local = plaintext_recall.select(UID, 1, _deps(rows))
    verdict = plaintext_recall.compare(local, diag)
    assert verdict == {"verdict": "comparable", "same": True, "diff": []}
    assert "normalized" not in diag


def test_shadow_with_sealed_cards_compares_against_the_normalized_baseline():
    import nacl.public
    from test_enclave_envelope_core import _make_envelope

    sk = nacl.public.PrivateKey.generate()
    secret = {"summary": "咪咪最近不吃饭，一直趴在沙发上", "content": "", "title": "猫咪"}
    sealed = {**_make_envelope(UID, "sealed_cat", json.dumps(secret).encode(), bytes(sk.public_key)),
              "visibility": "shared", "status": "active", "created_at": NOW_ISO}
    rows = _rows(WINDOWS["cn"])
    moments = _moments() + [sealed]
    decrypted, _ = chat._decrypt_history_items([dict(r) for r in rows], UID, sk)
    args = {**plaintext_recall.V2_QUERY_ARGS, "authorized_user_id": UID, "content_sk": sk,
            "input_fp_out": {}}
    full, _, _ = chat._build_context_memories([dict(m) for m in moments], decrypted, args)
    diag = args["input_fp_out"]
    assert "sealed_cat" in [c["id"] for c in full]           # the enclave really decrypted it
    local = plaintext_recall.select(UID, 1, _deps(rows, moments=moments))
    assert local.sealed_cards == 1
    verdict = plaintext_recall.compare(local, diag)
    assert verdict["verdict"] == "normalized" and verdict["same"] is True
    assert verdict["full_same"] is False                     # the approved policy difference


def test_shadow_catches_a_local_path_that_drops_a_plaintext_card(monkeypatch):
    rows = _rows(WINDOWS["cn"])
    _, _, _, diag = _enclave([dict(r) for r in rows], _moments(), fp=True)
    original = plaintext_recall.plaintext_cards

    def lossy(moments, uid, inner_out=None):
        cards, sealed = original(moments, uid, inner_out)
        return [c for c in cards if c["id"] != "cat_cn"], sealed
    monkeypatch.setattr(plaintext_recall, "plaintext_cards", lossy)
    local = plaintext_recall.select(UID, 1, _deps(rows))
    assert plaintext_recall.compare(local, diag)["verdict"] == "incomparable"


def test_shadow_without_enclave_diagnostics_is_unmeasured_not_equal():
    local = plaintext_recall.select(UID, 1, _deps(_rows(WINDOWS["cn"])))
    assert plaintext_recall.compare(local, None)["verdict"] == "unmeasured"
    assert plaintext_recall.compare(local, {"input_fingerprint": {}, "summary": {}})["verdict"] == "unmeasured"


def test_shadow_trace_survives_the_durable_detail_cap():
    import debug_trace
    local = plaintext_recall.select(UID, 1, _deps(_rows(WINDOWS["cn"])))
    detail = {"driver": "v2", **plaintext_recall.compare(local, None), "local_ms": 1.0,
              "sealed_cards": 0, "local_hybrid": None, "remote_hybrid": None}
    for sample in (detail, {"driver": "v2", "verdict": "normalized", "same": True, "diff": [],
                            "full_same": False, "local_ms": 1.0, "sealed_cards": 3,
                            "local_hybrid": "ready", "remote_hybrid": "ready"}):
        assert debug_trace._safe_detail(sample) == sample


# --------------------------------------------------------------------------- #
# 5. serve_worker modes
# --------------------------------------------------------------------------- #

def _enclave_reply(user_id, params):
    body = {"user_id": user_id, "context_memories": [{"id": "e"}], "context_memory_trace": {},
            "context_memory_log": {"mode": "x"}}
    if params.get("context_input_fp") == "1":
        body["context_input_diagnostics"] = {"input_fingerprint": {"x": 1}, "summary": {"ids": "y"}}
    return body


@pytest.fixture()
def worker(monkeypatch):
    from model_api_runtime.v2 import serve_worker
    calls = []

    def fake_gate(path, _api_key, *, params, runtime_token):
        calls.append(dict(params))
        return _enclave_reply(UID, params), None
    monkeypatch.setattr(serve_worker.core_enclave, "_enclave_get_json_for_gate", fake_gate)
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _u: "rt")
    return serve_worker, calls


def test_mode_off_is_the_enclave_call_exactly_as_before(worker, monkeypatch):
    serve_worker, calls = worker
    touched = []
    monkeypatch.setattr(plaintext_recall, "select", lambda *a, **k: touched.append(1))
    monkeypatch.setattr(serve_worker, "_submit_recall_shadow", lambda *a: touched.append(2))
    out = serve_worker._read_context_memories(UID, through_seq=7)
    assert calls == [{"before_seq": 8, "limit": 4, "include_image_body": "0",
                      "context_trace": "1", "context_recent": "1"}]
    assert out == {"context_memories": [{"id": "e"}], "context_memory_trace": {},
                   "context_memory_log": {"mode": "x"}}
    assert touched == []
    assert query_service.active_scheduler() is None


def test_mode_shadow_keeps_the_enclave_result_and_compares(worker, monkeypatch):
    serve_worker, calls = worker
    monkeypatch.setenv(plaintext_recall.MODE_ENV, "shadow")
    emitted = []
    monkeypatch.setattr(serve_worker, "_emit_v2_debug_trace_for_user",
                        lambda uid, event, **kw: emitted.append((event, kw["detail"])))
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps", lambda: _deps(_rows(WINDOWS["cn"])))
    out = serve_worker._read_context_memories(UID, through_seq=1)
    assert out["context_memories"] == [{"id": "e"}]
    assert calls[0]["context_input_fp"] == "1"
    for _ in range(200):
        if emitted:
            break
        time.sleep(0.01)
    assert emitted and emitted[0][0] == "memory.recall.shadow"
    assert emitted[0][1]["verdict"] in {"incomparable", "comparable"}


def test_mode_on_serves_plaintext_locally_and_falls_back_to_the_enclave(worker, monkeypatch):
    serve_worker, calls = worker
    monkeypatch.setenv(plaintext_recall.MODE_ENV, "on")
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps", lambda: _deps(_rows(WINDOWS["cn"])))
    out = serve_worker._read_context_memories(UID, through_seq=1)
    assert calls == [] and "cat_cn" in [c["id"] for c in out["context_memories"]]
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps",
                        lambda: _deps(_rows(WINDOWS["cn"]), mode="on"))
    out = serve_worker._read_context_memories(UID, through_seq=1)
    assert len(calls) == 1 and out["context_memories"] == [{"id": "e"}]


def test_shadow_runs_one_at_a_time_and_never_blocks_the_turn(worker, monkeypatch):
    serve_worker, _ = worker
    emitted = []
    monkeypatch.setattr(serve_worker, "_emit_v2_debug_trace_for_user",
                        lambda uid, event, **kw: emitted.append(kw["detail"]))
    gate = threading.Event()

    def slow_select(*_a, **_k):
        gate.wait(5)
        raise plaintext_recall.NotServedHere("sealed_history")
    monkeypatch.setattr(plaintext_recall, "select", slow_select)
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps", lambda: None)
    started = time.monotonic()
    serve_worker._submit_recall_shadow(UID, 1, None)
    serve_worker._submit_recall_shadow(UID, 1, None)
    assert time.monotonic() - started < 1.0
    assert {"verdict": "skipped", "reason": "shadow_busy"}.items() <= emitted[0].items()
    gate.set()
    for _ in range(200):
        if len(emitted) == 2:
            break
        time.sleep(0.01)
    assert emitted[1]["verdict"] == "not_served"
    assert serve_worker._RECALL_SHADOW_PERMIT.acquire(blocking=False)
    serve_worker._RECALL_SHADOW_PERMIT.release()


def test_enclave_response_is_unchanged_without_the_fingerprint_parameter():
    rows = _rows(WINDOWS["cn"])
    _, _, _, diag = _enclave(rows, _moments(), fp=False)
    assert diag is None


# --------------------------------------------------------------------------- #
# 6. parent encoder: priority, bounds, token, no truncation
# --------------------------------------------------------------------------- #

class _GatedEmbedder:
    model_id, dim = "gated", 4

    def __init__(self):
        self.order, self.release = [], {}

    def _gate(self, key):
        self.order.append(key)
        event = self.release.setdefault(key, threading.Event())
        event.wait(5)

    def encode_query(self, text):
        self._gate(("q", text))
        return [1.0, 0.0, 0.0, 0.0]

    def encode_passages(self, texts):
        self._gate(("s", tuple(texts)))
        return [[0.0, 1.0, 0.0, 0.0] for _ in texts]


def _wait(cond):
    for _ in range(500):
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_a_waiting_query_runs_before_the_next_sweep_segment():
    """Segment "a" is computing; segment "b" is queued BEFORE the query arrives.
    When "a" ends the worker must take the query, not "b" (order of arrival
    would pick "b")."""
    emb = _GatedEmbedder()
    sched = query_service.Scheduler(emb)
    try:
        seg_a = threading.Thread(target=lambda: sched.encode_segment(["a"]))
        seg_a.start()
        assert _wait(lambda: emb.order == [("s", ("a",))])
        seg_b = threading.Thread(target=lambda: sched.encode_segment(["b"]))
        seg_b.start()
        assert _wait(lambda: len(sched._segments) == 1)       # "b" is waiting
        job = sched.submit_query(["hello"], time.monotonic() + 5)
        emb.release.setdefault(("s", ("a",)), threading.Event()).set()
        assert _wait(lambda: len(emb.order) == 2)
        assert emb.order[1] == ("q", "hello"), emb.order      # not segment "b"
        emb.release.setdefault(("q", "hello"), threading.Event()).set()
        assert job.done.wait(2) and job.vectors == [[1.0, 0.0, 0.0, 0.0]]
        emb.release.setdefault(("s", ("b",)), threading.Event()).set()
        seg_a.join(2)
        seg_b.join(2)
        assert emb.order == [("s", ("a",)), ("q", "hello"), ("s", ("b",))]
    finally:
        for event in emb.release.values():
            event.set()
        sched.close()


def test_queue_bound_expired_drop_and_a_running_job_keeps_the_worker():
    emb = _GatedEmbedder()
    sched = query_service.Scheduler(emb)
    try:
        running = sched.submit_query(["first"], time.monotonic() + 5)
        assert _wait(lambda: emb.order == [("q", "first")])
        expired = sched.submit_query(["late"], time.monotonic() + 0.01)
        queued = [sched.submit_query([f"q{i}"], time.monotonic() + 5) for i in range(3)]
        with pytest.raises(query_service.Refused) as busy:
            sched.submit_query(["overflow"], time.monotonic() + 5)
        assert busy.value.reason == "encoder_busy"
        time.sleep(0.05)
        assert not running.done.is_set()            # caller may give up; the job still holds the worker
        emb.release.setdefault(("q", "first"), threading.Event()).set()
        assert expired.done.wait(2) and expired.error == "deadline_exceeded"
        assert ("q", "late") not in emb.order       # dropped, never computed
        for i, job in enumerate(queued):
            emb.release.setdefault(("q", f"q{i}"), threading.Event()).set()
            assert job.done.wait(2)
    finally:
        for event in emb.release.values():
            event.set()
        sched.close()


def test_close_ends_queued_work_as_unavailable():
    emb = _GatedEmbedder()
    sched = query_service.Scheduler(emb)
    sched.submit_query(["busy"], time.monotonic() + 5)
    assert _wait(lambda: emb.order == [("q", "busy")])
    queued = sched.submit_query(["waiting"], time.monotonic() + 5)
    sched.close()
    assert queued.done.wait(2) and queued.error == "encoder_service_unavailable"
    for event in emb.release.values():
        event.set()


@pytest.fixture()
def service(monkeypatch):
    emb = _embedder()
    monkeypatch.setenv(query_service.PORT_ENV, "0")
    query_service.reserve(port=0)
    # bind an ephemeral port: reserve() wrote 0, start() binds and reports it
    port = query_service.start(emb)
    monkeypatch.setenv(query_service.PORT_ENV, str(port))
    yield emb, port, os.environ[query_service.TOKEN_ENV]
    query_service.stop()


def test_client_gets_vectors_with_the_token_and_is_refused_without_it(service):
    emb, port, token = service
    reply = query_client.Client(port, token).encode(["where does my feline nap"], time.monotonic() + 5)
    assert reply.model_id == emb.model_id and reply.dim == emb.dim
    assert reply.vectors == [emb.encode_query("where does my feline nap")]
    with pytest.raises(recall_policy.Fallback) as bad:
        query_client.Client(port, "wrong").encode(["x"], time.monotonic() + 5)
    assert bad.value.reason == "encode_failed"


def test_oversized_query_is_refused_never_truncated(service):
    _, port, token = service
    big = "猫" * (query_service.MAX_TEXT_BYTES // 3 + 1)
    with pytest.raises(recall_policy.Fallback) as exc:
        query_client.Client(port, token).encode([big], time.monotonic() + 5)
    assert exc.value.reason == "query_too_large"
    with pytest.raises(query_service.Refused) as server_side:
        query_service._encode_request(query_service.active_scheduler(), {"texts": [big], "budget_ms": 1000})
    assert server_side.value.reason == "query_too_large"


def test_client_reports_an_absent_service_and_a_spent_budget():
    with pytest.raises(recall_policy.Fallback) as down:
        query_client.Client(1, "t").encode(["x"], time.monotonic() + 1)
    assert down.value.reason == "encoder_service_unavailable"
    with pytest.raises(recall_policy.Fallback) as late:
        query_client.Client(1, "t").encode(["x"], time.monotonic() - 1)
    assert late.value.reason == "deadline_exceeded"


def test_sweep_encodes_directly_without_the_service_and_in_segments_with_it(monkeypatch):
    class Recorder:
        def __init__(self):
            self.calls = []

        def encode_passages(self, texts):
            self.calls.append(list(texts))
            return [[1.0] for _ in texts]

    rec = Recorder()
    monkeypatch.setattr(query_service, "_scheduler", None)
    assert query_service.encode_passages(rec, list("abcdefghij")) == [[1.0]] * 10
    assert rec.calls == [list("abcdefghij")]
    rec2 = Recorder()
    sched = query_service.Scheduler(rec2)
    monkeypatch.setattr(query_service, "_scheduler", sched)
    try:
        assert query_service.encode_passages(rec2, list("abcdefghij")) == [[1.0]] * 10
        assert rec2.calls == [list("abcd"), list("efgh"), list("ij")]
    finally:
        sched.close()


# --------------------------------------------------------------------------- #
# 7. slot processes never build the model (real spawn)
# --------------------------------------------------------------------------- #

def _child_builds_embedder(conn):
    sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
    from memory.embedding import e5_onnx, sweep
    built = []
    original = e5_onnx.E5SmallOnnxEmbedder.__init__

    def record(self, *a, **k):
        built.append(1)
        return original(self, *a, **k)
    e5_onnx.E5SmallOnnxEmbedder.__init__ = record
    import importlib
    importlib.import_module("model_api_runtime.v2.serve_worker")  # the slot assembly import
    try:
        sweep.get_embedder()
        conn.send(("built", len(built)))
    except RuntimeError as exc:
        conn.send((str(exc), len(built)))


def test_a_spawned_slot_process_refuses_to_build_the_model(monkeypatch):
    monkeypatch.setenv(query_service.OWNER_ENV, str(os.getpid()))   # this process owns the model
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_child_builds_embedder, args=(child,))
    proc.start()
    assert parent.poll(60), "child did not report"
    message, built = parent.recv()
    proc.join(10)
    assert message == "embedder_not_owned_by_this_process"
    assert built == 0


def test_the_owner_process_may_build_it(monkeypatch):
    monkeypatch.setenv(query_service.OWNER_ENV, "0")
    query_service.claim_embedder_ownership()
    assert os.environ[query_service.OWNER_ENV] == str(os.getpid())
    query_service.assert_embedder_owner()        # no raise in the owner


# --------------------------------------------------------------------------- #
# 8. codex r1: diagnostics never change the result; bounded reads; evidence-based
#    fingerprint; wire size
# --------------------------------------------------------------------------- #

def _sealed_moment(sk, mid="sealed_cat"):
    from test_enclave_envelope_core import _make_envelope
    secret = {"summary": "咪咪最近不吃饭，一直趴在沙发上", "content": "", "title": "猫咪"}
    return {**_make_envelope(UID, mid, json.dumps(secret).encode(), bytes(sk.public_key)),
            "visibility": "shared", "status": "active", "created_at": NOW_ISO}


def _enclave_with_sk(rows, moments, sk, *, hybrid=None, fp=True):
    decrypted, _ = chat._decrypt_history_items([dict(r) for r in rows], UID, sk)
    args = {**plaintext_recall.V2_QUERY_ARGS, "authorized_user_id": UID, "content_sk": sk}
    if hybrid is not None:
        args["hybrid"] = hybrid
    if fp:
        args["input_fp_out"] = {}
    picked, trace, log = chat._build_context_memories([dict(m) for m in moments], decrypted, args)
    return picked, trace, log, args.get("input_fp_out")


@pytest.mark.parametrize("target", ["input_fingerprint", "decision_summary"])
def test_a_diagnostics_failure_keeps_the_normal_result(monkeypatch, target):
    rows = _rows(WINDOWS["cn"])
    plain_picked, plain_trace, plain_log, _ = _enclave([dict(r) for r in rows], _moments())
    local = plaintext_recall.select(UID, 1, _deps(rows))     # before the fault is injected

    def boom(*_a, **_k):
        raise RuntimeError("diag")
    monkeypatch.setattr(recall_select, target, boom)
    picked, trace, log, diag = _enclave([dict(r) for r in rows], _moments(), fp=True)
    assert picked == plain_picked and trace == plain_trace
    assert _strip_timing(log) == _strip_timing(plain_log)
    assert diag == {"error": "diagnostics_failed:RuntimeError"}
    assert plaintext_recall.compare(local, diag)["verdict"] == "unmeasured"


def test_a_failing_normalized_rerun_keeps_the_normal_result(monkeypatch):
    import nacl.public
    sk = nacl.public.PrivateKey.generate()
    rows = _rows(WINDOWS["cn"])
    moments = _moments() + [_sealed_moment(sk)]
    plain = _enclave_with_sk(rows, moments, sk, fp=False)
    real = recall_select.select_context_memories
    calls = []

    def second_fails(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("normalized")
        return real(*a, **k)
    monkeypatch.setattr(recall_select, "select_context_memories", second_fails)
    picked, trace, log, diag = _enclave_with_sk(rows, moments, sk)
    assert len(calls) == 2
    assert picked == plain[0] and _strip_timing(log) == _strip_timing(plain[2])
    assert diag == {"error": "diagnostics_failed:RuntimeError"}


def test_a_failed_first_encode_is_replayed_not_retried(hybrid_env, monkeypatch):
    import nacl.public
    sk = nacl.public.PrivateKey.generate()
    embedder = _embedder()
    rows = _rows(WINDOWS["two"])
    moments = _moments() + [_sealed_moment(sk)]
    calls = []

    def failing(_embedder, texts, _deadline, timing=None):
        calls.append(tuple(texts))
        raise recall_policy.Fallback("encode_failed")
    monkeypatch.setattr(recall_hybrid, "encode_queries", failing)
    state = {"deadline": time.monotonic() + 5.0, "started": time.monotonic(), "embedder": embedder,
             "model_id": embedder.model_id, "stored": {}, "fallback_reason": None,
             "vectors_ms": 1.0, "vectors_requested": 0, "vectors_rejected": 0}
    _, _, log, diag = _enclave_with_sk(rows, moments, sk, hybrid=state)
    assert len(calls) == 1                                   # normalized re-run did not encode again
    assert log["hybrid"]["fallback_reason"] == "encode_failed"
    assert diag["input_fingerprint"]["hybrid"] == "fallback:encode_failed"
    assert diag["normalized"]["input_fingerprint"]["hybrid"] == "fallback:encode_failed"


def test_a_slow_vector_read_is_bounded_and_holds_its_slot(hybrid_env, monkeypatch):
    monkeypatch.setenv(recall_policy.BUDGET_ENV, "150")
    embedder = _embedder()
    release = threading.Event()
    deps = _deps(_rows(WINDOWS["two"]), encoder=_FakeClient(embedder), embedder=embedder)

    def slow(_u, model_id, ids):
        release.wait(5)
        return _stored_payload(embedder, model_id, ids)
    deps.stored_vectors = slow
    started = time.monotonic()
    first = plaintext_recall.select(UID, 3, deps)
    assert time.monotonic() - started < 1.0
    assert first.payload["context_memory_log"]["hybrid"]["fallback_reason"] == "deadline_exceeded"
    second = plaintext_recall.select(UID, 3, deps)        # the slow read still holds the slot
    assert second.payload["context_memory_log"]["hybrid"]["fallback_reason"] == "vectors_busy"
    release.set()
    assert _wait(lambda: plaintext_recall._VECTOR_READ_PERMIT.acquire(blocking=False))
    plaintext_recall._VECTOR_READ_PERMIT.release()


def test_a_database_error_on_the_vector_read_stays_lexical(hybrid_env):
    embedder = _embedder()
    rows = _rows(WINDOWS["two"])
    lexical = plaintext_recall.select(UID, 3, _deps(rows))
    deps = _deps(rows, encoder=_FakeClient(embedder), embedder=embedder)

    def broken(*_a):
        raise ConnectionError("pool exhausted")
    deps.stored_vectors = broken
    local = plaintext_recall.select(UID, 3, deps)
    assert local.payload["context_memory_log"]["hybrid"]["fallback_reason"] == "vectors_unavailable"
    assert local.payload["context_memories"] == lexical.payload["context_memories"]


def test_a_shadow_past_its_limit_is_unmeasured_and_keeps_its_permit(worker, monkeypatch):
    serve_worker, _ = worker
    monkeypatch.setenv(plaintext_recall.SHADOW_TIMEOUT_ENV, "100")
    emitted = []
    monkeypatch.setattr(serve_worker, "_emit_v2_debug_trace_for_user",
                        lambda uid, event, **kw: emitted.append(kw["detail"]))
    release = threading.Event()

    def slow_select(*_a, **_k):
        release.wait(5)
        return plaintext_recall.select(UID, 1, _deps(_rows(WINDOWS["cn"])))
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps", lambda: None)
    real_select = plaintext_recall.select
    monkeypatch.setattr(plaintext_recall, "select",
                        lambda u, s, d: slow_select() if d is None else real_select(u, s, d))
    serve_worker._submit_recall_shadow(UID, 1, {"input_fingerprint": {"x": 1}, "summary": {"y": 1}})
    assert _wait(lambda: len(emitted) == 1)
    assert emitted[0]["verdict"] == "unmeasured" and emitted[0]["reason"] == "shadow_timeout"
    assert not serve_worker._RECALL_SHADOW_PERMIT.acquire(blocking=False)   # still running
    release.set()
    assert _wait(lambda: serve_worker._RECALL_SHADOW_PERMIT.acquire(blocking=False))
    serve_worker._RECALL_SHADOW_PERMIT.release()
    time.sleep(0.05)
    assert len(emitted) == 1                                 # the late result is never reported


def test_mode_on_past_its_limit_uses_the_enclave(worker, monkeypatch):
    serve_worker, calls = worker
    monkeypatch.setenv(plaintext_recall.MODE_ENV, "on")
    monkeypatch.setenv(plaintext_recall.LOCAL_TIMEOUT_ENV, "100")
    release = threading.Event()
    monkeypatch.setattr(serve_worker, "_plaintext_recall_deps", lambda: None)
    monkeypatch.setattr(plaintext_recall, "select", lambda *_a: release.wait(5))
    started = time.monotonic()
    out = serve_worker._read_context_memories(UID, through_seq=1)
    assert time.monotonic() - started < 1.0
    assert out["context_memories"] == [{"id": "e"}] and len(calls) == 1
    release.set()
    assert _wait(lambda: serve_worker._LOCAL_RECALL_PERMIT.acquire(blocking=False))
    serve_worker._LOCAL_RECALL_PERMIT.release()


def _hybrid_pair(monkeypatch, *, local_embedder=None, enclave_fail=None, local_min=None):
    embedder = _embedder()
    rows = _rows(WINDOWS["two"])
    moments = _moments()
    ids = recall_policy.plaintext_candidate_ids(moments, UID)
    stored, _ = recall_policy.decode_vectors(
        _stored_payload(embedder, embedder.model_id, ids), embedder.model_id, embedder.dim)
    state = {"deadline": time.monotonic() + 5.0, "started": time.monotonic(), "embedder": embedder,
             "model_id": embedder.model_id, "stored": stored, "fallback_reason": None,
             "vectors_ms": 1.0, "vectors_requested": len(ids), "vectors_rejected": 0}
    if enclave_fail:
        def failing(*_a, **_k):
            raise recall_policy.Fallback(enclave_fail)
        monkeypatch.setattr(recall_hybrid, "encode_queries", failing)
    _, _, _, diag = _enclave([dict(r) for r in rows], [dict(m) for m in moments], hybrid=state, fp=True)
    if local_min is not None:
        monkeypatch.setenv(recall_policy.MIN_COSINE_ENV, local_min)
    local_emb = local_embedder or embedder
    local = plaintext_recall.select(UID, 3, _deps(rows, moments=moments,
                                                  encoder=_FakeClient(local_emb), embedder=embedder))
    return plaintext_recall.compare(local, diag), diag, local


def test_same_hybrid_inputs_are_comparable(hybrid_env, monkeypatch):
    verdict, diag, local = _hybrid_pair(monkeypatch)
    assert verdict == {"verdict": "comparable", "same": True, "diff": []}
    assert diag["input_fingerprint"]["hybrid"] == "active:" == local.input_fingerprint["hybrid"]


def test_an_enclave_encode_failure_is_not_comparable_with_a_local_success(hybrid_env, monkeypatch):
    verdict, diag, _ = _hybrid_pair(monkeypatch, enclave_fail="deadline_exceeded")
    assert diag["input_fingerprint"]["hybrid"] == "fallback:deadline_exceeded"
    assert verdict["verdict"] == "incomparable" and "hybrid" in verdict["reason"]


def test_a_different_threshold_is_not_comparable(hybrid_env, monkeypatch):
    verdict, _, _ = _hybrid_pair(monkeypatch, local_min="0.2")
    assert verdict["verdict"] == "incomparable" and "min_cosine" in verdict["reason"]


def test_different_query_vectors_are_not_comparable(hybrid_env, monkeypatch):
    other = fake_embedding.FakeEmbedder(dim=64, aliases={"feline": "kitten"})
    other.model_id = _embedder().model_id                     # same label, different vectors
    verdict, _, _ = _hybrid_pair(monkeypatch, local_embedder=other)
    assert verdict["verdict"] == "incomparable" and "query_vectors" in verdict["reason"]


class _Echo:
    """Unit vector per text; records exactly what the server received."""
    model_id, dim = "echo", 4

    def __init__(self):
        self.seen = []

    def encode_query(self, text):
        self.seen.append(text)
        return [1.0, 0.0, 0.0, 0.0]

    def encode_passages(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def echo_service(monkeypatch):
    emb = _Echo()
    query_service.reserve(port=0)
    port = query_service.start(emb)
    yield emb, port, os.environ[query_service.TOKEN_ENV]
    query_service.stop()


def test_two_legal_non_ascii_queries_are_not_refused_on_the_wire(echo_service):
    emb, port, token = echo_service
    texts = ["猫" * 180_000, "狗" * 180_000]              # 540 KB each in UTF-8, legal
    assert all(len(t.encode("utf-8")) <= query_service.MAX_TEXT_BYTES for t in texts)
    reply = query_client.Client(port, token).encode(texts, time.monotonic() + 30)
    assert len(reply.vectors) == 2 and emb.seen == texts


def test_escaped_characters_round_trip_unchanged_at_the_quota(echo_service):
    emb, port, token = echo_service
    edge = ("\x01\\\"\n" * (query_service.MAX_TEXT_BYTES // 4))[: query_service.MAX_TEXT_BYTES]
    assert len(edge.encode("utf-8")) == query_service.MAX_TEXT_BYTES
    reply = query_client.Client(port, token).encode([edge, "\x7f\t\"x\""], time.monotonic() + 30)
    assert len(reply.vectors) == 2 and emb.seen == [edge, "\x7f\t\"x\""]
    with pytest.raises(recall_policy.Fallback) as over:
        query_client.Client(port, token).encode([edge + "a"], time.monotonic() + 30)
    assert over.value.reason == "query_too_large"


# --------------------------------------------------------------------------- #
# 9. codex r2: the encoder lives for the parent process, across _serve restarts
# --------------------------------------------------------------------------- #

def _encoder_threads():
    return [t for t in threading.enumerate() if t.name == "query-encoder" and t.is_alive()]


@pytest.fixture()
def parent(monkeypatch):
    from model_api_runtime.v2 import serve_worker
    monkeypatch.setenv(plaintext_recall.MODE_ENV, "shadow")
    monkeypatch.setenv(recall_policy.HYBRID_ENV, "1")
    monkeypatch.setenv(recall_policy.MIN_COSINE_ENV, "0.1")
    monkeypatch.setenv(query_service.PORT_ENV, "0")
    monkeypatch.setenv(query_service.OWNER_ENV, "")
    monkeypatch.setattr(serve_worker.atexit, "register", lambda *_a: None)
    return serve_worker


def test_a_serve_restart_keeps_one_service_and_its_token(parent, monkeypatch):
    embedder = _embedder()
    monkeypatch.setattr(parent.memory_embedding_sweep, "get_embedder", lambda: embedder)
    before = len(_encoder_threads())
    parent._start_plaintext_recall_encoder()                  # first _serve generation
    assert _wait(lambda: query_service.active_scheduler() is not None)
    token, port = os.environ[query_service.TOKEN_ENV], os.environ[query_service.PORT_ENV]
    scheduler = query_service.active_scheduler()
    parent._start_plaintext_recall_encoder()                  # same PID, restarted _serve
    assert os.environ[query_service.TOKEN_ENV] == token
    assert os.environ[query_service.PORT_ENV] == port
    assert query_service.active_scheduler() is scheduler
    assert len(_encoder_threads()) == before + 1
    reply = query_client.from_env().encode(["where does my feline nap"], time.monotonic() + 5)
    assert reply.vectors == [embedder.encode_query("where does my feline nap")]


def test_a_failed_bind_leaves_no_worker_behind(parent):
    import socket
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        before = len(_encoder_threads())
        query_service.reserve(port=holder.getsockname()[1])
        with pytest.raises(OSError):
            query_service.start(_embedder())
        assert query_service.active_scheduler() is None
        assert _wait(lambda: len(_encoder_threads()) == before)
    finally:
        holder.close()


def test_a_slow_load_across_a_restart_starts_one_loader_and_one_service(parent, monkeypatch):
    embedder = _embedder()
    gate = threading.Event()
    loads = []

    def slow():
        loads.append(1)
        gate.wait(5)
        return embedder
    monkeypatch.setattr(parent.memory_embedding_sweep, "get_embedder", slow)
    before = len(_encoder_threads())
    parent._start_plaintext_recall_encoder()
    assert _wait(lambda: loads == [1])
    token = os.environ[query_service.TOKEN_ENV]
    parent._start_plaintext_recall_encoder()                  # restart while still loading
    assert os.environ[query_service.TOKEN_ENV] == token
    assert query_service.active_scheduler() is None           # nothing published yet
    gate.set()
    assert _wait(lambda: query_service.active_scheduler() is not None)
    time.sleep(0.05)
    assert loads == [1] and len(_encoder_threads()) == before + 1
    reply = query_client.from_env().encode(["x"], time.monotonic() + 5)
    assert len(reply.vectors) == 1
