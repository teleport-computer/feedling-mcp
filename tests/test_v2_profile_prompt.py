import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import context, profile_store, worker


def _deps(selection):
    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        select_profile_for_turn=lambda _uid, **_kwargs: selection,
    )



def test_selection_failure_records_only_content_free_unavailable_state(monkeypatch):
    events = []

    class Recorder:
        async def record_best_effort(self, kind, payload):
            events.append((kind, payload))
            return True

    deps = _deps(profile_store.ProfilePromptSelection(state="empty"))
    deps.select_profile_for_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("PRIVATE provider or storage detail")
    )
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)

    selection = asyncio.run(
        worker._select_profile_prompt_for_turn(
            user_id="u",
            deps=deps,
            trajectory_recorder=Recorder(),
        )
    )

    assert selection.state == "unavailable"
    assert selection.memory == ""
    assert selection.style == ""
    assert events == [
        (
            "profile_prompt_state",
            {"state": "unavailable"},
        )
    ]
    assert "PRIVATE" not in json.dumps(events)


def test_same_profile_two_turn_prompt_prefix_bytes_are_identical():
    common = {
        "system_prompt": "stable system",
        "agent_memory": "relationship facts",
        "user_profile": "interaction style",
    }
    first = worker._make_build_messages_fn(
        **common,
        tail=[{"role": "user", "content": "turn one"}],
    )([])
    second = worker._make_build_messages_fn(
        **common,
        tail=[
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": "reply one"},
            {"role": "user", "content": "turn two"},
        ],
    )([])

    def _profile_prefix(messages):
        assert context.AGENT_MEMORY_HEADER in messages[0]["content"]
        assert context.USER_PROFILE_HEADER in messages[0]["content"]
        return json.dumps(
            messages[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    assert _profile_prefix(first) == _profile_prefix(second)
