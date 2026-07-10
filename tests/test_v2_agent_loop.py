"""Pure agent-loop tests: fake `decide`/`run_tools`, no DB, no provider, no store."""
import pytest

from model_api_runtime.v2 import agent_loop


def _ok(action_type: str, data):
    return {"action_results": {action_type: [{"ok": True, "data": data}]},
            "action_digest": {action_type: {"ok": True, "count": 1}}}


@pytest.mark.asyncio
async def test_single_round_when_planner_wants_reply_immediately():
    calls = []

    async def decide(round_idx, prior):
        calls.append((round_idx, dict(prior)))
        return agent_loop.Decision(actions=[{"type": "memory_fetch", "payload": {"ids": ["a"]}}],
                                   wants_reply=True)

    async def run_tools(actions):
        return _ok("memory_fetch", {"body": "card"})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.stop_reason == "wants_reply"
    assert res.rounds == 1
    # wants_reply does NOT skip this round's tools — the plan was [fetch, final_response].
    assert res.action_results["memory_fetch"][0]["data"] == {"body": "card"}
    assert res.action_digest["memory_fetch"]["count"] == 1
    assert calls[0][1] == {}  # first round sees no prior results


@pytest.mark.asyncio
async def test_second_round_sees_first_round_results_and_accumulates():
    seen = []

    async def decide(round_idx, prior):
        seen.append(dict(prior))
        if round_idx == 0:
            return agent_loop.Decision(actions=[{"type": "memory_index", "payload": {}}])
        return agent_loop.Decision(actions=[{"type": "memory_fetch", "payload": {"ids": ["a"]}}],
                                   wants_reply=True)

    async def run_tools(actions):
        t = actions[0]["type"]
        return _ok(t, {"t": t})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.rounds == 2
    assert res.stop_reason == "wants_reply"
    assert seen[1]["memory_index"][0]["data"] == {"t": "memory_index"}   # observation fed back
    assert set(res.action_results) == {"memory_index", "memory_fetch"}   # accumulated, not replaced


@pytest.mark.asyncio
async def test_hits_max_rounds_and_stops_without_final_text():
    rounds = []

    async def decide(round_idx, prior):
        rounds.append(round_idx)
        return agent_loop.Decision(actions=[{"type": "web_search", "payload": {"q": str(round_idx)}}])

    async def run_tools(actions):
        return _ok("web_search", {"hit": actions[0]["payload"]["q"]})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools, max_rounds=3)
    assert rounds == [0, 1, 2]
    assert res.rounds == 3
    assert res.stop_reason == "max_rounds"
    assert res.final_text is None   # caller must force a responder call — never a filler bubble


@pytest.mark.asyncio
async def test_identical_plan_twice_stops_as_no_progress():
    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[{"type": "memory_index", "payload": {"k": 1}}])

    async def run_tools(actions):
        return _ok("memory_index", {"items": []})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools, max_rounds=5)
    assert res.stop_reason == "no_progress"
    assert res.rounds == 2   # round 0 ran; round 1 repeated the same signature and stopped


@pytest.mark.asyncio
async def test_all_actions_failing_stops_as_no_progress():
    """A planner that keeps asking for a tool that keeps erroring must not burn the BYOK key."""
    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[{"type": "web_fetch", "payload": {"url": str(round_idx)}}])

    async def run_tools(actions):
        return {"action_results": {"web_fetch": [{"ok": False, "error": "boom"}]},
                "action_digest": {"web_fetch": {"ok": False, "count": 1}}}

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools, max_rounds=5)
    assert res.stop_reason == "no_progress"
    assert res.rounds == 1
    assert res.action_results["web_fetch"][0]["ok"] is False   # failures still visible to responder


@pytest.mark.asyncio
async def test_empty_plan_stops_immediately_and_never_calls_run_tools():
    ran = False

    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[])

    async def run_tools(actions):
        nonlocal ran
        ran = True
        return _ok("x", {})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.stop_reason == "no_actions"
    assert res.rounds == 1
    assert res.action_results == {}
    assert ran is False


@pytest.mark.asyncio
async def test_final_text_from_decide_is_returned_verbatim():
    """The native-tools seam: a backend that authors the reply while stopping."""
    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[], wants_reply=True, final_text="hi there")

    async def run_tools(actions):
        raise AssertionError("must not run tools for an empty plan")

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.final_text == "hi there"
    assert res.stop_reason == "wants_reply"


def test_agent_loop_is_pure():
    """Dependency direction: the loop must not reach for DB, provider, or hosted."""
    import pathlib
    src = pathlib.Path(agent_loop.__file__).read_text()
    for forbidden in ("provider_client", "jobs_store", "import hosted", "from hosted",
                      "agent_runtime", "core.store", "psycopg"):
        assert forbidden not in src, f"agent_loop.py must not reference {forbidden}"
