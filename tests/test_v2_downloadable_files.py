"""Runtime V2 downloadable-file security and delivery contracts."""
from __future__ import annotations

import asyncio
import base64
import io
import inspect
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client
import conftest
import db
from provider_types import ToolResult
from core import store as core_store
from hosted import file_text
from model_api_runtime.v2 import document_render
from model_api_runtime.v2 import effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import tool_loop
from model_api_runtime.v2 import worker
from model_api_runtime.v2 import context as v2_context
from capabilities import tool_schema


_PROVIDER = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-test",
    api_key="test-key",
)


def test_file_delivery_policy_is_semantic_and_hides_internal_paths():
    prompt = v2_context.CHAT_SYSTEM_PROMPT
    description = tool_schema.DESCRIPTIONS[tool_schema.FILE_REPLY_TOOL]

    assert "semantically, not by matching specific words" in prompt
    assert "save, open, download, share, or use outside the chat" in prompt
    assert "never ask the user for an internal workspace path" in prompt
    assert "not from exact keywords, wording, language" in description
    assert "user never needs to know /workspace" in description
    assert "Do not call this merely because" in description
    assert "Word means .docx and PDF means .pdf" in prompt
    assert "Never substitute Markdown" in prompt
    assert "real Word/PDF bytes" in description


def _run_loop(monkeypatch, responses, *, on_file_reply=None, dispatch=None):
    calls = []
    replies = []
    response_iter = iter(responses)

    async def fake_chat(config, messages, *, tools=None, **kwargs):
        calls.append(tuple(spec.name for spec in (tools or ())))
        return next(response_iter)

    async def on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def dispatch_tools(tool_calls):
        if dispatch is not None:
            return await dispatch(tool_calls)
        return [ToolResult(call_id=tc.id, content="ok") for tc in tool_calls]

    async def no_fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)
    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_PROVIDER,
            build_messages=lambda transcript: [{"role": "user", "content": "turn"}],
            dispatch_tools=dispatch_tools,
            on_reply=on_reply,
            on_file_reply=on_file_reply,
            fold_new_messages=no_fold,
            add_usage=lambda usage: None,
            max_calls=3,
        )
    )
    return outcome, calls, replies


def test_file_capable_chat_uses_v2_output_budget_without_changing_global_default(
    monkeypatch,
):
    default_max = inspect.signature(
        provider_client.chat_completion_async
    ).parameters["max_tokens"].default
    seen_kwargs = []

    async def fake_chat(config, messages, *, tools=None, **kwargs):
        seen_kwargs.append(dict(kwargs))
        return {"reply": "ok", "tool_calls": [], "usage": {}}

    async def on_reply(text, *, final, reasoning=""):
        return None

    async def on_file(path, revision):
        return None

    async def dispatch_tools(tool_calls):
        return []

    async def no_fold():
        return []

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)
    asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_PROVIDER,
            build_messages=lambda transcript: [
                {"role": "user", "content": "turn"}
            ],
            dispatch_tools=dispatch_tools,
            on_reply=on_reply,
            on_file_reply=on_file,
            fold_new_messages=no_fold,
            add_usage=lambda usage: None,
            max_calls=3,
        )
    )

    assert default_max == 700
    assert seen_kwargs == [{"max_tokens": 4096}]

    seen_kwargs.clear()
    asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_PROVIDER,
            build_messages=lambda transcript: [
                {"role": "user", "content": "turn"}
            ],
            dispatch_tools=dispatch_tools,
            on_reply=on_reply,
            fold_new_messages=no_fold,
            add_usage=lambda usage: None,
            max_calls=3,
        )
    )

    assert seen_kwargs == [{}]


def test_send_file_is_chat_only_and_invokes_explicit_callback(monkeypatch):
    files = []

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, calls, replies = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {"path": "/workspace/计划.md", "revision": 2},
                    }
                ],
                "usage": {},
            },
            {"reply": "文件已经发给你了。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
    )

    assert "send_file" in calls[0]
    assert files == [("/workspace/计划.md", 2)]
    assert replies == [("文件已经发给你了。", True)]
    assert outcome.replied_intermediate is True


def test_send_file_is_not_offered_without_delivery_callback(monkeypatch):
    _, calls, _ = _run_loop(
        monkeypatch,
        [{"reply": "plain", "tool_calls": [], "usage": {}}],
    )
    assert "send_file" not in calls[0]


def test_send_file_cannot_share_a_batch_with_workspace_write(monkeypatch):
    dispatched = []
    files = []

    async def dispatch(tool_calls):
        dispatched.extend(tc.name for tc in tool_calls)
        return [ToolResult(call_id=tc.id, content="ok") for tc in tool_calls]

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, _, replies = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "w1",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/a.md",
                            "content": "safe",
                            "expected_revision": 0,
                        },
                    },
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {"path": "/workspace/a.md", "revision": 1},
                    },
                ],
                "usage": {},
            },
            {"reply": "fallback", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
    )

    assert dispatched == []
    assert files == []
    assert replies == [("fallback", True)]
    assert outcome.stop_reason == "final_text"


def test_workspace_file_result_rejects_host_and_non_workspace_paths():
    with pytest.raises(ValueError):
        worker._workspace_file_reply_from_result(
            {"path": "/agent-data/users/other/secret.md", "content": "secret"}
        )
    with pytest.raises(ValueError):
        worker._workspace_file_reply_from_result(
            {"path": "/artifacts/private.md", "content": "secret"}
        )


def test_workspace_file_result_bounds_content_and_sanitizes_name():
    result = worker._workspace_file_reply_from_result(
        {
            "path": "/workspace/quarterly\u202ereport.md",
            "content": "# report\n",
            "mime_type": "text/markdown",
        }
    )
    assert result.name == "quarterlyreport.md"
    assert result.mime_type == "text/markdown"
    assert result.data == b"# report\n"

    with pytest.raises(ValueError):
        worker._workspace_file_reply_from_result(
            {"path": "/workspace/empty.md", "content": ""}
        )
    with pytest.raises(ValueError):
        worker._workspace_file_reply_from_result(
            {
                "path": "/workspace/large.md",
                "content": "x" * (worker._WORKSPACE_FILE_MAX_BYTES + 1),
            }
        )


def test_workspace_file_result_renders_real_word_and_pdf_documents():
    source = (
        "# 三天健身饮食计划\n\n"
        "| 天数 | 训练 | 饮食 |\n"
        "| --- | --- | --- |\n"
        "| 第一天 | 深蹲 3 组 | 高蛋白早餐 |\n\n"
        "- 每天饮水 2 升\n"
    )

    word = worker._workspace_file_reply_from_result(
        {
            "path": "/workspace/三天健身饮食计划.docx",
            "content": source,
            "mime_type": "text/markdown",
        }
    )
    assert word.name == "三天健身饮食计划.docx"
    assert word.mime_type == document_render.DOCX_MIME
    assert word.data.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(word.data)) as archive:
        assert archive.testzip() is None
        assert "word/document.xml" in archive.namelist()
    extracted_word = file_text.extract_file_text(
        word.data, name=word.name, mime=word.mime_type
    )
    assert extracted_word.method == "docx"
    assert "三天健身饮食计划" in (extracted_word.text or "")
    assert "深蹲 3 组" in (extracted_word.text or "")

    pdf = worker._workspace_file_reply_from_result(
        {
            "path": "/workspace/三天健身饮食计划.pdf",
            "content": source,
            "mime_type": "text/markdown",
        }
    )
    assert pdf.name == "三天健身饮食计划.pdf"
    assert pdf.mime_type == document_render.PDF_MIME
    assert pdf.data.startswith(b"%PDF-")
    extracted_pdf = file_text.extract_file_text(
        pdf.data, name=pdf.name, mime=pdf.mime_type
    )
    assert extracted_pdf.method == "pdf"
    assert "三天健身饮食计划" in (extracted_pdf.text or "")
    assert "深蹲 3 组" in (extracted_pdf.text or "")


def test_workspace_loader_binds_path_and_exact_revision(monkeypatch):
    seen = {}

    class Entry:
        path = "/workspace/report.md"
        content = "body"
        mime_type = "text/markdown"
        revision = 3

    class Backend:
        def read(self, path):
            seen["path"] = path
            return Entry()

    store = object()

    def backend(bound_store, *, runtime_token):
        seen["store"] = bound_store
        seen["runtime_token"] = runtime_token
        return Backend()

    monkeypatch.setattr(serve_worker, "production_workspace_backend", backend)
    loaded = serve_worker._load_workspace_file(
        store,
        runtime_token="rt",
        path="/workspace/report.md",
        expected_revision=3,
    )
    assert loaded["content"] == "body"
    assert seen == {
        "store": store,
        "runtime_token": "rt",
        "path": "/workspace/report.md",
    }

    with pytest.raises(ValueError, match="revision conflict"):
        serve_worker._load_workspace_file(
            store,
            runtime_token="rt",
            path="/workspace/report.md",
            expected_revision=2,
        )
    with pytest.raises(ValueError):
        serve_worker._load_workspace_file(
            store,
            runtime_token="rt",
            path="/memory/WORKING.md",
            expected_revision=1,
        )


def test_transactional_reply_sink_preserves_file_metadata(monkeypatch):
    captured = {}

    class Store:
        def _build_chat_message(
            self, role, source, envelope, content_type="text", extra=None
        ):
            captured.update(
                role=role,
                source=source,
                envelope=envelope,
                content_type=content_type,
                extra=extra,
            )
            return {"id": envelope["id"], "ts": 1.0}

    class Connection:
        def execute(self, *args):
            pass

    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda user_id: Store())
    monkeypatch.setattr(
        serve_worker.db,
        "chat_append_effect_with_cursor",
        lambda *args, **kwargs: (7, False, lambda: None),
    )

    payload = {
        "envelope": {"id": "a" * 32},
        "message_extra": {
            "content_type": "file",
            "file_name": "report.md",
            "file_mime": "text/markdown",
            "file_byte_count": 9,
        },
    }
    post_commit = serve_worker._sink_reply_in_transaction(
        "user-1", payload, Connection()
    )
    assert callable(post_commit)
    assert captured["content_type"] == "file"
    assert captured["extra"] == {
        "file_name": "report.md",
        "file_mime": "text/markdown",
        "file_byte_count": 9,
    }


def test_reply_metadata_rejects_unbounded_or_forged_values():
    with pytest.raises(RuntimeError):
        serve_worker._reply_message_fields(
            {
                "message_extra": {
                    "content_type": "file",
                    "file_name": "x.md",
                    "file_mime": "text/markdown",
                    "file_byte_count": worker._WORKSPACE_FILE_MAX_BYTES + 1,
                }
            }
        )


def test_process_job_commits_text_then_workspace_file_as_one_final_reply(
    monkeypatch,
):
    """Exercise the real V2 job/outbox/transactional chat sink boundary."""
    user_id = "u_v2_downloadable_file"
    conftest.seed_user(user_id)
    with db.get_pool().connection() as connection:
        connection.execute("DELETE FROM agent_jobs")
        connection.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (user_id,))
        connection.execute("DELETE FROM chat_messages WHERE user_id=%s", (user_id,))
    conftest.set_v2_runtime_owner(user_id)

    db.chat_append_strict(
        user_id,
        "m1",
        1.0,
        {
            "id": "m1",
            "role": "user",
            "source": "model_api",
            "body_ct": "cipher-m1",
            "nonce": "n",
            "K_user": "k",
            "K_enclave": "e",
        },
        5000,
    )
    input_seq = db.chat_seq_for_msg_id(user_id, "m1")
    assert input_seq is not None

    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    job = jobs_store.claim_next_job("download-worker")
    assert job is not None and job["id"] == job_id

    def build_envelope(store, plaintext, *, item_id=None):
        return (
            {
                "v": 1,
                "id": item_id,
                "owner_user_id": store.user_id,
                "body_ct": base64.b64encode(plaintext).decode("ascii"),
                "nonce": "n",
                "K_user": "k",
            },
            "",
        )

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        build_envelope,
    )

    loaded = []

    def load_file(store, *, runtime_token, path, expected_revision):
        loaded.append((store.user_id, runtime_token, path, expected_revision))
        return {
            "path": path,
            "content": "# final report\n",
            "mime_type": "text/markdown",
            "revision": expected_revision,
        }

    def apply_effects(uid):
        return effect_outbox.apply_pending_effects(
            uid,
            dispatch=(
                lambda effect_type, payload:
                serve_worker._sink_reply(uid, payload)
                if effect_type == "reply"
                else None
            ),
            dispatch_reply_in_transaction=(
                lambda _effect_type, payload, connection:
                serve_worker._sink_reply_in_transaction(uid, payload, connection)
            ),
        )

    async def direct_loop(**kwargs):
        await kwargs["on_file_reply"]("/workspace/report.md", 4)
        with db.get_pool().connection() as connection:
            visible_before_final = connection.execute(
                "SELECT count(*) FROM chat_messages "
                "WHERE user_id=%s AND doc->>'role'='openclaw'",
                (user_id,),
            ).fetchone()[0]
        assert visible_before_final == 0
        await kwargs["on_reply"]("文件已生成。", final=True)
        return tool_loop.LoopOutcome(
            final_text="文件已生成。",
            rounds=2,
            stop_reason="final_text",
            replied_intermediate=True,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    def read_after_seq(_uid, after_seq):
        if after_seq >= input_seq:
            return []
        return [
            {
                "id": "m1",
                "seq": input_seq,
                "ts": 1.0,
                "role": "user",
                "content": "生成报告",
            }
        ]

    deps = worker.TurnDeps(
        read_messages=lambda _uid: read_after_seq(_uid, 0),
        read_messages_after_seq=read_after_seq,
        resolve_provider=lambda _uid: (_PROVIDER, {}),
        mint_enclave_token=lambda _uid: "rt-download",
        apply_pending_effects=apply_effects,
        load_workspace_file=load_file,
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_PROVIDER,
            api_key=None,
            runtime_token="rt-download",
        )
    )
    assert status == "completed"
    assert loaded == [
        (user_id, "rt-download", "/workspace/report.md", 4),
    ]

    store = core_store.get_store(user_id)
    store.reload()
    replies = [
        message
        for message in store.chat_messages
        if message.get("role") == "openclaw"
        and message.get("source") == "model_api"
    ]
    assert [message["content_type"] for message in replies] == ["text", "file"]
    assert replies[0]["ts"] < replies[1]["ts"]
    file_message = replies[1]
    assert file_message["file_name"] == "report.md"
    assert file_message["file_mime"] == "text/markdown"
    assert file_message["file_byte_count"] == len("# final report\n".encode("utf-8"))
    assert worker.v2_cursor.load_seq(store) == input_seq

    apply_effects(user_id)
    store.reload()
    replayed = [
        message
        for message in store.chat_messages
        if message.get("role") == "openclaw"
        and message.get("source") == "model_api"
    ]
    assert len(replayed) == 2
    with pytest.raises(RuntimeError):
        serve_worker._reply_message_fields(
            {
                "message_extra": {
                    "content_type": "file",
                    "file_name": "x.md",
                    "file_mime": "text/markdown",
                    "file_byte_count": 1,
                    "host_path": "/tmp/x",
                }
            }
        )
