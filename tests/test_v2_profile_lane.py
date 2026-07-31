from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker


class _Metrics:
    def __init__(self):
        self.usage = []
        self.flushes = []

    def add_call(self, usage):
        self.usage.append(usage)

    def flush(self, *, failed, status):
        self.flushes.append((failed, status))


def _deps(read_profile_cards):
    return worker.TurnDeps(
        read_messages=lambda *_args: [],
        resolve_provider=lambda *_args: (None, {}),
        mint_enclave_token=lambda *_args: "",
        read_profile_cards=read_profile_cards,
    )


def _wire_storage(monkeypatch):
    written = []
    completed = []
    failed = []
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: None)

    def _build(
        _uid,
        *,
        state,
        source,
        last_attempt,
        memory_text=None,
        user_text=None,
        previous=None,
        disabled=False,
    ):
        document = {
            "v": 1,
            "state": state,
            "source": dict(source),
            "last_attempt": dict(last_attempt),
            "disabled": disabled,
        }
        if memory_text is not None:
            document["memory"] = {
                "envelope": {"body_ct": "memory"},
                "chars": len(memory_text),
            }
            document["user"] = {
                "envelope": {"body_ct": "user"},
                "chars": len(user_text),
            }
            document["_plaintext"] = (memory_text, user_text)
        elif previous and previous.get("memory"):
            document["memory"] = previous["memory"]
            document["user"] = previous["user"]
        return document

    def _update(_uid, recompute):
        candidate = recompute({})
        written.append(candidate)
        return types.SimpleNamespace(status="written", document=candidate)

    monkeypatch.setattr(
        worker.v2_profile_store,
        "build_profile_document",
        _build,
    )
    monkeypatch.setattr(
        worker.v2_profile_store,
        "update_profile_cas",
        _update,
    )
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_completed",
        lambda job_id, *, claimed_by: completed.append((job_id, claimed_by)) or True,
    )
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_failed",
        lambda job_id, code, *, claimed_by, **_kwargs: (
            failed.append((job_id, code, claimed_by)) or True
        ),
    )
    return written, completed, failed


def test_profile_lane_empty_garden_completes_without_provider_call(monkeypatch):
    written, completed, failed = _wire_storage(monkeypatch)

    async def _must_not_call(*_args, **_kwargs):
        raise AssertionError("empty Garden must use zero provider calls")

    monkeypatch.setattr(worker.provider_client, "chat_completion_async", _must_not_call)
    deps = _deps(
        lambda _uid: {
            "rendered": "",
            "eligible_card_count": 0,
            "card_count": 0,
            "max_updated_at": "",
        }
    )
    tm = _Metrics()

    outcome = asyncio.run(
        worker._run_profile(
            10,
            "u-empty",
            deps,
            object(),
            claimed_by="worker-a",
            tm=tm,
        )
    )

    assert outcome == "completed"
    assert written[0]["state"] == "empty"
    assert completed == [(10, "worker-a")]
    assert failed == []
    assert tm.usage == []
    assert tm.flushes == [(False, "ok")]


def test_profile_lane_generates_and_persists_both_fields(monkeypatch):
    written, completed, failed = _wire_storage(monkeypatch)

    async def _provider(*_args, **_kwargs):
        return {
            "reply": '{"memory":"长期事实","user":"相处方式"}',
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    monkeypatch.setattr(worker.provider_client, "chat_completion_async", _provider)
    deps = _deps(
        lambda _uid: {
            "rendered": "[card m1]\ncontent: 用户喜欢直接反馈",
            "eligible_card_count": 1,
            "card_count": 1,
            "max_updated_at": "2026-07-31T00:00:00Z",
        }
    )
    tm = _Metrics()

    outcome = asyncio.run(
        worker._run_profile(
            11,
            "u-ok",
            deps,
            object(),
            claimed_by="worker-b",
            tm=tm,
        )
    )

    assert outcome == "completed"
    assert written[0]["state"] == "ok"
    assert written[0]["_plaintext"] == ("长期事实", "相处方式")
    assert completed == [(11, "worker-b")]
    assert failed == []
    assert tm.usage == [{"prompt_tokens": 10, "completion_tokens": 5}]


def test_profile_provider_failure_is_background_only_and_arms_backoff(monkeypatch):
    written, completed, failed = _wire_storage(monkeypatch)

    async def _provider(*_args, **_kwargs):
        raise RuntimeError("secret upstream detail")

    monkeypatch.setattr(worker.provider_client, "chat_completion_async", _provider)
    deps = _deps(
        lambda _uid: {
            "rendered": "[card m1]\ncontent: fact",
            "eligible_card_count": 1,
            "card_count": 1,
            "max_updated_at": "2026-07-31T00:00:00Z",
        }
    )
    tm = _Metrics()

    outcome = asyncio.run(
        worker._run_profile(
            12,
            "u-fail",
            deps,
            object(),
            claimed_by="worker-c",
            tm=tm,
        )
    )

    assert outcome == "failed"
    assert completed == []
    assert len(failed) == 1
    assert failed[0][0] == 12
    assert "secret upstream detail" not in failed[0][1]
    assert written[0]["state"] == "pending"
    assert written[0]["last_attempt"]["attempts"] == 1
    assert written[0]["last_attempt"]["retry_not_before"] > 0
    assert tm.flushes[-1][0] is True
