"""唤醒道的 MCP 工具面与折叠 schema 恢复(T154)。

在此之前唤醒道有两个洞:

1. **没有 MCP。** `_run_wake` 从不调 `deps.load_mcp_turn`,用户配的 MCP 服务器
   只在 chat 轮存在 —— 心跳/定时提醒里伴侣没有任何外部工具。
2. **折叠了却搜不回来。** prompt frontier 在上下文压力下会把非常驻工具折成
   「只剩一句描述、参数表为空」的发现态。chat 自 T143 起用 `mcp_tool_search`
   把说明书取回,而唤醒道既没有 recovery state、又把 `mcp_tool_search` 放在
   `wake_disabled_tool_names` 里 —— 折掉的工具在这一轮里再也拿不回参数。
   这一条**与 MCP 无关**:平台工具(workspace_read 等)一样会被折。

顺带补上一个此前全无覆盖的口子:dispatch 处对「schema 没加载就直接调」的
运行时拒绝(worker 里那句 "tool schema is not loaded")。它是 durable 写和
远端请求之前的最后一道闸,而 2026-08-20 之前**整个仓库没有一条测试提到它**
—— 把 `requires_resolution` 改成恒 False,133 条 MCP 相关测试全绿。

风格沿用 test_v2_wake_worker.py(真 DB + 打桩 provider)与
test_v2_worker_mcp.py(_FakeMcpTurn)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from capabilities import tool_schema as cap_tool_schema
from provider_types import ToolResult, ToolSpec
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import tool_loop
from model_api_runtime.v2 import worker
from core import store as core_store

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"), reason="needs PG")


_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="sk-user-byok",
    base_url="",
)

_MCP_SPEC = ToolSpec(
    name="mcp__town__walk",
    description="walk around the town and report what is there",
    parameters={
        "type": "object",
        "properties": {"where": {"type": "string"}},
        "required": ["where"],
    },
)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
        conn.execute(
            "DELETE FROM v2_mcp_mutation_attempts WHERE user_id=%s", (uid,)
        )
    conftest.set_v2_runtime_owner(uid)


def _claim(job_id: int) -> str:
    job = jobs_store.claim_next_job("wake-mcp-test")
    assert job is not None and job["id"] == job_id
    return str(job["claimed_by"])


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()


def _apply_effects(user_id):
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(
                core_store.get_store(user_id), str(payload.get("text") or "")
            )
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=dispatch)


def _wake_deps(*, tail=None, load_mcp_turn=None, emit_debug_trace=None):
    rows = list(tail if tail is not None else [])
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt-wake-enclave",
        read_tail=lambda uid, after_ts, limit: list(rows),
        has_genuine_user_history=lambda _uid: bool(rows),
        apply_pending_effects=_apply_effects,
        load_mcp_turn=load_mcp_turn,
        emit_debug_trace=emit_debug_trace,
    )


def _script_provider(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None, **_kwargs):
        calls.append({"messages": messages, "tools": tools})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _tool_result_texts(messages) -> list[str]:
    """Every tool result the model saw in this request.

    `run_tool_loop` hands prior rounds back as `ToolExchange` objects, not
    dicts — reading them with `.get` silently finds nothing, which would make
    a refusal assertion pass for the wrong reason.
    """
    texts: list[str] = []
    for message in messages:
        for result in getattr(message, "results", ()) or ():
            texts.append(str(getattr(result, "content", "") or ""))
        if isinstance(message, dict):
            texts.append(str(message.get("content") or ""))
    return texts


def _spy_reply(monkeypatch):
    written = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda store, text: written.append(text) or {"id": f"r{len(written)}"},
    )
    return written


_TAIL = [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]


class _FakeMcpTurn:
    """Duck-typed stand-in for a loaded McpTurn (no enclave, no network)."""

    def __init__(self, specs, recorder, *, read_only_names=(), instructions=()):
        self.tool_specs = list(specs)
        self._rec = recorder
        self._read_only_names = frozenset(read_only_names)
        self.instructions = list(instructions)

    @property
    def is_empty(self):
        return not self.tool_specs

    def handles(self, name):
        return str(name).startswith("mcp__")

    def is_read_only(self, name):
        return name in self._read_only_names

    @property
    def mutating_tool_names(self):
        return frozenset(
            spec.name
            for spec in self.tool_specs
            if spec.name not in self._read_only_names
        )

    async def dispatch(self, call):
        self._rec.append((call.name, dict(call.args or {})))
        return ToolResult(call_id=call.id, content="the town is quiet")


class _FoldedFakeMcpTurn(_FakeMcpTurn):
    """A turn whose only tool arrives folded and must be searched for first."""

    def __init__(self, recorder, *, read_only_names=()):
        folded = ToolSpec(
            name=_MCP_SPEC.name,
            description=_MCP_SPEC.description,
            parameters={"type": "object", "properties": {}},
        )
        super().__init__([folded], recorder, read_only_names=read_only_names)
        self.collapsed_names = {_MCP_SPEC.name}
        self._resolved = False

    def current_tool_specs(self):
        return list(self.tool_specs)

    def requires_resolution(self, name):
        return name in self.collapsed_names and not self._resolved

    def resolve_tool_schemas(self, args):
        names = list(args.get("names") or [])
        resolved = [_MCP_SPEC.name] if _MCP_SPEC.name in names else []
        if resolved:
            self._resolved = True
            self.tool_specs[:] = [_MCP_SPEC]
        return {
            "resolved": resolved,
            "not_found": [n for n in names if n not in resolved],
            "tools": (
                [{
                    "name": _MCP_SPEC.name,
                    "description": _MCP_SPEC.description,
                    "parameters": _MCP_SPEC.parameters,
                }]
                if resolved
                else []
            ),
        }


def _make_load_turn_mcp(turn, seen=None):
    async def _fake_load(store, *, api_key=None, runtime_token="",
                         enclave_sem=None, lane="chat"):
        if seen is not None:
            seen.append({
                "user_id": store.user_id,
                "api_key": api_key,
                "runtime_token": runtime_token,
                "enclave_sem": enclave_sem,
                "lane": lane,
            })
        return turn
    return _fake_load


# ---------------------------------------------------------------------------
# 1. 唤醒道拿得到 MCP 工具面
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lane", ["heartbeat", "scheduled", "manual_wake", "screen_watch"]
)
def test_wake_offers_and_dispatches_mcp_tool(monkeypatch, lane):
    """心跳/定时/手动唤醒都拿到 MCP,并且调用路由到 turn 的 dispatcher。

    Seven 的原始需求就是这一条:「让 AI 在每次心跳的时候,使用 MCP 去小镇逛
    一下」。全 lane 参数化是因为「唤醒道全给,包括 scheduled」是明确拍过的。
    """
    uid = f"u_wake_mcp_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)

    dispatched = []
    seen_load = []
    turn = _FakeMcpTurn(
        [_MCP_SPEC], dispatched, read_only_names={_MCP_SPEC.name}
    )
    calls = _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [
                {"id": "w1", "name": _MCP_SPEC.name, "args": {"where": "park"}}
            ],
            "usage": {},
        },
        {"reply": "小镇上很安静", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(
        tail=_TAIL,
        load_mcp_turn=_make_load_turn_mcp(turn, seen=seen_load),
    )
    enclave_sem = asyncio.Semaphore(2)

    status = asyncio.run(worker._run_wake(
        job_id, uid, lane, deps, _BYOK, enclave_sem, claimed_by
    ))

    assert status == "completed"
    # 凭据契约:唤醒没有用户在场,只能用 runtime_token 解 MCP 配置信封。
    assert seen_load and seen_load[0]["user_id"] == uid
    assert seen_load[0]["api_key"] is None
    assert seen_load[0]["runtime_token"] == "rt-wake-enclave"
    assert seen_load[0]["enclave_sem"] is enclave_sem
    # 观测连线:装配层默认写 chat,调用点漏传的话 admin 里这条 wake 会伪装成
    # 聊天轮。只测 wrapper 本身抓不到「调用点漏传」。
    assert seen_load[0]["lane"] == lane
    offered = {spec.name for spec in calls[0]["tools"]}
    assert _MCP_SPEC.name in offered
    assert dispatched == [(_MCP_SPEC.name, {"where": "park"})]


def test_wake_without_folding_does_not_offer_mcp_tool_search(monkeypatch):
    """没有东西需要恢复时不发这把工具 —— 它是恢复口,不是常驻工具。

    与下面的压力用例成对:这条钉住「不需要就不给」,那条钉住「需要就必须给」。
    单看任何一条都可能把 T154 之前的坏行为(**永远不给**)误判成正确。
    """
    uid = "u_wake_no_tool_search"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)
    calls = _script_provider(monkeypatch, [
        {"reply": "在的", "tool_calls": [], "usage": {}},
    ])

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", _wake_deps(tail=_TAIL), _BYOK,
        asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    offered = {spec.name for spec in calls[0]["tools"]}
    assert cap_tool_schema.MCP_TOOL_SEARCH_TOOL not in offered
    # 但目录本身是全的 —— 「不给恢复口」不等于「工具变少了」。
    assert "workspace_read" in offered


# ---------------------------------------------------------------------------
# 2. 折叠的平台工具在唤醒道能搜回来(这条与 MCP 无关,是 T154 之前就存在的洞)
# ---------------------------------------------------------------------------

def _force_pressure(monkeypatch):
    """把一个无关工具的说明书撑爆,逼 prompt frontier 进压力折叠形态。"""
    monkeypatch.setitem(
        cap_tool_schema.DESCRIPTIONS,
        "identity_get",
        "oversized optional manual " * 6_000,
    )
    monkeypatch.setattr(tool_loop, "_CATALOG", None)
    return provider_client.ProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-test",
        api_key="sk-user-byok",
        base_url="",
        context_window_tokens=40_000,
    )


def test_wake_pressure_folded_platform_schema_is_searchable(monkeypatch):
    """唤醒轮里被压力折掉的平台工具,能用 mcp_tool_search 拿回完整参数表。

    T154 之前这一轮会怎样:workspace_read 被折成空参数表,而 mcp_tool_search
    被 `wake_disabled_tool_names` 关着 —— 模型看得见名字、永远填不对参数。
    """
    uid = "u_wake_folded_platform"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)
    pressure_config = _force_pressure(monkeypatch)

    calls = _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [{
                "id": "find",
                "name": cap_tool_schema.MCP_TOOL_SEARCH_TOOL,
                "args": {"names": ["workspace_read"]},
            }],
            "usage": {},
        },
        {"reply": "参数表拿到了", "tool_calls": [], "usage": {}},
    ])

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", _wake_deps(tail=_TAIL), pressure_config,
        asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    first = {spec.name: spec for spec in calls[0]["tools"]}
    second = {spec.name: spec for spec in calls[1]["tools"]}
    # T154 之前这里是空的:mcp_tool_search 被 wake_disabled_tool_names 无条件
    # 拿掉,所以「需要恢复」这个条件永远等不到它出现。
    assert cap_tool_schema.MCP_TOOL_SEARCH_TOOL in first
    # 目录不变(名字一直在),变的只是说明书。
    assert "workspace_read" in first and "workspace_read" in second
    assert first["workspace_read"].parameters["properties"] == {}
    assert second["workspace_read"].parameters["required"] == ["path"]
    # 没被点名的那个仍是折叠态 —— 搜索不是「全部展开」。
    assert second["identity_get"].parameters["properties"] == {}


def test_wake_refuses_folded_platform_call_before_dispatch(monkeypatch):
    """折叠态下直接调平台工具 → 运行时拒绝,不进 executor。

    这条闸此前**在 chat 和 wake 两边都零覆盖**:把 `requires_resolution`
    改成恒 False,全部既有 MCP 测试照绿。
    """
    uid = "u_wake_folded_refused"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)
    pressure_config = _force_pressure(monkeypatch)

    dispatched = []
    real_dispatch = worker.v2_executor.dispatch_tool_calls

    async def _spy_dispatch(calls, **kwargs):
        dispatched.extend(str(tc.name) for tc in calls)
        return await real_dispatch(calls, **kwargs)

    monkeypatch.setattr(
        worker.v2_executor, "dispatch_tool_calls", _spy_dispatch
    )

    calls = _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [{
                "id": "guess",
                "name": "workspace_read",
                "args": {"path": "notes.md"},
            }],
            "usage": {},
        },
        {"reply": "好,我先查一下工具说明", "tool_calls": [], "usage": {}},
    ])

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", _wake_deps(tail=_TAIL), pressure_config,
        asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    # 没有落到 executor
    assert "workspace_read" not in dispatched
    # 模型收到的是一句可执行的指路,不是沉默的空结果
    tool_texts = _tool_result_texts(calls[1]["messages"])
    assert tool_texts, "no tool results reached the second round at all"
    assert any(
        "tool schema is not loaded" in text
        and cap_tool_schema.MCP_TOOL_SEARCH_TOOL in text
        for text in tool_texts
    )


# ---------------------------------------------------------------------------
# 3. 折叠的 MCP 工具在唤醒道也能搜回来
# ---------------------------------------------------------------------------

def test_wake_tool_search_injects_full_mcp_schema_before_dispatch(monkeypatch):
    uid = "u_wake_folded_mcp"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)

    dispatched = []
    turn = _FoldedFakeMcpTurn(dispatched, read_only_names={_MCP_SPEC.name})
    calls = _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [{
                "id": "find",
                "name": cap_tool_schema.MCP_TOOL_SEARCH_TOOL,
                "args": {"names": [_MCP_SPEC.name]},
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [
                {"id": "walk", "name": _MCP_SPEC.name, "args": {"where": "市集"}}
            ],
            "usage": {},
        },
        {"reply": "逛完了", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(tail=_TAIL, load_mcp_turn=_make_load_turn_mcp(turn))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    first = {spec.name: spec for spec in calls[0]["tools"]}
    second = {spec.name: spec for spec in calls[1]["tools"]}
    assert first[_MCP_SPEC.name].parameters["properties"] == {}
    assert second[_MCP_SPEC.name] == _MCP_SPEC
    assert dispatched == [(_MCP_SPEC.name, {"where": "市集"})]


def test_wake_refuses_unresolved_mcp_call_before_remote_request(monkeypatch):
    """折叠的 MCP 工具没搜就调 → 拒绝,**远端一个字节都不发**。"""
    uid = "u_wake_unresolved_mcp"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)

    dispatched = []
    turn = _FoldedFakeMcpTurn(dispatched, read_only_names={_MCP_SPEC.name})
    calls = _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [
                {"id": "guess", "name": _MCP_SPEC.name, "args": {"where": "x"}}
            ],
            "usage": {},
        },
        {"reply": "我先查说明书", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(tail=_TAIL, load_mcp_turn=_make_load_turn_mcp(turn))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    assert dispatched == []
    tool_texts = _tool_result_texts(calls[1]["messages"])
    assert tool_texts, "no tool results reached the second round at all"
    assert any("tool schema is not loaded" in text for text in tool_texts)


# ---------------------------------------------------------------------------
# 4. 写操作先落台账 —— 这正是重投安全谓词读的那张表
# ---------------------------------------------------------------------------

def test_wake_mcp_mutation_records_durable_attempt(monkeypatch):
    """唤醒道的 MCP 写在发请求前先落 `v2_mcp_mutation_attempts`。

    这不只是审计:`_SCHEDULED_REQUEUE_SAFETY_SQL` 就是用这张表判断
    「这一轮还是不是 pristine」。没有它,一个已经发过远端写的唤醒 job
    会被整轮重投,远端写做第二遍。
    """
    uid = "u_wake_mcp_mutation"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    # `start_mcp_mutation_attempt` 要求 job 处于 running 且租约有效 —— 生产里由
    # `process_job` 的 mark_running 完成,直接调 `_run_wake` 的测试必须自己做。
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)
    _spy_reply(monkeypatch)

    dispatched = []
    # read_only_names 留空 => 这个工具按写处理(loader 的失败闭合口径)
    turn = _FakeMcpTurn([_MCP_SPEC], dispatched)
    _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [
                {"id": "w1", "name": _MCP_SPEC.name, "args": {"where": "park"}}
            ],
            "usage": {},
        },
        {"reply": "写完了", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(tail=_TAIL, load_mcp_turn=_make_load_turn_mcp(turn))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    assert dispatched == [(_MCP_SPEC.name, {"where": "park"})]
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT outcome FROM v2_mcp_mutation_attempts WHERE job_id=%s",
            (job_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "known"
    assert jobs_store.has_ambiguous_mcp_mutation(job_id=job_id) is False


# ---------------------------------------------------------------------------
# 5. 可观测性:唤醒轮也发 mcp.turn.usage
# ---------------------------------------------------------------------------

def test_wake_emits_mcp_turn_usage_trace_on_wake_lane(monkeypatch):
    """没有这条 trace,「这次心跳到底拿没拿到 MCP」只能靠读代码回答。"""
    uid = "u_wake_mcp_trace"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)

    events = []

    def _emit(user_id, event, **kwargs):
        # 宽签名:同一个 deps 钩子还会收到 provider roundtrip 等别的事件,
        # 窄签名会把它们变成一串被吞掉的 TypeError 警告。
        events.append({
            "user_id": user_id,
            "event": event,
            "status": str(kwargs.get("status") or ""),
            "detail": dict(kwargs.get("detail") or {}),
        })

    dispatched = []
    turn = _FakeMcpTurn(
        [_MCP_SPEC], dispatched, read_only_names={_MCP_SPEC.name}
    )
    _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [
                {"id": "w1", "name": _MCP_SPEC.name, "args": {"where": "park"}}
            ],
            "usage": {},
        },
        {"reply": "逛完了", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(
        tail=_TAIL,
        load_mcp_turn=_make_load_turn_mcp(turn),
        emit_debug_trace=_emit,
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    usage = [e for e in events if e["event"] == "mcp.turn.usage"]
    assert len(usage) == 1
    assert usage[0]["status"] == "ok"
    # lane 必须是唤醒道本身,不能像 chat 那样写死
    assert usage[0]["detail"]["lane"] == "heartbeat"
    assert usage[0]["detail"]["outcome"] == "completed"
    assert usage[0]["detail"]["offered_tool_count"] == 1
    assert usage[0]["detail"]["call_count"] == 1


def test_wake_without_mcp_servers_emits_no_usage_trace(monkeypatch):
    """零 MCP 服务器的用户不该多出一条空 trace(也证明不是无条件发的)。"""
    uid = "u_wake_no_mcp_trace"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)

    events = []

    def _emit(user_id, event, **_kwargs):
        events.append(event)

    _script_provider(monkeypatch, [
        {"reply": "在的", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(tail=_TAIL, emit_debug_trace=_emit)

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    assert "mcp.turn.usage" not in events


def test_wake_mcp_instructions_reach_the_system_context_once(monkeypatch):
    """服务器自写的使用说明要进唤醒轮的系统提示,且一轮只进一次。

    这是 chat 早有、唤醒此前完全没有的一段:MCP spec 的
    `initialize.result.instructions`。没有它,模型拿到一堆工具却不知道这台
    服务器希望它怎么用 —— 而唤醒轮没有用户在旁边纠正。
    """
    uid = "u_wake_mcp_instructions"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _spy_reply(monkeypatch)

    turn = _FakeMcpTurn(
        [_MCP_SPEC], [],
        read_only_names={_MCP_SPEC.name},
        instructions=[("town", "开门时间是早上八点,别在半夜敲门。")],
    )
    calls = _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [
                {"id": "w1", "name": _MCP_SPEC.name, "args": {"where": "park"}}
            ],
            "usage": {},
        },
        {"reply": "逛完了", "tool_calls": [], "usage": {}},
    ])
    deps = _wake_deps(tail=_TAIL, load_mcp_turn=_make_load_turn_mcp(turn))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    assert len(calls) == 2, "expected two provider rounds"
    for index, call in enumerate(calls):
        systems = [
            str(message.get("content") or "")
            for message in call["messages"]
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        assert systems, f"round {index} had no system message at all"
        joined = "\n".join(systems)
        # 每一轮都该带(系统上下文每次重建),但**单个请求内不能翻倍** ——
        # 只看第 0 轮是抓不到「随轮次累积」的。
        assert joined.count("开门时间是早上八点") == 1, (
            f"round {index}: instructions appeared "
            f"{joined.count('开门时间是早上八点')} times"
        )


def test_wake_mcp_mutation_frontier_is_the_user_reply_cursor(monkeypatch):
    """写台账记的 frontier 是**用户输入**边界,不是 all-role 快照。

    早绑 `wake_reply_cursor_seq` 而不是 late-bound `cursor_box["seq"]`:后者由
    all-role 快照初始化,若最新一行是 assistant,会造出一个 reply cursor 永远
    覆盖不到的假 frontier(codex 复核指出)。
    """
    from model_api_runtime.v2 import cursor as v2_cursor

    uid = "u_wake_mcp_frontier"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)
    _spy_reply(monkeypatch)

    # 复现原 bug 的形状,而不是随便造一个非零值:
    # 一条已被回答的 user 行(reply cursor 停在这里)+ 之后一条 assistant 行。
    # all-role 快照因此**大于** reply cursor —— 旧实现记 assistant 快照,
    # 新实现记 user frontier,两者可区分。
    # (我第一版把 cursor 人工设成 41 而 chat_max_seq 仍是 0,那既不是生产
    #  不变量,也复现不了这个 bug。)
    store = core_store.get_store(uid)
    now = 1_700_000_000.0
    db.chat_append(uid, "m_user_answered", now, {
        "id": "m_user_answered", "role": "user", "source": "chat",
    }, 5000)
    expected_seq = int(db.chat_seq_for_msg_id(uid, "m_user_answered"))
    assert expected_seq > 0, "precondition: answered user row must have a seq"
    db.chat_append(uid, "m_assistant_after", now + 1, {
        "id": "m_assistant_after", "role": "openclaw", "source": "model_api",
    }, 5000)
    assert int(db.chat_max_seq(uid)) > expected_seq, (
        "precondition: all-role snapshot must exceed the reply cursor, "
        "otherwise the two implementations are indistinguishable"
    )
    profile = db.get_blob_strict(uid, "model_api_runtime") or {}
    profile[v2_cursor.CURSOR_KEY] = expected_seq
    db.set_blob(uid, "model_api_runtime", profile)
    assert int(v2_cursor.load_seq(store)) == expected_seq

    turn = _FakeMcpTurn([_MCP_SPEC], [])
    _script_provider(monkeypatch, [
        {
            "reply": "",
            "tool_calls": [
                {"id": "w1", "name": _MCP_SPEC.name, "args": {"where": "park"}}
            ],
            "usage": {},
        },
        {"reply": "写完了", "tool_calls": [], "usage": {}},
    ])
    # seq-native 的回复走真信封路径(不再经过 _write_encrypted_reply 打桩),
    # 所以要把信封构造也换成桩,否则回合会以「公钥未接线」失败。
    def _fake_envelope(_store, _text, *, item_id=None):
        return (
            {
                "v": 1,
                "id": str(item_id),
                "owner_user_id": "ignored-by-store",
                "visibility": "shared",
                "body_ct": "ciphertext",
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        )

    monkeypatch.setattr(
        worker.core_envelope, "_build_shared_envelope_for_store", _fake_envelope
    )
    deps = _wake_deps(tail=_TAIL, load_mcp_turn=_make_load_turn_mcp(turn))
    # seq-native 才有 frontier 语义;legacy 路径记 0(下面单独钉)。
    deps.read_messages_after_seq = lambda _uid, _after_seq: []
    # seq-native 的 reply effect 要求生产那条事务性 sink,测试里的简易
    # dispatch 到不了(`transactional reply sink is not wired`)。
    deps.apply_pending_effects = serve_worker._apply_pending_effects_for_user

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(2), claimed_by,
    ))

    assert status == "completed"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT input_frontier_seq FROM v2_mcp_mutation_attempts "
            "WHERE job_id=%s",
            (job_id,),
        ).fetchall()
    assert len(rows) == 1
    assert int(rows[0][0]) == expected_seq


def test_production_mcp_surface_trace_reports_the_real_wake_lane(monkeypatch):
    """生产装配层的 `mcp.surface.resolved` 必须报真实 lane。

    这一条钉的是 `serve_worker._load_mcp_turn_observed`,不是 worker —— T154
    之前它把 `detail.lane` 写死成 `"chat"`。唤醒复用同一个 deps,所以四条 wake
    lane 的工具面加载会在 admin 里全部伪装成聊天轮,和同一轮的
    `mcp.turn.usage`(带真实 lane)互相矛盾:排查的人会看到两个互斥的事实。
    """
    from model_api_runtime.v2 import serve_worker

    uid = "u_wake_surface_lane"
    conftest.seed_user(uid)
    _reset(uid)
    store = core_store.get_store(uid)

    turn = _FakeMcpTurn([_MCP_SPEC], [], read_only_names={_MCP_SPEC.name})
    turn.summary = {"expected": 1, "resolved": 1, "kept": 1, "offered": 1}

    async def _fake_load(_store, **_kwargs):
        assert "lane" not in _kwargs, "lane 是纯观测参数,不该下传给 loader"
        return turn

    monkeypatch.setattr(serve_worker.mcp_tools, "load_turn_mcp", _fake_load)
    monkeypatch.setattr(
        serve_worker, "_mcp_catalog_fingerprint_if_new", lambda _s: ""
    )
    emitted = []
    monkeypatch.setattr(
        serve_worker,
        "_emit_v2_debug_trace",
        lambda _store, event, **kwargs: emitted.append(
            (event, dict(kwargs.get("detail") or {}))
        ),
    )
    monkeypatch.setattr(
        serve_worker.mcp_status, "record_runtime_results", lambda *a, **k: None
    )

    asyncio.run(serve_worker._load_mcp_turn_observed(store, lane="heartbeat"))
    resolved = [d for event, d in emitted if event == "mcp.surface.resolved"]
    assert len(resolved) == 1
    assert resolved[0]["lane"] == "heartbeat"

    emitted.clear()
    asyncio.run(serve_worker._load_mcp_turn_observed(store))
    resolved = [d for event, d in emitted if event == "mcp.surface.resolved"]
    assert len(resolved) == 1
    assert resolved[0]["lane"] == "chat", "未指定时保留旧默认,不改既有调用点"
