import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import context, profile_store, worker


def _deps(selection):
    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_summary_with_seq=lambda _uid: ("- legacy summary", 1.0, 2, 5),
        read_tail_after_seq=lambda _uid, _after, _limit, **_kwargs: [
            {"id": "m6", "seq": 6, "role": "user", "content": "latest"}
        ],
        select_profile_for_turn=lambda _uid, **_kwargs: selection,
    )


def _read(monkeypatch, selection):
    async def _exact(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    monkeypatch.setattr(worker, "_assert_prompt_tail_exact", _exact)
    return asyncio.run(
        worker._read_seq_adaptive_prompt_context(
            user_id="u",
            deps=_deps(selection),
            through_seq=6,
            target_turns=40,
            provider_config=object(),
            enclave_sem=asyncio.Semaphore(1),
            claimed_by=None,
            job_id=None,
            add_usage=None,
            trajectory_recorder=None,
        )
    )


def test_ok_profile_suppresses_summary_and_skips_summary_bounding(monkeypatch):
    selection = profile_store.ProfilePromptSelection(
        memory="relationship facts",
        user="interaction style",
        state="ok",
    )
    monkeypatch.setattr(
        worker,
        "_bound_materialized_summary",
        lambda *_args, **_kwargs: pytest.fail(
            "ok profile must bypass provider-backed summary bounding"
        ),
    )

    (
        summary,
        tail,
        _optional,
        _truncated,
        _watermark,
        memory,
        user_profile,
        coverage_notice,
    ) = _read(monkeypatch, selection)

    assert summary == ""
    assert memory == "relationship facts"
    assert user_profile == "interaction style"
    assert tail[0]["content"] == "latest"
    assert coverage_notice == ""


@pytest.mark.parametrize("state", ["empty", "unavailable"])
def test_nonusable_or_disabled_profile_keeps_summary_path_temporarily(
    monkeypatch, state
):
    selection = profile_store.ProfilePromptSelection(
        state=state,
    )
    calls = []

    async def _bound(_uid, summary, *_args, **_kwargs):
        calls.append(summary)
        return summary + "\n- bounded"

    monkeypatch.setattr(worker, "_bound_materialized_summary", _bound)
    result = _read(monkeypatch, selection)

    assert result[0] == "- legacy summary\n- bounded"
    assert result[5:7] == ("", "")
    assert calls == ["- legacy summary"]


def test_profile_flag_off_is_forwarded_without_suppressing_summary(monkeypatch):
    seen = {}

    def _select(_uid, *, enabled):
        seen["enabled"] = enabled
        return profile_store.ProfilePromptSelection(state="empty")

    deps = _deps(profile_store.ProfilePromptSelection(state="empty"))
    deps.select_profile_for_turn = _select

    async def _exact(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker, "_PROFILE_ENABLED", False)
    monkeypatch.setattr(worker, "_assert_prompt_tail_exact", _exact)
    result = asyncio.run(
        worker._read_seq_adaptive_prompt_context(
            user_id="u",
            deps=deps,
            through_seq=6,
            target_turns=40,
            provider_config=object(),
            enclave_sem=asyncio.Semaphore(1),
            claimed_by=None,
            job_id=None,
            add_usage=None,
            trajectory_recorder=None,
        )
    )

    assert seen["enabled"] is False
    assert result[0] == "- legacy summary"
    assert result[5:7] == ("", "")


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
    assert selection.user == ""
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
        "summary": "",
        "agent_memory": "relationship facts",
        "user_profile": "interaction style",
    }
    first = worker._make_build_messages_fn(
        **common,
        tail=[{"role": "user", "content": "turn one"}],
        coverage_hole_notice="3 earlier messages omitted",
    )([])
    second = worker._make_build_messages_fn(
        **common,
        tail=[
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": "reply one"},
            {"role": "user", "content": "turn two"},
        ],
        coverage_hole_notice="9 earlier messages omitted",
    )([])

    def _profile_prefix(messages):
        index = next(
            i
            for i, message in enumerate(messages)
            if str(message.get("content") or "").startswith(
                context.AGENT_MEMORY_HEADER
            )
        )
        return json.dumps(
            messages[: index + 1],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    assert _profile_prefix(first) == _profile_prefix(second)
