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
from agent_protocol_core import self_thinking
import conftest
import db
from provider_types import ProviderMedia, ToolExchange, ToolResult
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
from core.downloadable_reply import sanitize_downloadable_reply


_PROVIDER = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-test",
    api_key="test-key",
)


@pytest.fixture(autouse=True)
def _isolate_self_thinking_correction(monkeypatch):
    """File-delivery tests exercise attachment contracts, not reply format."""
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")


def test_file_delivery_policy_is_semantic_and_hides_internal_paths():
    prompt = v2_context.CHAT_SYSTEM_PROMPT
    description = next(
        spec.description
        for spec in tool_schema.build_tool_specs()
        if spec.name == tool_schema.FILE_REPLY_TOOL
    )

    assert "not from exact keywords, wording, language" in description
    assert "save, open, download, share, or use outside the chat" in description
    assert "绝不要询问对方内部 workspace 路径" in prompt
    assert "user never needs to know /workspace" in description
    assert "Do not call this merely because" in description
    assert "Word means .docx and PDF means .pdf" in description
    assert "绝不要拿 Markdown 顶替" not in prompt
    assert (
        "Even when reformatting an existing file, never substitute Markdown for "
        "another supported format the user explicitly requested."
    ) in description
    assert "memory-grounded summary or deliverable about a specific subject" in next(
        spec.description
        for spec in tool_schema.build_tool_specs()
        if spec.name == "memory_search"
    )
    assert "别拿无关偏好或事件冒充这个问题的答案" in prompt
    assert "real Word/PDF bytes" in description


def test_canvas_policy_is_offline_semantic_and_hides_source():
    prompt = v2_context.CHAT_SYSTEM_PROMPT
    description = tool_schema.DESCRIPTIONS[tool_schema.FILE_REPLY_TOOL]

    assert "你也可以主动选择制作 Canvas" in prompt
    assert ".io.html" in prompt
    assert "离线、自包含" in prompt
    assert "用对方当前回复语言写简洁的 <title>" in prompt
    assert "不要向对方展示内部源码或实现术语" in prompt
    assert "presented by IO as an interactive" in description
    assert "you have decided an experience belongs" in description
    assert "everything the work needs inline so it opens offline" in description
    assert "discuss its source only if they ask" in description


def _run_loop(
    monkeypatch,
    responses,
    *,
    on_file_reply=None,
    dispatch=None,
    required_file_suffixes=None,
    file_requirement_messages=(),
    resolve_required_file_suffixes=None,
    on_file_requirement_changed=None,
    fold_batches=(),
    max_calls=3,
    trajectory_events=None,
    tool_events=None,
    build_messages=None,
    suppress_native_reasoning=False,
):
    calls = []
    replies = []
    messages_seen = []
    response_iter = iter(responses)
    fold_iter = iter(fold_batches)

    async def fake_chat(config, messages, *, tools=None, **kwargs):
        calls.append(tuple(spec.name for spec in (tools or ())))
        messages_seen.append(messages)
        response = next(response_iter)
        if isinstance(response, BaseException):
            raise response
        return response

    async def on_reply(text, *, final, reasoning=""):
        replies.append((text, final))

    async def dispatch_tools(tool_calls):
        if dispatch is not None:
            return await dispatch(tool_calls)
        return [ToolResult(call_id=tc.id, content="ok") for tc in tool_calls]

    async def no_fold():
        return next(fold_iter, [])

    async def record_trajectory(event_kind, payload):
        if trajectory_events is not None:
            trajectory_events.append((event_kind, payload))

    async def record_tool(tc, event_kind, payload):
        if tool_events is not None:
            tool_events.append((tc.id, tc.name, event_kind, payload))

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)
    message_builder = build_messages or (
        lambda transcript: [{"role": "user", "content": "turn"}]
    )
    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_PROVIDER,
            build_messages=message_builder,
            dispatch_tools=dispatch_tools,
            on_reply=on_reply,
            on_file_reply=on_file_reply,
            required_file_suffixes=required_file_suffixes,
            file_requirement_messages=file_requirement_messages,
            resolve_required_file_suffixes=resolve_required_file_suffixes,
            on_file_requirement_changed=on_file_requirement_changed,
            fold_new_messages=no_fold,
            add_usage=lambda usage: None,
            max_calls=max_calls,
            suppress_native_reasoning=suppress_native_reasoning,
            on_trajectory_event=(
                record_trajectory if trajectory_events is not None else None
            ),
            on_tool_event=(record_tool if tool_events is not None else None),
        )
    )
    return outcome, calls, replies, messages_seen


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
    assert seen_kwargs == [{
        "require_reply": False,
        "max_tokens": provider_client.CHAT_OUTPUT_MAX_TOKENS,
    }]

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

    assert seen_kwargs == [{"require_reply": False}]


def test_send_file_is_chat_only_and_invokes_explicit_callback(monkeypatch):
    files = []
    tool_events = []

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/计划.md",
                            "revision": 2,
                            "completion_message": "文件已经发给你了。",
                        },
                    }
                ],
                "usage": {},
            },
            {"reply": "文件已经发给你了。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        tool_events=tool_events,
    )

    assert "send_file" in calls[0]
    assert files == [("/workspace/计划.md", 2)]
    assert replies == [("文件已经发给你了。", True)]
    assert outcome.replied_intermediate is True
    assert [
        (call_id, name, event_kind)
        for call_id, name, event_kind, _payload in tool_events
    ] == [
        ("f1", "send_file", "tool_call_started"),
        ("f1", "send_file", "tool_call_result"),
    ]


def test_chinese_file_delivery_uses_chinese_compact_control_messages(monkeypatch):
    files = []
    request = (
        "请生成并发送一个中文纯文本文件，文件名为中文附件，"
        "正文写这是中文测试，请发送真实附件。"
    )

    async def on_file(path, revision):
        files.append((path, revision))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content=(
                    "ok: workspace_write applied at revision 1; use the same "
                    "path and revision 1 with send_file"
                ),
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    outcome, calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-cn",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/中文附件.txt",
                        "content": "这是中文测试。",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-cn",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/中文附件.txt",
                        "revision": 1,
                        "completion_message": "中文附件已经生成，可以下载了。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(".txt",),
        file_requirement_messages=[{"role": "user", "content": request}],
        max_calls=4,
        suppress_native_reasoning=True,
    )

    assert calls[1] == ("send_file",)
    assert files == [("/workspace/中文附件.txt", 1)]
    assert replies == [("中文附件已经生成，可以下载了。", True)]
    assert outcome.stop_reason == "final_text"
    assert "现在调用 send_file" in messages_seen[1][0]["content"]
    assert request in messages_seen[1][0]["content"]
    assert "completion_message" in messages_seen[1][0]["content"]
    assert "必须使用中文" in messages_seen[1][0]["content"]
    assert self_thinking.INSTRUCTION.strip() not in messages_seen[1][0]["content"]
    assert len(messages_seen) == 2


def test_canvas_send_file_delivers_model_authored_display_metadata(monkeypatch):
    files = []

    async def on_file(path, revision, *, title, subtitle):
        files.append((path, revision, title, subtitle))

    outcome, _, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "canvas-1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/接星星.io.html",
                            "revision": 2,
                            "title": "接星星",
                            "subtitle": "六十秒接住尽可能多的星星",
                            "completion_message": "画布已经更新，可以直接打开了。",
                        },
                    }
                ],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
    )

    assert files == [
        (
            "/workspace/接星星.io.html",
            2,
            "接星星",
            "六十秒接住尽可能多的星星",
        )
    ]
    assert replies == [("画布已经更新，可以直接打开了。", True)]
    assert outcome.replied_intermediate is True


def test_required_file_turn_offers_only_delivery_pipeline_tools(monkeypatch):
    files = []

    async def on_file(path, revision):
        files.append((path, revision))

    _, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/summary.md",
                            "revision": 1,
                            "completion_message": "文档已生成。",
                        },
                    }
                ],
                "usage": {},
            },
            {"reply": "文档已生成。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        required_file_suffixes=(".md",),
    )

    offered = set(calls[0])
    assert offered <= {
        "memory_index",
        "memory_search",
        "memory_fetch",
        "workspace_list",
        "workspace_read",
        "workspace_write",
        "send_file",
    }
    assert {"workspace_write", "send_file"}.issubset(offered)
    assert files == [("/workspace/summary.md", 1)]
    assert replies == [("文档已生成。", True)]


def test_send_file_is_not_offered_without_delivery_callback(monkeypatch):
    _, calls, _, _ = _run_loop(
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

    outcome, _, replies, _ = _run_loop(
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
                        "args": {
                            "path": "/workspace/a.md",
                            "revision": 1,
                            "completion_message": "File delivered.",
                        },
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("给我一个 Word 文档", (".docx",)),
        ("给我一个Word文档", (".docx",)),
        ("我想要一份 Word 文档", (".docx",)),
        ("请生成一个 PDF 发给我", (".pdf",)),
        ("请生成PDF文件发给我", (".pdf",)),
        ("给我生成一个可以下载的健身计划文档", ()),
        ("我想要一份睡眠报告，PDF 格式", (".pdf",)),
        ("I want a sleep report in PDF format", (".pdf",)),
        ("I want to know what PDF is", None),
        ("给我一份下周的工作清单", None),
        ("can you make a plan for my week?", None),
        ("give me a report on how I slept", None),
        ("help me make a checklist for packing", None),
        ("给我一个报告", None),
        ("请根据你对我们的记忆，整理一份 Markdown文档，并提供下载。", (".md",)),
        ("将我们的记忆总结一下，生成一份Md 文档发给我", (".md",)),
        ("把 Markdown 文件转换成 Word 文档", (".docx",)),
        ("把Markdown文件转成Word文档", (".docx",)),
        ("生成一份纯文本 TXT 文件", (".txt",)),
        ("导出 CSV 文件", (".csv",)),
        ("生成 TSV 文件", (".tsv",)),
        ("生成 JSON 文件", (".json",)),
        ("生成 XML 文件", (".xml",)),
        ("生成 YML 文件", (".yaml",)),
        ("生成 HTML 文件", (".html",)),
        ("生成 RTF 文件", (".rtf",)),
        ("生成 SVG 文件", (".svg",)),
        ("Word 和 PDF 有什么区别？", None),
        ("请帮我看看这个 Word 文档", None),
        ("如何创建 Word 文档？", None),
        ("请解释怎么导出 PDF，不要替我生成文件", None),
        ("你能帮我创建Word文档吗？", (".docx",)),
        ("不要 Markdown 文档，给我Word文档", (".docx",)),
        ("生成 Word 文档，不要 PDF", (".docx",)),
        ("不要生成 Word 文档", None),
        ("不用生成 PDF 文件", None),
        ("别帮我制作 Word 文档", None),
        ("Don't create a Word document; create a PDF instead", (".pdf",)),
        ("Do not generate a PDF file; make a Word document instead", (".docx",)),
    ],
)
def test_required_file_suffixes_detects_clear_file_intent(text, expected):
    assert v2_context.required_file_suffixes(
        [{"role": "user", "content": text}]
    ) == expected


@pytest.mark.parametrize(
    ("suffix", "required_suffix"),
    [
        (".md", ".md"),
        (".markdown", ".md"),
        (".txt", ".txt"),
        (".csv", ".csv"),
        (".tsv", ".tsv"),
        (".json", ".json"),
        (".xml", ".xml"),
        (".yaml", ".yaml"),
        (".yml", ".yaml"),
        (".html", ".html"),
        (".htm", ".html"),
        (".rtf", ".rtf"),
        (".svg", ".svg"),
        (".docx", ".docx"),
        (".pdf", ".pdf"),
    ],
)
def test_non_canvas_file_delivery_ignores_canvas_optional_fields(
    monkeypatch,
    suffix,
    required_suffix,
):
    files = []

    async def on_file(path, revision):
        files.append((path, revision))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content=(
                    "ok: workspace_write applied at revision 1; use the same "
                    "path and revision 1 with send_file"
                ),
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    path = f"/workspace/文件测试{suffix}"
    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write",
                    "name": "workspace_write",
                    "args": {
                        "path": path,
                        "content": "# 文件测试\n",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send",
                    "name": "send_file",
                    "args": {
                        "path": path,
                        "revision": 1,
                        "title": "文件测试",
                        "subtitle": "一份测试文件",
                        "completion_message": "文件已经做好。",
                    },
                }],
                "usage": {},
            },
            {
                "reply": "文件已经生成，可以下载了。",
                "tool_calls": [],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(required_suffix,),
        max_calls=5,
    )

    assert calls[1] == ("send_file",)
    assert files == [(path, 1)]
    assert replies == [("文件已经生成，可以下载了。", True)]
    assert outcome.stop_reason == "final_text"


def test_file_marker_without_staged_attachment_is_not_a_download_link():
    cleaned, removed = sanitize_downloadable_reply(
        "文件已经生成：[file: 小林与小凛的记忆总结.md]",
        attachment_staged=False,
    )

    assert removed is True
    assert "[file:" not in cleaned


def test_file_marker_with_staged_attachment_becomes_plain_confirmation():
    cleaned, removed = sanitize_downloadable_reply(
        "文件已经生成：[file: 小林与小凛的记忆总结.md]",
        attachment_staged=True,
    )

    assert removed is True
    assert cleaned == "文件已生成，可在下方下载。"


def test_required_file_recovery_resets_tool_only_stall_before_delivery(
    monkeypatch,
):
    files = []
    trajectory_events = []

    async def on_file(path, revision):
        files.append((path, revision))

    async def dispatch(tool_calls):
        results = []
        for call in tool_calls:
            if call.name == "workspace_write":
                results.append(ToolResult(
                    call_id=call.id,
                    content=(
                        "ok: workspace_write applied at revision 1; use the same "
                        "path and revision 1 with send_file"
                    ),
                    metadata={"workspace_revision": 1},
                ))
            else:
                results.append(ToolResult(call_id=call.id, content="ok"))
        return results

    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "index-1",
                    "name": "memory_index",
                    "args": {"limit": 1},
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "index-2",
                    "name": "memory_index",
                    "args": {"limit": 20},
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "fetch-1",
                    "name": "memory_fetch",
                    "args": {"ids": ["memory-1"]},
                }],
                "usage": {},
            },
            {
                "reply": "这是记忆总结，但附件还没有生成。",
                "tool_calls": [],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-1",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/小林与小凛的记忆总结.md",
                        "content": "# 小林与小凛的记忆总结",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-1",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/小林与小凛的记忆总结.md",
                        "revision": 1,
                        "completion_message": "Markdown 文档已经发给你了。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(".md",),
        file_requirement_messages=[{
            "role": "user",
            "content": "将我们的记忆总结一下，生成一份Md 文档发给我",
        }],
        max_calls=8,
        trajectory_events=trajectory_events,
    )

    assert "workspace_write" not in calls[3]
    assert calls[4] == ("workspace_write",)
    assert calls[5] == ("send_file",)
    assert files == [("/workspace/小林与小凛的记忆总结.md", 1)]
    assert replies == [("Markdown 文档已经发给你了。", True)]
    assert outcome.stop_reason == "final_text"
    assert all(
        kind != "required_file_missing" for kind, _payload in trajectory_events
    )


@pytest.mark.parametrize(
    "text",
    [
        "请和我做一个可交互的 Canvas，我们的记忆知识图谱，直接交给我。",
        "Create an interactive knowledge graph work for me",
        "请解释一下什么是可交互知识图谱",
    ],
)
def test_shared_work_intent_is_not_preclassified_from_user_words(text):
    assert v2_context.required_file_suffixes(
        [{"role": "user", "content": text}]
    ) is None


def test_model_selected_shared_work_uses_compact_delivery_after_large_write(
    monkeypatch,
):
    files = []
    trajectory_events = []

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content=(
                    "ok: workspace_write applied at revision 1; use the same "
                    "path and revision 1 with send_file"
                ),
            )
            for call in tool_calls
        ]

    def large_after_write(transcript):
        has_write = any(
            isinstance(item, ToolExchange)
            and any(call.name == "workspace_write" for call in item.calls)
            for item in transcript
        )
        return [
            {
                "role": "system",
                "content": "x" * (40_000 if has_write else 100),
            },
            {"role": "user", "content": "make the Canvas"},
            *transcript,
        ]

    outcome, calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "w1",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/记忆知识图谱.io.html",
                            "content": "<!doctype html><title>记忆知识图谱</title>",
                            "expected_revision": 0,
                        },
                    }
                ],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/记忆知识图谱.io.html",
                            "revision": 1,
                            "title": "记忆知识图谱",
                            "subtitle": "把重要记忆整理成可探索的关系图",
                            "completion_message": "记忆知识图谱已经准备好了，可以直接打开。",
                        },
                    }
                ],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        max_calls=3,
        trajectory_events=trajectory_events,
        build_messages=large_after_write,
    )

    assert {"workspace_write", "send_file"}.issubset(calls[0])
    assert calls[1:] == [("send_file",)]
    assert files == [("/workspace/记忆知识图谱.io.html", 1)]
    assert replies == [("记忆知识图谱已经准备好了，可以直接打开。", True)]
    assert outcome.stop_reason == "final_text"
    assert len(str(messages_seen[1])) < 2_000
    assert "记忆知识图谱.io.html" in str(messages_seen[1])
    assert "completion_message" in str(messages_seen[1])
    assert "write it in English" in str(messages_seen[1])
    assert "The work the user asked for was saved" not in str(messages_seen)
    assert len(messages_seen) == 2
    assert all(
        kind != "required_file_missing" for kind, _payload in trajectory_events
    )


def test_chinese_canvas_compact_delivery_preserves_requested_card_metadata(
    monkeypatch,
):
    files = []
    request = (
        "请生成并发送一个 Canvas，文件名为修前基线-中文.io.html。"
        "附件卡标题使用中文 Canvas 基线，副标题使用交互测试，并用中文回复。"
    )

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision, title, subtitle))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 1",
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    outcome, calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-cn-canvas",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/修前基线-中文.io.html",
                        "content": "<main>中文 Canvas 基线</main>",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-cn-canvas",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/修前基线-中文.io.html",
                        "revision": 1,
                        "title": "中文 Canvas 基线",
                        "subtitle": "交互测试",
                        "completion_message": "中文 Canvas 已经生成，可以直接打开。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        file_requirement_messages=[{"role": "user", "content": request}],
        max_calls=3,
        suppress_native_reasoning=True,
    )

    assert calls[1] == ("send_file",)
    assert files == [
        (
            "/workspace/修前基线-中文.io.html",
            1,
            "中文 Canvas 基线",
            "交互测试",
        )
    ]
    assert replies == [("中文 Canvas 已经生成，可以直接打开。", True)]
    assert outcome.stop_reason == "final_text"
    compact_prompt = messages_seen[1][0]["content"]
    assert request in compact_prompt
    assert "completion_message" in compact_prompt
    assert "title 和 subtitle" in compact_prompt
    assert self_thinking.INSTRUCTION.strip() not in compact_prompt


def test_canvas_delivery_validates_language_when_request_is_multimodal(
    monkeypatch,
):
    files = []

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision, title, subtitle))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 1",
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    outcome, calls, replies, _messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-canvas",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/board.io.html",
                        "content": "<main>Canvas board</main>",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-canvas-wrong-language",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/board.io.html",
                        "revision": 1,
                        "title": "Canvas board",
                        "subtitle": "Interactive board",
                        "completion_message": "画布已经生成，可以直接打开。",
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-canvas-corrected",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/board.io.html",
                        "revision": 1,
                        "title": "Canvas board",
                        "subtitle": "Interactive board",
                        "completion_message": (
                            "<think>private delivery reasoning</think>"
                            "Your canvas is ready to open."
                        ),
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        file_requirement_messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Build me a canvas board, reply in English.",
                },
            ],
        }],
        max_calls=4,
        suppress_native_reasoning=True,
    )

    assert calls[1:] == [("send_file",), ("send_file",)]
    assert files == [
        (
            "/workspace/board.io.html",
            1,
            "Canvas board",
            "Interactive board",
        )
    ]
    assert replies == [("Your canvas is ready to open.", True)]
    assert outcome.stop_reason == "final_text"


def test_canvas_update_compact_delivery_preserves_request_and_corrects_metadata(
    monkeypatch,
):
    files = []
    trajectory_events = []
    tool_events = []
    user_request = "把标题改成「星光方块」，副标题改成「今晚一起玩」。"

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision, title, subtitle))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 2",
                metadata={"workspace_revision": 2},
            )
            for call in tool_calls
        ]

    outcome, calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-update",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/小游戏.io.html",
                        "content": "<title>星光方块</title><main>game</main>",
                        "expected_revision": 1,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "missing-metadata",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/小游戏.io.html",
                        "revision": 2,
                        "title": "星光方块",
                        "subtitle": "今晚一起玩",
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "corrected-delivery",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/小游戏.io.html",
                        "revision": 2,
                        "title": "星光方块",
                        "subtitle": "今晚一起玩",
                        "completion_message": "已经按你的要求更新好了。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        file_requirement_messages=[{"role": "user", "content": user_request}],
        max_calls=4,
        trajectory_events=trajectory_events,
        tool_events=tool_events,
    )

    assert calls[1:] == [("send_file",), ("send_file",)]
    assert files == [
        ("/workspace/小游戏.io.html", 2, "星光方块", "今晚一起玩")
    ]
    assert user_request in str(messages_seen[1])
    assert "completion_message" in str(messages_seen[1])
    assert "title 和 subtitle" in str(messages_seen[1])
    validation_exchange = messages_seen[2][-1]
    assert isinstance(validation_exchange, ToolExchange)
    assert [result.call_id for result in validation_exchange.results] == [
        "missing-metadata"
    ]
    assert "completion_message" in validation_exchange.results[0].content
    assert [event[2] for event in tool_events if event[0] == "missing-metadata"] == [
        "tool_call_started",
        "tool_call_result",
    ]
    assert any(
        kind == "tool_batch_validation_failed"
        for kind, _payload in trajectory_events
    )
    assert len(messages_seen) == 3
    assert replies == [("已经按你的要求更新好了。", True)]
    assert outcome.stop_reason == "final_text"


def test_generic_validation_retry_does_not_consume_canvas_delivery_retry(
    monkeypatch,
):
    files = []
    initial_messages = [{"role": "user", "content": "先帮我整理思路"}]
    late_canvas_request = [{
        "role": "user",
        "content": "请把它做成中文 Canvas。",
    }]

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision, title, subtitle))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content=(
                    "ok: workspace_write applied at revision 1"
                    if call.name == "workspace_write"
                    else "tool-observation"
                ),
                metadata=(
                    {"workspace_revision": 1}
                    if call.name == "workspace_write"
                    else None
                ),
            )
            for call in tool_calls
        ]

    outcome, _calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "先搜索",
                "tool_calls": [{
                    "id": "generic-invalid",
                    "name": "memory_search",
                    "args": {},
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-canvas",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/retry-isolation.io.html",
                        "content": "<main>isolation</main>",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "canvas-invalid",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/retry-isolation.io.html",
                        "revision": 1,
                        "title": "纠错隔离",
                        "subtitle": "镜像 A",
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "canvas-corrected",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/retry-isolation.io.html",
                        "revision": 1,
                        "title": "纠错隔离",
                        "subtitle": "镜像 A",
                        "completion_message": "中文 Canvas 已经做好了。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        file_requirement_messages=initial_messages,
        resolve_required_file_suffixes=v2_context.required_file_suffixes,
        fold_batches=[late_canvas_request],
        max_calls=5,
        build_messages=lambda transcript: list(transcript),
    )

    generic_exchange = next(
        item for item in messages_seen[1]
        if isinstance(item, ToolExchange)
        and item.calls[0].id == "generic-invalid"
    )
    assert "invalid args for memory_search" in generic_exchange.results[0].content
    canvas_exchange = next(
        item for item in messages_seen[3]
        if isinstance(item, ToolExchange)
        and item.calls[0].id == "canvas-invalid"
    )
    assert "completion_message" in canvas_exchange.results[0].content
    assert files == [(
        "/workspace/retry-isolation.io.html",
        1,
        "纠错隔离",
        "镜像 A",
    )]
    assert replies == [("中文 Canvas 已经做好了。", True)]
    assert outcome.stop_reason == "final_text"


def test_send_file_validation_retry_does_not_consume_generic_validation_retry(
    monkeypatch,
):
    files = []
    trajectory_events = []
    tool_events = []
    initial_messages = [{"role": "user", "content": "给我一个 Word 文档"}]
    cancellation = [{"role": "user", "content": "不用文件了，直接回答"}]

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision, title, subtitle))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content=(
                    "ok: workspace_write applied at revision 1"
                    if call.name == "workspace_write"
                    else "tool-observation"
                ),
                metadata=(
                    {"workspace_revision": 1}
                    if call.name == "workspace_write"
                    else None
                ),
            )
            for call in tool_calls
        ]

    outcome, _calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-before-cancel",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/cancelled.docx",
                        "content": "# cancelled",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "delivery-invalid",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/cancelled.docx",
                        "revision": 1,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "搜索",
                "tool_calls": [{
                    "id": "generic-after-cancel",
                    "name": "memory_search",
                    "args": {},
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "generic-corrected",
                    "name": "memory_search",
                    "args": {"query": "纠错隔离"},
                }],
                "usage": {},
            },
            {"reply": "搜索完成。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(".docx",),
        file_requirement_messages=initial_messages,
        resolve_required_file_suffixes=v2_context.required_file_suffixes,
        fold_batches=[[], cancellation],
        max_calls=6,
        trajectory_events=trajectory_events,
        tool_events=tool_events,
        build_messages=lambda transcript: list(transcript),
    )

    assert [event[2] for event in tool_events if event[0] == "delivery-invalid"] == [
        "tool_call_started",
        "tool_call_result",
    ]
    assert any(
        kind == "tool_batch_validation_failed"
        and payload["calls"][0].id == "delivery-invalid"
        for kind, payload in trajectory_events
    )
    assert [
        event[2] for event in tool_events if event[0] == "generic-after-cancel"
    ] == ["tool_call_started", "tool_call_result"]
    assert any(
        kind == "tool_batch_validation_failed"
        and payload["calls"][0].id == "generic-after-cancel"
        and "invalid args for memory_search" in payload["results"][0].content
        for kind, payload in trajectory_events
    )
    assert files == []
    assert replies == [("搜索完成。", True)]
    assert outcome.stop_reason == "final_text"


def test_canvas_update_repeated_invalid_metadata_fails_as_delivery_not_connection(
    monkeypatch,
):
    files = []

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision, title, subtitle))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 2",
                metadata={"workspace_revision": 2},
            )
            for call in tool_calls
        ]

    with pytest.raises(
        tool_loop.CanvasDeliveryIncomplete,
        match="invalid_canvas_delivery_args",
    ):
        _run_loop(
            monkeypatch,
            [
                {
                    "reply": "",
                    "tool_calls": [{
                        "id": "write-update",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/小游戏.io.html",
                            "content": "<title>小游戏</title>",
                            "expected_revision": 1,
                        },
                    }],
                    "usage": {},
                },
                {
                    "reply": "",
                    "tool_calls": [{
                        "id": "missing-metadata-1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/小游戏.io.html",
                            "revision": 2,
                        },
                    }],
                    "usage": {},
                },
                {
                    "reply": "",
                    "tool_calls": [{
                        "id": "missing-metadata-2",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/小游戏.io.html",
                            "revision": 2,
                        },
                    }],
                    "usage": {},
                },
            ],
            on_file_reply=on_file,
            dispatch=dispatch,
            file_requirement_messages=[{
                "role": "user",
                "content": "修改标题和副标题",
            }],
            max_calls=4,
        )

    assert files == []


def test_ordinary_workspace_write_remains_a_draft_without_auto_delivery(monkeypatch):
    files = []

    async def on_file(path, revision):
        files.append((path, revision))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 1",
            )
            for call in tool_calls
        ]

    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "w1",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/notes.md",
                            "content": "# Notes",
                            "expected_revision": 0,
                        },
                    }
                ],
                "usage": {},
            },
            {"reply": "我先把草稿保存好了。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        max_calls=2,
    )

    assert "workspace_write" in calls[0]
    assert calls[1] == ("workspace_write",)
    assert files == []
    assert replies == [("我先把草稿保存好了。", True)]
    assert outcome.stop_reason == "final_text"


def test_pending_canvas_delivery_rejects_wrong_target_then_delivers_exact_one(
    monkeypatch,
):
    files = []

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 1",
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/panel.io.html",
                        "content": "<title>面板</title><main>panel</main>",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "wrong-file",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/other.io.html",
                        "revision": 9,
                        "title": "面板",
                        "subtitle": "一个简洁的交互面板",
                        "completion_message": "新版面板已经准备好了。",
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "exact-file",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/panel.io.html",
                        "revision": 1,
                        "title": "面板",
                        "subtitle": "一个简洁的交互面板",
                        "completion_message": "新版面板已经准备好了。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        max_calls=5,
    )

    assert calls[1] == ("send_file",)
    assert calls[2] == ("send_file",)
    assert files == [("/workspace/panel.io.html", 1)]
    assert replies == [("新版面板已经准备好了。", True)]
    assert outcome.stop_reason == "final_text"


def test_repeated_ordinary_file_target_mismatch_is_not_a_canvas_failure(
    monkeypatch,
):
    async def on_file(path, revision):
        raise AssertionError("a mismatched file must not be delivered")

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 1",
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    with pytest.raises(
        tool_loop.FileDeliveryIncomplete,
        match="pending_delivery_target_mismatch",
    ) as failure:
        _run_loop(
            monkeypatch,
            [
                {
                    "reply": "",
                    "tool_calls": [{
                        "id": "write",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/notes.md",
                            "content": "# Notes",
                            "expected_revision": 0,
                        },
                    }],
                    "usage": {},
                },
                {
                    "reply": "",
                    "tool_calls": [{
                    "id": "wrong-file-1",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/other.md",
                        "revision": 9,
                        "completion_message": "The file is ready.",
                    },
                    }],
                    "usage": {},
                },
                {
                    "reply": "",
                    "tool_calls": [{
                    "id": "wrong-file-2",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/other.md",
                        "revision": 9,
                        "completion_message": "The file is ready.",
                    },
                    }],
                    "usage": {},
                },
            ],
            on_file_reply=on_file,
            dispatch=dispatch,
            required_file_suffixes=(".md",),
            max_calls=4,
        )

    assert not isinstance(failure.value, tool_loop.CanvasDeliveryIncomplete)


def test_io_html_satisfies_generic_html_delivery_requirement(monkeypatch):
    files = []

    async def on_file(path, revision, *, title="", subtitle=""):
        files.append((path, revision))

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=call.id,
                content="ok: workspace_write applied at revision 1",
                metadata={"workspace_revision": 1},
            )
            for call in tool_calls
        ]

    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-game",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/接星星小游戏.io.html",
                        "content": "<title>接星星小游戏</title>",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-game",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/接星星小游戏.io.html",
                        "revision": 1,
                        "title": "接星星小游戏",
                        "subtitle": "六十秒内接住尽可能多的星星",
                        "completion_message": "小游戏已经发给你了，可以直接打开。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(".html",),
        max_calls=4,
    )

    assert calls[1] == ("send_file",)
    assert files == [("/workspace/接星星小游戏.io.html", 1)]
    assert replies == [("小游戏已经发给你了，可以直接打开。", True)]
    assert outcome.stop_reason == "final_text"


def test_existing_canvas_read_can_be_delivered_without_rewriting_source(monkeypatch):
    files = []
    dispatched = []
    path = "/workspace/打砖块小游戏.io.html"

    async def on_file(file_path, revision, *, title="", subtitle=""):
        files.append((file_path, revision, title, subtitle))

    async def dispatch(tool_calls):
        dispatched.extend(call.name for call in tool_calls)
        return [
            ToolResult(
                call_id=call.id,
                content='{"path":"/workspace/打砖块小游戏.io.html","revision":7}',
                metadata={
                    "workspace_read_path": path,
                    "workspace_revision": 7,
                },
            )
            for call in tool_calls
        ]

    outcome, calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "read-existing",
                    "name": "workspace_read",
                    "args": {"path": path},
                }],
                "usage": {},
            },
            {
                "reply": f"已经更新，打开 private://workspace{path}",
                "tool_calls": [],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-existing",
                    "name": "send_file",
                    "args": {
                        "path": path,
                        "revision": 7,
                        "title": "霓虹砖块",
                        "subtitle": "击碎所有砖块，刷新最高分",
                        "completion_message": "标题和副标题已经更新好了。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(".html",),
        file_requirement_messages=[{
            "role": "user",
            "content": "把这个 Canvas 的标题和副标题重新改一下，发给我。",
        }],
        max_calls=4,
    )

    assert dispatched == ["workspace_read"]
    assert set(calls[2]) == {"workspace_write", "send_file"}
    assert "existing exact workspace revision" in messages_seen[2][0]["content"]
    assert files == [
        (path, 7, "霓虹砖块", "击碎所有砖块，刷新最高分")
    ]
    assert replies == [("标题和副标题已经更新好了。", True)]
    assert outcome.stop_reason == "final_text"


def test_existing_canvas_delivery_choice_can_still_rewrite_changed_source(
    monkeypatch,
):
    files = []
    path = "/workspace/打砖块小游戏.io.html"

    async def on_file(file_path, revision, *, title="", subtitle=""):
        files.append((file_path, revision, title, subtitle))

    async def dispatch(tool_calls):
        results = []
        for call in tool_calls:
            if call.name == "workspace_read":
                results.append(ToolResult(
                    call_id=call.id,
                    content='{"path":"/workspace/打砖块小游戏.io.html","revision":7}',
                    metadata={
                        "workspace_read_path": path,
                        "workspace_revision": 7,
                    },
                ))
            else:
                results.append(ToolResult(
                    call_id=call.id,
                    content="ok: workspace_write applied at revision 8",
                    metadata={"workspace_revision": 8},
                ))
        return results

    outcome, calls, _, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [{
                    "id": "read-existing",
                    "name": "workspace_read",
                    "args": {"path": path},
                }],
                "usage": {},
            },
            {"reply": "修改完成。", "tool_calls": [], "usage": {}},
            {
                "reply": "",
                "tool_calls": [{
                    "id": "write-update",
                    "name": "workspace_write",
                    "args": {
                        "path": path,
                        "content": "<html>updated game</html>",
                        "expected_revision": 7,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "send-update",
                    "name": "send_file",
                    "args": {
                        "path": path,
                        "revision": 8,
                        "title": "新版打砖块",
                        "subtitle": "加入新的关卡与计分规则",
                        "completion_message": "新版已经发给你，可以直接打开。",
                    },
                }],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        dispatch=dispatch,
        required_file_suffixes=(".html",),
        max_calls=5,
    )

    assert set(calls[2]) == {"workspace_write", "send_file"}
    assert calls[3] == ("send_file",)
    assert files == [
        (path, 8, "新版打砖块", "加入新的关卡与计分规则")
    ]
    assert outcome.stop_reason == "final_text"


@pytest.mark.parametrize("required_suffix", [".pdf", ".docx", ".json"])
def test_io_html_does_not_satisfy_non_html_requirement(required_suffix):
    assert tool_loop._file_suffix_for_requirement(
        "/workspace/game.io.html",
        frozenset({required_suffix}),
    ) == ".io.html"


def test_required_file_suffixes_follow_latest_user_correction():
    assert v2_context.required_file_suffixes(
        [
            {"role": "user", "content": "给我一个 Word 文档"},
            {"role": "user", "content": "不用文件了，直接回答"},
        ]
    ) is None
    assert v2_context.required_file_suffixes(
        [
            {"role": "user", "content": "给我一个 Word 文档"},
            {"role": "user", "content": "不要生成 Word 文档"},
        ]
    ) is None
    assert v2_context.required_file_suffixes(
        [
            {"role": "user", "content": "Create a Word document for me"},
            {
                "role": "user",
                "content": "Don't create a Word document; create a PDF instead",
            },
        ]
    ) == (".pdf",)
    assert v2_context.required_file_suffixes(
        [
            {"role": "user", "content": "给我一个 Word 文档"},
            {"role": "user", "content": "改成 PDF 文档"},
        ]
    ) == (".pdf",)
    assert v2_context.required_file_suffixes(
        [
            {"role": "user", "content": "给我一个 Word 文档"},
            {"role": "user", "content": "另外也生成一份 PDF"},
        ]
    ) == (".docx", ".pdf")


def test_empty_file_requirement_requires_any_downloadable_file(monkeypatch):
    files = []

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {"reply": "这是你的工作清单。", "tool_calls": [], "usage": {}},
            {
                "reply": "",
                "tool_calls": [{
                    "id": "w1",
                    "name": "workspace_write",
                    "args": {
                        "path": "/workspace/工作清单.md",
                        "content": "# 工作清单",
                        "expected_revision": 0,
                    },
                }],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [{
                    "id": "f1",
                    "name": "send_file",
                    "args": {
                        "path": "/workspace/工作清单.md",
                        "revision": 1,
                        "completion_message": "工作清单已经发给你了。",
                    },
                }],
                "usage": {},
            },
            {"reply": "工作清单已经发给你了。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        required_file_suffixes=(),
        max_calls=4,
    )

    assert calls[1] == ("workspace_write",)
    assert calls[2] == ("send_file",)
    assert files == [("/workspace/工作清单.md", 1)]
    assert replies == [("工作清单已经发给你了。", True)]
    assert outcome.stop_reason == "final_text"


def test_late_folded_word_request_enables_file_completion_guard(monkeypatch):
    files = []
    initial_messages = [{"role": "user", "content": "先帮我想想健身计划"}]
    late_word_request = [{"role": "user", "content": "请做成 Word 文档"}]

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, _, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "r1",
                        "name": "workspace_read",
                        "args": {"path": "/workspace/notes.md"},
                    }
                ],
                "usage": {},
            },
            {"reply": "这里是计划。", "tool_calls": [], "usage": {}},
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "w1",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/计划.docx",
                            "content": "# 计划",
                            "expected_revision": 0,
                        },
                    }
                ],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/计划.docx",
                            "revision": 1,
                            "completion_message": "Word 文档已发给你。",
                        },
                    }
                ],
                "usage": {},
            },
        ],
        on_file_reply=on_file,
        file_requirement_messages=initial_messages,
        resolve_required_file_suffixes=v2_context.required_file_suffixes,
        fold_batches=[late_word_request],
        max_calls=5,
    )

    assert files == [("/workspace/计划.docx", 1)]
    assert replies == [("Word 文档已发给你。", True)]
    assert outcome.stop_reason == "final_text"


def test_late_folded_cancellation_disables_file_completion_guard(monkeypatch):
    initial_messages = [{"role": "user", "content": "给我一个 Word 文档"}]
    cancellation = [{"role": "user", "content": "不用文件了，直接回答"}]

    outcome, _, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "r1",
                        "name": "workspace_read",
                        "args": {"path": "/workspace/notes.md"},
                    }
                ],
                "usage": {},
            },
            {"reply": "好的，直接回答。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=lambda path, revision: None,
        required_file_suffixes=(".docx",),
        file_requirement_messages=initial_messages,
        resolve_required_file_suffixes=v2_context.required_file_suffixes,
        fold_batches=[cancellation],
        max_calls=4,
    )

    assert replies == [("好的，直接回答。", True)]
    assert outcome.stop_reason == "final_text"


def test_late_cancellation_discards_an_already_staged_file(monkeypatch):
    initial_messages = [{"role": "user", "content": "给我一个 Word 文档"}]
    cancellation = [{"role": "user", "content": "不用文件了，直接回答"}]
    staged_files = []

    async def on_file(path, revision):
        staged_files.append((path, revision))

    async def on_requirement_changed():
        staged_files.clear()

    outcome, _, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/计划.docx",
                            "revision": 1,
                        },
                    }
                ],
                "usage": {},
            },
            {"reply": "好的，改为直接回答。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        required_file_suffixes=(".docx",),
        file_requirement_messages=initial_messages,
        resolve_required_file_suffixes=v2_context.required_file_suffixes,
        on_file_requirement_changed=on_requirement_changed,
        fold_batches=[cancellation],
        max_calls=3,
    )

    assert staged_files == []
    assert replies == [("好的，改为直接回答。", True)]
    assert outcome.stop_reason == "final_text"


def test_required_word_file_retries_plain_text_until_docx_is_delivered(monkeypatch):
    files = []
    trajectory_events = []

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, calls, replies, messages_seen = _run_loop(
        monkeypatch,
        [
            {"reply": "这是你的计划。", "tool_calls": [], "usage": {}},
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "w1",
                        "name": "workspace_write",
                        "args": {
                            "path": "/workspace/计划.docx",
                            "content": "# 计划",
                            "expected_revision": 0,
                        },
                    }
                ],
                "usage": {},
            },
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/计划.docx",
                            "revision": 1,
                            "completion_message": "Word 文档已经发给你了。",
                        },
                    }
                ],
                "usage": {},
            },
            {"reply": "Word 文档已经发给你了。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        required_file_suffixes=(".docx",),
        max_calls=4,
        trajectory_events=trajectory_events,
    )

    assert replies == [("Word 文档已经发给你了。", True)]
    assert files == [("/workspace/计划.docx", 1)]
    assert outcome.stop_reason == "final_text"
    assert all(
        kind != "required_file_missing" for kind, _payload in trajectory_events
    )
    assert calls[1] == ("workspace_write",)
    assert calls[2] == ("send_file",)
    assert "REQUIRED FILE DELIVERY" in messages_seen[1][0]["content"]


def test_required_file_retry_preserves_native_tool_exchange(monkeypatch):
    responses = iter([
        {
            "reply": "",
            "tool_calls": [{
                "id": "memory-1",
                "name": "memory_search",
                "args": {"query": "our relationship"},
            }],
            "assistant_turn": {
                "wire": "anthropic",
                "payload": [{
                    "type": "tool_use",
                    "id": "memory-1",
                    "name": "memory_search",
                    "input": {"query": "our relationship"},
                }],
            },
            "usage": {},
        },
        {"reply": "I found the memories.", "tool_calls": [], "usage": {}},
        {"reply": "Still preparing the file.", "tool_calls": [], "usage": {}},
    ])
    provider_messages = []

    async def fake_chat(config, messages, *, tools=None, **kwargs):
        provider_messages.append(messages)
        return next(responses)

    async def dispatch(tool_calls):
        return [ToolResult(call_id=call.id, content="[]") for call in tool_calls]

    async def on_reply(text, *, final, reasoning=""):
        return None

    async def no_fold():
        return []

    def build_messages(transcript):
        return [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "make a Word document"},
            *transcript,
        ]

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_chat)
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_PROVIDER,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        on_file_reply=lambda path, revision: None,
        required_file_suffixes=(".docx",),
        fold_new_messages=no_fold,
        add_usage=lambda usage: None,
        max_calls=4,
    ))

    assert outcome.stop_reason == "required_file_missing"
    assert len(provider_messages) == 3
    assert isinstance(provider_messages[1][-1], ToolExchange)
    assert "REQUIRED FILE DELIVERY" in provider_messages[2][0]["content"]


def test_required_word_file_rejects_markdown_substitution(monkeypatch):
    files = []

    async def on_file(path, revision):
        files.append((path, revision))

    outcome, _, replies, _ = _run_loop(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "f1",
                        "name": "send_file",
                        "args": {
                            "path": "/workspace/计划.md",
                            "revision": 1,
                            "completion_message": "已经发给你了。",
                        },
                    }
                ],
                "usage": {},
            },
            {"reply": "已经发给你了。", "tool_calls": [], "usage": {}},
            {"reply": "仍未生成。", "tool_calls": [], "usage": {}},
        ],
        on_file_reply=on_file,
        required_file_suffixes=(".docx",),
        max_calls=3,
    )

    assert files == []
    assert replies == [("已经发给你了。", True)]
    assert outcome.stop_reason == "required_file_missing"
    assert outcome.final_text == "已经发给你了。"


def test_required_file_missing_retries_once_then_keeps_original_text(monkeypatch):
    trajectory_events = []
    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {"reply": "这是你的完整睡眠报告。", "tool_calls": [], "usage": {}},
            {"reply": "仍未生成附件。", "tool_calls": [], "usage": {}},
        ],
        required_file_suffixes=(".pdf",),
        max_calls=6,
        trajectory_events=trajectory_events,
    )

    assert len(calls) == 2
    assert replies == [("这是你的完整睡眠报告。", True)]
    assert outcome.final_text == "这是你的完整睡眠报告。"
    assert outcome.stop_reason == "required_file_missing"
    assert [kind for kind, _payload in trajectory_events].count(
        "required_file_missing"
    ) == 1


def test_required_file_retry_provider_error_keeps_original_text(
    monkeypatch,
):
    outcome, calls, replies, _ = _run_loop(
        monkeypatch,
        [
            {"reply": "这是你的完整睡眠报告。", "tool_calls": [], "usage": {}},
            RuntimeError("retry provider unavailable"),
        ],
        required_file_suffixes=(".pdf",),
        max_calls=6,
    )

    assert len(calls) == 2
    assert replies == [("这是你的完整睡眠报告。", True)]
    assert outcome.final_text == "这是你的完整睡眠报告。"
    assert outcome.stop_reason == "required_file_missing"


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

    shared_work = worker._workspace_file_reply_from_result(
        {
            "path": "/workspace/knowledge.io.html",
            "content": "<!doctype html><title>Knowledge</title>",
            "mime_type": "text/markdown",
        },
        title="Knowledge",
        subtitle="Interactive knowledge Canvas",
    )
    assert shared_work.mime_type == "text/html"
    with pytest.raises(ValueError):
        worker._workspace_file_reply_from_result(
            {
                "path": "/workspace/too-large.io.html",
                "content": "x" * (tool_schema.SHARED_WORK_MAX_BYTES + 1),
            },
            title="Too large",
            subtitle="Oversized Canvas",
        )

    with pytest.raises(ValueError):
        worker._workspace_file_reply_from_result(
            {
                "path": "/workspace/large.md",
                "content": "x" * (worker._WORKSPACE_FILE_MAX_BYTES + 1),
            }
        )


@pytest.mark.parametrize(
    ("name", "expected_mime"),
    [
        ("file.md", "text/markdown"),
        ("file.markdown", "text/markdown"),
        ("file.txt", "text/plain"),
        ("file.csv", "text/csv"),
        ("file.tsv", "text/tab-separated-values"),
        ("file.json", "application/json"),
        ("file.xml", "application/xml"),
        ("file.yaml", "application/yaml"),
        ("file.yml", "application/yaml"),
        ("file.html", "text/html"),
        ("file.htm", "text/html"),
        ("file.rtf", "text/rtf"),
        ("file.svg", "image/svg+xml"),
    ],
)
def test_workspace_text_download_uses_filename_mime(name, expected_mime):
    result = worker._workspace_file_reply_from_result(
        {
            "path": f"/workspace/{name}",
            "content": "test content\n",
            # workspace_write stores editable source as Markdown-like text.
            "mime_type": "text/markdown",
        }
    )

    assert result.name == name
    assert result.mime_type == expected_mime
    assert result.data == b"test content\n"


def test_workspace_write_rejects_oversized_shared_work_source():
    error = tool_schema.validate_tool_args(
        "workspace_write",
        {
            "path": "/workspace/too-large.io.html",
            "content": "x" * (tool_schema.SHARED_WORK_MAX_BYTES + 1),
            "expected_revision": 0,
        },
    )
    assert error == "Canvas source exceeds the 256 KB UTF-8 limit"


def test_workspace_canvas_file_result_preserves_display_metadata(monkeypatch):
    result = worker._workspace_file_reply_from_result(
        {
            "path": "/workspace/接星星.io.html",
            "content": "<html><title>接星星</title></html>",
            "mime_type": "text/html",
        },
        title="  接星星  ",
        subtitle="六十秒内   接住尽可能多的星星",
    )

    assert result.display_title == "接星星"
    assert result.display_subtitle == "六十秒内 接住尽可能多的星星"
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda *_args, **_kwargs: ({"id": "a" * 32}, None),
    )
    effect = worker._build_encrypted_file_reply_effect_payload(
        object(), result, effect_id="effect-canvas"
    )
    assert effect["message_extra"]["file_display_title"] == "接星星"
    assert effect["message_extra"]["file_display_subtitle"] == "六十秒内 接住尽可能多的星星"

    with pytest.raises(ValueError, match="title and subtitle"):
        worker._workspace_file_reply_from_result(
            {
                "path": "/workspace/接星星.io.html",
                "content": "<html></html>",
            },
            title="接星星",
        )


@pytest.mark.parametrize(
    ("name", "mime_type", "data"),
    [
        ("修前基线-中文.md", "text/markdown", b"# baseline\n"),
        ("修前基线-中文.docx", document_render.DOCX_MIME, b"PK\x03\x04\xff\x00"),
        ("修前基线-中文.pdf", document_render.PDF_MIME, b"%PDF-1.7\n\xff"),
        (
            "修前基线-中文.io.html",
            "text/html",
            b"<html><body>Canvas baseline</body></html>",
        ),
    ],
)
def test_workspace_file_effect_seals_all_attachment_formats_as_binary(
    monkeypatch,
    name,
    mime_type,
    data,
):
    captured = {}

    def build_envelope(_store, plaintext, **kwargs):
        captured["plaintext"] = plaintext
        captured.update(kwargs)
        return {"id": "b" * 32}, ""

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        build_envelope,
    )
    reply = worker.WorkspaceFileReply(
        path=f"/workspace/{name}",
        name=name,
        mime_type=mime_type,
        data=data,
    )

    effect = worker._build_encrypted_file_reply_effect_payload(
        object(), reply, effect_id=f"effect-{name}"
    )

    assert captured["plaintext"] == reply.data
    assert captured["content_kind"] == "binary"
    assert effect["message_extra"]["file_name"] == name


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
            "file_name": "接星星.io.html",
            "file_display_title": "接星星",
            "file_display_subtitle": "六十秒接星星小游戏",
            "file_mime": "text/html",
            "file_byte_count": 9,
        },
    }
    post_commit = serve_worker._sink_reply_in_transaction(
        "user-1", payload, Connection()
    )
    assert callable(post_commit)
    assert captured["content_type"] == "file"
    assert captured["extra"] == {
        "file_name": "接星星.io.html",
        "file_display_title": "接星星",
        "file_display_subtitle": "六十秒接星星小游戏",
        "file_mime": "text/html",
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
    with pytest.raises(RuntimeError, match="title and subtitle"):
        serve_worker._reply_message_fields(
            {
                "message_extra": {
                    "content_type": "file",
                    "file_name": "接星星.io.html",
                    "file_display_title": "接星星",
                    "file_mime": "text/html",
                    "file_byte_count": 100,
                }
            }
        )


def test_reply_payload_sequence_accepts_image_primary_and_image_followups():
    payload = {
        "envelope": {"id": "a" * 32},
        "message_extra": {
            "content_type": "image",
            "image_mime": "image/png",
            "image_byte_count": 100,
        },
        worker.REPLY_FOLLOWUPS_KEY: [
            {
                "envelope": {"id": "b" * 32},
                "message_extra": {
                    "content_type": "image",
                    "image_mime": "image/webp",
                    "image_byte_count": 200,
                },
            }
        ],
    }
    sequence = serve_worker._reply_payload_sequence(payload)
    assert [serve_worker._reply_message_fields(item)[0] for item in sequence] == [
        "image",
        "image",
    ]


def test_generated_image_effect_uses_plaintext_binary_envelope_when_off(monkeypatch):
    """非加密 V2 生图必须把二进制保存成 body_b64，而不是当 UTF-8 文本。"""

    class Store:
        user_id = "u_v2_plaintext_generated_image"

    monkeypatch.setattr(
        worker.core_envelope,
        "resolve_content_encryption",
        lambda _user_id: "off",
    )
    raw = b"\x89PNG\r\n\x1a\n\x00binary-image"

    payload = worker._build_encrypted_image_reply_effect_payload(
        Store(),
        worker.GeneratedImageReply(
            name="result.png",
            mime_type="image/png",
            data=raw,
        ),
        effect_id="generated-image-effect",
    )

    envelope = payload["envelope"]
    assert base64.b64decode(envelope["body_b64"], validate=True) == raw
    assert envelope["body_size_bytes"] == len(raw)
    assert "body" not in envelope and "body_ct" not in envelope
    assert payload["message_extra"] == {
        "content_type": "image",
        "image_mime": "image/png",
        "image_byte_count": len(raw),
    }


def test_process_job_commits_single_generated_image_without_empty_followups(
    monkeypatch,
):
    user_id = "u_v2_single_generated_image"
    conftest.seed_user(user_id)
    with db.get_pool().connection() as connection:
        connection.execute("DELETE FROM agent_jobs")
        connection.execute(
            "DELETE FROM v2_effect_outbox WHERE user_id=%s", (user_id,)
        )
        connection.execute(
            "DELETE FROM chat_messages WHERE user_id=%s", (user_id,)
        )
    conftest.set_v2_runtime_owner(user_id)

    db.chat_append_strict(
        user_id,
        "image-request",
        1.0,
        {
            "id": "image-request",
            "role": "user",
            "source": "model_api",
            "body_ct": "cipher-request",
            "nonce": "n",
            "K_user": "k",
            "K_enclave": "e",
        },
        5000,
    )
    input_seq = db.chat_seq_for_msg_id(user_id, "image-request")
    assert input_seq is not None

    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    job = jobs_store.claim_next_job("image-worker")
    assert job is not None and job["id"] == job_id

    def build_envelope(
        store,
        plaintext,
        *,
        item_id=None,
        content_kind="text",
    ):
        assert content_kind == "binary"
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

    def apply_effects(uid):
        return effect_outbox.apply_pending_effects(
            uid,
            dispatch=lambda _effect_type, _payload: None,
            dispatch_reply_in_transaction=(
                lambda _effect_type, payload, connection:
                serve_worker._sink_reply_in_transaction(uid, payload, connection)
            ),
        )

    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def direct_loop(**kwargs):
        await kwargs["on_reply"](
            "",
            final=True,
            media=(
                ProviderMedia(
                    mime_type="image/png",
                    data_base64=base64.b64encode(image_bytes).decode("ascii"),
                    name="result.png",
                ),
            ),
        )
        return tool_loop.LoopOutcome(
            final_text="",
            rounds=1,
            stop_reason="final_media",
            replied_intermediate=False,
            delivered_media_count=1,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)

    def read_after_seq(_uid, after_seq):
        if after_seq >= input_seq:
            return []
        return [
            {
                "id": "image-request",
                "seq": input_seq,
                "ts": 1.0,
                "role": "user",
                "content": "生成一张图片",
            }
        ]

    deps = worker.TurnDeps(
        read_messages=lambda _uid: read_after_seq(_uid, 0),
        read_messages_after_seq=read_after_seq,
        resolve_provider=lambda _uid: (_PROVIDER, {}),
        mint_enclave_token=lambda _uid: "rt-image",
        apply_pending_effects=apply_effects,
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_PROVIDER,
            api_key=None,
            runtime_token="rt-image",
        )
    )

    assert status == "completed"
    with db.get_pool().connection() as connection:
        reply = connection.execute(
            "SELECT doc FROM chat_messages WHERE user_id=%s "
            "AND doc->>'role'='openclaw'",
            (user_id,),
        ).fetchone()[0]
        effect = connection.execute(
            "SELECT payload FROM v2_effect_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()[0]
    assert reply["content_type"] == "image"
    assert reply["image_mime"] == "image/png"
    assert base64.b64decode(reply["body_ct"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert worker.REPLY_FOLLOWUPS_KEY not in effect


def test_transactional_image_reply_group_persists_primary_part_metadata(monkeypatch):
    captured = []

    class Store:
        def _build_chat_message(
            self, role, source, envelope, content_type="text", extra=None
        ):
            captured.append((envelope["id"], content_type, dict(extra or {})))
            return {"id": envelope["id"], "ts": float(len(captured))}

        def reload_chat_strict(self):
            pass

        def notify_chat_waiters(self):
            pass

    class Connection:
        def execute(self, *args):
            pass

    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda user_id: Store())
    monkeypatch.setattr(
        serve_worker.db,
        "chat_append_effect_with_cursor",
        lambda *args, **kwargs: (len(captured), False, lambda: None),
    )
    monkeypatch.setattr(
        serve_worker.capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda *args, **kwargs: None,
    )

    payload = {
        "envelope": {"id": "a" * 32},
        "message_extra": {
            "content_type": "image",
            "image_mime": "image/png",
            "image_byte_count": 100,
        },
        "reply_to_message_id": "user-message",
        worker.REPLY_FOLLOWUPS_KEY: [
            {
                "envelope": {"id": "b" * 32},
                "message_extra": {
                    "content_type": "image",
                    "image_mime": "image/webp",
                    "image_byte_count": 200,
                },
            }
        ],
    }
    post_commit = serve_worker._sink_reply_in_transaction(
        "user-1", payload, Connection()
    )

    assert [(kind, extra["reply_part_index"]) for _, kind, extra in captured] == [
        ("image", 0),
        ("image", 1),
    ]
    assert all(extra["reply_part_count"] == 2 for _, _, extra in captured)
    assert all(extra["reply_to_message_id"] == "user-message" for _, _, extra in captured)
    assert callable(post_commit)


def test_reply_image_metadata_rejects_svg_and_oversized_payloads():
    with pytest.raises(RuntimeError, match="mime"):
        serve_worker._reply_message_fields(
            {
                "message_extra": {
                    "content_type": "image",
                    "image_mime": "image/svg+xml",
                    "image_byte_count": 100,
                }
            }
        )
    with pytest.raises(RuntimeError, match="size"):
        serve_worker._reply_message_fields(
            {
                "message_extra": {
                    "content_type": "image",
                    "image_mime": "image/png",
                    "image_byte_count": 2_000_001,
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

    envelope_kinds = []

    def build_envelope(
        store, plaintext, *, item_id=None, content_kind="text"
    ):
        envelope_kinds.append(content_kind)
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
        assert kwargs["required_file_suffixes"] is None
        await kwargs["on_file_reply"]("/workspace/report.md", 4)
        with db.get_pool().connection() as connection:
            visible_before_final = connection.execute(
                "SELECT count(*) FROM chat_messages "
                "WHERE user_id=%s AND doc->>'role'='openclaw'",
                (user_id,),
            ).fetchone()[0]
        assert visible_before_final == 0
        await kwargs["on_reply"](
            "已生成文件，可下载：\n"
            "sandbox:/workspace/report.md\n"
            "需要我把内容贴出来吗？",
            final=True,
        )
        return tool_loop.LoopOutcome(
            final_text="文件已生成，可在下方下载。",
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
    assert envelope_kinds.count("binary") == 1

    store = core_store.get_store(user_id)
    store.reload()
    replies = [
        message
        for message in store.chat_messages
        if message.get("role") == "openclaw"
        and message.get("source") == "model_api"
    ]
    assert [message["content_type"] for message in replies] == ["text", "file"]
    visible_text = base64.b64decode(replies[0]["body_ct"]).decode("utf-8")
    assert visible_text == "文件已生成，可在下方下载。"
    assert "sandbox:" not in visible_text
    assert replies[0]["ts"] < replies[1]["ts"]
    assert replies[0]["reply_to_message_id"] == "m1"
    assert replies[1]["reply_to_message_id"] == "m1"
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
