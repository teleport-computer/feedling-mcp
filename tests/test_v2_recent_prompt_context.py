from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile_store, worker


def _row(seq: int, role: str, *, genuine: bool) -> dict:
    return {
        "id": f"m{seq}",
        "seq": seq,
        "ts": float(seq),
        "role": role,
        "content": f"content-{seq}",
        "_genuine_user": genuine,
    }


def test_split_recent_window_uses_inclusive_required_seed_boundary():
    window = {
        "rows": [
            _row(10, "user", genuine=True),
            _row(11, "assistant", genuine=False),
            _row(20, "user", genuine=True),
            _row(21, "assistant", genuine=False),
        ],
        "source_truncated": False,
    }

    optional, required, truncated = worker._split_recent_turn_window(
        window,
        required_from_seq=20,
    )

    assert [[row["seq"] for row in turn] for turn in optional] == [[10, 11]]
    assert [row["seq"] for row in required] == [20, 21]
    assert truncated is False


def test_split_recent_window_marks_every_wake_history_turn_optional():
    window = {
        "rows": [
            _row(1, "assistant", genuine=False),
            _row(10, "user", genuine=True),
            _row(11, "assistant", genuine=False),
            _row(20, "user", genuine=True),
        ],
        "source_truncated": False,
    }

    optional, required, truncated = worker._split_recent_turn_window(
        window,
        required_from_seq=None,
    )

    assert [[row["seq"] for row in turn] for turn in optional] == [
        [10, 11],
        [20],
    ]
    assert required == []
    assert truncated is True


def test_read_recent_prompt_context_uses_only_recent_rows_and_profile():
    calls = []

    def read_recent(user_id, max_turns, row_cap, *, through_seq):
        calls.append((user_id, max_turns, row_cap, through_seq))
        return {
            "rows": [
                _row(30, "user", genuine=True),
                _row(31, "assistant", genuine=False),
                _row(40, "user", genuine=True),
            ],
            "source_truncated": False,
        }

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_recent_turns=read_recent,
        select_profile_for_turn=lambda _uid, **_kwargs: (
            profile_store.ProfilePromptSelection(
                memory="memory",
                user="user",
                state="last_good",
                memory_chars=6,
                user_chars=4,
                age_seconds=12.0,
            )
        ),
    )

    selected = asyncio.run(
        worker._read_recent_prompt_context(
            user_id="u",
            deps=deps,
            through_seq=40,
            target_turns=40,
            required_from_seq=40,
            enclave_sem=asyncio.Semaphore(1),
            trajectory_recorder=None,
        )
    )

    assert calls == [("u", 40, worker._RECENT_TURN_ROW_CAP, 40)]
    assert [[row["seq"] for row in turn] for turn in selected.optional_turns] == [
        [30, 31]
    ]
    assert [row["seq"] for row in selected.required_tail] == [40]
    assert selected.agent_memory == "memory"
    assert selected.user_profile == "user"
    assert selected.profile_state == "last_good"
