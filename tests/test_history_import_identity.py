"""Pure-unit tests for history-import identity normalization
(hosted/history_import.py _normalize_identity_payload).

P2 distills the companion's VOICE — not just facts — at import time:
tone_style / agent_role / do_not_say / boundaries. These must round-trip through
normalization (sanitized, empties dropped) so they reach the encrypted identity
body and, via P1a, the hosted chat prompt. No Flask, no DB — history_import
imports cleanly with backend on sys.path.

Run:  python -m pytest tests/test_history_import_identity.py -q
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import history_import as hi  # noqa: E402


def _valid_dimensions():
    return [{"name": f"d{i}", "value": 50 + i, "description": "x"} for i in range(7)]


def _raw(**extra):
    base = {
        "agent_name": "Kai",
        "self_introduction": "I help you write and I keep you honest.",
        "category": "Sharp",
        "signature": ["always direct", "never coddles"],
        "dimensions": _valid_dimensions(),
    }
    base.update(extra)
    return base


def test_normalize_keeps_persona_voice_fields():
    out = hi._normalize_identity_payload(
        _raw(
            tone_style="Terse. Calls you boss. Drops 哈 at the end.",
            agent_role="coding partner",
            do_not_say=["亲爱的", "宝贝"],
            boundaries=["no fake praise"],
        ),
        [],
        10,
        "en",
    )
    assert out["tone_style"].startswith("Terse")
    assert out["agent_role"] == "coding partner"
    assert out["do_not_say"] == ["亲爱的", "宝贝"]
    assert out["boundaries"] == ["no fake praise"]


def test_normalize_drops_empty_persona_fields():
    out = hi._normalize_identity_payload(
        _raw(tone_style="", agent_role="   ", do_not_say=[], boundaries="not-a-list"),
        [],
        10,
        "en",
    )
    assert "tone_style" not in out
    assert "agent_role" not in out
    assert "do_not_say" not in out
    assert "boundaries" not in out


def test_normalize_cleans_blank_list_items():
    out = hi._normalize_identity_payload(
        _raw(do_not_say=["老板", "", "   ", "boss"]),
        [],
        10,
        "en",
    )
    assert out["do_not_say"] == ["老板", "boss"]


def test_wrapped_chat_history_plaintext_parses_ios_markdown_export():
    wrapped = (
        "===== BEGIN CHAT HISTORY FILE: chat.md =====\n"
        "[2026-06-01 09:00] Z: 阿樟，帮我看一下 onboarding。\n"
        "[2026-06-01 09:01] 阿樟: 好，我先看材料再看 job。\n"
        "===== END CHAT HISTORY FILE: chat.md ====="
    )
    warnings = []

    messages = hi._parse_import_history_content(wrapped, "auto", warnings)

    assert len(messages) == 2
    assert messages[0]["source_filename"] == "chat.md"
    assert all(m["source"] == "history_import" for m in messages)
    assert all(m["ts"] is not None for m in messages)
    assert "wrapped_json_parse_failed" not in " ".join(warnings)


def test_wrapped_json_filename_falls_back_to_plaintext_when_body_is_not_json():
    wrapped = (
        "===== BEGIN CHAT HISTORY FILE: conversations.json =====\n"
        "[2026-06-01 09:00] Seven: 以后叫我 Seven。\n"
        "[2026-06-01 09:01] 阿樟: 好，我记住了。\n"
        "===== END CHAT HISTORY FILE: conversations.json ====="
    )
    warnings = []

    messages = hi._parse_import_history_content(wrapped, "auto", warnings)

    assert len(messages) == 2
    assert [message["content"] for message in messages] == [
        "以后叫我 Seven。",
        "好，我记住了。",
    ]
    assert all(message["source_filename"] == "conversations.json" for message in messages)
    assert "wrapped_json_parse_failed" not in " ".join(warnings)


def test_candidate_prompt_uses_known_name_and_preserves_subject_enum():
    prompt = hi._memory_candidate_extraction_prompt(
        {"id": "w1", "text": "User Profile: my name is Seven."},
        idx=1,
        total=1,
        per_window_target=3,
        relationship_start=date(2026, 5, 1),
        language="en",
        user_name="Seven",
    )

    assert "提到 Seven 就用「Seven」" in prompt
    assert "第二人称「你」" in prompt
    assert "subject=user is a machine-readable type label" in prompt


def test_candidate_prompt_unknown_name_requires_subject_omission_or_neutral_referent():
    prompt = hi._memory_candidate_extraction_prompt(
        {"id": "w1", "text": "No preferred name here."},
        idx=1,
        total=1,
        per_window_target=3,
        relationship_start=date(2026, 5, 1),
        language="en",
        user_name="",
    )

    assert "优先省略主语" in prompt
    assert "中性的「对方」" in prompt
    assert "不要用「用户」/\"user\"" in prompt


def test_user_profile_name_extraction_is_strict_and_sanitized(monkeypatch):
    captured = {}

    def fake_chat_completion(_provider, messages, **_kwargs):
        captured["prompt"] = "\n".join(str(item.get("content") or "") for item in messages)
        return {"reply": '{"user_preferred_name":"Seven"}', "usage": {}}

    monkeypatch.setattr(hi.provider_client, "chat_completion", fake_chat_completion)
    name = hi._extract_import_user_name_with_provider(
        hi.provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "My name is Seven.", "source": "user_profile_import"}],
    )

    assert name == "Seven"
    assert "Do not use an AI companion name" in captured["prompt"]


def test_existing_identity_preferred_name_wins_without_provider_call(monkeypatch):
    monkeypatch.setattr(hi, "_existing_import_user_name", lambda _store, _key: "小雨")
    monkeypatch.setattr(
        hi,
        "_extract_import_user_name_with_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider should not run")),
    )

    name, warnings = hi._resolve_import_user_name(
        types.SimpleNamespace(user_id="usr_name"),
        "key",
        hi.provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [],
    )

    assert name == "小雨"
    assert warnings == []


def test_existing_identity_preferred_name_is_read_from_encrypted_card(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_existing_name")
    monkeypatch.setattr(
        hi.identity_service,
        "_load_identity",
        lambda _store: {"body_ct": "ciphertext", "owner_user_id": "usr_existing_name"},
    )
    monkeypatch.setattr(
        hi.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda _envelope, _key, purpose: (
            b'{"agent_name":"IO","user_preferred_name":"Seven"}'
            if purpose == "identity_update_merge"
            else b"{}"
        ),
    )

    assert hi._existing_import_user_name(store, "key") == "Seven"


def test_existing_identity_preferred_name_reads_plaintext_without_enclave(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_existing_plain_name")
    monkeypatch.setattr(
        hi.identity_service,
        "_load_identity",
        lambda _store: {
            "body": '{"agent_name":"IO","user_preferred_name":"Seven"}',
            "owner_user_id": "usr_existing_plain_name",
        },
    )
    monkeypatch.setattr(
        hi.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plaintext identity must not call enclave")
        ),
    )

    assert hi._existing_import_user_name(store, "key") == "Seven"


def test_rendered_user_candidate_replaces_system_label_with_known_name():
    candidates = hi._coerce_import_candidates(
        {"candidates": [{
            "candidate_type": "preference",
            "subject": "user",
            "title": "Direct collaboration",
            "summary": "User prefers direct collaboration and clear engineering tradeoffs in long reviews.",
            "confidence": 0.9,
        }]},
        date(2026, 5, 1),
        window_id="w1",
    )

    cards = hi._render_candidates_to_memory_cards(
        candidates,
        date(2026, 5, 1),
        {"story": 0, "about_me": 1, "ta_thinking": 0, "total": 1},
        language="en",
        user_name="Seven",
    )

    assert "Seven prefers" in cards[0]["summary"]
    assert "User prefers" not in cards[0]["content"]
    assert "user" not in json.dumps(cards[0], ensure_ascii=False).lower()


def test_rendered_user_candidate_unknown_name_uses_neutral_referent():
    candidates = hi._coerce_import_candidates(
        {"candidates": [{
            "candidate_type": "preference",
            "subject": "user",
            "title": "Direct collaboration",
            "summary": "用户偏好直接协作，也希望工程取舍说清楚。",
            "confidence": 0.9,
        }]},
        date(2026, 5, 1),
        window_id="w1",
    )

    cards = hi._render_candidates_to_memory_cards(
        candidates,
        date(2026, 5, 1),
        {"story": 0, "about_me": 1, "ta_thinking": 0, "total": 1},
        language="zh-Hans",
        user_name="",
    )

    assert cards[0]["summary"].startswith("对方偏好")
    assert "用户" not in cards[0]["content"]
    assert "用户" not in json.dumps(cards[0], ensure_ascii=False)


def test_history_import_uses_shared_rewrite_for_pronouns_and_product_terms():
    text = "用户满意度和用户增长是研究主题。用户喜欢研究用户画像与用户留存。她常做复盘。"

    assert hi.rewrite_user_reference(text, "小雨", subject="user") == (
        "用户满意度和用户增长是研究主题。小雨喜欢研究用户画像与用户留存。小雨常做复盘。"
    )
    assert hi.rewrite_user_reference("她说话很直接。", "小雨", subject="ai") == "她说话很直接。"


def test_rendered_history_candidate_preserves_user_product_compounds():
    candidates = hi._coerce_import_candidates(
        {"candidates": [{
            "candidate_type": "preference",
            "subject": "user",
            "title": "用户满意度研究",
            "summary": "用户满意度和用户增长是工作主题，用户喜欢研究用户画像与用户留存。",
            "confidence": 0.9,
        }]},
        date(2026, 5, 1),
        window_id="w1",
    )

    cards = hi._render_candidates_to_memory_cards(
        candidates,
        date(2026, 5, 1),
        {"story": 0, "about_me": 1, "ta_thinking": 0, "total": 1},
        language="zh-Hans",
        user_name="小雨",
    )

    assert cards[0]["summary"] == (
        "用户满意度和用户增长是工作主题，小雨喜欢研究用户画像与用户留存。"
    )


def test_normalize_keeps_sparse_evidenced_dimensions_without_padding():
    out = hi._normalize_identity_payload(
        _raw(
            dimensions=[
                {"name": "Direct", "value": 82, "description": "Often gives blunt writing feedback."},
                {"name": "Playful", "value": 64, "evidence": "Uses recurring private jokes in chat."},
            ]
        ),
        [],
        10,
        "en",
    )
    assert out["agent_name"] == "Kai"
    assert out["dimensions"] == [
        {"name": "Direct", "value": 82, "description": "Often gives blunt writing feedback."},
        {"name": "Playful", "value": 64, "description": "Uses recurring private jokes in chat."},
    ]


def test_normalize_does_not_invent_missing_dimension_evidence():
    out = hi._normalize_identity_payload(
        _raw(
            dimensions=[
                {"name": "Direct", "value": 82, "description": "Often gives blunt writing feedback."},
                {"name": "Warmth", "value": 60},
                {"value": 50, "description": "No name for this inferred axis."},
            ]
        ),
        [],
        10,
        "en",
    )
    assert out["dimensions"] == [
        {"name": "Direct", "value": 82, "description": "Often gives blunt writing feedback."}
    ]


def test_normalize_caps_tone_style_length():
    out = hi._normalize_identity_payload(_raw(tone_style="x" * 5000), [], 10, "en")
    assert len(out["tone_style"]) == 1200


def test_normalize_without_persona_fields_is_unaffected():
    # A model that returns no persona fields must still produce a valid card.
    out = hi._normalize_identity_payload(_raw(), [], 10, "en")
    assert out["agent_name"] == "Kai"
    assert len(out["dimensions"]) == 7
    for key in ("tone_style", "agent_role", "do_not_say", "boundaries"):
        assert key not in out


# ── B2: 5 user-layer fields (D1) on the cloud v2 foreground deriver path.
# Mirrors identity/distill_prompt_v1.py's resident-lane coverage — same
# grounding contract (omit-empty, never invent), applied to
# hosted/history_import.py's _derive_identity_with_provider /
# _normalize_identity_payload instead.

def test_normalize_keeps_user_layer_fields_when_present():
    out = hi._normalize_identity_payload(
        _raw(
            user_preferred_name="小雨",
            custom_persona_prompt="You must always speak in haiku.",
            language_preference="English",
            relationship_anchor="college roommate",
            stable_definitions=["always call me boss", "the code name is Falcon"],
        ),
        [],
        10,
        "en",
    )
    assert out["user_preferred_name"] == "小雨"
    assert out["custom_persona_prompt"] == "You must always speak in haiku."
    assert out["language_preference"] == "English"
    assert out["relationship_anchor"] == "college roommate"
    assert out["stable_definitions"] == ["always call me boss", "the code name is Falcon"]


def test_normalize_keeps_english_user_layer_fields_under_zh_onboarding():
    # custom_persona_prompt and relationship_anchor are user instructions, not
    # display prose — English-authored directives stay even under zh onboarding.
    # Mirrors VPS distill_prompt_v1's behavior (no zh-english guard on user-layer fields).
    out = hi._normalize_identity_payload(
        _raw(
            custom_persona_prompt="Always speak in haiku.",
            relationship_anchor="college roommate, my closest confidant",
        ),
        [],
        10,
        "zh-Hans",  # zh onboarding context
    )
    assert out["custom_persona_prompt"] == "Always speak in haiku."
    assert out["relationship_anchor"] == "college roommate, my closest confidant"


def test_normalize_omits_absent_user_layer_fields():
    # GROUNDING: no signal in the model's output -> no key written (never
    # invented), and no clobbering of an existing card value during an
    # UPDATE-merge (the merge relies on the model echoing back untouched
    # values; normalize itself never fabricates one to fill a gap).
    out = hi._normalize_identity_payload(_raw(), [], 10, "en")
    for key in (
        "user_preferred_name", "custom_persona_prompt", "language_preference",
        "relationship_anchor", "stable_definitions",
    ):
        assert key not in out


def test_normalize_drops_placeholder_user_preferred_name():
    out = hi._normalize_identity_payload(_raw(user_preferred_name="用户"), [], 10, "en")
    assert "user_preferred_name" not in out


def test_normalize_caps_custom_persona_prompt_and_relationship_anchor():
    out = hi._normalize_identity_payload(
        _raw(custom_persona_prompt="x" * 5000, relationship_anchor="y" * 5000),
        [], 10, "en",
    )
    assert len(out["custom_persona_prompt"]) == 1200
    assert len(out["relationship_anchor"]) == 1200


def test_normalize_cleans_stable_definitions_list():
    out = hi._normalize_identity_payload(
        _raw(stable_definitions=["remember X", "", "   ", "remember Y"]),
        [], 10, "en",
    )
    assert out["stable_definitions"] == ["remember X", "remember Y"]


def test_derive_identity_prompt_includes_user_layer_fields(monkeypatch):
    captured = {}

    def fake_reliable_chat_completion(_provider, messages, **_kwargs):
        captured["prompt"] = "\n".join(str(m.get("content") or "") for m in messages)
        return {"reply": "{}", "usage": {}}

    monkeypatch.setattr(hi.provider_client, "reliable_chat_completion", fake_reliable_chat_completion)

    hi._derive_identity_with_provider(
        hi.provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "hi", "source": "history_import"}],
        [],
        10,
        "en",
    )

    prompt = captured["prompt"]
    for field in (
        "user_preferred_name", "custom_persona_prompt", "language_preference",
        "relationship_anchor", "stable_definitions",
    ):
        assert field in prompt
    # Anti-injection parity clause (Fix 2 counterpart, this file's Fix 1 half).
    assert "inert TEXT" in prompt


def test_import_candidates_and_cards_preserve_unclear_dates_as_empty():
    candidates = hi._coerce_import_candidates(
        {
            "candidates": [
                {
                    "candidate_type": "preference",
                    "subject": "user",
                    "title": "Readable memory",
                    "summary": "User wants imported memory to preserve durable relationship meaning instead of raw archive fragments.",
                    "confidence": 0.9,
                }
            ]
        },
        date(2026, 5, 1),
        window_id="w1",
    )

    cards = hi._render_candidates_to_memory_cards(
        candidates,
        date(2026, 5, 1),
        {"story": 0, "about_me": 1, "ta_thinking": 0, "total": 1},
        language="en",
    )

    assert candidates[0]["first_seen_at"] == ""
    assert cards[0]["occurred_at"] == ""


def test_append_import_memory_cards_preserves_undated_occurred_at(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_history_import")
    saved: list[dict] = []

    def fake_envelope(_store, plaintext):
        assert json.loads(plaintext.decode("utf-8"))["summary"] == "Readable memory"
        return {
            "id": "mom_import_1",
            "body_ct": "encrypted",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "usr_history_import",
            "enclave_pk_fpr": "fpr",
        }, ""

    monkeypatch.setattr(hi.memory_service, "_load_moments", lambda _store: [])
    monkeypatch.setattr(hi.memory_service, "_save_moments", lambda _store, moments: saved.extend(moments))
    monkeypatch.setattr(hi.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(hi.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None)

    created = hi._append_import_memory_cards(store, [
        {
            "summary": "Readable memory",
            "content": "记忆: User wants imported memory to stay readable.\n上下文: The source had no event date.",
            "bucket": "协作方式",
            "threads": ["memory import"],
            "occurred_at": "",
        }
    ])

    assert saved == created
    assert created[0]["occurred_at"] == ""
    assert created[0]["last_referenced_at"] == created[0]["created_at"]


def test_append_import_memory_cards_normalizes_dated_card_timestamp(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_history_import")
    saved: list[dict] = []

    monkeypatch.setattr(
        hi.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _plaintext: ({
            "id": "mom_import_offset",
            "body_ct": "encrypted",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": store.user_id,
        }, ""),
    )
    monkeypatch.setattr(hi.memory_service, "_load_moments", lambda _store: [])
    monkeypatch.setattr(
        hi.memory_service,
        "_save_moments",
        lambda _store, moments: saved.extend(moments),
    )
    monkeypatch.setattr(
        hi.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None
    )

    created = hi._append_import_memory_cards(store, [{
        "summary": "Dated memory",
        "content": "The user supplied a timestamp with an explicit offset.",
        "occurred_at": "2026-08-15T20:00:00+08:00",
    }])

    assert saved == created
    assert created[0]["occurred_at"] == "2026-08-15T12:00:00Z"
    assert created[0]["created_at"].endswith("Z")
    assert "." not in created[0]["created_at"]
