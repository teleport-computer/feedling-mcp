from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import plaintext, service, worker  # noqa: E402
from memory import memory_core  # noqa: E402


class _ProfileLLM:
    def __init__(self, replies: list[str]):
        self.replies = iter(replies)
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            text=next(self.replies),
            usage={},
            cached=False,
            output_ref=kwargs["task_id"],
        )


def _profile_reply(memory: str, style: str) -> str:
    return json.dumps({"memory": memory, "style": style}, ensure_ascii=False)


def test_genesis_profile_uses_v2_prompt_and_bounces_required_empty_once():
    llm = _ProfileLLM([
        _profile_reply("", "短句接住情绪"),
        _profile_reply("用户养了一只狗", "短句接住情绪"),
    ])
    output = {
        "persona": {"content": "## 你是谁\n你叫 Mira。"},
        "voice_workset": {"behavior_notes": ["短句"], "exemplars": []},
    }

    result = worker.build_profile_output_from_sources(
        user_id="u1",
        job_id="j1",
        runtime=object(),
        rendered_cards="- id=m1 | summary=养狗 | content=狗叫蛋子",
        memory_material=True,
        output=output,
        llm=llm,
    )

    assert len(llm.calls) == 2
    first_messages = llm.calls[0]["messages"]
    assert first_messages == worker.v2_profile.build_profile_prompt(
        "- id=m1 | summary=养狗 | content=狗叫蛋子\n"
        "<UNTRUSTED_GENESIS_STYLE_SOURCE>\n"
        '{"behavior_notes":["短句"],"exemplars":[],"persona":"## 你是谁\\n你叫 Mira。"}'
        "\n</UNTRUSTED_GENESIS_STYLE_SOURCE>"
    )
    assert llm.calls[1]["messages"][-1]["role"] == "user"
    assert "MEMORY 字段为空" in llm.calls[1]["messages"][-1]["content"]
    assert result["profile"] == {
        "memory": "用户养了一只狗",
        "style": "短句接住情绪",
        "memory_touched": True,
        "style_touched": True,
        "provider_calls": 2,
    }


def test_genesis_profile_allows_empty_untouched_side():
    llm = _ProfileLLM([_profile_reply("", "保持直接")])
    result = worker.build_profile_output_from_sources(
        user_id="u1",
        job_id="j2",
        runtime=object(),
        rendered_cards="",
        memory_material=False,
        output={"persona": {"content": "保持直接"}},
        llm=llm,
    )

    assert len(llm.calls) == 1
    assert result["profile"]["memory"] == ""
    assert result["profile"]["memory_touched"] is False
    assert result["profile"]["style_touched"] is True


def test_genesis_profile_no_material_skips_provider():
    llm = _ProfileLLM([])
    result = worker.build_profile_output_from_sources(
        user_id="u1",
        job_id="j3",
        runtime=object(),
        rendered_cards="",
        memory_material=False,
        output={},
        llm=llm,
    )

    assert llm.calls == []
    assert result["profile"]["memory_touched"] is False
    assert result["profile"]["style_touched"] is False


def test_genesis_profile_source_uses_only_current_valid_proposals(monkeypatch):
    store = types.SimpleNamespace(user_id="u1")
    monkeypatch.setattr(
        memory_core,
        "index",
        lambda *_args, **_kwargs: pytest.fail("Genesis must not read the Garden"),
    )
    monkeypatch.setattr(
        service.memory_actions,
        "trace_memory_content_truncation",
        lambda *_args, **_kwargs: None,
    )

    captured = {}
    monkeypatch.setattr(
        plaintext.worker,
        "build_profile_output_from_sources",
        lambda **kwargs: captured.update(kwargs) or {"profile": {}},
    )
    output = {
        "memories": [
            {"summary": "新卡", "content": "新正文"},
            {"content": "没有摘要会被丢弃"},
            {"summary": "analysis to=functions.memory_write", "content": "协议残片"},
        ]
    }

    plaintext._attach_plaintext_profile(
        store,
        "key",
        "j-source",
        runtime=object(),
        output=output,
        key_prefix="j-source:profile",
        llm=None,
    )

    rendered = captured["rendered_cards"]
    assert captured["memory_material"] is True
    assert "新正文" in rendered
    assert "没有摘要会被丢弃" not in rendered
    assert "协议残片" not in rendered


def _stored_profile() -> dict:
    return {
        "v": 1,
        "state": "ok",
        "source": {"card_count": 1, "max_updated_at": "old", "generated_at": "old"},
        "last_attempt": {"at": "old", "reject_code": "", "attempts": 1},
        "disabled": False,
        "memory": {"envelope": {"body_ct": "m", "id": "memory"}, "chars": 10},
        "style": {"envelope": {"body_ct": "s", "id": "style"}, "chars": 9},
    }


@pytest.mark.parametrize(
    (
        "memory_touched",
        "style_touched",
        "memory_output",
        "style_output",
        "expected_memory_text",
        "expected_style_text",
    ),
    [
        (True, False, "new-memory", "ignored-style", "new-memory", None),
        (False, True, "ignored-memory", "new-style", None, "new-style"),
    ],
)
def test_profile_cas_preserves_untouched_side_at_final_write(
    monkeypatch,
    memory_touched,
    style_touched,
    memory_output,
    style_output,
    expected_memory_text,
    expected_style_text,
):
    store = types.SimpleNamespace(user_id="u1")
    captured: dict = {}
    source_stats = iter([(4, "first"), (5, "latest")])
    monkeypatch.setattr(
        service.db,
        "memory_profile_source_stats",
        lambda _uid: next(source_stats),
    )
    monkeypatch.setattr(
        service.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: pytest.fail("untouched profile side must not decrypt"),
    )

    def fake_build(_uid, **kwargs):
        captured.update(kwargs)
        return {"candidate": True}

    def fake_cas(_uid, recompute, **_kwargs):
        recompute(_stored_profile())
        document = recompute(_stored_profile())
        return types.SimpleNamespace(status="written", document=document)

    monkeypatch.setattr(
        service.v2_profile_store,
        "build_profile_document_patching_fields",
        fake_build,
    )
    monkeypatch.setattr(service.v2_profile_store, "update_profile_cas", fake_cas)

    ref, digest, status = service.write_profile_artifact(
        store,
        "j4",
        {"profile": {
            "memory": memory_output,
            "style": style_output,
            "memory_touched": memory_touched,
            "style_touched": style_touched,
            "provider_calls": 1,
        }},
        "key",
    )

    assert (ref, status) == ("user_blob:v2_agent_profile", "written")
    assert digest
    assert captured["memory_text"] == expected_memory_text
    assert captured["style_text"] == expected_style_text
    assert captured["previous"] == _stored_profile()
    assert captured["source"]["card_count"] == 5
    assert captured["source"]["max_updated_at"] == "latest"


@pytest.mark.parametrize(
    ("memory_text", "style_text", "preserved_field"),
    [
        ("new-memory", None, "style"),
        (None, "new-style", "memory"),
    ],
)
def test_profile_patch_builder_copies_untouched_envelope_byte_for_byte(
    memory_text,
    style_text,
    preserved_field,
):
    previous = _stored_profile()
    document = service.v2_profile_store.build_profile_document_patching_fields(
        "u1",
        state="ok",
        source={"card_count": 2, "max_updated_at": "new", "generated_at": "new"},
        last_attempt={"at": "new", "reject_code": "", "attempts": 1},
        memory_text=memory_text,
        style_text=style_text,
        previous=previous,
        seal_text=lambda _uid, text: {"body_ct": f"sealed:{text}"},
    )

    assert document[preserved_field] == previous[preserved_field]
    touched_field = "memory" if memory_text is not None else "style"
    touched_text = memory_text if memory_text is not None else style_text
    assert document[touched_field]["envelope"] == {"body_ct": f"sealed:{touched_text}"}


def test_profile_patch_builder_preserves_disabled_state_by_default():
    previous = _stored_profile()
    previous["disabled"] = True

    document = service.v2_profile_store.build_profile_document_patching_fields(
        "u1",
        state="ok",
        source={"card_count": 2, "max_updated_at": "new", "generated_at": "new"},
        last_attempt={"at": "new", "reject_code": "", "attempts": 1},
        memory_text="new-memory",
        style_text=None,
        previous=previous,
        seal_text=lambda _uid, text: {"body_ct": f"sealed:{text}"},
    )

    assert document["disabled"] is True
