"""optional 重放窗口锚定的热路径行为测试。

与 tests/test_v2_tail_anchor_store.py 的分工：那里测锚点的存取（读写、单调、
CASCADE），这里测锚点如何影响 prompt 组装。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import provider_client
from capabilities import registry as cap_registry
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker

from conftest import seed_user, set_v2_runtime_owner

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed optional-anchor tests require the PostgreSQL test fixture",
)


def _append(uid, role, text, *, source=None):
    """插入一条真实 chat 行并返回真实 seq。

    seq 是全表共享的 identity 列，绝不等于该用户自己的第几条消息。

    ``store.append_chat`` 的 doc["source"] 来自其第二个位置参数（见
    backend/chat/chat_core.py 里 `store.append_chat("user", "verify_ping",
    synthetic_env)` 的真实用法），不是 envelope 内的字段，因此这里把
    ``source`` 覆盖到那个位置参数上，而不是塞进 envelope。
    """
    from core import store as core_store

    store = core_store.get_store(uid)
    envelope = {
        "v": 1,
        "body_ct": text,
        "nonce": "n",
        "K_user": "k_test",
        "id": f"{uid}-{text}",
    }
    row_source = source or ("user_message" if role == "user" else "model_api")
    row = store.append_chat(
        role,
        row_source,
        envelope,
        strict=True,
    )
    seq = db.chat_seq_for_msg_id(uid, row["id"])
    assert seq is not None
    return seq


def test_genuine_turn_count_after_seq_counts_only_real_user_turns():
    """与 chat_recent_genuine_turn_boundary_seq 同口径：只数 user/human，
    且排除 verify_ping / resident_maintenance 合成行。"""
    seed_user("u_optanchor_cnt")
    first = _append("u_optanchor_cnt", "user", "m1")
    _append("u_optanchor_cnt", "assistant", "r1")
    _append("u_optanchor_cnt", "user", "m2")
    _append("u_optanchor_cnt", "user", "m3")
    _append("u_optanchor_cnt", "user", "ping", source="verify_ping")
    through = db.chat_max_seq("u_optanchor_cnt")

    assert db.chat_genuine_turn_count_after_seq(
        "u_optanchor_cnt", after_seq=first, through_seq=through
    ) == 2, "verify_ping 行不得计入"
    assert db.chat_genuine_turn_count_after_seq(
        "u_optanchor_cnt", after_seq=0, through_seq=through
    ) == 3


def test_anchor_hysteresis_ceiling_exceeds_target():
    """没有滞后区就退化回逐轮滑动窗口——即本次要修的 bug 本身。"""
    from model_api_runtime.v2 import worker

    assert worker._CHAT_TAIL_ANCHOR_MAX_TURNS > worker._CHAT_TAIL_MAX_TURNS


# --- Hot-path acceptance rig, copied from
# archive/v2-tail-anchor-wrong-wiring:tests/test_v2_tail_anchor_store.py (the
# helpers there are already proven to exercise a real `worker.process_job`
# call end to end). Kept together with their original comments because those
# comments record the exact pitfalls that make a rig silently no-op instead
# of exercising the code under test. -----------------------------------------

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="",
)


class _FakeCapResult:
    def to_dict(self):
        return {"ok": True, "data": {}, "error": None, "trace": {}, "warnings": []}


def _reset_anchor_hotpath_user(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_chat_tail_anchor WHERE user_id=%s", (uid,))
    # claim_next_job's candidate query INNER JOINs v2_runtime_state — without
    # an authoritative v2-owned row here the job is unclaimable and
    # claim_next_job silently returns None (mirrors
    # tests/test_v2_atomic_reply_cursor.py's `_reset`).
    set_v2_runtime_owner(uid)


def _seed_chat_row(uid: str, msg_id: str, ts: float, role: str) -> int:
    """Real ``chat_messages`` row (plaintext-shaped — no live enclave in this
    test process, same convention as ``_append`` above), via the strict
    write path the production chat lane itself uses. Returns the real seq.

    Carries a plaintext ``content`` field (in addition to the usual
    ``body_ct`` ciphertext placeholder) because
    ``model_api_runtime.v2.coalesce.coalesce_pending``'s default decrypt
    reads ``row["content"]`` — without it every "pending" row looks empty
    and gets silently dropped, so the chat lane takes the "no coalesced
    input" shortcut and returns before ever reaching the code this test
    exists to exercise (caught by asserting the anchor/boundary spies were
    actually invoked, not just trusting a "completed" status)."""
    db.chat_append_strict(
        uid, msg_id, ts,
        {
            "id": msg_id, "role": role, "ts": ts,
            "body_ct": f"cipher-{msg_id}", "nonce": "n",
            "K_user": "k", "K_enclave": "e",
            "content": f"plaintext-{msg_id}",
        },
        5000,
    )
    seq = db.chat_seq_for_msg_id(uid, msg_id)
    assert seq is not None
    return seq


def _fake_sealed_envelope(item_id: str, *, body: str = "reply-ciphertext") -> dict:
    """Same shape as tests/test_v2_atomic_reply_cursor.py's ``_envelope``."""
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": "ignored-by-store",
        "visibility": "shared",
        "body_ct": body,
        "nonce": "nonce",
        "K_user": "sealed-user-key",
        "K_enclave": "sealed-enclave-key",
    }


def _optional_anchor_deps(uid: str, monkeypatch, *, watermark_seq: int) -> "worker.TurnDeps":
    """Same rig as the archive's ``_seq_native_chat_deps``, with two changes
    needed to actually make the optional window non-empty (the trap called
    out in the task brief):

    1. ``_summary_with_seq`` returns a CONFIGURABLE watermark instead of the
       archive's hard-coded ``("", 0.0, 0, 0)``. Watermark=0 means "never
       compacted" -> every row in history is strictly after the watermark ->
       every row lands in the REQUIRED tail and the optional-replay window is
       permanently empty. A test built on that rig cannot observe window
       sliding no matter what the production code does — it would pass for
       the wrong reason.

    2. ``read_recent_turns`` is wired (the archive rig left it ``None``).
       ``worker._read_seq_adaptive_prompt_context`` only populates
       ``optional_tail_turns`` when ``deps.read_recent_turns is not None``
       (see worker.py's call site guard) — without it, optional replay is
       empty regardless of the watermark, for a second, independent reason.
       The real production wiring is ``serve_worker._read_recent_turns``,
       which additionally runs rows through the live-enclave decrypt path;
       this test process has no live enclave, so this fake instead calls
       ``db.chat_recent_turn_rows`` directly — the rows seeded by
       ``_seed_chat_row`` already carry plaintext ``content``, so no decrypt
       step is needed to exercise the real turn-grouping/windowing SQL and
       the real ``worker._adaptive_replay_parts`` split logic downstream.
    """
    from model_api_runtime.v2 import serve_worker

    def _rows(after_seq, *, limit=None, oldest_first=True, through_seq=None):
        return db.chat_messages_after_seq(
            uid, after_seq, limit=limit, oldest_first=oldest_first,
            through_seq=through_seq,
        )

    def _messages_after(_uid, after_seq):
        return [r for r in _rows(after_seq) if r.get("role") in {"user", "human"}]

    def _tail_after(_uid, after_seq, limit, *, through_seq=None):
        return _rows(after_seq, limit=limit, oldest_first=False, through_seq=through_seq)

    def _summary_with_seq(_uid):
        return "PRIOR SUMMARY", 0.0, 1, int(watermark_seq)

    def _recent_turns(_uid, max_turns, row_cap, *, through_seq=None):
        """Mirrors ``serve_worker._read_recent_turns``'s ``_genuine_user``
        annotation (production derives it from the raw row's role/source
        before decrypt overwrites content; this test process has no live
        enclave, so the row is already plaintext-shaped, but the annotation
        logic must still be copied — NOT left unset.

        Why this matters: ``worker._adaptive_replay_parts`` (worker.py
        ~2824-2836) unconditionally overwrites every REQUIRED-tail row's
        ``_genuine_user`` with whatever this window observed for the same
        seq. Leaving every row unannotated pins that value to ``False`` for
        every row this window covers, including the genuine new user rows in
        the required tail — so ``required_turn_count``
        (worker.py:3119, ``sum(1 for row in required_tail if
        _is_genuine_user_seed(row))``) reads as 0 on every turn regardless of
        how many real user turns are actually in the required tail. That in
        turn froze the OLD (pre-anchor) formula's
        ``optional_limit = target_turns - required_turn_count`` to a
        constant, making ``optional_turns[-optional_limit:]`` identical
        across turns by construction — independent of whether the code under
        test anchors the window or still slides it. A mutation test that
        force-disables the anchor branch (falls back to the old formula)
        exposed this: the acceptance test kept passing. Real production
        behavior needs this row correctly labelled so ``required_turn_count``
        actually reflects the two new genuine user turns seeded mid-test.
        """
        window = db.chat_recent_turn_rows(
            _uid, max_turns=max_turns, row_cap=row_cap, through_seq=through_seq,
        )
        rows = list(window.get("rows") or [])
        for row in rows:
            role = str(row.get("role") or "").strip().lower()
            source = str(row.get("source") or "")
            row["_genuine_user"] = (
                role in {"user", "human"}
                and source not in {"verify_ping", "resident_maintenance"}
            )
        return {**window, "rows": rows}

    async def _fake_chat_completion(config, messages, *, tools=None, **_kwargs):
        return {
            "reply": "anchor hot-path reply", "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake_chat_completion)
    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult())
    # Bypass the live-enclave envelope build (none in this test process) —
    # same fake as test_v2_atomic_reply_cursor.py.
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _text, *, item_id=None: (_fake_sealed_envelope(str(item_id)), ""),
    )

    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_messages_after,
        read_tail_after_seq=_tail_after,
        read_compaction_tail_after_seq=_tail_after,
        read_summary_with_seq=_summary_with_seq,
        read_recent_turns=_recent_turns,
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )


def _spy_on_boundary_query(monkeypatch):
    """Wrap (not stub) ``db.chat_recent_genuine_turn_boundary_seq`` so calls
    are counted while the real implementation still runs — a stub would hide
    whether the wiring under test paid for a real boundary query or not.

    Copied verbatim from
    archive/v2-tail-anchor-wrong-wiring:tests/test_v2_tail_anchor_store.py.
    """
    calls: list[tuple[tuple, dict]] = []
    original = db.chat_recent_genuine_turn_boundary_seq

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "chat_recent_genuine_turn_boundary_seq", _spy)
    return calls


def _run_one_turn(uid, deps, monkeypatch, *, tag: str) -> None:
    """追加一条新用户消息并完整跑一个 chat 回合。

    调用形态照抄
    ``test_prompt_prefix_is_byte_identical_across_consecutive_turns`` 里已经
    跑通的写法（enqueue_job(uid, "chat") / claim_next_job(worker_id) /
    process_job(job, deps, provider_config=..., api_key=None,
    runtime_token=...)) —— 不套用本文件旧版 docstring 里示意性的参数拼法。
    """
    _seed_chat_row(uid, f"{uid}-{tag}", 3000.0 + hash(tag) % 1000, "user")
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job(f"turn-worker-{tag}")
    assert job is not None, f"job 未被 claim（tag={tag}）——rig 没搭对"
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
    ))
    assert status == "completed", f"回合 tag={tag} 未 completed：{status!r}"


def test_anchor_holds_within_hysteresis_band(monkeypatch):
    """滞后区内：锚点值不变，且不额外付出锚点专属的那次边界查询代价。

    ``db.chat_recent_genuine_turn_boundary_seq`` 在 worker.py 里被调用两处：
    一处为 ``compact_through_seq`` 服务，每回合无条件调用一次（与本设计无关，
    改动前后都存在）；另一处仅在 ``stored_anchor is None or turns_after_anchor
    >= _CHAT_TAIL_ANCHOR_MAX_TURNS`` 时才调用，这才是滞后区设计声称要跳过的
    那次。因此滞后区内的期望调用次数是 1（只有前者），不是 0——断言 0 会把
    这条测试钉死在与当前实现不符的期望上，对着一个正确实现报红。
    """
    uid = "u_optanchor_hold"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)

    seqs = [
        _seed_chat_row(uid, f"{uid}-h{i}", 1000.0 + i,
                       "user" if i % 2 == 0 else "openclaw")
        for i in range(60)
    ]
    watermark = seqs[-1]
    # 锚点落在滞后区内：其后的真实用户轮数远小于 _CHAT_TAIL_ANCHOR_MAX_TURNS
    jobs_store.set_chat_tail_anchor(uid, seqs[-6])
    before = jobs_store.get_chat_tail_anchor(uid)

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    boundary_calls = _spy_on_boundary_query(monkeypatch)
    _run_one_turn(uid, deps, monkeypatch, tag="hold")

    assert jobs_store.get_chat_tail_anchor(uid) == before, "滞后区内锚点不应移动"
    assert len(boundary_calls) == 1, (
        f"滞后区内只应付出 compact_through_seq 那次无条件边界查询，"
        f"锚点专属的那次不应触发；实际被调用 {len(boundary_calls)} 次"
    )


def test_anchor_advances_once_past_the_ceiling(monkeypatch):
    """越过滞后上限：锚点前移一次，且锚点专属的那次边界查询确实被调用。

    期望调用次数是 2：``compact_through_seq`` 的无条件那次，加上越过滞后
    上限后触发的锚点专属那次（见上面 hold 测试的说明）。只断言「非空」
    区分不出这两种情形——hold 场景本身也会有 1 次非零调用。
    """
    uid = "u_optanchor_advance"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)

    # 制造远超滞后上限的用户轮数：锚点钉在最早，其后累积 > 上限
    total_turns = worker._CHAT_TAIL_ANCHOR_MAX_TURNS + 10
    seqs = []
    for i in range(total_turns * 2):
        seqs.append(_seed_chat_row(uid, f"{uid}-a{i}", 1000.0 + i,
                                   "user" if i % 2 == 0 else "openclaw"))
    watermark = seqs[-1]
    jobs_store.set_chat_tail_anchor(uid, seqs[0])
    before = jobs_store.get_chat_tail_anchor(uid)

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    boundary_calls = _spy_on_boundary_query(monkeypatch)
    _run_one_turn(uid, deps, monkeypatch, tag="advance")

    after = jobs_store.get_chat_tail_anchor(uid)
    assert after > before, f"越过上限后锚点应前移（{before} -> {after}）"
    assert len(boundary_calls) == 2, (
        f"越过上限时锚点专属的那次边界查询必须真的触发（预期 2 次，含 "
        f"compact_through_seq 的无条件那次）；实际 {len(boundary_calls)} 次"
    )


def test_prefix_stable_again_after_an_advance(monkeypatch):
    """前移那一轮前缀变化是设计内的；此后必须重新稳定。

    这条防的是「锚点每轮都在前移」——那样每轮都合法地变，但等于没修。

    同 ``test_prompt_prefix_is_byte_identical_across_consecutive_turns``：把
    ``_CHAT_TAIL_MAX_TURNS`` 下调到 3。历史只有 30 轮时，若不下调，
    ``target_turns - required_turn_count`` 算出来的 ``optional_limit`` 在
    s1/s2 两轮之间虽然不同，但两者都远大于候选池的 30 轮，切片会截断到同一个
    「全量池」而"碰巧"长得一样——旧公式的退化不会被观察到，测试就测不出
    正在验收的那个 bug（用短路禁用锚点分支实测验证过：不下调时这条测试
    对着错误实现仍然全绿）。
    """
    import json

    uid = "u_optanchor_restable"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)
    monkeypatch.setattr(worker, "_CHAT_TAIL_MAX_TURNS", 3)

    seqs = [
        _seed_chat_row(uid, f"{uid}-r{i}", 1000.0 + i,
                       "user" if i % 2 == 0 else "openclaw")
        for i in range(60)
    ]
    watermark = seqs[-1]
    jobs_store.set_chat_tail_anchor(uid, seqs[0])  # 立刻会触发一次前移

    captured: list[list[dict]] = []

    async def _capture(config, messages, *, tools=None, **_kw):
        captured.append(json.loads(json.dumps(messages, ensure_ascii=False,
                                              sort_keys=True, default=str)))
        return {"reply": "ok", "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    monkeypatch.setattr(provider_client, "chat_completion_async", _capture)

    _run_one_turn(uid, deps, monkeypatch, tag="adv")   # 第 1 轮：可能前移
    anchor_after_advance = jobs_store.get_chat_tail_anchor(uid)
    _run_one_turn(uid, deps, monkeypatch, tag="s1")    # 第 2 轮
    _run_one_turn(uid, deps, monkeypatch, tag="s2")    # 第 3 轮

    assert jobs_store.get_chat_tail_anchor(uid) == anchor_after_advance, (
        "前移之后锚点必须重新稳定，不能每轮都动"
    )
    assert len(captured) == 3
    prefix_2 = _prefix_before_required(captured[1], f"plaintext-{uid}-s1")
    prefix_3 = _prefix_before_required(captured[2], f"plaintext-{uid}-s1")
    assert json.dumps(prefix_2, sort_keys=True, ensure_ascii=False) == \
           json.dumps(prefix_3, sort_keys=True, ensure_ascii=False), (
        "前移之后的连续两轮，前缀应重新逐字节一致"
    )


def _prefix_before_required(messages, required_first_content):
    """messages 中 required tail 第一条之前的全部消息（含 system 与摘要块）。"""
    for index, message in enumerate(messages):
        if message.get("content") == required_first_content:
            return messages[:index]
    raise AssertionError("required tail 的第一条未出现在 prompt 中")


def test_prompt_prefix_is_byte_identical_across_consecutive_turns(monkeypatch):
    """验收标准本身：锚点不变期间，连续两回合的 prompt 前缀逐字节一致。

    这条测试存在的唯一理由，是上一次实施把锚点接到了不影响 prompt 的
    compact_through_seq 上，而当时的测试（只断言锚点保持/前移、边界查询被跳过）
    照样全绿。任何「看起来接上了」的实现，都必须先过这一关。

    ``worker._CHAT_TAIL_MAX_TURNS`` 被下调到 3：optional 候选池有 30 轮历史
    （远大于 3），这样 ``db.chat_recent_turn_rows`` 的 "最近 N 轮" 窗口才会
    在两个连续回合之间真的移动一整轮——用默认的 40 轮上限、只有 30 轮历史，
    窗口根本填不满、永远等于全量历史，两回合会碰巧长得一样，测试就测不出
    正在验收的那个 bug（同样是"看起来测了、其实没测到"的陷阱）。
    """
    import json

    uid = "u_optanchor_prefix"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)
    monkeypatch.setattr(worker, "_CHAT_TAIL_MAX_TURNS", 3)

    # 30 轮历史（60 行，user/openclaw 交替）落在 watermark 之下 → 成为 optional
    # 重放素材。watermark = 这批历史里最后一行的 seq，即"从未压缩过、但已经有
    # 一版摘要覆盖到这里"的口径（配合上面改造过的 _summary_with_seq）。
    seqs = []
    for i in range(60):
        role = "user" if i % 2 == 0 else "openclaw"
        seqs.append(_seed_chat_row(uid, f"{uid}-h{i}", 1000.0 + i, role))
    watermark = seqs[-1]

    captured: list[list[dict]] = []

    async def _capture_chat_completion(config, messages, *, tools=None, **_kw):
        captured.append(json.loads(json.dumps(messages, ensure_ascii=False,
                                              sort_keys=True, default=str)))
        return {"reply": "ok", "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    monkeypatch.setattr(provider_client, "chat_completion_async",
                        _capture_chat_completion)

    # 两个连续回合，中间只追加一条新用户消息——调用形态照抄归档分支
    # test_process_job_chat_lane_* 两个集成测试里已经跑通过的写法
    # （enqueue_job(uid, "chat") / claim_next_job(worker_id) /
    # process_job(job, deps, provider_config=..., api_key=None,
    # runtime_token=...)），不套用本文件旧版 docstring 里示意性的参数拼法。
    for turn in range(2):
        _seed_chat_row(uid, f"{uid}-new{turn}", 2000.0 + turn, "user")
        jobs_store.enqueue_job(uid, "chat")
        job = jobs_store.claim_next_job(f"prefix-worker-{turn}")
        assert job is not None, "job 未被 claim——rig 没搭对"
        status = asyncio.run(worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
        ))
        assert status == "completed", f"回合 {turn} 未 completed：{status!r}"

    assert len(captured) == 2, f"期望两次 provider 调用，实到 {len(captured)}"

    # 陷阱自检：optional 候选池必须真的非空，否则本测试什么也没测到就"通过"了
    # （见 _optional_anchor_deps 的 docstring）。两个回合的 prompt 里都应该能
    # 找到至少一条 watermark 之前的历史消息（`_CHAT_TAIL_MAX_TURNS=3` 只留最近
    # 几轮，所以出现的是 h5x 附近而非最老的 h0——只要有任意一条 "-h" 历史行，
    # 就证明 optional 候选池非空；一条都没有则说明 rig 又退化回了"全部落进
    # required"的陷阱状态）。
    for turn_index, prompt in enumerate(captured):
        contents = {m.get("content") for m in prompt if isinstance(m, dict)}
        history_rows = [c for c in contents if isinstance(c, str) and f"{uid}-h" in c]
        assert history_rows, (
            f"回合 {turn_index} 的 prompt 里没有任何历史行——optional 候选池为空，"
            "掉进了 watermark=0 同款陷阱，测试测不到任何东西"
        )

    first_required = f"plaintext-{uid}-new0"
    prefix_a = _prefix_before_required(captured[0], first_required)
    prefix_b = _prefix_before_required(captured[1], first_required)

    assert json.dumps(prefix_a, sort_keys=True, ensure_ascii=False) == \
           json.dumps(prefix_b, sort_keys=True, ensure_ascii=False), (
        "两回合之间 prompt 前缀发生了变化——optional 窗口仍在逐轮滑动"
    )
