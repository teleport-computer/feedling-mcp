import asyncio
import json
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from capabilities import tool_schema  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402


def test_memory_organize_catalog_is_closed_and_explicit_only():
    by_name = {spec.name: spec for spec in tool_schema.build_tool_specs()}
    spec = by_name[tool_schema.MEMORY_ORGANIZE_TOOL]

    assert spec.parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert "only when the user explicitly asks" in spec.description
    assert tool_schema.validate_tool_args(spec.name, {}) is None
    assert tool_schema.validate_tool_args(spec.name, {"force": True}) == "unknown field: force"


def test_memory_organize_disabled_returns_explainable_state(monkeypatch):
    monkeypatch.setattr(worker, "_DREAM_TOOL_ENABLED", False)
    monkeypatch.setattr(
        worker.jobs_store,
        "enqueue_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )
    tc = SimpleNamespace(id="organize-disabled")

    result = asyncio.run(worker._dispatch_memory_organize_tool("u1", tc))

    assert json.loads(result.content) == {
        "status": "disabled",
        "message": "整理功能暂时关闭",
    }


def test_memory_organize_force_enqueues_and_reports_coalescing(monkeypatch):
    monkeypatch.setattr(worker, "_DREAM_TOOL_ENABLED", True)
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: None)
    calls = []
    notifications = []

    def _enqueue(user_id, lane, **kwargs):
        calls.append((user_id, lane, kwargs))
        return 42, True

    monkeypatch.setattr(worker.jobs_store, "enqueue_job", _enqueue)
    monkeypatch.setattr(
        worker.core_wake_bus,
        "notify",
        lambda *args: notifications.append(args),
    )
    tc = SimpleNamespace(id="organize-coalesced")

    result = asyncio.run(worker._dispatch_memory_organize_tool("u2", tc))

    assert calls == [("u2", "dream", {"reason": "user_requested"})]
    assert notifications == []
    assert json.loads(result.content)["status"] == "coalesced"


def test_memory_organize_new_job_notifies_worker(monkeypatch):
    monkeypatch.setattr(worker, "_DREAM_TOOL_ENABLED", True)
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: {"dream_enabled": True})
    monkeypatch.setattr(worker.jobs_store, "enqueue_job", lambda *_args, **_kwargs: (43, False))
    notifications = []
    monkeypatch.setattr(worker.core_wake_bus, "notify", lambda *args: notifications.append(args))

    result = asyncio.run(
        worker._dispatch_memory_organize_tool("u3", SimpleNamespace(id="organize-new"))
    )

    assert notifications == [("v2_jobs", "u3")]
    assert json.loads(result.content)["status"] == "queued"


def test_memory_organize_enqueue_failure_is_model_visible(monkeypatch):
    monkeypatch.setattr(worker, "_DREAM_TOOL_ENABLED", True)
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: None)
    monkeypatch.setattr(
        worker.jobs_store,
        "enqueue_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db detail")),
    )

    result = asyncio.run(
        worker._dispatch_memory_organize_tool("u4", SimpleNamespace(id="organize-error"))
    )

    assert json.loads(result.content) == {
        "status": "error",
        "message": "记忆整理暂时无法开始",
    }
