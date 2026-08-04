"""V1 resident downloadable-file delivery regression coverage."""
from __future__ import annotations

import base64
import io
import os
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
os.environ.setdefault("AGENT_MODE", "cli")
os.environ.setdefault("AGENT_CLI_CMD", "agent {message}")
os.environ.setdefault("CHECKPOINT_FILE", "/tmp/feedling_v1_download_checkpoint.json")

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    fake_encryption = types.ModuleType("content_encryption")
    fake_encryption.build_envelope = lambda **kwargs: {"v": 1, **kwargs}
    sys.modules.setdefault("content_encryption", fake_encryption)

import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import chat_core  # noqa: E402
from core import store as core_store  # noqa: E402
from model_api_runtime.v2 import document_render  # noqa: E402
from tools import chat_resident_consumer as resident  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _envelope(user_id: str, msg_id: str) -> dict:
    return {
        "v": 1,
        "id": msg_id,
        "body_ct": _b64(f"ciphertext:{msg_id}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


@pytest.fixture()
def store(backend_env):
    response = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert response.status_code == 201
    return core_store.get_store(response.get_json()["user_id"])


def _quiet_response_side_effects(monkeypatch):
    monkeypatch.setattr(
        chat_core.chat_consumer, "_record_consumer_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        chat_core.debug_trace, "trace_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(chat_core, "_maybe_mark_first_chat_ok", lambda *args: None)
    monkeypatch.setattr(core_store.wake_bus, "notify", lambda *args: None)
    from proactive import capture_scheduler

    monkeypatch.setattr(capture_scheduler, "record_chat_append", lambda *args: {})


def test_v1_text_and_file_followup_commit_as_one_ordered_reply(
    store, monkeypatch
):
    _quiet_response_side_effects(monkeypatch)
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "v1_file_parent")
    )

    body, status = chat_core.write_response(
        store,
        {
            "envelope": _envelope(store.user_id, "v1_file_primary"),
            "reply_to_message_id": parent["id"],
            "file_followups": [
                {
                    "envelope": _envelope(store.user_id, "v1_file_card"),
                    "file_name": "一日计划.docx",
                    "file_mime": document_render.DOCX_MIME,
                    "file_byte_count": 1234,
                }
            ],
        },
        consumer_id="resident-v1",
        consumer_info={},
        allow_verify_reply=False,
    )

    assert status == 200
    assert body["id"] == "v1_file_primary"
    rows = db.chat_load(store.user_id)
    persisted_parent = next(row for row in rows if row["id"] == parent["id"])
    primary = next(row for row in rows if row["id"] == "v1_file_primary")
    card = next(row for row in rows if row["id"] == "v1_file_card")
    assert persisted_parent["reply_message_id"] == primary["id"]
    assert primary["content_type"] == "text"
    assert primary["reply_to_message_id"] == parent["id"]
    assert card["content_type"] == "file"
    assert card["file_name"] == "一日计划.docx"
    assert card["file_mime"] == document_render.DOCX_MIME
    assert card["file_byte_count"] == 1234
    assert card["reply_to_message_id"] == parent["id"]
    assert float(card["ts"]) > float(primary["ts"])


def test_v1_text_and_images_commit_as_one_ordered_reply(store, monkeypatch):
    _quiet_response_side_effects(monkeypatch)
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "v1_image_parent")
    )

    body, status = chat_core.write_response(
        store,
        {
            "envelope": _envelope(store.user_id, "v1_image_primary"),
            "reply_to_message_id": parent["id"],
            "image_followups": [
                {
                    "envelope": _envelope(store.user_id, "v1_image_one"),
                    "image_mime": "image/png",
                    "image_byte_count": 123,
                },
                {
                    "envelope": _envelope(store.user_id, "v1_image_two"),
                    "image_mime": "image/webp",
                    "image_byte_count": 456,
                },
            ],
        },
        consumer_id="resident-v1",
        consumer_info={},
        allow_verify_reply=False,
    )

    assert status == 200
    assert body["id"] == "v1_image_primary"
    rows = {row["id"]: row for row in db.chat_load(store.user_id)}
    assert rows["v1_image_primary"]["reply_part_index"] == 0
    assert rows["v1_image_primary"]["reply_part_count"] == 3
    assert rows["v1_image_one"]["content_type"] == "image"
    assert rows["v1_image_one"]["reply_part_index"] == 1
    assert rows["v1_image_two"]["reply_part_index"] == 2
    assert rows["v1_image_two"]["reply_to_message_id"] == parent["id"]


def _png_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (48, 32), (20, 40, 60)).save(out, format="PNG")
    return out.getvalue()


def test_resident_stages_generated_image_inside_private_outbox(tmp_path, monkeypatch):
    outbox = tmp_path / "outbound-files"
    monkeypatch.setattr(resident, "OUTBOUND_FILE_DIR", outbox)
    resident._begin_outbound_file_turn("turn-image", None)
    source = outbox / "model-result.bin"
    source.write_bytes(_png_bytes())

    reply = resident._handle_stage_image_ipc(
        {
            "op": "stage_image",
            "request_id": "request-image",
            "path": str(source),
            "name": "结果图.png",
        }
    )

    assert reply["ok"] is True
    files, images = resident._finish_outbound_attachment_turn("turn-image")
    assert files == []
    assert len(images) == 1
    assert images[0].name == "结果图.png"
    assert images[0].mime_type == "image/png"
    assert not source.exists()


def test_resident_rejects_generated_image_outside_private_outbox(tmp_path, monkeypatch):
    outbox = tmp_path / "outbound-files"
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    monkeypatch.setattr(resident, "OUTBOUND_FILE_DIR", outbox)
    resident._begin_outbound_file_turn("turn-image-escape", None)

    reply = resident._handle_stage_image_ipc(
        {
            "op": "stage_image",
            "request_id": "request-image-escape",
            "path": str(outside),
        }
    )

    assert reply["ok"] is False
    assert reply["error"] == "path_outside_outbound_dir"
    resident._finish_outbound_attachment_turn("turn-image-escape")


def test_v1_text_file_and_confirmed_memory_activity_commit_together(
    store, monkeypatch
):
    _quiet_response_side_effects(monkeypatch)
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "v1_memory_file_parent")
    )
    monkeypatch.setattr(
        chat_core.chat_activity_store,
        "resident_activity_rows",
        lambda *_args: [
            {
                "id": 1,
                "job_id": None,
                "kind": "tool_activity",
                "created_at": 10.0,
                "detail_json": {
                    "activity_id": "v1:memory-call-1",
                    "tool_name": "memory_search",
                    "call_id": "v1:memory-call-1",
                    "state": "success",
                    "memory_count": 4,
                    "memory_categories": [
                        {"key": "relationship", "count": 3},
                        {"key": "family", "count": 1},
                    ],
                },
            }
        ],
    )

    body, status = chat_core.write_response(
        store,
        {
            "envelope": _envelope(store.user_id, "v1_memory_file_primary"),
            "reply_to_message_id": parent["id"],
            "file_followups": [
                {
                    "envelope": _envelope(store.user_id, "v1_memory_file_card"),
                    "file_name": "我们的关系小档案.pdf",
                    "file_mime": document_render.PDF_MIME,
                    "file_byte_count": 2048,
                }
            ],
        },
        consumer_id="resident-v1",
        consumer_info={},
        allow_verify_reply=False,
    )

    assert status == 200, body
    rows = db.chat_load(store.user_id)
    primary = next(row for row in rows if row["id"] == "v1_memory_file_primary")
    card = next(row for row in rows if row["id"] == "v1_memory_file_card")
    assert primary["reply_to_message_id"] == parent["id"]
    assert primary["activity_events"] == [
        {
            "id": "v1:memory-call-1",
            "kind": "tool",
            "name": "memory_search",
            "status": "success",
            "job_id": "",
            "call_id": "v1:memory-call-1",
            "started_at": 10.0,
            "finished_at": 10.0,
            "memory_count": 4,
            "memory_categories": [
                {"key": "relationship", "count": 3},
                {"key": "family", "count": 1},
            ],
        }
    ]
    assert card["reply_to_message_id"] == parent["id"]
    assert card["file_name"] == "我们的关系小档案.pdf"


def test_v1_file_sequence_rolls_back_if_followup_insert_fails(store, monkeypatch):
    _quiet_response_side_effects(monkeypatch)
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "v1_atomic_parent")
    )
    store.append_chat(
        "openclaw", "chat", _envelope(store.user_id, "v1_collision")
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        chat_core.write_response(
            store,
            {
                "envelope": _envelope(store.user_id, "v1_rolled_back_primary"),
                "reply_to_message_id": parent["id"],
                "file_followups": [
                    {
                        "envelope": _envelope(store.user_id, "v1_collision"),
                        "file_name": "plan.pdf",
                        "file_mime": document_render.PDF_MIME,
                        "file_byte_count": 100,
                    }
                ],
            },
            consumer_id="resident-v1",
            consumer_info={},
            allow_verify_reply=False,
        )

    persisted_parent = next(
        row for row in db.chat_load(store.user_id) if row["id"] == parent["id"]
    )
    assert persisted_parent.get("reply_message_id") in (None, "")
    assert persisted_parent.get("reply_status") != "replied"
    assert not any(
        row["id"] == "v1_rolled_back_primary" for row in db.chat_load(store.user_id)
    )


def test_v1_competing_reply_sequences_store_only_the_winner(store, monkeypatch):
    _quiet_response_side_effects(monkeypatch)
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "v1_sequence_race_parent")
    )

    def post(label: str):
        return chat_core.write_response(
            store,
            {
                "envelope": _envelope(store.user_id, f"v1_primary_{label}"),
                "reply_to_message_id": parent["id"],
                "file_followups": [
                    {
                        "envelope": _envelope(store.user_id, f"v1_card_{label}"),
                        "file_name": f"plan-{label}.pdf",
                        "file_mime": document_render.PDF_MIME,
                        "file_byte_count": 100,
                    }
                ],
            },
            consumer_id=f"resident-{label}",
            consumer_info={},
            allow_verify_reply=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(post, ("a", "b")))

    assert sorted(status for _body, status in results) == [200, 409]
    rows = db.chat_load(store.user_id)
    stored_reply_ids = {
        row["id"]
        for row in rows
        if row["id"].startswith(("v1_primary_", "v1_card_"))
    }
    assert stored_reply_ids in (
        {"v1_primary_a", "v1_card_a"},
        {"v1_primary_b", "v1_card_b"},
    )


def test_resident_stages_and_renders_real_docx(tmp_path, monkeypatch):
    outbox = tmp_path / "outbound-files"
    monkeypatch.setattr(resident, "OUTBOUND_FILE_DIR", outbox)
    resident._begin_outbound_file_turn("turn-docx", (".docx",))
    source = outbox / "source.md"
    source.write_text("# 一日计划\n\n- 早餐\n- 训练", encoding="utf-8")

    reply = resident._handle_stage_file_ipc(
        {
            "op": "stage_file",
            "request_id": "request-docx",
            "path": str(source),
            "name": "一日计划.docx",
        }
    )

    assert reply["ok"] is True
    staged = resident._finish_outbound_file_turn("turn-docx")
    assert len(staged) == 1
    assert staged[0].name == "一日计划.docx"
    assert staged[0].mime_type == document_render.DOCX_MIME
    assert staged[0].data.startswith(b"PK")
    assert not source.exists()


def test_resident_stages_and_renders_real_pdf(tmp_path, monkeypatch):
    outbox = tmp_path / "outbound-files"
    monkeypatch.setattr(resident, "OUTBOUND_FILE_DIR", outbox)
    resident._begin_outbound_file_turn("turn-pdf", (".pdf",))
    source = outbox / "source.md"
    source.write_text("# 一日计划\n\n- 早餐\n- 训练", encoding="utf-8")

    reply = resident._handle_stage_file_ipc(
        {
            "op": "stage_file",
            "request_id": "request-pdf",
            "path": str(source),
            "name": "一日计划.pdf",
        }
    )

    assert reply["ok"] is True
    staged = resident._finish_outbound_file_turn("turn-pdf")
    assert len(staged) == 1
    assert staged[0].name == "一日计划.pdf"
    assert staged[0].mime_type == document_render.PDF_MIME
    assert staged[0].data.startswith(b"%PDF-")
    assert not source.exists()


@pytest.mark.parametrize("source", ["chat", "model_api"])
def test_file_only_cli_result_is_success_not_parse_failure(
    tmp_path, monkeypatch, source
):
    outbox = tmp_path / "outbound-files"
    monkeypatch.setattr(resident, "OUTBOUND_FILE_DIR", outbox)
    monkeypatch.setattr(resident, "AGENT_MODE", "cli")
    monkeypatch.setattr(resident, "_resident_chat_runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(
        resident, "_prepend_io_cli_capability_catalog", lambda content: content
    )
    monkeypatch.setattr(resident, "_commit_io_cli_catalog_injection", lambda: None)
    monkeypatch.setattr(
        resident,
        "_foreground_agent_message",
        lambda content, current_ts: content,
    )
    parse_results = iter((False, True))
    monkeypatch.setattr(
        resident, "_consume_reply_parse_failed", lambda: next(parse_results)
    )
    posted = {}

    def fake_agent(message, **kwargs):
        source = outbox / "source.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# 一日计划\n\n- 早餐\n- 训练", encoding="utf-8")
        staged = resident._handle_stage_file_ipc(
            {
                "op": "stage_file",
                "request_id": "request-file-only",
                "path": str(source),
                "name": "一日计划.docx",
            }
        )
        assert staged["ok"] is True
        return {"messages": []}

    def fake_post(reply, **kwargs):
        posted["reply"] = reply
        posted["kwargs"] = kwargs
        return {"id": "reply-file-only"}

    monkeypatch.setattr(resident, "call_agent", fake_agent)
    monkeypatch.setattr(resident, "post_reply", fake_post)
    mock_notify = []
    monkeypatch.setattr(
        resident,
        "_notify_agent_turn_failure",
        lambda *args, **kwargs: mock_notify.append((args, kwargs)),
    )
    successes = []
    monkeypatch.setattr(
        resident, "_note_agent_turn_success", lambda: successes.append(True)
    )
    resident._seen_ids.clear()
    resident._seen_ids_order.clear()

    result = resident._process_messages(
        [
            {
                "id": "user-file-only",
                "role": "user",
                "source": source,
                "content": "给我生成一个一日计划 Word 文档。",
                "ts": 1234.0,
            }
        ]
    )

    assert result == pytest.approx(1234.0)
    assert posted["reply"] == "文件已经准备好了。"
    assert len(posted["kwargs"]["file_followups"]) == 1
    assert not any(key.startswith("turn_failure_") for key in posted["kwargs"])
    assert mock_notify == []
    assert successes == [True]


def test_resident_rejects_markdown_for_required_word(tmp_path, monkeypatch):
    outbox = tmp_path / "outbound-files"
    monkeypatch.setattr(resident, "OUTBOUND_FILE_DIR", outbox)
    resident._begin_outbound_file_turn("turn-word", (".docx",))
    source = outbox / "source.md"
    source.write_text("# Plan", encoding="utf-8")

    reply = resident._handle_stage_file_ipc(
        {
            "op": "stage_file",
            "request_id": "request-md",
            "path": str(source),
            "name": "plan.md",
        }
    )

    assert reply["ok"] is False
    assert reply["error"] == "wrong_file_suffix"
    assert resident._finish_outbound_file_turn("turn-word") == []


def test_v1_completion_guard_matches_natural_word_request():
    requirement = resident._required_outbound_file_suffixes(
        "我今天想让你安排一日三餐和健身计划，给我一个 Word 文档。"
    )
    assert requirement == (".docx",)
    assert resident._missing_outbound_file_suffixes(requirement, []) == (".docx",)


@pytest.mark.parametrize(
    "text",
    [
        "给我一份下周的工作清单",
        "can you make a plan for my week?",
        "give me a report on how I slept",
        "help me make a checklist for packing",
        "给我一个报告",
    ],
)
def test_v1_completion_guard_ignores_conversational_requests(text):
    assert resident._required_outbound_file_suffixes(text) is None


def _patch_v1_foreground_file_turn(monkeypatch):
    monkeypatch.setattr(resident, "AGENT_MODE", "cli")
    monkeypatch.setattr(resident, "_resident_chat_runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(
        resident, "_prepend_io_cli_capability_catalog", lambda content: content
    )
    monkeypatch.setattr(resident, "_commit_io_cli_catalog_injection", lambda: None)
    monkeypatch.setattr(
        resident,
        "_foreground_agent_message",
        lambda content, current_ts: content,
    )
    monkeypatch.setattr(resident, "_consume_reply_parse_failed", lambda: False)
    monkeypatch.setattr(resident, "_note_agent_turn_success", lambda: None)
    monkeypatch.setattr(resident, "_worldbook_context_for_foreground", lambda _text: "")
    resident._seen_ids.clear()
    resident._seen_ids_order.clear()


def test_v1_conversational_request_does_not_spend_file_retry(monkeypatch):
    _patch_v1_foreground_file_turn(monkeypatch)
    calls = []
    posted = {}

    def fake_agent(message, **kwargs):
        calls.append((message, kwargs))
        return {"messages": ["这是你的下周工作清单。"]}

    def fake_post(reply, **kwargs):
        posted["reply"] = reply
        posted["kwargs"] = kwargs
        return {"id": "reply-v1-conversation"}

    monkeypatch.setattr(resident, "call_agent", fake_agent)
    monkeypatch.setattr(resident, "post_reply", fake_post)

    result = resident._process_messages(
        [
            {
                "id": "user-v1-conversation",
                "role": "user",
                "source": "chat",
                "content": "给我一份下周的工作清单",
                "ts": 1235.0,
            }
        ]
    )

    assert result == pytest.approx(1235.0)
    assert len(calls) == 1
    assert posted["reply"] == "这是你的下周工作清单。"
    assert posted["kwargs"].get("file_followups") in (None, [])


def test_v1_missing_explicit_file_keeps_original_model_reply(monkeypatch):
    _patch_v1_foreground_file_turn(monkeypatch)
    replies = iter(
        (
            {"messages": ["这是你的完整睡眠报告。"]},
            {"messages": ["仍未生成附件。"]},
        )
    )
    calls = []
    posted = {}
    trace_types = []

    def fake_agent(message, **kwargs):
        calls.append((message, kwargs))
        return next(replies)

    def fake_post(reply, **kwargs):
        posted["reply"] = reply
        posted["kwargs"] = kwargs
        return {"id": "reply-v1-missing-file"}

    monkeypatch.setattr(resident, "call_agent", fake_agent)
    monkeypatch.setattr(resident, "post_reply", fake_post)
    monkeypatch.setattr(
        resident,
        "_emit_debug_trace",
        lambda _subsystem, event_type, **_kwargs: trace_types.append(event_type),
    )

    result = resident._process_messages(
        [
            {
                "id": "user-v1-missing-file",
                "role": "user",
                "source": "chat",
                "content": "给我一份 PDF 睡眠报告",
                "ts": 1236.0,
            }
        ]
    )

    assert result == pytest.approx(1236.0)
    assert len(calls) == 2
    assert posted["reply"] == "这是你的完整睡眠报告。"
    assert posted["kwargs"].get("file_followups") in (None, [])
    assert trace_types.count("required_file_missing") == 1


def test_v1_file_retry_error_keeps_original_reply_without_failure_notice(monkeypatch):
    _patch_v1_foreground_file_turn(monkeypatch)
    calls = []
    posted = {}
    notices = []

    def fake_agent(message, **kwargs):
        calls.append((message, kwargs))
        if len(calls) == 1:
            return {"messages": ["这是你的完整睡眠报告。"]}
        raise RuntimeError("retry provider unavailable")

    def fake_post(reply, **kwargs):
        posted["reply"] = reply
        posted["kwargs"] = kwargs
        return {"id": "reply-v1-retry-error"}

    monkeypatch.setattr(resident, "call_agent", fake_agent)
    monkeypatch.setattr(resident, "post_reply", fake_post)
    monkeypatch.setattr(
        resident,
        "_notify_agent_turn_failure",
        lambda *args, **kwargs: notices.append((args, kwargs)),
    )

    result = resident._process_messages(
        [
            {
                "id": "user-v1-retry-error",
                "role": "user",
                "source": "chat",
                "content": "给我一份 PDF 睡眠报告",
                "ts": 1237.0,
            }
        ]
    )

    assert result == pytest.approx(1237.0)
    assert len(calls) == 2
    assert posted["reply"] == "这是你的完整睡眠报告。"
    assert posted["kwargs"].get("file_followups") in (None, [])
    assert notices == []


def test_v1_visible_reply_strips_codex_local_file_citation():
    cleaned, removed = resident._sanitize_outbound_file_reply(
        '已生成并附上 Word 文档：:codex-file-citation{path="/private/tmp/secret/plan.docx" purpose="output"}\n\n'
        "内容已经准备好。"
    )

    assert removed is True
    assert cleaned == "已生成并附上 Word 文档。\n内容已经准备好。"
    assert "/private/tmp" not in cleaned
    assert "codex-file-citation" not in cleaned


def test_v1_staged_file_replaces_pi_sandbox_path_with_download_copy():
    cleaned, removed = resident._sanitize_outbound_file_reply(
        "已生成文件，可下载：\n"
        "sandbox:/Users/example/Library/Application%20Support/IO/outbound-files/report.md\n"
        "需要我把内容贴出来吗？",
        attachment_staged=True,
    )

    assert removed is True
    assert cleaned == "文件已生成，可在下方下载。"
    assert "sandbox:" not in cleaned
    assert "/Users/" not in cleaned


def test_v1_unstaged_internal_path_is_detected_for_failure_handling():
    cleaned, removed = resident._sanitize_outbound_file_reply(
        "Download: file:///private/tmp/report.pdf"
    )

    assert removed is True
    assert "file://" not in cleaned
    assert "/private/tmp" not in cleaned


def test_v1_outbound_prompt_limits_expensive_document_qa():
    prompt = resident._outbound_file_prompt_block()

    assert "at most one lightweight check" in prompt
    assert "do not repeatedly render" in prompt


def test_v1_memory_read_prompt_requires_index_then_fetch():
    prompt = resident._memory_read_prompt_block()

    assert "memory-index" in prompt
    assert "items[].id" in prompt
    assert "memory-fetch" in prompt
    assert f"python {resident._IO_CLI_PATH} memory-index" in prompt
    assert f"python {resident._IO_CLI_PATH} memory-fetch" in prompt
    assert "Never claim memories are unavailable" in prompt


def test_foreground_agent_call_keeps_resident_fresh_without_claiming(monkeypatch):
    heartbeat_seen = threading.Event()
    polls = []
    refreshes = []

    def fake_poll(since, timeout=None, claim=True):
        polls.append((since, timeout, claim))
        heartbeat_seen.set()
        return {"messages": []}

    def fake_agent(_message, images=None, raw_text=False):
        assert heartbeat_seen.wait(1.0)
        return "ok"

    monkeypatch.setattr(resident, "AGENT_MODE", "http")
    monkeypatch.setattr(resident, "RESIDENT_BUSY_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(resident, "poll_chat", fake_poll)
    monkeypatch.setattr(
        resident, "_maybe_refresh_decrypt_health", lambda: refreshes.append(True)
    )
    monkeypatch.setattr(resident, "call_agent_http", fake_agent)

    assert resident.call_agent("hello", raw_text=True, lane="chat") == "ok"
    assert polls
    assert all(timeout == 0 and claim is False for _, timeout, claim in polls)
    assert refreshes

    count_after_return = len(polls)
    time.sleep(0.03)
    assert len(polls) == count_after_return


def test_background_agent_call_does_not_start_busy_poll(monkeypatch):
    polls = []

    def fake_agent(_message, images=None, raw_text=False):
        time.sleep(0.03)
        return "ok"

    monkeypatch.setattr(resident, "AGENT_MODE", "http")
    monkeypatch.setattr(resident, "RESIDENT_BUSY_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(
        resident,
        "poll_chat",
        lambda since, timeout=None, claim=True: polls.append(
            (since, timeout, claim)
        ),
    )
    monkeypatch.setattr(resident, "call_agent_http", fake_agent)

    assert resident.call_agent("background", raw_text=True) == "ok"
    assert polls == []


def test_resident_posts_primary_and_encrypted_file_followup_together(monkeypatch):
    sealed_plaintexts = []
    posted = {}

    def fake_build_envelope(**kwargs):
        plaintext = bytes(kwargs["plaintext"])
        sealed_plaintexts.append(plaintext)
        marker = f"sealed-{len(sealed_plaintexts)}"
        return {
            "v": 1,
            "id": marker,
            "body_ct": _b64(plaintext),
            "nonce": _b64(b"\x00" * 12),
            "K_user": _b64(b"\x01" * 32),
            "K_enclave": _b64(b"\x02" * 32),
            "visibility": "shared",
            "owner_user_id": "user-v1",
        }

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "sealed-1", "ts": 1.0, "v": 1}

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(_url, *, json, headers, timeout):
        posted.update(json)
        return Response()

    monkeypatch.setattr(resident, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(
        resident,
        "_whoami_cache",
        {
            "user_id": "user-v1",
            "user_pk": b"u" * 32,
            "enclave_pk": b"e" * 32,
        },
    )
    monkeypatch.setattr(
        resident, "_refresh_whoami_for_encrypted_reply", lambda: True
    )
    monkeypatch.setattr(resident, "_build_envelope", fake_build_envelope)
    monkeypatch.setattr(resident._HTTP, "post", fake_post)

    result = resident.post_reply(
        "文件已准备好。",
        reply_to_message_id="parent-v1",
        file_followups=[
            resident.StagedChatFile(
                source_path="/tmp/source.md",
                name="计划.pdf",
                mime_type=document_render.PDF_MIME,
                data=b"%PDF-real-bytes",
            )
        ],
    )

    assert result["id"] == "sealed-1"
    assert sealed_plaintexts == [
        "文件已准备好。".encode("utf-8"),
        b"%PDF-real-bytes",
    ]
    assert posted["reply_to_message_id"] == "parent-v1"
    assert len(posted["file_followups"]) == 1
    followup = posted["file_followups"][0]
    assert followup["file_name"] == "计划.pdf"
    assert followup["file_mime"] == document_render.PDF_MIME
    assert followup["file_byte_count"] == len(b"%PDF-real-bytes")
