from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile_store, worker


def _deps(selection):
    return worker.TurnDeps(
        read_messages=lambda *_args: [],
        resolve_provider=lambda *_args: (None, {}),
        mint_enclave_token=lambda *_args: "",
        read_summary_with_seq=lambda _uid: ("- old summary", 0.0, 1, 0),
        read_tail_after_seq=lambda _uid, _after, _limit, **_kwargs: [
            {"id": "m1", "seq": 1, "role": "user", "content": "hello"}
        ],
        select_profile_for_turn=lambda *_args, **_kwargs: selection,
    )


def test_seq_prompt_uses_profile_and_skips_summary_bounding(monkeypatch):
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    monkeypatch.setattr(
        worker,
        "_bound_materialized_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("profile mode must not enter provider-backed summary bound")
        ),
    )

    async def _exact(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker, "_assert_prompt_tail_exact", _exact)
    selection = profile_store.ProfilePromptSelection(
        summary="",
        memory="durable facts",
        user="be direct",
        used_profile=True,
    )

    result = asyncio.run(
        worker._read_seq_adaptive_prompt_context(
            user_id="u-profile-prompt",
            deps=_deps(selection),
            through_seq=1,
            target_turns=40,
            provider_config=object(),
            enclave_sem=asyncio.Semaphore(1),
            claimed_by="worker-a",
            job_id=1,
            add_usage=None,
            trajectory_recorder=None,
        )
    )

    summary, memory, user, notice, tail, *_rest = result
    assert summary == ""
    assert memory == "durable facts"
    assert user == "be direct"
    assert notice == ""
    assert tail[0]["content"] == "hello"


def test_profile_fallback_keeps_summary_path(monkeypatch):
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    bounded = []

    async def _bound(_uid, summary, *_args, **_kwargs):
        bounded.append(summary)
        return summary + "\n- bounded"

    async def _exact(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker, "_bound_materialized_summary", _bound)
    monkeypatch.setattr(worker, "_assert_prompt_tail_exact", _exact)
    selection = profile_store.ProfilePromptSelection(
        summary="- old summary",
        fallback_reason="state:pending",
    )

    result = asyncio.run(
        worker._read_seq_adaptive_prompt_context(
            user_id="u-profile-fallback",
            deps=_deps(selection),
            through_seq=1,
            target_turns=40,
            provider_config=object(),
            enclave_sem=asyncio.Semaphore(1),
            claimed_by="worker-b",
            job_id=2,
            add_usage=None,
            trajectory_recorder=None,
        )
    )

    assert bounded == ["- old summary"]
    assert result[0] == "- old summary\n- bounded"
    assert result[1:4] == ("", "", "")
