from __future__ import annotations

import asyncio
import sys
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import tool_schema
from model_api_runtime.v2 import serve_worker, worker
from workspace import sandbox as workspace_sandbox
from workspace.artifacts import ArtifactWorkspace
from workspace.backends import (
    InMemoryWorkspaceBackend,
    WorkspaceConflict,
    WorkspaceInvalidPath,
    WorkspaceReadOnly,
    canonical_path,
)
from workspace.prompt import render_trusted_prefix_blocks
from workspace import service as workspace_service
from workspace import e2b_sandbox
from workspace.sandbox import (
    LazySandbox,
    MemoryTestSandboxProvider,
    SandboxRequiredOperation,
    SandboxUsageUnavailable,
)


def test_path_boundary_rejects_relative_traversal_and_unknown_roots():
    for value in (
        "workspace/a.md",
        "/workspace/../memory/WORKING.md",
        "/workspace//a.md",
        "/workspace/a.md/",
        "/tmp/a",
    ):
        with pytest.raises(WorkspaceInvalidPath):
            canonical_path(value)
    assert canonical_path("/workspace/notes.md") == "/workspace/notes.md"


def test_revision_cas_and_read_only_namespaces():
    backend = InMemoryWorkspaceBackend()
    created = backend.write(
        "/workspace/plan.md", "one", expected_revision=0,
    )
    assert created.revision == 1
    with pytest.raises(WorkspaceConflict):
        backend.write("/workspace/plan.md", "lost", expected_revision=0)
    updated = backend.write(
        "/workspace/plan.md", "two", expected_revision=created.revision,
    )
    assert updated.revision == 2
    assert backend.read("/workspace/plan.md").content == "two"

    backend.put_read_only(
        "/artifacts/a.txt", "artifact", kind="artifact", expected_revision=0,
    )
    with pytest.raises(WorkspaceReadOnly):
        backend.write("/artifacts/a.txt", "tamper", expected_revision=1)
    with pytest.raises(WorkspaceReadOnly):
        backend.delete("/artifacts/a.txt", expected_revision=1)


def test_prompt_prefix_is_deterministic_and_excludes_dynamic_workspace():
    backend = InMemoryWorkspaceBackend()
    backend.put_read_only(
        "/skills/z.md", "Z", kind="skill", expected_revision=0,
    )
    backend.put_read_only(
        "/skills/a.md", "A", kind="skill", expected_revision=0,
    )
    backend.write("/workspace/dynamic.md", "DO NOT CACHE", expected_revision=0)

    first = render_trusted_prefix_blocks(backend)
    second = render_trusted_prefix_blocks(backend)
    assert first == second
    assert [block.name for block in first] == [
        "skill:/skills/a.md", "skill:/skills/z.md", "working-memory",
    ]
    assert "DO NOT CACHE" not in "\n".join(block.content for block in first)

    memory = backend.read("/memory/WORKING.md")
    backend.write(
        "/memory/WORKING.md", "# Updated", expected_revision=memory.revision,
    )
    third = render_trusted_prefix_blocks(backend)
    assert third[:-1] == first[:-1]
    assert third[-1].cache_key != first[-1].cache_key


def test_prompt_prefix_fails_instead_of_silently_truncating_skills():
    class _SentinelBackend:
        def list(self, *_args, **_kwargs):
            return [
                SimpleNamespace(path=f"/skills/{index:03d}.md")
                for index in range(500)
            ]

        def read(self, _path):  # pragma: no cover - limit fails first
            raise AssertionError("truncated skill set must not be rendered")

    with pytest.raises(RuntimeError, match="skill prompt limit"):
        render_trusted_prefix_blocks(_SentinelBackend())


def test_production_workspace_prompt_loader_preserves_trust_partition(
    monkeypatch,
):
    backend = InMemoryWorkspaceBackend()
    backend.put_read_only(
        "/skills/runtime.md",
        "Always preserve the user-visible error contract.",
        kind="skill",
        expected_revision=0,
    )
    backend.write(
        "/memory/WORKING.md",
        "Editable scratch state",
        expected_revision=0,
    )
    monkeypatch.setattr(
        serve_worker,
        "production_workspace_backend",
        lambda *_args, **_kwargs: backend,
    )

    rendered = serve_worker._load_workspace_prompt(
        SimpleNamespace(),
        runtime_token="runtime-token",
    )

    assert len(rendered["trusted_system_blocks"]) == 1
    assert "<feedling-skill" in rendered["trusted_system_blocks"][0]
    assert "Always preserve" in rendered["trusted_system_blocks"][0]
    assert rendered["working_memory"] == ""
    assert "Editable scratch state" not in str(
        rendered["trusted_system_blocks"]
    )


def test_explicit_working_memory_read_lazily_creates_default():
    backend = InMemoryWorkspaceBackend()

    rendered = workspace_service.read_text(
        backend,
        path="/memory/WORKING.md",
    )

    assert rendered["path"] == "/memory/WORKING.md"
    assert rendered["revision"] == 1
    assert "editable task/agent working state" in rendered["content"]


def test_workspace_prompt_context_loads_once_and_fails_with_stable_code():
    calls = []
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "token",
        load_workspace_prompt=lambda _store, **kwargs: (
            calls.append(kwargs["runtime_token"])
            or {
                "trusted_system_blocks": ("skill",),
                "working_memory": "scratch",
            }
        ),
    )

    loaded = asyncio.run(worker._load_workspace_prompt_context(
        deps,
        SimpleNamespace(),
        runtime_token="token",
        enclave_sem=asyncio.Semaphore(1),
    ))
    assert loaded == (("skill",), "")
    assert calls == ["token"]

    deps.load_workspace_prompt = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("decrypted private value"))
    )
    with pytest.raises(worker.WorkspacePromptUnavailable):
        asyncio.run(worker._load_workspace_prompt_context(
            deps,
            SimpleNamespace(),
            runtime_token="token",
            enclave_sem=asyncio.Semaphore(1),
        ))

    deps.load_workspace_prompt = lambda *_args, **_kwargs: {
        "trusted_system_blocks": ("",),
        "working_memory": "scratch",
    }
    with pytest.raises(worker.WorkspacePromptUnavailable):
        asyncio.run(worker._load_workspace_prompt_context(
            deps,
            SimpleNamespace(),
            runtime_token="token",
            enclave_sem=asyncio.Semaphore(1),
        ))
    assert worker._safe_failure_code(
        "turn_failed",
        worker.WorkspacePromptUnavailable(),
    ) == "turn_failed:workspace_prompt_unavailable"


def test_production_deps_wire_workspace_prompt_loader():
    assert (
        serve_worker.build_production_deps().load_workspace_prompt
        is serve_worker._load_workspace_prompt
    )


def test_sandbox_is_lazy_and_artifact_ingest_never_uses_host_filesystem():
    backend = InMemoryWorkspaceBackend()
    provider = MemoryTestSandboxProvider()
    events = []
    lazy = LazySandbox(provider, user_id="u_workspace", on_acquire=events.append)
    artifacts = ArtifactWorkspace(backend, lazy)

    assert provider.acquire_count == 0
    assert lazy.acquired is False
    entry = artifacts.ingest(
        source_ref="chat-message-1",
        filename="notes.txt",
        mime_type="text/plain",
        data=b"hello artifact",
    )
    assert provider.acquire_count == 1
    assert [(event.provider, event.purpose) for event in events] == [
        ("memory-test", "materialize_artifact"),
    ]
    assert lazy.acquired is True
    assert entry.kind == "artifact"
    assert artifacts.read_text_view(entry.path).content == "hello artifact"
    assert provider.acquire_count == 1  # encrypted VFS text read: no second acquire

    with pytest.raises(Exception):
        lazy.run_shell("echo unsafe")


def test_sandbox_session_is_not_published_when_usage_event_fails():
    provider = MemoryTestSandboxProvider()
    lazy = LazySandbox(
        provider,
        user_id="u_workspace",
        on_acquire=lambda _event: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )

    with pytest.raises(SandboxUsageUnavailable, match="usage ledger unavailable"):
        lazy.ensure(SandboxRequiredOperation.MATERIALIZE_ARTIFACT)
    assert provider.acquire_count == 1
    assert lazy.acquired is False


def test_sandbox_release_reports_bounded_duration_and_usage_identity():
    provider = MemoryTestSandboxProvider()
    now = [10.0]
    releases = []
    lazy = LazySandbox(
        provider,
        user_id="u_workspace",
        on_acquire=lambda _event: 77,
        on_release=releases.append,
        clock=lambda: now[0],
    )
    lazy.ensure(SandboxRequiredOperation.MATERIALIZE_ARTIFACT)
    now[0] = 11.25
    lazy.close(outcome="completed")

    assert lazy.acquired is False
    assert len(releases) == 1
    event = releases[0]
    assert event.usage_ref == 77
    assert event.duration_ms == 1250
    assert event.outcome == "completed"


def test_cvm_provider_registration_is_deployment_owned(monkeypatch):
    provider = MemoryTestSandboxProvider()
    provider.name = "cvm"
    factories = dict(workspace_sandbox._PROVIDER_FACTORIES)
    monkeypatch.setattr(workspace_sandbox, "_PROVIDER_FACTORIES", factories)
    workspace_sandbox.register_cvm_sandbox_provider(lambda: provider)
    monkeypatch.setenv("FEEDLING_V2_SANDBOX_PROVIDER", "cvm")

    assert workspace_sandbox.configured_sandbox_provider() is provider


class _FakeE2BFiles:
    def __init__(self):
        self.values = {}

    def write(self, path, value):
        self.values[path] = value

    def read(self, path):
        return self.values[path]


class _FakeE2BCommands:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def run(self, command, *, timeout):
        self.calls.append((command, timeout))
        if command == e2b_sandbox._EXTRACT_COMMAND:
            self.files.values[e2b_sandbox._ARTIFACT_TEXT_PATH] = b"hello from e2b"
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")


class _FakeE2BSandbox:
    create_calls = []

    def __init__(self):
        self.files = _FakeE2BFiles()
        self.commands = _FakeE2BCommands(self.files)
        self.kill_count = 0

    @classmethod
    def create(cls, **kwargs):
        cls.create_calls.append(kwargs)
        sandbox = cls()
        digest = kwargs["template"].rsplit("-", 1)[-1]
        sandbox.files.values[e2b_sandbox._TEMPLATE_VERSION_PATH] = digest.encode()
        return sandbox

    def kill(self):
        self.kill_count += 1


def test_e2b_adapter_is_secure_offline_bounded_and_uses_fixed_extractor(monkeypatch):
    _FakeE2BSandbox.create_calls = []
    monkeypatch.setenv("E2B_API_KEY", "test-e2b-key")
    template = "feedling-runtime-v2-artifacts-v1-" + "0123456789abcdef" * 4
    monkeypatch.setenv("FEEDLING_V2_E2B_TEMPLATE", template)
    monkeypatch.setenv("FEEDLING_V2_E2B_OUTPUT_MAX_CHARS", "5")
    provider = e2b_sandbox.E2BSandboxProvider(sandbox_cls=_FakeE2BSandbox)

    session = provider.acquire(user_id="u_e2b", purpose="materialize_artifact")
    created = _FakeE2BSandbox.create_calls[0]
    assert created == {
        "template": template,
        "timeout": 300,
        "secure": True,
        "allow_internet_access": False,
        "api_key": "test-e2b-key",
    }
    malicious_name = "contract.pdf; touch /tmp/not-allowed"
    path = session.materialize(
        name=malicious_name,
        data=b"artifact bytes",
        mime_type="application/pdf",
    )
    assert path == e2b_sandbox._ARTIFACT_PATH
    assert session.extract_text(path=path, mime_type="application/pdf") == "hello"
    assert session._sandbox.commands.calls == [(e2b_sandbox._EXTRACT_COMMAND, 30)]
    assert malicious_name not in session._sandbox.commands.calls[0][0]
    metadata = session._sandbox.files.values[e2b_sandbox._ARTIFACT_META_PATH]
    assert malicious_name.encode("utf-8") in metadata

    session.run_code(language="python", source="print('model source')")
    assert session._sandbox.commands.calls[-1] == (
        "python /tmp/feedling-code.py", 30,
    )
    assert "model source" not in session._sandbox.commands.calls[-1][0]
    with pytest.raises(workspace_sandbox.SandboxUnavailable, match="unsupported"):
        session.run_code(language="javascript", source="console.log('not installed')")
    raw = session._sandbox
    session.close()
    session.close()
    assert raw.kill_count == 1


def test_e2b_adapter_requires_key_and_template_before_sdk_call(monkeypatch):
    _FakeE2BSandbox.create_calls = []
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("FEEDLING_V2_E2B_TEMPLATE", raising=False)
    provider = e2b_sandbox.E2BSandboxProvider(sandbox_cls=_FakeE2BSandbox)

    with pytest.raises(workspace_sandbox.SandboxUnavailable, match="E2B_API_KEY"):
        provider.acquire(user_id="u_e2b", purpose="shell")
    assert _FakeE2BSandbox.create_calls == []


def test_e2b_adapter_rejects_mutable_or_mismatched_template_identity(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "test-e2b-key")
    provider = e2b_sandbox.E2BSandboxProvider(sandbox_cls=_FakeE2BSandbox)

    monkeypatch.setenv("FEEDLING_V2_E2B_TEMPLATE", "feedling-artifacts-latest")
    with pytest.raises(workspace_sandbox.SandboxUnavailable, match="content-addressed"):
        provider.acquire(user_id="u_e2b", purpose="shell")

    class WrongVersionSandbox(_FakeE2BSandbox):
        @classmethod
        def create(cls, **kwargs):
            sandbox = super().create(**kwargs)
            sandbox.files.values[e2b_sandbox._TEMPLATE_VERSION_PATH] = b"f" * 64
            return sandbox

    monkeypatch.setenv(
        "FEEDLING_V2_E2B_TEMPLATE",
        "feedling-runtime-v2-artifacts-v1-" + "0123456789abcdef" * 4,
    )
    with pytest.raises(workspace_sandbox.SandboxUnavailable, match="identity"):
        provider = e2b_sandbox.E2BSandboxProvider(sandbox_cls=WrongVersionSandbox)
        provider.acquire(user_id="u_e2b", purpose="shell")


def test_production_file_read_fails_closed_before_decrypt_without_provider(monkeypatch):
    backend = InMemoryWorkspaceBackend()
    store = SimpleNamespace(
        user_id="u_file_fail_closed",
        chat_messages=[{
            "id": "m1", "file_name": "contract.pdf", "file_mime": "application/pdf",
        }],
    )
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: store)
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(serve_worker, "production_workspace_backend", lambda *_a, **_k: backend)
    monkeypatch.setattr(
        serve_worker,
        "configured_sandbox_provider",
        serve_worker.DisabledSandboxProvider,
    )
    monkeypatch.setattr(
        serve_worker.cap_registry,
        "run_capability",
        lambda *_a, **_k: pytest.fail("artifact bytes decrypted before sandbox acquisition"),
    )

    got = serve_worker._read_files(store.user_id, ["m1"])
    assert got["m1"]["error"] == "sandbox_unavailable"


def test_production_file_read_fails_before_decrypt_when_usage_ledger_is_down(monkeypatch):
    backend = InMemoryWorkspaceBackend()
    provider = MemoryTestSandboxProvider()
    store = SimpleNamespace(
        user_id="u_file_unbilled",
        chat_messages=[{
            "id": "m1", "file_name": "notes.txt", "file_mime": "text/plain",
        }],
    )
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: store)
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(serve_worker, "production_workspace_backend", lambda *_a, **_k: backend)
    monkeypatch.setattr(serve_worker, "configured_sandbox_provider", lambda: provider)
    monkeypatch.setattr(
        serve_worker.jobs_store,
        "record_sandbox_acquisition",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    monkeypatch.setattr(
        serve_worker.cap_registry,
        "run_capability",
        lambda *_a, **_k: pytest.fail("artifact decrypted without billable acquisition"),
    )

    got = serve_worker._read_files(store.user_id, ["m1"])
    assert got["m1"]["error"] == "sandbox_usage_unavailable"
    assert provider.acquire_count == 1


def test_production_file_read_materializes_once_then_uses_encrypted_text_view(monkeypatch):
    backend = InMemoryWorkspaceBackend()
    provider = MemoryTestSandboxProvider()
    store = SimpleNamespace(
        user_id="u_file_sandbox",
        chat_messages=[{
            "id": "m1", "file_name": "notes.txt", "file_mime": "text/plain",
        }],
    )
    calls = []
    billing = []
    finalized = []

    def capability(*_args, **_kwargs):
        calls.append("decrypt")
        return SimpleNamespace(to_dict=lambda: {"data": {
            "file_b64": base64.b64encode(b"sandboxed text").decode("ascii"),
            "file_name": "notes.txt",
            "file_mime": "text/plain",
        }})

    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: store)
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "token")
    monkeypatch.setattr(serve_worker, "production_workspace_backend", lambda *_a, **_k: backend)
    monkeypatch.setattr(serve_worker, "configured_sandbox_provider", lambda: provider)
    monkeypatch.setattr(serve_worker.cap_registry, "run_capability", capability)
    monkeypatch.setattr(
        serve_worker.jobs_store,
        "record_sandbox_acquisition",
        lambda uid, **event: billing.append((uid, event)) or 41,
    )
    monkeypatch.setattr(
        serve_worker.jobs_store,
        "finish_sandbox_acquisition",
        lambda usage_id, uid, **event: finalized.append(
            (usage_id, uid, event)
        ) or True,
    )

    first = serve_worker._read_files(store.user_id, ["m1"])
    second = serve_worker._read_files(store.user_id, ["m1"])
    assert first["m1"]["text"] == "sandboxed text"
    assert second["m1"]["text"] == "sandboxed text"
    assert calls == ["decrypt"]
    assert provider.acquire_count == 1
    assert billing == [(
        store.user_id,
        {"provider": "memory-test", "purpose": "materialize_artifact"},
    )]
    assert len(finalized) == 1
    assert finalized[0][0:2] == (41, store.user_id)
    assert finalized[0][2]["outcome"] == "closed"
    assert finalized[0][2]["duration_ms"] >= 0


def test_workspace_tool_schemas_are_closed_and_revision_fenced():
    assert tool_schema.validate_tool_args(
        "workspace_write",
        {"path": "/workspace/a.md", "content": "x", "expected_revision": 0},
    ) is None
    assert tool_schema.validate_tool_args(
        "workspace_delete",
        {"path": "/workspace/a.md", "expected_revision": -1},
    ) == "expected_revision must be a non-negative integer"
    assert tool_schema.validate_tool_args(
        "workspace_read", {"path": "/workspace/a.md", "host_path": "/etc/passwd"},
    ) == "unknown field: host_path"


def test_workspace_write_maps_to_encrypted_effect_and_validates_at_sink_boundary():
    call = SimpleNamespace(
        name="workspace_write",
        args={"path": "/workspace/a.md", "content": "private", "expected_revision": 0},
    )
    logical, payload = worker._write_tool_effect_payload(call)
    assert logical == "workspace"
    assert payload == {"op": "workspace_write", **call.args}
    assert worker.ENCRYPTED_TOOL_EFFECT_TYPES[logical] == "workspace_encrypted_v1"

    serve_worker._validate_decrypted_tool_effect(
        "workspace", {**payload, "effect_id": "e"},
    )
    with pytest.raises(RuntimeError, match="invalid encrypted workspace operation"):
        serve_worker._validate_decrypted_tool_effect(
            "workspace", {"op": "workspace_shell", "effect_id": "e"},
        )
