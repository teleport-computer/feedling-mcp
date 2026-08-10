"""C3 — `executor.dispatch_tool_calls`（spec 2026-07-13 PR-C Task 4）。

用假 capabilities.registry.run_capability（monkeypatch，返回罐装 CapabilityResult-shaped
dict）+ 一个记录调用的 enqueue_write_effect 驱动，纯 asyncio，无 DB。断言：
(a) 两个 READ tool_calls 都跑并按各自 call_id 拿到 ToolResult；
(b) 有 turn_authorization 的 WRITE tool_call 只调 enqueue_write_effect 一次、拿到
    "queued" ToolResult，且绝不经 run_capability 内联跑；
(c) 无 turn_authorization 的 WRITE tool_call 拿到拒绝 ToolResult，且不 enqueue；
(d) 未知工具名 → error ToolResult（不抛异常）；
(e) args_ok=False 的 ToolCall → error ToolResult（不抛异常）；
(f) 混合列表整体按 tool_calls 原序、每个 call_id 都在返回里出现一次。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import executor as v2_executor  # noqa: E402
from provider_types import ToolCall, ToolResult  # noqa: E402


class _FakeResult:
    """Stand-in for capabilities.types.CapabilityResult — dispatch_tool_calls only
    ever touches .to_dict()."""

    def __init__(self, ok, data=None, error=None):
        self._ok, self._data, self._error = ok, data, error

    def to_dict(self):
        if self._ok:
            return {"ok": True, "data": self._data or {}, "trace": {}, "warnings": []}
        return {"ok": False, "error": self._error or {"code": "boom"}}


class _Store:
    user_id = "u1"


def _run(
    tool_calls,
    *,
    turn_authorization,
    run_capability,
    enqueue_write_effect=None,
    observe_photo=None,
    monkeypatch,
):
    monkeypatch.setattr(cap_registry, "run_capability", run_capability)
    calls = []
    if enqueue_write_effect is None:
        def enqueue_write_effect(tc):
            calls.append(tc)
    return asyncio.run(v2_executor.dispatch_tool_calls(
        tool_calls, store=_Store(), api_key="k", runtime_token="rt",
        enclave_sem=asyncio.Semaphore(8), turn_authorization=turn_authorization,
        enqueue_write_effect=enqueue_write_effect,
        observe_photo=observe_photo,
    )), calls


def test_two_reads_dispatch_and_return_results_by_call_id(monkeypatch):
    ran = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)
        return _FakeResult(True, {"body": f"result-for-{action_type}"})

    tool_calls = [
        ToolCall(id="c1", name="memory_index", args={}),
        ToolCall(id="c2", name="web_search", args={"query": "x"}),
    ]
    results, enqueued = _run(
        tool_calls, turn_authorization=False, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert sorted(ran) == ["memory_index", "web_search"]
    assert [r.call_id for r in results] == ["c1", "c2"]
    by_id = {r.call_id: r for r in results}
    assert "result-for-memory_index" in by_id["c1"].content
    assert "result-for-web_search" in by_id["c2"].content
    assert enqueued == []


def test_one_read_exception_is_isolated_from_successful_sibling(monkeypatch):
    ran = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)
        if action_type == "memory_index":
            raise RuntimeError("sensitive adapter detail must not escape")
        return _FakeResult(True, {"body": "web result"})

    tool_calls = [
        ToolCall(id="failed", name="memory_index", args={}),
        ToolCall(id="succeeded", name="web_search", args={"query": "x"}),
    ]
    results, enqueued = _run(
        tool_calls, turn_authorization=False,
        run_capability=_run_capability, monkeypatch=monkeypatch)

    assert sorted(ran) == ["memory_index", "web_search"]
    assert [result.call_id for result in results] == ["failed", "succeeded"]
    assert results[0].content == "error: capability_failed"
    assert "sensitive adapter detail" not in results[0].content
    assert "web result" in results[1].content
    assert enqueued == []


def test_photo_read_with_image_invokes_observer_and_hides_base64(monkeypatch):
    seen = []

    def _run_capability(*_args, **_kwargs):
        return _FakeResult(True, {
            "photo_id": "p1",
            "has_image": True,
            "image_media_type": "image/jpeg",
            "image_b64": "cGl4ZWxz",
        })

    async def _observe_photo(mime, image_b64):
        seen.append((mime, image_b64))
        return "a red bicycle beside a wall"

    results, enqueued = _run(
        [ToolCall(
            id="photo-1",
            name="photo_read",
            args={"photo_id": "p1", "include_image": True},
        )],
        turn_authorization=False,
        run_capability=_run_capability,
        observe_photo=_observe_photo,
        monkeypatch=monkeypatch,
    )

    assert seen == [("image/jpeg", "cGl4ZWxz")]
    assert "UNTRUSTED VISUAL OBSERVATION" in results[0].content
    assert "a red bicycle beside a wall" in results[0].content
    assert "cGl4ZWxz" not in results[0].content
    assert "image_b64" not in results[0].content
    assert enqueued == []


def test_photo_read_without_include_image_never_invokes_observer(monkeypatch):
    async def _observe_photo(*_args):
        raise AssertionError("observer must remain pull-on-demand")

    results, enqueued = _run(
        [ToolCall(
            id="photo-meta",
            name="photo_read",
            args={"photo_id": "p1"},
        )],
        turn_authorization=False,
        run_capability=lambda *_a, **_k: _FakeResult(True, {"photo_id": "p1"}),
        observe_photo=_observe_photo,
        monkeypatch=monkeypatch,
    )

    assert "p1" in results[0].content
    assert enqueued == []


def test_screen_read_with_image_uses_native_observer_and_hides_pixels(monkeypatch):
    seen = []

    async def _observe_photo(mime, image_b64):
        seen.append((mime, image_b64))
        return "a terminal window with green text"

    results, enqueued = _run(
        [ToolCall(
            id="screen-image",
            name="screen_read",
            args={"frame_id": "f1", "include_image": True},
        )],
        turn_authorization=False,
        run_capability=lambda *_a, **_k: _FakeResult(True, {
            "frame_id": "f1",
            "image_mime": "image/png",
            "image_b64": "cGl4ZWxz",
        }),
        observe_photo=_observe_photo,
        monkeypatch=monkeypatch,
    )

    assert seen == [("image/png", "cGl4ZWxz")]
    assert "a terminal window with green text" in results[0].content
    assert "cGl4ZWxz" not in results[0].content
    assert enqueued == []


def test_screen_read_default_pixels_also_use_observer_and_hide_base64(monkeypatch):
    seen = []

    async def _observe_photo(mime, image_b64):
        seen.append((mime, image_b64))
        return "the current screen shows a settings page"

    results, enqueued = _run(
        [ToolCall(
            id="screen-default-image",
            name="screen_read",
            args={"frame_id": "f1"},
        )],
        turn_authorization=False,
        run_capability=lambda *_a, **_k: _FakeResult(True, {
            "frame_id": "f1",
            "media_type": "image/png",
            "image_b64": "cGl4ZWxz",
        }),
        observe_photo=_observe_photo,
        monkeypatch=monkeypatch,
    )

    assert seen == [("image/png", "cGl4ZWxz")]
    assert "the current screen shows a settings page" in results[0].content
    assert "cGl4ZWxz" not in results[0].content
    assert "image_b64" not in results[0].content
    assert enqueued == []


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (type("RateLimited", (RuntimeError,), {"error_code": "vision_model_rate_limited"})(
            "raw upstream response must not escape"
        ), "error: vision_model_rate_limited"),
        (RuntimeError("secret provider URL must not escape"), "error: vision_model_failed"),
    ],
)
def test_photo_observer_failure_exposes_only_stable_code(monkeypatch, exc, expected):
    async def _observe_photo(*_args):
        raise exc

    results, _ = _run(
        [ToolCall(
            id="photo-error",
            name="photo_read",
            args={"photo_id": "p1", "include_image": True},
        )],
        turn_authorization=False,
        run_capability=lambda *_a, **_k: _FakeResult(True, {
            "photo_id": "p1",
            "image_media_type": "image/jpeg",
            "image_b64": "cGl4ZWxz",
        }),
        observe_photo=_observe_photo,
        monkeypatch=monkeypatch,
    )

    assert results[0].content == expected
    assert str(exc) not in results[0].content


def test_read_parallelism_is_bounded_by_configured_limit(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    started_count = 0

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        nonlocal active, max_active, started_count
        with lock:
            active += 1
            started_count += 1
            max_active = max(max_active, active)
            started.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return _FakeResult(True, {"body": action_type})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)

    async def scenario():
        task = asyncio.create_task(v2_executor.dispatch_tool_calls(
            [
                ToolCall(id="c1", name="memory_index", args={}),
                ToolCall(id="c2", name="web_search", args={"query": "x"}),
            ],
            store=_Store(),
            api_key="k",
            runtime_token="rt",
            enclave_sem=asyncio.Semaphore(8),
            turn_authorization=False,
            enqueue_write_effect=lambda _tc: None,
            read_parallelism=1,
        ))
        assert await asyncio.to_thread(started.wait, 1.0)
        await asyncio.sleep(0.03)
        with lock:
            assert started_count == 1
        release.set()
        return await task

    results = asyncio.run(scenario())
    assert [result.call_id for result in results] == ["c1", "c2"]
    assert max_active == 1


def test_write_with_authorization_is_enqueued_not_run_inline(monkeypatch):
    ran = []

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        ran.append(action_type)   # must never be called for a write
        return _FakeResult(True, {})

    tool_calls = [ToolCall(id="w1", name="memory_write", args={"actions": [{
        "op": "add", "summary": "tea", "content": "likes tea",
    }]})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert ran == []   # NOT run inline
    assert len(enqueued) == 1 and enqueued[0].id == "w1"
    assert results[0].call_id == "w1"
    assert "queued" in results[0].content
    assert "memory_write" in results[0].content


def test_async_write_enqueue_is_awaited_without_blocking_event_loop(monkeypatch):
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("write must not run inline")),
    )
    started = threading.Event()
    release = threading.Event()
    ticks = {"n": 0}

    def slow_sync_persist():
        started.set()
        assert release.wait(timeout=2)

    async def enqueue_write_effect(_tc):
        await asyncio.to_thread(slow_sync_persist)

    async def scenario():
        dispatch_task = asyncio.create_task(v2_executor.dispatch_tool_calls(
            [ToolCall(
                id="w-async",
                name="identity_patch",
                args={"signature": "new"},
            )],
            store=_Store(),
            api_key="k",
            runtime_token="rt",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=enqueue_write_effect,
        ))

        async def heartbeat():
            while not release.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        assert await asyncio.to_thread(started.wait, 1.0)
        await asyncio.sleep(0.03)
        assert not dispatch_task.done()
        assert ticks["n"] >= 3
        release.set()
        results = await dispatch_task
        await heartbeat_task
        return results

    results = asyncio.run(scenario())
    assert results[0].content == "queued: identity_patch"


@pytest.mark.parametrize("failure_site", ["fence", "enqueue"])
def test_write_fence_and_enqueue_failures_remain_terminal(monkeypatch, failure_site):
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("write must not run inline")),
    )

    async def before_write():
        if failure_site == "fence":
            raise RuntimeError("write fence failed")

    async def enqueue_write_effect(_tc):
        if failure_site == "enqueue":
            raise RuntimeError("write enqueue failed")

    async def scenario():
        return await v2_executor.dispatch_tool_calls(
            [ToolCall(id="w-terminal", name="identity_patch", args={"signature": "new"})],
            store=_Store(),
            api_key="k",
            runtime_token="rt",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=enqueue_write_effect,
            before_write=before_write,
        )

    with pytest.raises(RuntimeError, match=f"write {failure_site} failed"):
        asyncio.run(scenario())


def test_write_without_authorization_is_refused_not_enqueued(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for a refused write")

    tool_calls = [ToolCall(
        id="w2", name="identity_patch", args={"patch": {"signature": "new"}})]
    results, enqueued = _run(
        tool_calls, turn_authorization=False, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "w2"
    assert "authorization" in results[0].content


def test_unknown_tool_name_returns_error_result_no_raise(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for an unknown tool")

    tool_calls = [ToolCall(id="u1", name="not_a_real_tool", args={})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "u1"
    assert "unknown tool" in results[0].content


def test_args_not_ok_returns_error_result_no_raise(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for unparseable args")

    tool_calls = [ToolCall(id="b1", name="memory_write", args={}, args_raw="{not json", args_ok=False)]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "b1"
    assert "error" in results[0].content


def test_invalid_schema_args_never_run_or_enqueue(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("run_capability must not be called for invalid args")

    tool_calls = [
        ToolCall(id="missing", name="web_search", args={}),
        ToolCall(id="wrong-type", name="memory_index", args={"limit": "many"}),
        ToolCall(id="unknown", name="identity_get", args={"surprise": True}),
    ]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert [r.call_id for r in results] == ["missing", "wrong-type", "unknown"]
    assert all("invalid args" in r.content for r in results)


def test_schedule_args_cannot_override_trusted_internal_op(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        raise AssertionError("write tools must never run inline")

    # worker._write_tool_effect_payload supplies this trusted key.  A model-provided
    # value must be rejected before the ToolCall reaches that outbox mapper.
    tool_calls = [ToolCall(
        id="smuggle-op", name="schedule_wake",
        args={"at": "2026-07-15T09:00:00+08:00", "op": "cancel_wake"},
    )]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "smuggle-op"
    assert "invalid args" in results[0].content
    assert "unknown field: op" in results[0].content


def test_mixed_batch_preserves_original_order_and_every_call_id(monkeypatch):
    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        return _FakeResult(True, {"body": "ok"})

    tool_calls = [
        ToolCall(id="a", name="memory_write", args={"actions": [{
            "op": "add", "summary": "tea", "content": "likes tea",
        }]}),                                                                    # write, authorized
        ToolCall(id="b", name="bogus_tool", args={}),                          # unknown
        ToolCall(id="c", name="memory_index", args={}),                        # read
        ToolCall(id="d", name="schedule_wake", args={"when": "x"}, args_ok=False, args_raw="oops"),  # bad args
    ]
    results, enqueued = _run(
        tool_calls, turn_authorization=True, run_capability=_run_capability, monkeypatch=monkeypatch)

    assert [r.call_id for r in results] == ["a", "b", "c", "d"]
    assert all(isinstance(r, ToolResult) for r in results)
    assert [tc.id for tc in enqueued] == ["a"]


# ------------------------------------------------------------------
# Codex I1: a rename (agent_name) must carry self_introduction in the same
# identity_patch call. The check is LIVE-only + pre-enqueue here — it hands the
# model a fixable tool error THIS turn instead of the write silently failing at
# the sink at end-of-turn. It must NOT enqueue the effect. A paired rename, or
# any non-rename patch, still enqueues normally.
# ------------------------------------------------------------------

def _ok_run_capability(action_type, store, *, api_key, runtime_token, params):
    return _FakeResult(True, {"body": "ok"})


def test_identity_rename_without_self_introduction_errors_and_is_not_enqueued(monkeypatch):
    tool_calls = [ToolCall(id="r1", name="identity_patch", args={"agent_name": "Ori"})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].call_id == "r1"
    assert results[0].content == "error: rename_requires_self_introduction"


def test_identity_rename_inside_patch_object_without_intro_is_caught(monkeypatch):
    # The rename may arrive inside the free-form `patch` object, not top-level;
    # the check merges both sides (merge_patch_fields) before judging.
    tool_calls = [ToolCall(id="r2", name="identity_patch", args={"patch": {"agent_name": "Ori"}})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)

    assert enqueued == []
    assert results[0].content == "error: rename_requires_self_introduction"


def test_identity_paired_rename_is_enqueued(monkeypatch):
    tool_calls = [ToolCall(id="r3", name="identity_patch", args={
        "agent_name": "Ori", "self_introduction": "I'm your co-pilot.",
    })]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)

    assert [tc.id for tc in enqueued] == ["r3"]
    assert results[0].content == "queued: identity_patch"


def test_identity_non_rename_patch_is_enqueued(monkeypatch):
    # A patch that touches no agent_name is not a rename — no pairing needed.
    tool_calls = [ToolCall(id="r4", name="identity_patch", args={"self_introduction": "hi"})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)

    assert [tc.id for tc in enqueued] == ["r4"]
    assert results[0].content == "queued: identity_patch"


# ------------------------------------------------------------------
# Item 3/4: relationship_days is validated LIVE, pre-enqueue (like the rename
# pairing check) — a malformed / over-cap value hands the model a fixable error
# THIS turn and is NOT enqueued. A valid value enqueues normally. Kept out of
# tool_schema.validate_tool_args (which also gates replay).
# ------------------------------------------------------------------

def test_identity_relationship_days_invalid_errors_and_is_not_enqueued(monkeypatch):
    tool_calls = [ToolCall(id="d1", name="identity_patch",
                           args={"patch": {"relationship_days": "300"}})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)
    assert enqueued == []
    assert results[0].content == "error: relationship_days_must_be_non_negative_int"


def test_identity_relationship_days_over_cap_errors_and_is_not_enqueued(monkeypatch):
    tool_calls = [ToolCall(id="d2", name="identity_patch",
                           args={"patch": {"relationship_days": 10 ** 9}})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)
    assert enqueued == []
    assert results[0].content == "error: relationship_days_out_of_range"


def test_identity_valid_relationship_days_is_enqueued(monkeypatch):
    tool_calls = [ToolCall(id="d3", name="identity_patch",
                           args={"patch": {"relationship_days": 300}})]
    results, enqueued = _run(
        tool_calls, turn_authorization=True,
        run_capability=_ok_run_capability, monkeypatch=monkeypatch)
    assert [tc.id for tc in enqueued] == ["d3"]
    assert results[0].content == "queued: identity_patch"


# ------------------------------------------------------------------
# Item 1: the write producer FREEZES the absolute anchor at enqueue time. A
# relationship_days patch gets an absolute relationship_started_at (= today - N)
# added as trusted top-level metadata; the sink consumes it verbatim so a delayed
# replay cannot drift the anchor.
# ------------------------------------------------------------------

def test_write_tool_effect_payload_freezes_relationship_anchor():
    from datetime import date, timedelta
    from model_api_runtime.v2 import worker as v2_worker
    tc = ToolCall(id="f1", name="identity_patch", args={"patch": {"relationship_days": 300}})
    effect_type, payload = v2_worker._write_tool_effect_payload(tc)
    assert effect_type == "identity"
    # relationship_days is the 1-based "第 N 天" (met day = 第 1 天), so N=300
    # freezes to elapsed N-1=299 → today-299.
    assert payload["relationship_started_at"] == (date.today() - timedelta(days=299)).isoformat()
    # the relative value is preserved for audit; the frozen absolute wins at the sink
    assert payload["patch"]["relationship_days"] == 300


def test_write_tool_effect_payload_freezes_top_level_relationship_days():
    # Regression: relationship_days is a FIRST-CLASS top-level arg (like agent_name),
    # not only reachable inside `patch`. A weak model naturally calls
    # identity_patch(relationship_days=N) — the same shape it uses for agent_name.
    # The producer must freeze the anchor for THIS shape too (it used to read only
    # payload["patch"], so a top-level call skipped the freeze). 1-based: N=300 → 299.
    from datetime import date, timedelta
    from model_api_runtime.v2 import worker as v2_worker
    tc = ToolCall(id="ftl", name="identity_patch", args={"relationship_days": 300})
    effect_type, payload = v2_worker._write_tool_effect_payload(tc)
    assert effect_type == "identity"
    assert payload["relationship_started_at"] == (date.today() - timedelta(days=299)).isoformat()


def test_write_tool_effect_payload_no_freeze_without_relationship_days():
    from model_api_runtime.v2 import worker as v2_worker
    tc = ToolCall(id="f2", name="identity_patch", args={"patch": {"signature": ["x"]}})
    _effect_type, payload = v2_worker._write_tool_effect_payload(tc)
    assert "relationship_started_at" not in payload  # byte-identical to a pre-item-1 row


def test_frozen_and_resolve_agree_on_same_relationship_days():
    # Item 1 invariant LOCK: the V2 producer (worker._frozen_relationship_anchor,
    # frozen at enqueue) and the direct/fallback consumer
    # (actions._resolve_relationship_anchor) must resolve the SAME 1-based N to the
    # SAME absolute anchor — else a V2 replay that re-derives from N drifts off the
    # frozen date. The other tests pin each side to a hand-written today-(N-1); this
    # one ties the two REAL ends together, so a future one-sided edit to the -1
    # (only worker, or only actions) can no longer pass silently.
    from model_api_runtime.v2 import worker as v2_worker
    from identity import actions as identity_actions
    from identity import card_policy
    MAX = card_policy.MAX_RELATIONSHIP_DAYS
    for n in (1, 45, MAX):
        frozen = v2_worker._frozen_relationship_anchor({"relationship_days": n})
        # handed the producer's frozen date, the consumer returns it verbatim…
        assert identity_actions._resolve_relationship_anchor(n, trusted_frozen=frozen) == frozen
        # …and with NO trusted value it recomputes from N to the SAME anchor.
        assert identity_actions._resolve_relationship_anchor(n, trusted_frozen=None) == frozen
