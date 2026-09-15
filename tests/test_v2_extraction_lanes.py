import asyncio
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from core import store as core_store
from model_api_runtime.v2 import extraction, jobs_store, profile_store, worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user", base_url="")


def _seed_v2(uid: str) -> None:
    conftest.seed_user(uid)
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_capture_batches")
        conn.execute("DELETE FROM agent_jobs")
    yield


def _job_row(job_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()


# A Dream session only asks the model once the garden reaches the kernel's
# minimum (10 cards); below that the component skips. Fake-extract tests that
# exercise the post-model path therefore carry a garden that size.
_DREAM_FILLER_CARDS = [
    {"id": f"old-fill-{i}", "summary": f"别的旧卡 {i}", "content": f"别的正文 {i}。",
     "occurred_at": "2026-01-01T00:00:00Z"}
    for i in range(10)
]


def _deps(**over):
    def _envelope(uid, inner, item_id=None):
        return {
            "id": item_id or "mom_test",
            "owner_user_id": uid,
            "visibility": "shared",
            "body_ct": "CT",
            "nonce": "NONCE",
            "K_user": "KU",
            "K_enclave": "KE",
            "_inner": inner,
        }

    base = dict(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after, limit: [
            {
                "id": "m1",
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "我换工作了",
            }
        ],
        read_compaction_tail_after_seq=lambda uid, after, limit, **kw: [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "我换工作了",
            }
        ],
        read_memory_context=lambda uid: {
            "ai_name": "小克", "user_name": "Z", "buckets": "B",
            "threads": "T", "identity": "I", "cards": "C",
            "card_items": [
                {"id": "old-a", "summary": "计划去京都", "content": "想看红叶。",
                 "occurred_at": "2026-03-01T00:00:00Z"},
                {"id": "old-b", "summary": "订了京都机票", "content": "11 月出发。",
                 "occurred_at": "2026-05-01T00:00:00Z"},
                *_DREAM_FILLER_CARDS,
            ]},
        build_memory_envelope=_envelope,
        apply_memory_actions=lambda uid, actions: {
            "status": "ok", "applied": len(actions)},
        read_capture_state=lambda uid: {
            "last_captured_until_message_id": "",
            "last_captured_until_ts": 0.0,
            "last_captured_until_seq": 0,
            "capture_seq_initialized": True,
        },
        get_prepared_capture_batch=jobs_store.get_prepared_capture_batch,
        prepare_capture_batch=jobs_store.prepare_capture_batch,
        authorize_capture_provider_call=jobs_store.authorize_capture_provider_call,
        commit_capture_batch=jobs_store.commit_capture_batch,
        fail_capture_job=jobs_store.fail_capture_job,
        cancel_capture_job=jobs_store.cancel_capture_job,
        capture_enabled=lambda _uid: True,
        dream_enabled=lambda _uid: True,
    )
    base.update(over)
    return worker.TurnDeps(**base)


def _trace_collector():
    events = []

    def _emit(user_id, event_type, **kwargs):
        events.append({"user_id": user_id, "type": event_type, **kwargs})

    return events, _emit


def test_dream_is_a_lane_with_background_priority():
    assert "dream" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["dream"] == jobs_store.LANE_PRIORITY["capture"]


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_passes_its_own_output_budget(monkeypatch, lane):
    uid = f"u_x_budget_{lane}"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")
    seen = []

    async def _fake_extract(**kwargs):
        # Both lanes are session-only: the component decides the truncation
        # re-ask wording, extract() only owns the wire budgets.
        assert kwargs["parse_retry"] is None and kwargs["session"] is not None
        retry_prompt = "截断重问由组件决定"
        seen.append((kwargs["max_tokens"], retry_prompt))
        retry_budgets.append(kwargs.get("truncation_retry_max_tokens"))
        return [], None

    retry_budgets = []

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    status = asyncio.run(
        worker.process_job(
            job,
            _deps(),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(seen) == 1
    budget, retry_prompt = seen[0]
    assert budget == extraction.max_output_tokens_for_lane(lane)
    assert "截断" in retry_prompt
    assert retry_prompt != "P"
    assert retry_budgets == [
        extraction.truncation_retry_max_output_tokens_for_lane(lane)
    ]


def test_capture_lane_accepts_eight_cards_in_all_real_parse_routes(monkeypatch):
    uid = "u_x_capture_policy"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    reply = json.dumps({
        "cards": [
            {
                "action": "add",
                "summary": f"Memory {index}",
                "content": f"Durable memory content number {index}.",
            }
            for index in range(8)
        ]
    })
    seen = {}

    async def _fake_extract(**kwargs):
        # The component session is the only parse route the worker has; the
        # legacy direct/retry parsers are placeholders that refuse to run.
        assert kwargs["parse"](reply) == (None, "component_session_required")
        assert kwargs["parse_retry"] is None
        session = kwargs["session"]
        assert session.next_prompt()
        session.feed(reply)
        outcome = session.result()
        seen.update(session=(len(outcome.cards), outcome.error))
        return outcome.cards, outcome.error

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    status = asyncio.run(worker.process_job(
        job,
        _deps(),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert seen == {"session": (8, None)}


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_applies_actions_and_completes(monkeypatch, lane):
    uid = f"u_x_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        assert provider_config is _BYOK          # BYOK-only
        if lane == "capture":
            return ([{"action": "add", "summary": "s", "content": "c"}], None)
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "rationale": "同一京都计划从意向推进到出票",
            "result": {"summary": "s", "content": "c"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(
        profile_store,
        "repair_stuck_profile_retry",
        lambda *_args, **_kwargs: pytest.fail(
            "extraction success must not repair foreground provider state"
        ),
    )
    applied = {}
    ordering = []

    def _apply(uid_, actions):
        ordering.append("memory_write")
        applied.update(n=len(actions))
        return {"status": "ok"}

    async def _profile_enqueue(uid_, *, reason, force):
        ordering.append("profile_enqueue")
        assert uid_ == uid
        assert reason == "dream_refresh"
        assert force is True
        return True

    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)
    deps = _deps(apply_memory_actions=_apply)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert applied == ({"n": 1} if lane == "dream" else {})
    assert ordering == (
        ["memory_write", "profile_enqueue"] if lane == "dream" else []
    )
    assert _job_row(job_id)[0] == "completed"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_ignores_content_block_metadata_for_language(monkeypatch, lane):
    """Both V2 call sites must pass only the user's text into language choice."""
    uid = f"u_x_block_language_{lane}"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")
    row = {
        "id": "m-block",
        "seq": 1,
        "ts": 1.0,
        "role": "user",
        "raw_role": "user",
        "source": "chat",
        "capture_eligible": True,
        "content": [
            {"type": "text", "text": "我今天很难过"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,AAAA"},
            },
        ],
    }
    seen = {}

    def _fake_prompt(**kwargs):
        seen["locale"] = kwargs["locale"]
        seen["window"] = (
            kwargs.get("window") or kwargs.get("recent_conversations") or ""
        )
        return "prompt"

    async def _fake_extract(**_kwargs):
        return [], None

    if lane == "capture":
        real_request = worker.garden_component.capture_request

        def _spy_request(**kwargs):
            _fake_prompt(**kwargs)
            return real_request(**kwargs)

        monkeypatch.setattr(worker.garden_component, "capture_request", _spy_request)
    else:
        real_open = worker.garden_component.open_dream_session

        def _spy_open(garden, **kwargs):
            _fake_prompt(**kwargs)
            return real_open(garden, **kwargs)

        monkeypatch.setattr(worker.garden_component, "open_dream_session", _spy_open)
    monkeypatch.setattr(extraction, "extract", _fake_extract)
    deps = _deps(
        read_tail=lambda _uid, _after, _limit: [row],
        read_compaction_tail_after_seq=lambda _uid, _after, _limit, **_kw: [row],
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert seen["locale"] == "zh-Hans"
    assert "我今天很难过" in seen["window"]
    assert "image_url" not in seen["window"]


def test_dream_lifecycle_trace_correlates_model_and_write_outcome(monkeypatch):
    uid = "u_x_dream_trace"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream", trace_id="trace-dream")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "rationale": "同一京都计划的演进",
            "result": {"summary": "s", "content": "c"},
        }], None)

    async def _profile_enqueue(*_args, **_kwargs):
        return True

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)
    traces, emit_trace = _trace_collector()

    status = asyncio.run(worker.process_job(
        job,
        _deps(emit_debug_trace=emit_trace),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert [row["type"] for row in traces] == [
        "memory.dream.start",
        "memory.dream.model.start",
        "memory.dream.model.done",
        "memory.dream.done",
    ]
    assert all(
        row["trace_id"] == str(job["trace_id"])
        and row["job_id"] == str(job_id)
        for row in traces
    )
    assert traces[-1]["detail"] == {
        "runtime": "hosted_v2",
        "lane": "dream",
        "outcome": "applied",
        "degraded_context": False,
        "counts": {
            "actions": 1,
            "active_cards": 12,
            "applied": 1,
            "failed": 0,
            "merged": 1,
            "model_attempts": 1,
            "organized": 2,
            "proposals": 1,
            "skipped": 0,
        },
    }


def test_dream_partial_write_is_visible_without_marking_job_failed(monkeypatch):
    uid = "u_x_dream_partial_trace"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([
            {
                "op": "thicken",
                "card_ids": ["old-a"],
                "rationale": "同一计划的补充",
                "result": {"summary": "s1", "content": "c1"},
            },
            {
                "op": "supersede",
                "card_ids": ["old-b"],
                "rationale": "同一计划的更新",
                "result": {"summary": "s2", "content": "c2"},
            },
        ], None)

    def _partial(_uid, actions):
        return {
            "results": [
                {"status": "ok", "action": actions[0]["type"]},
                {"status": "error", "error": "storage_error"},
            ],
        }

    async def _profile_enqueue(*_args, **_kwargs):
        return True

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)
    traces, emit_trace = _trace_collector()
    status = asyncio.run(worker.process_job(
        job,
        _deps(apply_memory_actions=_partial, emit_debug_trace=emit_trace),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert _job_row(job_id)[0] == "completed"
    terminal = traces[-1]
    assert terminal["type"] == "memory.dream.done"
    assert terminal["status"] == "warning"
    assert terminal["detail"]["outcome"] == "partial"
    assert terminal["detail"]["counts"]["applied"] == 1
    assert terminal["detail"]["counts"]["failed"] == 1


def test_dream_all_writes_rejected_emits_write_rejected_terminal(monkeypatch):
    uid = "u_x_dream_write_rejected_trace"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "rationale": "同一计划的演进",
            "result": {"summary": "s", "content": "c"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    traces, emit_trace = _trace_collector()
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            apply_memory_actions=lambda _uid, _actions: {
                "results": [{"status": "error", "error": "storage_error"}],
            },
            emit_debug_trace=emit_trace,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert _job_row(job_id)[0] == "failed"
    terminal = traces[-1]
    assert terminal["type"] == "memory.dream.error"
    assert terminal["status"] == "error"
    assert terminal["detail"]["outcome"] == "write_rejected"
    assert terminal["detail"]["counts"]["applied"] == 0
    assert terminal["detail"]["counts"]["failed"] == 1


def test_dream_metric_failure_does_not_emit_a_second_overall_terminal(monkeypatch):
    uid = "u_x_dream_metric_terminal_trace"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "rationale": "同一计划的演进",
            "result": {"summary": "s", "content": "c"},
        }], None)

    async def _profile_enqueue(*_args, **_kwargs):
        return True

    class _Metrics:
        def __init__(self):
            self.flush_calls = 0

        def bind_provider(self, _provider):
            return None

        def add_call(self, _usage):
            return None

        def flush(self, *, failed, status):
            self.flush_calls += 1
            if self.flush_calls == 1:
                raise RuntimeError("metric_write_failed")

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)
    traces, emit_trace = _trace_collector()
    metrics = _Metrics()
    assert asyncio.run(worker.process_job(
        job,
        _deps(emit_debug_trace=emit_trace),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
        tm=metrics,
    )) == "failed"

    overall = [
        row for row in traces
        if row["type"] in {"memory.dream.done", "memory.dream.error"}
    ]
    assert [row["type"] for row in overall] == ["memory.dream.done"]
    assert metrics.flush_calls == 2


def _stub_dream_readside(monkeypatch, *, index, fetch=None):
    """Drive the production Dream context reader with a stubbed readside.

    ``serve_worker._read_dream_memory_context`` runs for real; only the
    enclave-bound ``memory_core`` calls and the token/identity helpers are
    replaced, so the reader's own outcome classification is what is tested.
    """
    from model_api_runtime.v2 import serve_worker

    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(
        serve_worker, "_load_identity_card_view", lambda _store, *, runtime_token: {}
    )
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))
    monkeypatch.setattr("memory.memory_core.index", index)
    if fetch is not None:
        monkeypatch.setattr("memory.memory_core.fetch", fetch)
    return serve_worker


def _raise(exc):
    def _call(*_a, **_k):
        raise exc

    return _call


@pytest.mark.parametrize(
    "index, fetch",
    [
        pytest.param(lambda *a, **k: ({"error": "readside_unavailable"}, 503), None, id="index-503"),
        pytest.param(_raise(RuntimeError("enclave_error:ReadTimeout")), None, id="index-timeout"),
        pytest.param(
            lambda *a, **k: ({"items": [{"id": f"mem_{i}"} for i in range(12)]}, 200),
            lambda *a, **k: ({"error": "readside_unavailable"}, 503),
            id="fetch-503",
        ),
    ],
)
def test_dream_failed_card_read_fails_with_backoff_instead_of_advancing_ledger(
    monkeypatch, index, fetch,
):
    """Prod 09-10 / 09-13: a failed card read used to complete as a no-op and
    advance the Dream ledger (``already_dreamed`` forever after)."""
    from proactive import dream_scheduler

    uid = "u_x_dream_card_read_failed"
    _seed_v2(uid)
    db.memory_replace_all(uid, [_dream_db_card(uid, f"mem_{i}") for i in range(12)])
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    serve_worker = _stub_dream_readside(monkeypatch, index=index, fetch=fetch)
    provider_calls = []

    async def _provider(*_a, **_k):
        provider_calls.append(1)
        raise AssertionError("an unreadable garden must not reach the provider")

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _provider)
    traces, emit_trace = _trace_collector()
    recorded = []

    def _record(user_id, lane, status, detail):
        recorded.append((lane, status))
        serve_worker._record_extraction_status(user_id, lane, status, detail)

    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=serve_worker._read_dream_memory_context,
            emit_debug_trace=emit_trace,
            record_extraction_status=_record,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert provider_calls == []
    assert _job_row(job_id) == ("failed", "extraction_failed:dream_context_unavailable")
    assert recorded == [("dream", "failed")]
    context_error = next(
        row for row in traces if row["type"] == "memory.extraction.context.error"
    )
    assert context_error["detail"]["outcome"] == "unavailable"
    terminal = traces[-1]
    assert terminal["type"] == "memory.dream.error"
    assert terminal["detail"]["outcome"] == "context_unavailable"
    assert terminal["detail"]["degraded_context"] is True

    store = core_store.get_store_per_load_mode(uid, reason="test dream ledger")
    state = dream_scheduler.load_dream_state(store)
    assert state["last_dream_completed_at"] == 0.0
    assert state["last_dream_signature"] == ""
    assert state["last_dreamed_seed_card_count"] == 0
    assert state["dream_fail_streak"] == 1
    assert state["last_dream_failed_at"] > 0.0
    # The next tick backs off instead of saying ``already_dreamed``.
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    tick = dream_scheduler.tick_memory_dream(
        store, submit=lambda *_a, **_k: {"enqueued": True}
    )
    assert tick["reason"] == "failure_backoff"


def _stub_dream_enclave(monkeypatch, *, index_drop=(), fetch_drop=(), fetch_flag=()):
    """Drive the real Dream reader AND the real ``memory_core`` index/fetch
    (lifecycle filter, owner scoping, ``user_card_count``, fetch envelope) over
    the user's real DB cards; only the enclave decrypt round-trip is faked.

    ``index_drop`` / ``fetch_drop`` are card ids the fake enclave cannot
    decrypt (reported in ``unavailable_ids`` exactly like the enclave route);
    ``fetch_flag`` are ids it returns a body for AND reports unavailable.
    """
    import memory_readside_core
    from model_api_runtime.v2 import serve_worker

    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(
        serve_worker, "_load_identity_card_view", lambda _store, *, runtime_token: {}
    )
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": []}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))
    calls = []

    def _enclave(_api_key, candidates, *, operation, payload=None, runtime_token=None):
        calls.append(operation)
        drop = set(index_drop if operation == "index" else fetch_drop)
        items, unavailable = [], []
        for card in candidates:
            mid = card["id"]
            if mid in drop:
                unavailable.append(mid)
                continue
            items.append({
                "id": mid, "bucket": "life", "summary": f"summary {mid}",
                "content": f"body {mid}", "occurred_at": card.get("occurred_at"),
            })
            if operation == "fetch" and mid in fetch_flag:
                unavailable.append(mid)
        return {"user_id": card.get("owner_user_id") if candidates else "",
                "items": items, "unavailable_ids": unavailable}

    monkeypatch.setattr(memory_readside_core, "post_enclave_readside", _enclave)
    return serve_worker, calls


@pytest.mark.parametrize(
    "stub",
    [
        # Prod shape: every card fails to decrypt, the readside still answers
        # HTTP 200 with ``items=[]`` — but ``user_card_count`` is 12.
        pytest.param({"index_drop": [f"mem_{i}" for i in range(12)]}, id="index-200-all-undecryptable"),
        pytest.param({"fetch_drop": ["mem_3"]}, id="fetch-200-card-unavailable"),
        pytest.param({"fetch_flag": ["mem_3"]}, id="fetch-200-body-but-flagged-unavailable"),
    ],
)
def test_dream_200_read_that_is_not_the_whole_garden_fails_instead_of_noop(
    monkeypatch, stub,
):
    """A 200 answer is not proof of a readable garden. An enclave that cannot
    decrypt any card answers ``items=[]``; treating that as an empty garden
    completed Dream as a no-op and advanced the ledger."""
    uid = "u_x_dream_200_incomplete"
    _seed_v2(uid)
    db.memory_replace_all(uid, [_dream_db_card(uid, f"mem_{i}") for i in range(12)])
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    serve_worker, enclave_calls = _stub_dream_enclave(monkeypatch, **stub)
    provider_calls = _no_provider_call(monkeypatch)
    traces, emit_trace = _trace_collector()

    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=serve_worker._read_dream_memory_context,
            emit_debug_trace=emit_trace,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert enclave_calls[0] == "index"
    assert provider_calls == []
    assert _job_row(job_id) == ("failed", "extraction_failed:dream_context_unavailable")
    assert traces[-1]["type"] == "memory.dream.error"
    assert traces[-1]["detail"]["outcome"] == "context_unavailable"


def test_dream_fully_readable_garden_reaches_the_provider_through_the_same_fakes(
    monkeypatch,
):
    """Control for the failure cases above: same real reader, same fake enclave,
    nothing dropped -> every card reaches the provider."""
    uid = "u_x_dream_200_complete"
    _seed_v2(uid)
    db.memory_replace_all(uid, [_dream_db_card(uid, f"mem_{i}") for i in range(12)])
    jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    serve_worker, enclave_calls = _stub_dream_enclave(monkeypatch)
    prompts = []

    async def _provider(_cfg, messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return {"reply": '{"consolidations": []}', "stop_reason": "end_turn"}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _provider)

    status = asyncio.run(worker.process_job(
        job,
        _deps(read_dream_memory_context=serve_worker._read_dream_memory_context),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert enclave_calls[:2] == ["index", "fetch"]
    assert len(prompts) == 1
    # Cards reach the model with their bodies, not as a one-line summary.
    assert "- id=mem_3 | bucket=life" in prompts[0]
    assert "summary: summary mem_3" in prompts[0]
    assert "body mem_3" in prompts[0]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"items": [], "limit": 60, "truncated": False}, id="no-user-card-count"),
        pytest.param(
            {"items": [{"id": "mem_0", "summary": "S"}, {"summary": "no id"}],
             "limit": 60, "truncated": False, "user_card_count": 2},
            id="partially-malformed",
        ),
        pytest.param(
            {"items": ["junk"], "limit": 60, "truncated": False, "user_card_count": 1},
            id="all-malformed",
        ),
    ],
)
def test_dream_malformed_index_envelope_fails_instead_of_noop(monkeypatch, body):
    uid = "u_x_dream_index_malformed"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    fetches = []
    serve_worker = _stub_dream_readside(
        monkeypatch,
        index=lambda *a, **k: (body, 200),
        fetch=lambda _store, _key, payload, **k: (
            fetches.append(payload) or ({
                "items": [{"id": mid, "summary": "S", "content": "C"} for mid in payload["ids"]],
                "missing_ids": [], "unavailable_ids": [],
            }, 200)
        ),
    )
    provider_calls = _no_provider_call(monkeypatch)

    status = asyncio.run(worker.process_job(
        job,
        _deps(read_dream_memory_context=serve_worker._read_dream_memory_context),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert fetches == []
    assert provider_calls == []
    assert _job_row(job_id) == ("failed", "extraction_failed:dream_context_unavailable")


def test_dream_empty_successful_card_read_keeps_the_noop_completion(monkeypatch):
    uid = "u_x_dream_empty_read"
    _seed_v2(uid)
    db.memory_replace_all(uid, [])
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    # Real memory_core over a garden with no live cards: ``user_card_count`` 0.
    serve_worker, enclave_calls = _stub_dream_enclave(monkeypatch)
    assert enclave_calls == []
    _no_provider_call(monkeypatch)
    traces, emit_trace = _trace_collector()
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=serve_worker._read_dream_memory_context,
            emit_debug_trace=emit_trace,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert _job_row(job_id) == ("completed", None)
    assert "memory.extraction.context.error" not in [row["type"] for row in traces]
    assert traces[-1]["type"] == "memory.dream.done"
    assert traces[-1]["detail"]["outcome"] == "noop"
    assert traces[-1]["detail"]["degraded_context"] is False


def test_dream_prompt_cap_truncated_cards_are_still_an_intentional_partial_context(
    monkeypatch,
):
    """Cards beyond the component's prompt budget are a deliberate partial
    context, not a read failure: the run proceeds, the omitted cards are not
    shown (and cannot be retired), and the partial context stays visible."""
    uid = "u_x_dream_truncated_context"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    prompts = []

    async def _provider(_cfg, messages, **_kwargs):
        prompts.append(messages[0]["content"])
        # Targets one card the model never saw (omitted by the budget).
        return {"reply": json.dumps({"consolidations": [{
            "op": "supersede", "card_ids": ["card-19"], "rationale": "更新",
            "result": {"summary": "新摘要", "content": "新正文。"},
        }]}), "stop_reason": "end_turn"}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _provider)
    # 20 cards x ~4,000 chars: only the first 14 fit the 60,000-char budget.
    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": "正文" * 2000,
         "occurred_at": "2026-07-01T00:00:00Z"}
        for i in range(20)
    ]
    traces, emit_trace = _trace_collector()
    applied = []
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=lambda _uid: {
                "card_items": cards, "_diagnostic_cards_outcome": "ready",
            },
            emit_debug_trace=emit_trace,
            apply_memory_actions=lambda _uid, actions: (
                applied.extend(actions) or {"status": "ok"}
            ),
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert len(prompts) == 1
    assert "- id=card-13" in prompts[0] and "- id=card-14" not in prompts[0]
    assert applied == []                      # an unseen card is never a target
    types = [row["type"] for row in traces]
    context_error = next(
        row for row in traces if row["type"] == "memory.extraction.context.error"
    )
    assert context_error["detail"]["outcome"] == "truncated"
    assert types.index("memory.extraction.context.error") < types.index(
        "memory.dream.model.start"
    )
    assert _job_row(job_id) == ("completed", None)
    assert traces[-1]["detail"]["degraded_context"] is True
    assert traces[-1]["detail"]["counts"]["active_cards"] == 14


def _dream_reply(*consolidations):
    return {"reply": json.dumps({"consolidations": list(consolidations)}),
            "stop_reason": "end_turn"}


def _dream_garden_with_one_oversized_card():
    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": f"正文 {i}。",
         "occurred_at": "2026-07-01T00:00:00Z"}
        for i in range(12)
    ]
    # Longer than the 5,000-char body cap: shown to the model marked TRUNCATED.
    cards[3] = {**cards[3], "content": "长" * 6000}
    return cards


class _Recorder:
    def __init__(self):
        self.events = []

    async def record(self, kind, payload):
        self.events.append((kind, payload))

    async def record_best_effort(self, kind, payload):
        await self.record(kind, payload)
        return True


def test_dream_host_blocks_consolidations_touching_a_truncated_card(monkeypatch):
    """The prompt forbids rewriting a TRUNCATED card; the host enforces it.
    Only the offending proposal is dropped, the clean one still applies."""
    uid = "u_x_dream_truncated_guard"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    prompts = []

    async def _provider(_cfg, messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return _dream_reply(
            {"op": "thicken", "card_ids": ["card-3"], "rationale": "补充",
             "result": {"summary": "只看了一半", "content": "重写的正文。"}},
            {"op": "merge", "card_ids": ["card-5", "card-6"], "rationale": "同一件事",
             "result": {"summary": "合并", "content": "合并正文。"}},
        )

    async def _profile_enqueue(*_a, **_k):
        return True

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _provider)
    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)
    applied = []
    recorder = _Recorder()
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=lambda _uid: {
                "card_items": _dream_garden_with_one_oversized_card(),
                "_diagnostic_cards_outcome": "ready",
            },
            apply_memory_actions=lambda _uid, actions: (
                applied.extend(actions) or {"status": "ok"}
            ),
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
        trajectory_recorder=recorder,
    ))

    card_3_head = next(
        line for line in prompts[0].splitlines() if line.startswith("- id=card-3 |")
    )
    assert card_3_head.endswith("| TRUNCATED")
    assert status == "completed"
    assert [action["supersedes"] for action in applied] == [["card-5", "card-6"]]
    guard = [payload for kind, payload in recorder.events
             if kind == "dream_truncated_card_guard"]
    assert guard == [{"rejected": 1, "kept": 1, "truncated_cards": 1}]
    assert "card-3" not in json.dumps(guard)


def test_dream_fails_when_every_consolidation_touches_a_truncated_card(monkeypatch):
    """A run whose every proposal was forbidden fails (ledger stays put) with a
    content-free code instead of completing as a no-op."""
    uid = "u_x_dream_truncated_guard_all"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    async def _provider(_cfg, _messages, **_kwargs):
        return _dream_reply(
            {"op": "merge", "card_ids": ["card-3", "card-4"], "rationale": "同一件事",
             "result": {"summary": "合并", "content": "合并正文。"}},
        )

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _provider)
    applied = []
    traces, emit_trace = _trace_collector()
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=lambda _uid: {
                "card_items": _dream_garden_with_one_oversized_card(),
                "_diagnostic_cards_outcome": "ready",
            },
            apply_memory_actions=lambda _uid, actions: (
                applied.extend(actions) or {"status": "ok"}
            ),
            emit_debug_trace=emit_trace,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert applied == []
    assert _job_row(job_id) == (
        "failed", "extraction_failed:dream_truncated_card_rejected"
    )
    assert traces[-1]["type"] == "memory.dream.error"
    assert traces[-1]["detail"]["outcome"] == "guard_rejected"


def test_dream_fails_closed_on_a_memgarden_without_card_body_rendering(monkeypatch):
    """A consumer/worker running against an older memgarden would build a
    titles-only Dream prompt. Fail the run before any provider call instead."""
    from memory import garden_component

    uid = "u_x_dream_kernel_outdated"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    monkeypatch.setattr(garden_component, "dream_kernel_renders_card_bodies", lambda: False)
    provider_calls = _no_provider_call(monkeypatch)
    traces, emit_trace = _trace_collector()

    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_dream_memory_context=lambda _uid: {
                "card_items": _dream_garden_with_one_oversized_card(),
                "_diagnostic_cards_outcome": "ready",
            },
            emit_debug_trace=emit_trace,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert provider_calls == []
    assert _job_row(job_id) == ("failed", "extraction_failed:dream_kernel_outdated")
    assert traces[-1]["type"] == "memory.dream.error"
    assert traces[-1]["detail"]["outcome"] == "failed"


@pytest.mark.parametrize("second_reply_truncated", [False, True])
def test_dream_thinking_model_spending_the_budget_gets_the_truncation_retry(
    monkeypatch, second_reply_truncated,
):
    """Prod: thinking models answered HTTP 200 with only a thinking block and
    stop_reason=max_tokens. Dream re-sent the same prompt three times at the
    same budget and failed as ``upstream_unavailable``. Real transport + real
    Anthropic parser + real retry wrapper + real extract + real worker."""
    import httpx

    uid = "u_x_dream_thinking_budget"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    requests = []
    thinking_only = {
        "id": "msg_t", "type": "message", "role": "assistant",
        "content": [{"type": "thinking", "thinking": "...", "signature": "s"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 9000, "output_tokens": 12000},
    }
    answered = {
        "id": "msg_a", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": '{"consolidations": []}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 9000, "output_tokens": 40},
    }

    def handler(request):
        requests.append(json.loads(request.content))
        body = thinking_only if len(requests) == 1 or second_reply_truncated else answered
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        provider_client,
        "_shared_async_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(provider_client.asyncio, "sleep", _no_sleep)
    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": f"C{i}",
         "occurred_at": "2026-07-01T00:00:00Z"}
        for i in range(12)
    ]
    status = asyncio.run(worker.process_job(
        job,
        _deps(read_dream_memory_context=lambda _uid: {
            "ai_name": "小克", "user_name": "Z", "cards": "C", "card_items": cards,
        }),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    budget = extraction.max_output_tokens_for_lane("dream")
    retry_budget = extraction.truncation_retry_max_output_tokens_for_lane("dream")
    assert [row["max_tokens"] for row in requests] == [budget, retry_budget]
    if second_reply_truncated:
        assert status == "failed"
        assert _job_row(job_id) == ("failed", "extraction_failed:output_truncated")
    else:
        assert status == "completed"
        assert _job_row(job_id) == ("completed", None)


@pytest.mark.parametrize("fallback_reply_truncated", [False, True])
@pytest.mark.parametrize(
    "rejection",
    [
        pytest.param((400, {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "max_tokens: 24000 > 16000, which is the maximum allowed "
                       "number of output tokens for claude-sonnet-4-test",
        }}), id="anthropic-400"),
        pytest.param((422, {"error": {
            "message": "max_tokens is too large: 24000. This model supports at most "
                       "16384 completion tokens, whereas you provided 24000.",
        }}), id="relay-422"),
    ],
)
def test_dream_escalated_truncation_retry_rejected_as_too_large_falls_back_to_the_accepted_budget(
    monkeypatch, rejection, fallback_reply_truncated,
):
    """A model that accepts Dream's 12k budget but rejects the doubled 24k retry
    budget must not turn a recoverable concise retry into ``provider_config``.
    Real transport + parser + retry wrapper + extract + worker (session mode)."""
    import httpx

    uid = "u_x_dream_budget_rejected"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    budget = extraction.max_output_tokens_for_lane("dream")
    retry_budget = extraction.truncation_retry_max_output_tokens_for_lane("dream")
    requests = []
    thinking_only = {
        "id": "msg_t", "type": "message", "role": "assistant",
        "content": [{"type": "thinking", "thinking": "...", "signature": "s"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 9000, "output_tokens": budget},
    }
    answered = {
        "id": "msg_a", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": '{"consolidations": []}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 9000, "output_tokens": 40},
    }

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["max_tokens"] > 16000:
            return httpx.Response(rejection[0], json=rejection[1])
        first_call = len(requests) == 1
        return httpx.Response(
            200,
            json=thinking_only if first_call or fallback_reply_truncated else answered,
        )

    monkeypatch.setattr(
        provider_client,
        "_shared_async_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(provider_client.asyncio, "sleep", _no_sleep)
    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": f"C{i}",
         "occurred_at": "2026-07-01T00:00:00Z"}
        for i in range(12)
    ]
    status = asyncio.run(worker.process_job(
        job,
        _deps(read_dream_memory_context=lambda _uid: {
            "ai_name": "小克", "user_name": "Z", "cards": "C", "card_items": cards,
        }),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    budgets = [row["max_tokens"] for row in requests]
    # The rejected 24k wire may be re-sent once by a provider compatibility
    # fallback; what matters is that it is followed by the accepted budget.
    assert budgets[0] == budget
    assert budgets[-1] == budget
    assert set(budgets[1:-1]) == {retry_budget}
    first_prompt = requests[0]["messages"][-1]["content"]
    concise_prompts = {json.dumps(row["messages"]) for row in requests[1:]}
    assert len(concise_prompts) == 1  # the fallback re-asks the same concise prompt
    assert json.loads(next(iter(concise_prompts)))[-1]["content"] != first_prompt
    if fallback_reply_truncated:
        assert status == "failed"
        assert _job_row(job_id) == ("failed", "extraction_failed:output_truncated")
    else:
        assert status == "completed"
        assert _job_row(job_id) == ("completed", None)


def _dream_job_outcome(job_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, last_error, wake_result, wake_result_reason "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()


def _dream_db_card(user_id: str, memory_id: str) -> dict:
    ts = "2026-06-20T00:00:00Z"
    return {
        "v": 1, "id": memory_id, "type": "fact", "owner_user_id": user_id,
        "visibility": "shared", "body_ct": f"ct_{memory_id}",
        "nonce": f"n_{memory_id}", "K_user": f"ku_{memory_id}",
        "K_enclave": f"ke_{memory_id}", "occurred_at": ts, "created_at": ts,
        "updated_at": ts, "status": "active",
    }


def _no_provider_call(monkeypatch):
    calls = []

    async def _provider(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("a too-small garden must not reach the provider")

    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", _provider
    )
    return calls


def test_dream_on_too_small_garden_is_recorded_as_skipped_not_consolidated(
    monkeypatch,
):
    """Bug 17: memgarden declines to consolidate < its minimum; io used to call it
    ``completed`` and advance the Dream ledger as if a real dream had run."""
    from model_api_runtime.v2 import serve_worker
    from proactive import dream_scheduler

    uid = "u_x_dream_small_garden"
    _seed_v2(uid)
    db.memory_replace_all(uid, [_dream_db_card(uid, f"mem_{i}") for i in range(2)])
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    provider_calls = _no_provider_call(monkeypatch)
    traces, emit_trace = _trace_collector()
    recorded = []

    def _record(user_id, lane, status, detail):
        recorded.append((lane, status, dict(detail)))
        serve_worker._record_extraction_status(user_id, lane, status, detail)

    status = asyncio.run(worker.process_job(
        job,
        _deps(
            emit_debug_trace=emit_trace,
            record_extraction_status=_record,
            read_memory_context=lambda _uid: {"card_items": [
                {"id": "old-a", "summary": "计划去京都", "content": "想看红叶。"},
                {"id": "old-b", "summary": "订了京都机票", "content": "11 月出发。"},
            ]},
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert provider_calls == []
    assert _dream_job_outcome(job_id) == (
        "completed", None, "skipped", "not_enough_new_cards",
    )
    assert [row[:2] for row in recorded] == [("dream", "skipped")]
    assert recorded[0][2]["skip_reason"] == "not_enough_new_cards"
    terminal = traces[-1]
    assert terminal["type"] == "memory.dream.done"
    assert terminal["status"] == "ok"
    assert terminal["detail"]["outcome"] == "skipped"
    assert terminal["detail"]["counts"]["model_attempts"] == 0
    assert "memory.dream.model.done" not in [row["type"] for row in traces]

    store = core_store.get_store_per_load_mode(uid, reason="test dream ledger")
    state = dream_scheduler.load_dream_state(store)
    # Not a success: the consolidation ledger does not move ...
    assert state["last_dream_completed_at"] == 0.0
    assert state["last_dream_signature"] == ""
    assert state["last_dreamed_seed_card_count"] == 0
    # ... and not a failure: no backoff streak.
    assert state["dream_fail_streak"] == 0
    assert state["last_dream_skip_reason"] == "not_enough_new_cards"
    assert state["last_dream_skipped_at"] > 0.0

    # The next scheduler tick does not re-enqueue the same no-op right away.
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    submitted = []
    tick = dream_scheduler.tick_memory_dream(
        store,
        submit=lambda *_a, **_k: submitted.append(1) or {"enqueued": True},
    )
    assert tick["enqueued"] is False
    assert tick["reason"] == "not_enough_new_cards"
    assert submitted == []


def test_dream_with_enough_cards_still_reaches_the_model_and_is_not_skipped(
    monkeypatch,
):
    uid = "u_x_dream_big_enough_garden"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    calls = []

    async def _provider(_cfg, _messages, **_kwargs):
        calls.append(1)
        return {"reply": '{"consolidations": []}', "stop_reason": "end_turn"}

    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", _provider
    )
    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": f"C{i}",
         "occurred_at": "2026-07-01T00:00:00Z"}
        for i in range(12)
    ]
    traces, emit_trace = _trace_collector()
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_memory_context=lambda _uid: {
                "ai_name": "小克", "user_name": "Z", "cards": "C",
                "card_items": cards,
            },
            emit_debug_trace=emit_trace,
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert calls == [1]
    assert _dream_job_outcome(job_id) == ("completed", None, None, None)
    assert traces[-1]["detail"]["outcome"] == "noop"


def test_dream_blast_radius_fuse_fails_whole_job(monkeypatch):
    """2026-08-05 阀门重构:语义审查员已拆,834→1 的最后防线是确定性保险丝——
    单晚要退休的卡 > 活跃卡 80% 且 ≥10 张 → 整个 job 失败,不部分执行。"""
    uid = "u_x_dream_fuse"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": f"C{i}",
         "occurred_at": f"2026-07-{i + 1:02d}T00:00:00Z"}
        for i in range(15)
    ]

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        # 13/15 = 87% > 80% 且 ≥10 张 → 熔断
        return ([
            {
                "op": "merge",
                "card_ids": [f"card-{i}", f"card-{i + 1}"],
                "rationale": "同一线索的演进",
                "result": {"summary": "合并摘要", "content": "合并后的完整正文。"},
            }
            for i in range(0, 12, 2)
        ] + [{
            "op": "supersede",
            "card_ids": ["card-12"],
            "rationale": "同一事实的更新",
            "result": {"summary": "更新摘要", "content": "更新后的完整正文。"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    applied = {}

    traces, emit_trace = _trace_collector()
    deps = _deps(
        read_memory_context=lambda _uid: {
            "ai_name": "小克", "user_name": "Z", "buckets": "B",
            "threads": "T", "identity": "I", "cards": "C",
            "card_items": cards,
        },
        apply_memory_actions=lambda _uid, actions: applied.update(n=len(actions)) or {"status": "ok"},
        emit_debug_trace=emit_trace,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert applied == {}                       # 熔断在任何写入之前
    row = _job_row(job_id)
    assert row == ("failed", "extraction_failed:dream_blast_radius_exceeded")
    terminal = traces[-1]
    assert terminal["type"] == "memory.dream.error"
    assert terminal["status"] == "error"
    assert terminal["detail"]["outcome"] == "guard_rejected"


def test_dream_below_fuse_threshold_applies_normally(monkeypatch):
    uid = "u_x_dream_no_fuse"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    cards = [
        {"id": f"card-{i}", "summary": f"S{i}", "content": f"C{i}",
         "occurred_at": f"2026-07-{i + 1:02d}T00:00:00Z"}
        for i in range(15)
    ]

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        # 退休 4 张(<10 张下限)→ 永不熔断
        return ([
            {
                "op": "merge",
                "card_ids": [f"card-{i}", f"card-{i + 1}"],
                "rationale": "同一线索的演进",
                "result": {"summary": "合并摘要", "content": "合并后的完整正文。"},
            }
            for i in range(0, 4, 2)
        ], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    applied = {}

    deps = _deps(
        read_memory_context=lambda _uid: {
            "ai_name": "小克", "user_name": "Z", "buckets": "B",
            "threads": "T", "identity": "I", "cards": "C",
            "card_items": cards,
        },
        apply_memory_actions=lambda _uid, actions: applied.update(n=len(actions)) or {"status": "ok"},
    )

    async def _profile_enqueue(*_a, **_k):
        return True

    monkeypatch.setattr(worker, "_enqueue_profile_if_due", _profile_enqueue)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert applied == {"n": 2}
    assert _job_row(job_id)[0] == "completed"


def test_dream_missing_source_time_degrades_and_still_writes(monkeypatch):
    """2026-08-17(Seven 定):一张源卡缺时间不再让整轮失败,改为退到已知的最晚。

    ⚠️ 这是**端到端**的一条 —— 它同时证明降级留痕真的接在生产路径上,
    而不是只在 helper 单测里传(那正是 codex2 审出的第①项)。
    """
    uid = "u_x_dream_missing_occurred_at"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "rationale": "同一线索",
            "result": {"summary": "s", "content": "c"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    applied = []
    deps = _deps(
        read_memory_context=lambda _uid: {
            "ai_name": "小克", "user_name": "Z", "cards": "C",
            "card_items": [
                {"id": "old-a", "summary": "a", "occurred_at": "2026-03-01T00:00:00Z"},
                {"id": "old-b", "summary": "b", "occurred_at": ""},
                *_DREAM_FILLER_CARDS,
            ],
        },
        # 夹具必须返回 {"status": "ok"} —— 返回 None 会被 _memory_write_result_counts
        # 判成「全部失败」,于是撞上 memory_write_rejected,看起来像降级没生效。
        # (原用例断言的是 failed,所以这个缺陷一直没暴露。)
        apply_memory_actions=lambda _uid, actions: (
            applied.extend(actions) or {"status": "ok"}
        ),
    )

    class _Rec:
        def __init__(self): self.events = []
        async def record(self, kind, payload): self.events.append((kind, payload))
        async def record_best_effort(self, kind, payload):
            await self.record(kind, payload); return True

    recorder = _Rec()
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
        trajectory_recorder=recorder))

    assert status != "failed", (
        "一张源卡缺时间仍让整轮失败 —— 那正是要修掉的永久阻塞"
    )
    assert applied, "降级后应当照常产出动作"
    payload = str(applied)
    assert "2026-03-01T00:00:00Z" in payload, "没有退到已知的那张源卡时间"
    # **生产接线守卫**:降级必须真的留痕。只在 helper 单测里传回调 = 零留痕
    # (codex2 审出第①项)。拆掉 worker 里那行 action_kwargs 赋值,本断言必红。
    assert any(
        kind == "dream_source_time_degraded" for kind, _ in recorder.events
    ), "降级没有进 trajectory —— 生产侧根本没接回调"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_records_whole_turn_metric_on_success(monkeypatch, lane):
    """PR B review finding: `_run_extraction` makes a real `v2_extraction.extract`
    BYOK call but never flushed a `v2_turn_metrics` row on success.
    Extraction now surfaces the same normalized usage/cache telemetry as the
    native chat loop."""
    uid = f"u_x_metric_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        kw["usage_out"]({
            "prompt_tokens": 90,
            "completion_tokens": 9,
            "cache_read_tokens": 70,
            "cache_write_tokens": None,
            "cache_miss_tokens": 20,
        })
        if lane == "capture":
            return ([{"action": "add", "summary": "s", "content": "c"}], None)
        return ([{
            "op": "merge",
            "card_ids": ["old-a", "old-b"],
            "rationale": "同一京都计划从意向推进到出票",
            "result": {"summary": "s", "content": "c"},
        }], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    deps = _deps()

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT lane, prompt_tokens, completion_tokens, model_calls, failed, status, "
            "cache_read_tokens, cache_miss_tokens, usage_reported_calls, "
            "cache_reported_calls, provider, model "
            "FROM v2_turn_metrics WHERE job_id=%s", (job_id,)).fetchone()
    assert row is not None
    assert row[0] == lane
    # 2026-08-05 起 dream 不再有逐提案语义审查 —— 两条 lane 都只打一次 provider。
    expected_calls = 1
    assert row[1] == 90 * expected_calls and row[2] == 9 * expected_calls
    assert row[3] == expected_calls
    assert row[4] is False
    assert row[5] == "ok"
    assert row[6:] == (
        70 * expected_calls,
        20 * expected_calls,
        expected_calls,
        expected_calls,
        "anthropic",
        "claude-sonnet-4-test",
    )


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_zero_results_completes_without_applying_anything(monkeypatch, lane):
    """`nothing_worth_keeping` is SUCCESS — mirrors the wake lane's weak-wake-sleeps."""
    uid = f"u_x_empty_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _empty(*, provider_config, prompt, parse, **kw):
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _empty)
    applied = {"n": 0}
    deps = _deps(apply_memory_actions=lambda uid_, a: applied.update(n=applied["n"] + 1) or {})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert applied["n"] == 0
    assert _job_row(job_id)[0] == "completed"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_failure_is_silent_no_bubble_no_error_chip(monkeypatch, lane):
    uid = f"u_x_fail_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _err(*, provider_config, prompt, parse, **kw):
        return (None, "provider_call_failed:upstream_unavailable")

    monkeypatch.setattr(extraction, "extract", _err)
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(t=text) or {"id": "r"})
    emitted = []
    monkeypatch.setattr(worker, "_emit_status", lambda *a, **k: emitted.append(a))

    status = asyncio.run(worker.process_job(
        job, _deps(), provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert written == {}                       # no chat bubble
    assert emitted == []                       # no user-visible status/error chip
    row = _job_row(job_id)
    assert row == ("failed", "extraction_failed:upstream_unavailable")


def test_truncated_extraction_persists_content_free_failure_shape(monkeypatch):
    uid = "u_x_truncated_dream"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "dream")
    job = jobs_store.claim_next_job("w")
    limit = extraction.DREAM_MAX_OUTPUT_TOKENS

    async def _truncated(**kwargs):
        kwargs["failure_detail_out"]({
            "stop_reason": "length",
            "completion_tokens": limit,
            "max_tokens": kwargs["max_tokens"],
        })
        return None, "output_truncated"

    class Recorder:
        def __init__(self):
            self.events = []

        async def record(self, kind, payload):
            self.events.append((kind, payload))

        async def record_best_effort(self, kind, payload):
            await self.record(kind, payload)
            return True

    recorder = Recorder()
    monkeypatch.setattr(extraction, "extract", _truncated)
    status = asyncio.run(
        worker.process_job(
            job,
            _deps(),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
            trajectory_recorder=recorder,
        )
    )

    assert status == "failed"
    assert _job_row(job_id) == ("failed", "extraction_failed:output_truncated")
    turn_error = next(payload for kind, payload in recorder.events if kind == "turn_exception")
    assert turn_error == {
        "stage": "extraction",
        "error_class": "RuntimeError",
        "error_code": "extraction_failed:output_truncated",
        "stop_reason": "length",
        "completion_tokens": limit,
        "max_tokens": limit,
    }


def test_rejected_memory_write_fails_job_instead_of_marking_completed(monkeypatch):
    uid = "u_x_write_rejected"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{"action": "add", "type": "fact", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)

    def _reject(**kwargs):
        assert jobs_store.fail_capture_job(
            job_id=kwargs["job_id"],
            user_id=kwargs["user_id"],
            claimed_by=kwargs["claimed_by"],
            error="capture_semantic_rejection",
        )
        return {"rejected": True, "reason": "capture_semantic_rejection"}

    deps = _deps(prepare_capture_batch=_reject)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_row(job_id)[0] == "failed"


def test_nonempty_extraction_without_writer_fails_closed(monkeypatch):
    uid = "u_x_writer_missing"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(**_kwargs):
        return ([{"action": "add", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    deps = _deps(prepare_capture_batch=None)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert _job_row(job_id)[0] == "failed"


def test_extraction_rollback_during_llm_blocks_memory_write(monkeypatch):
    uid = "u_x_rollback"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        return ([{"action": "add", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    mode_checks = iter([True, False])
    applied = {"n": 0}
    deps = _deps(
        runtime_mode_enabled=lambda uid_: next(mode_checks),
        apply_memory_actions=lambda uid_, actions: (
            applied.update(n=len(actions)) or {"status": "ok"}),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK,
        api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert applied["n"] == 0
    assert _job_row(job_id)[0] == "failed"


def test_capture_prompt_degrades_when_memory_context_is_missing(monkeypatch):
    """Context fetch failure must degrade, not fail the job (spec §3.5)."""
    uid = "u_x_nocontext"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    seen = {}

    async def _cap(*, provider_config, session, **kw):
        # The prompt the model would actually receive is the session's.
        seen["prompt"] = session.next_prompt()
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _cap)
    status = asyncio.run(worker.process_job(
        job, _deps(read_memory_context=None), provider_config=_BYOK,
        api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert "我换工作了" in seen["prompt"]
    assert "(none)" in seen["prompt"]           # prompt builder's own fallback kicked in


def test_capture_prompt_includes_existing_card_ids(monkeypatch):
    """fd963bf9 (08-30) 起组件请求不带索引，模型抄不到 target_id —— 这条曾是 strict xfail。

    同时守名字：身份卡里存成「用户」的名字不许原样进提示词，称呼规则是 io 那版。
    """
    uid = "u_x_cards_context"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    seen = {}

    async def _capture(*, session, **_kwargs):
        seen["prompt"] = session.next_prompt()
        return [], None

    monkeypatch.setattr(extraction, "extract", _capture)
    deps = _deps(
        read_memory_context=lambda _uid: {
            "ai_name": " 小克 ", "user_name": "用户", "buckets": "工作",
            "capture_cards": [
                {"id": "mom_existing", "summary": "之前的工作记忆", "bucket": "工作"}
            ],
        }
    )
    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )
    assert status == "completed"
    assert "- mom_existing: [工作] 之前的工作记忆" in seen["prompt"]
    assert "target_id" in seen["prompt"]
    assert "用户's companion" not in seen["prompt"]
    from identity.user_naming import _naming_rule

    assert _naming_rule("用户", locale="zh-Hans") in seen["prompt"]


def _capture_db_card(user_id: str, memory_id: str) -> dict:
    ts = "2026-06-20T00:00:00Z"
    return {
        "v": 1, "id": memory_id, "type": "fact", "owner_user_id": user_id,
        "visibility": "shared", "body_ct": f"ct_{memory_id}",
        "nonce": f"n_{memory_id}", "K_user": f"ku_{memory_id}",
        "K_enclave": f"ke_{memory_id}", "occurred_at": ts, "created_at": ts,
        "updated_at": ts, "status": "active", "source": "memory_capture",
        "importance": 0.5,
    }


_CAPTURE_SUMMARIES = {
    "mom_job": ("工作", "Z 在字节跳动做产品经理，负责电商"),
    **{f"mom_fill_{i}": ("日常", f"第{i}次闲聊提到的天气和午饭") for i in range(80)},
}


def _stub_capture_readside(monkeypatch):
    """真 serve_worker 读侧 + 真 memory_core.index（生命周期过滤、owner、user_card_count），
    只把 enclave 解密那一跳换成按 id 给摘要。"""
    import memory_readside_core
    from model_api_runtime.v2 import serve_worker

    serve_worker.wire_assembly()
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(
        serve_worker, "_load_identity_card_view",
        lambda _store, *, runtime_token: {"agent_name": "小克", "user_preferred_name": "Z"},
    )
    monkeypatch.setattr("memory.memory_core.buckets", lambda *a, **k: ({"buckets": ["工作"]}, 200))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": []}, 200))
    calls = []

    def _enclave(_api_key, candidates, *, operation, payload=None, runtime_token=None):
        calls.append((operation, dict(payload or {})))
        items = []
        for card in candidates:
            bucket, summary = _CAPTURE_SUMMARIES[card["id"]]
            items.append({"id": card["id"], "bucket": bucket, "summary": summary,
                          "importance": 0.5, "status": "active"})
        return {"items": items, "unavailable_ids": []}

    monkeypatch.setattr(memory_readside_core, "post_enclave_readside", _enclave)
    return serve_worker, calls


def _supersede_reply(target: str) -> str:
    return json.dumps({"cards": [{
        "action": "supersede", "type": "fact", "target_id": target, "bucket": "工作",
        "threads": ["换工作"], "summary": "Z 下个月去腾讯做产品经理",
        "content": "Z 上周从字节跳动离职，下个月去腾讯继续做产品经理。",
        "importance": 0.7, "pulse": 0.4,
    }]}, ensure_ascii=False)


def _run_capture_e2e(monkeypatch, uid, replies):
    _seed_v2(uid)
    db.memory_replace_all(uid, [
        _capture_db_card(uid, mid) for mid in _CAPTURE_SUMMARIES
    ])
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    serve_worker, enclave_calls = _stub_capture_readside(monkeypatch)
    prompts = []

    async def _provider(_cfg, messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return {"reply": replies[min(len(prompts) - 1, len(replies) - 1)],
                "stop_reason": "end_turn"}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _provider)
    row = {
        "id": "m1", "seq": 1, "ts": 1.0, "role": "user", "raw_role": "user",
        "source": "chat", "capture_eligible": True,
        "content": "我上周从字节跳动离职了，下个月去腾讯做产品经理",
    }
    status = asyncio.run(worker.process_job(
        job,
        _deps(
            read_memory_context=serve_worker._read_memory_context,
            read_compaction_tail_after_seq=lambda *_a, **_k: [row],
        ),
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))
    with db.get_pool().connection() as conn:
        docs = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT moment_id, doc FROM memory_moments WHERE user_id=%s", (uid,)
            ).fetchall()
        }
    return status, job_id, prompts, docs, enclave_calls


def test_capture_supersedes_an_indexed_card_end_to_end(monkeypatch):
    """模型照抄索引里的 id → 过 jobs_store 的所有权/存在校验 → 旧卡被取代，而不是多一张。"""
    status, job_id, prompts, docs, enclave_calls = _run_capture_e2e(
        monkeypatch, "u_x_capture_index_e2e", [_supersede_reply("mom_job")]
    )
    assert status == "completed"
    assert _job_row(job_id)[0] == "completed"
    # 读侧一次全量 index（limit=0 → 硬上限），不是只读 60 张。
    assert ("index", {"ambient": False, "bucket": "", "thread": "", "limit": 1000, "query": ""}) in enclave_calls
    assert len(prompts) == 1
    index = prompts[0].split("target_id from here)]", 1)[1].split("\n[", 1)[0]
    index_rows = index.splitlines()
    assert index_rows[0] == "- mom_job: [工作] Z 在字节跳动做产品经理，负责电商"
    assert len(index_rows) == 60
    new_ids = set(docs) - set(_CAPTURE_SUMMARIES)
    assert len(new_ids) == 1
    new_id = new_ids.pop()
    assert docs["mom_job"]["status"] == "superseded"
    assert docs["mom_job"]["superseded_by"] == new_id


def test_capture_made_up_target_is_reasked_instead_of_rejecting_the_batch(monkeypatch):
    """编造的 id 以前会让 jobs_store 整批拒掉（capture_supersede_target_missing），
    同窗口的好卡一起丢、job 进失败退避。现在组件先重问，改对了照常落库。"""
    status, job_id, prompts, docs, _ = _run_capture_e2e(
        monkeypatch, "u_x_capture_index_reask",
        [_supersede_reply("mom_made_up"), _supersede_reply("mom_job")],
    )
    assert status == "completed"
    assert _job_row(job_id)[0] == "completed"
    assert len(prompts) == 2
    assert "你给的 target_id 不是现有的卡" in prompts[1]
    assert docs["mom_job"]["status"] == "superseded"


def test_capture_made_up_target_twice_drops_only_that_card(monkeypatch):
    status, job_id, prompts, docs, _ = _run_capture_e2e(
        monkeypatch, "u_x_capture_index_drop",
        [_supersede_reply("mom_made_up")],
    )
    assert status == "completed"
    assert _job_row(job_id)[0] == "completed"
    assert len(prompts) == 2
    assert set(docs) == set(_CAPTURE_SUMMARIES)
    assert docs["mom_job"]["status"] == "active"


def test_extraction_reads_go_through_the_enclave_semaphore(monkeypatch):
    """spec §4: read_memory_context (3 post_enclave round-trips) and read_tail (per-message
    decrypt) are BOTH enclave-bound. The enclave is a shared, capacity-bounded decrypt proxy
    (prod: 4 workers x 32 threads, GIL-bound crypto) — protecting it is the whole point of
    this subproject — so both must sit inside the turn's enclave_sem. A background lane that
    bypasses the gate can starve the interactive chat path."""
    import asyncio as _asyncio

    class _CountingSemaphore(_asyncio.Semaphore):
        def __init__(self, value=2):
            super().__init__(value)
            self.held = 0
            self.acquire_count = 0

        async def acquire(self):
            self.acquire_count += 1
            got = await super().acquire()
            self.held += 1
            return got

        def release(self):
            self.held -= 1
            super().release()

    uid = "u_x_sem"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    sem = _CountingSemaphore(2)
    inside = {"ctx": None, "tail": None}

    def _ctx(uid_):
        inside["ctx"] = sem.held          # must be >0 -> we are inside the gate
        return {"buckets": "B"}

    def _tail(uid_, after, limit, **_kwargs):
        inside["tail"] = sem.held
        return [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "content": "hi",
            }
        ]

    async def _empty(*, provider_config, prompt, parse, **kw):
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _empty)
    deps = _deps(
        read_memory_context=_ctx,
        read_compaction_tail_after_seq=_tail,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None,
        runtime_token="rt", enclave_sem=sem))

    assert status == "completed"
    assert inside["ctx"] == 1, "read_memory_context ran OUTSIDE enclave_sem"
    assert inside["tail"] == 1, "read_tail ran OUTSIDE enclave_sem"
    assert sem.acquire_count >= 1


def test_capture_all_non_live_batch_advances_without_provider(monkeypatch):
    uid = "u_x_non_live_only"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    provider_calls = []

    async def _provider_forbidden(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider_forbidden)
    deps = _deps(
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "verify-1",
                "seq": 1,
                "ts": 20.0,
                "role": "user",
                "raw_role": "user",
                "source": "verify_ping",
                "capture_eligible": False,
                "content": "synthetic secret",
            },
            {
                "id": "import-2",
                "seq": 2,
                "ts": 10.0,
                "role": "user",
                "raw_role": "user",
                "source": "history_import",
                "capture_eligible": False,
                "content": "old imported content",
            },
        ]
    )
    assert asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert provider_calls == []
    assert _job_row(job_id)[0] == "completed"
    state = db.get_blob_strict(uid, "capture_state")
    assert state["last_captured_until_seq"] == 2
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_moments WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0


def test_capture_mixed_batch_discloses_only_live_rows(monkeypatch):
    uid = "u_x_mixed_sources"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    prompts = []

    async def _capture(*, session, **_kwargs):
        # What is disclosed is what the component session would send.
        prompts.append(session.next_prompt())
        return [], None

    monkeypatch.setattr(extraction, "extract", _capture)
    deps = _deps(
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "import-1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "history_import",
                "capture_eligible": False,
                "content": "MUST_NOT_DISCLOSE",
            },
            {
                "id": "live-2",
                "seq": 2,
                "ts": 2.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "eligible live turn",
            },
        ]
    )
    assert asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert len(prompts) == 1
    assert "eligible live turn" in prompts[0]
    assert "MUST_NOT_DISCLOSE" not in prompts[0]
    assert db.get_blob_strict(uid, "capture_state")["last_captured_until_seq"] == 2


def test_empty_capture_successor_completes_without_backoff_or_provider(monkeypatch):
    uid = "u_x_empty_successor"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    provider_calls = []

    async def _provider_forbidden(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider_forbidden)
    assert asyncio.run(
        worker.process_job(
            job,
            _deps(read_compaction_tail_after_seq=lambda *_a, **_k: []),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert provider_calls == []
    assert _job_row(job_id)[0] == "completed"
    state = db.get_blob_strict(uid, "capture_state")
    assert state is None or int(state.get("capture_fail_streak") or 0) == 0


@pytest.mark.parametrize(
    "case,gate",
    [
        ("off", lambda _uid: False),
        (
            "error",
            lambda _uid: (_ for _ in ()).throw(RuntimeError("db down")),
        ),
    ],
)
def test_run_turn_capture_preflight_skips_provider_setup_and_chat_error(case, gate):
    uid = f"u_x_preflight_{case}"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    calls = []

    def _forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("provider/enclave setup must not run")

    deps = _deps(
        capture_enabled=gate,
        resolve_provider=_forbidden,
        mint_enclave_token=_forbidden,
        record_terminal_error=lambda *_args: calls.append("error-chip"),
    )
    assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    assert calls == []


def test_capture_opt_out_after_initial_gate_prevents_provider_call(monkeypatch):
    uid = "u_x_disable_before_provider"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    entered_context = threading.Event()
    release_context = threading.Event()
    provider_calls = []

    def _blocked_context(_uid):
        entered_context.set()
        assert release_context.wait(timeout=2.0)
        return {}

    async def _provider(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider)
    deps = _deps(read_memory_context=_blocked_context)

    async def _scenario():
        task = asyncio.create_task(
            worker.process_job(
                job,
                deps,
                provider_config=_BYOK,
                api_key=None,
                runtime_token="rt",
            )
        )
        assert await asyncio.to_thread(entered_context.wait, 2.0)
        await asyncio.to_thread(
            core_store.UserStore(uid).save_proactive_settings,
            {"capture_enabled": False},
        )
        release_context.set()
        return await task

    assert asyncio.run(_scenario()) == "failed"
    assert provider_calls == []
    assert db.get_blob_strict(uid, "capture_state")["capture_fail_streak"] == 0


def test_capture_live_halt_after_context_read_prevents_provider_call(monkeypatch):
    uid = "u_x_halt_before_provider"
    _seed_v2(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    entered_context = threading.Event()
    release_context = threading.Event()
    halted = threading.Event()
    provider_calls = []

    def _blocked_context(_uid):
        entered_context.set()
        assert release_context.wait(timeout=2.0)
        return {}

    async def _provider(**_kwargs):
        provider_calls.append(True)
        return [], None

    monkeypatch.setattr(extraction, "extract", _provider)
    monkeypatch.setattr(
        worker.kill_switch,
        "turns_halted_uncached",
        lambda **_kwargs: halted.is_set(),
    )
    deps = _deps(read_memory_context=_blocked_context)

    async def _scenario():
        task = asyncio.create_task(
            worker.process_job(
                job,
                deps,
                provider_config=_BYOK,
                api_key=None,
                runtime_token="rt",
            )
        )
        assert await asyncio.to_thread(entered_context.wait, 2.0)
        halted.set()
        release_context.set()
        return await task

    assert asyncio.run(_scenario()) == "failed"
    assert provider_calls == []
    status, last_error = _job_row(job["id"])
    assert (status, last_error) == ("failed", "turns_halted")
    state = db.get_blob_strict(uid, "capture_state") or {}
    assert int(state.get("capture_fail_streak") or 0) == 0


def test_empty_capture_successor_clears_stale_failure_state_and_notice(monkeypatch):
    """没有待处理消息的落卡任务完成时，清掉残留的失败子状态和「受阻」提示（Codex 第 10 轮）。"""
    from notices import core as notices_core
    from model_api_runtime.v2 import serve_worker

    uid = "u_x_empty_successor_stale"
    _seed_v2(uid)
    db.set_blob(uid, "capture_state", {
        "capture_fail_streak": 4,
        "last_capture_failed_at": 100.0,
        "capture_account_error_code": "quota_insufficient",
        "capture_account_fail_since": 50.0,
        "capture_window_fail_count": 2,
        "capture_fail_window_key": "after_seq:0",
    })
    from proactive import capture_jobs
    capture_jobs.notify_backoff(type("S", (), {"user_id": uid})(), lane="capture", status="failed",
                                streak=4, account_code="quota_insufficient")
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")
    deps = _deps(read_compaction_tail_after_seq=lambda *_a, **_k: [],
                 read_capture_state=serve_worker._read_capture_state)
    assert asyncio.run(worker.process_job(job, deps, provider_config=_BYOK, api_key=None,
                                          runtime_token="rt")) == "completed"
    state = db.get_blob_strict(uid, "capture_state")
    assert int(state["capture_fail_streak"]) == 0
    assert state["capture_account_error_code"] == ""
    assert float(state["capture_account_fail_since"]) == 0.0
    asyncio.run(worker._notify_capture_backoff(deps, job, "completed"))
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert rows["memory_backoff:capture"]["resolved"] is True
