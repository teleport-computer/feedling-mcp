"""Always On is a per-turn contract, independent of model tool selection."""

from __future__ import annotations

import asyncio
import json

import pytest

import conftest
import db
from core import envelope, store as core_store
from model_api_runtime.v2 import (
    context,
    jobs_store,
    prompt_frontier,
    serve_worker,
    worker,
)
from provider_types import ToolCall, ToolExchange, ToolResult
from test_v2_adaptive_tail import _limit
from test_v2_worker_tool_loop import (
    _BYOK,
    _deps,
    _patch_real_write,
    _reset,
    _script_provider,
    _text_round,
)


@pytest.mark.parametrize("mode", ["legacy", "selective", "lazy"])
def test_worldbook_snapshot_is_owner_scoped_without_store_or_section_loading(
    monkeypatch, mode
):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", mode)
    cached = core_store.UserStore("snapshot-owner")
    cached.world_books = [{"id": "cached-old"}]
    monkeypatch.setitem(core_store._stores, "snapshot-owner", cached)

    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot must not construct or hydrate UserStore")

    monkeypatch.setattr(core_store, "UserStore", forbidden)
    monkeypatch.setattr(core_store, "get_store", forbidden)
    monkeypatch.setattr(core_store, "get_store_per_load_mode", forbidden)
    monkeypatch.setattr(cached, "ensure_sections", forbidden)
    rows = [{"id": "current-owner-entry"}]
    reads = []

    def load(owner):
        reads.append(owner)
        return rows if owner == "snapshot-owner" else []

    monkeypatch.setattr(db, "world_book_load_strict", load)
    snapshot = core_store.read_worldbook_snapshot("snapshot-owner")
    other = core_store.read_worldbook_snapshot("other-owner")
    assert reads == ["snapshot-owner", "other-owner"]
    assert snapshot.user_id == "snapshot-owner"
    assert snapshot.world_books == ({"id": "current-owner-entry"},)
    assert other.user_id == "other-owner" and other.world_books == ()
    assert core_store._stores["snapshot-owner"] is cached
    assert cached.world_books == [{"id": "cached-old"}]
    assert not hasattr(snapshot, "chat_messages")
    assert not hasattr(snapshot, "upsert_world_book")
    rows[0]["id"] = "edited"
    assert snapshot.world_books == ({"id": "current-owner-entry"},)
    assert core_store.read_worldbook_snapshot("snapshot-owner").world_books == (
        {"id": "edited"},
    )
    rows.clear()
    assert core_store.read_worldbook_snapshot("snapshot-owner").world_books == ()

    def failed_read(owner):
        raise RuntimeError("snapshot_database_unavailable")

    monkeypatch.setattr(db, "world_book_load_strict", failed_read)
    with pytest.raises(RuntimeError, match="snapshot_database_unavailable"):
        core_store.read_worldbook_snapshot("snapshot-owner")
    assert cached.world_books == [{"id": "cached-old"}]


@pytest.fixture
def worldbook_turn(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", False)
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    _patch_real_write(monkeypatch)

    def write_plain(store, text, *, extra=None):
        return store.append_chat(
            "openclaw",
            "model_api",
            {"body": text, "owner_user_id": store.user_id},
            strict=True,
            extra=extra,
        )

    monkeypatch.setattr(worker, "_write_encrypted_reply", write_plain)
    monkeypatch.setattr(
        serve_worker.worldbook_core.worldbook_readside_core,
        "post_enclave_worldbook_match",
        lambda *a, **kw: pytest.fail("plaintext read must not depend on enclave"),
    )
    number = 0

    def run(uid, *, voice=False, reader=None, text="Ordinary conversation."):
        nonlocal number
        number += 1
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        calls = _script_provider(monkeypatch, [_text_round("A neutral answer.")])
        row = {
            "id": f"input-{number}",
            "ts": float(number),
            "role": "user",
            "content": text,
            "body": text,
            "source": "model_api",
            "owner_user_id": uid,
        }
        if voice:
            row.update(voice_call_id="voice-call", voice_turn_id=f"v-{number}")
        db.chat_append_strict(uid, row["id"], row["ts"], row, 10000)
        row["seq"] = db.chat_max_seq(uid)
        deps = _deps(messages=[row])
        deps.read_worldbook_context = reader or serve_worker._read_worldbook_context
        jobs_store.enqueue_job(uid, "chat")
        job = jobs_store.claim_next_job("worldbook-test")
        assert job and job["user_id"] == uid
        assert (
            asyncio.run(
                worker.process_job(
                    job,
                    deps,
                    provider_config=_BYOK,
                    api_key=None,
                    runtime_token="runtime-token",
                )
            )
            == "completed"
        )
        assert len(calls) == 1  # No tool call required for the first request.
        return calls[0]

    return run


def _owner(uid):
    conftest.seed_user(uid)
    _reset(uid)
    return uid


def _entry(uid, text, *, enabled=True, always_on=True):
    entry = {
        "id": "world",
        "name": "Setting",
        "enabled": enabled,
        "alwaysOn": always_on,
        "keywords": ["moon-library"],
        "content": text,
    }
    doc = {
        "id": "world",
        "owner_user_id": uid,
        "visibility": "shared",
        "body": json.dumps(entry),
    }
    assert db.world_book_upsert(uid, "world", "2026-10-07", doc)
    return doc


def _text(call):
    return "\n".join(m["content"] for m in call["messages"] if isinstance(m, dict))


@pytest.mark.parametrize("voice", [False, True])
def test_first_request_reads_fresh_constants_updates_deletes_and_isolates_owners(
    worldbook_turn, voice
):
    uid = _owner(f"u_always_on_{voice}")
    other = _owner(f"u_always_on_other_{voice}")
    _entry(other, "OTHER_OWNER_SECRET")
    _entry(uid, "COPPER_FEATHER_ORIGINAL")
    # Prime a cached store; subsequent DB-only edits deliberately do not evict it.
    cached = core_store.get_store(uid, require={core_store.StoreSection.WORLD_BOOKS})
    assert cached.world_books
    for sentinel in ("COPPER_FEATHER_ORIGINAL", "COPPER_FEATHER_EDITED"):
        _entry(uid, sentinel)
        call = worldbook_turn(uid, voice=voice)
        assert sentinel in _text(call)
        assert "OTHER_OWNER_SECRET" not in _text(call)
        assert any(t.name == "worldbook_match" for t in call["tools"])
        assert all(
            sentinel not in m.get("content", "")
            for m in call["messages"]
            if isinstance(m, dict) and m.get("role") == "system"
        )
        if sentinel.endswith("EDITED"):
            assert "COPPER_FEATHER_ORIGINAL" not in _text(call)
    assert db.world_book_delete_strict(uid, "world")
    cleared = _text(worldbook_turn(uid, voice=voice))
    assert "COPPER_FEATHER" not in cleared
    assert "WORLD BOOK CONTEXT" not in cleared


@pytest.mark.parametrize("kind", ["empty", "disabled", "keyword"])
def test_unmatched_and_disabled_entries_do_not_inject(worldbook_turn, kind):
    uid = _owner("u_always_negative_" + kind)
    if kind != "empty":
        _entry(
            uid,
            "MUST_NOT_EAGERLY_APPEAR",
            enabled=kind != "disabled",
            always_on=kind != "keyword",
        )
    text = _text(worldbook_turn(uid))
    assert "MUST_NOT_EAGERLY_APPEAR" not in text
    assert "WORLD BOOK CONTEXT" not in text


@pytest.mark.parametrize("voice", [False, True])
def test_keyword_matches_directly_without_a_tool_call(worldbook_turn, voice):
    uid = _owner(f"u_keyword_direct_{voice}")
    _entry(uid, "KEYWORD_CONSTANT", always_on=False)
    assert "KEYWORD_CONSTANT" in _text(
        worldbook_turn(uid, voice=voice, text="Moon-Library please.")
    )


@pytest.mark.parametrize("failure", ["storage", "invalid", "partial", "over_cap"])
def test_unavailable_or_partial_read_is_not_reported_as_empty(
    worldbook_turn, monkeypatch, failure
):
    uid = _owner("u_always_failure_" + failure)
    reader = None
    if failure == "storage":

        def fail(_):
            raise RuntimeError("PRIVATE_STORAGE_DETAIL")

        monkeypatch.setattr(db, "world_book_load_strict", fail)
    elif failure == "invalid":
        reader = lambda *a, **kw: None
    elif failure == "partial":
        assert db.world_book_upsert(
            uid,
            "broken",
            "2026-10-07",
            {
                "id": "broken",
                "owner_user_id": uid,
                "body": "not json",
            },
        )
        _entry(uid, "AVAILABLE_CONSTANT")
    else:
        _entry(uid, "z" * 20001)
    text = _text(worldbook_turn(uid, reader=reader))
    assert (
        "WORLD BOOK CONTEXT "
        + ("UNAVAILABLE" if failure in {"storage", "invalid"} else "PARTIAL")
        in text
    )
    assert "PRIVATE_STORAGE_DETAIL" not in text
    if failure == "partial":
        assert "AVAILABLE_CONSTANT" in text


@pytest.mark.parametrize("alphabet", ["x", "界"])
@pytest.mark.parametrize("window", [8192, 16384, 32768])
def test_worldbook_respects_actual_frontier_estimator_and_required_tool_exchange(
    alphabet, window
):
    native = ToolExchange(
        calls=(ToolCall("wb", "worldbook_match", {"query": "lore"}),),
        results=(ToolResult("wb", "REQUIRED_TOOL_RESULT"),),
    )
    builder = worker._make_build_messages_fn(
        system_prompt="REQUIRED_SYSTEM",
        summary="",
        tail=[{"id": "last", "seq": 1, "role": "user", "content": "REQUIRED_USER"}],
        worldbook_context=alphabet * 30000,
        tail_target_turns=40,
    )
    tools = [
        spec
        for spec in worker.cap_tool_schema.build_tool_specs()
        if spec.name == "worldbook_match"
    ]

    def plan_round():
        return builder.plan_provider_round(
            transcript=[native],
            tools=tools,
            required_tool_names=("worldbook_match",),
            model_limit=_limit(window),
            output_reserve_tokens=worker.PROMPT_OUTPUT_RESERVE_TOKENS,
            safety_margin_tokens=worker.PROMPT_SAFETY_MARGIN_TOKENS,
            utf8_bytes_per_token=worker.PROMPT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
            image_reserve_tokens=worker.PROMPT_IMAGE_RESERVE_TOKENS,
        )

    if window == 8192:
        # With production's byte-based estimator and 4096+1024 reserves,
        # required policy + user + native exchange + even a marker cannot fit.
        # Failure is the contract; silently dropping required content is not.
        with pytest.raises(prompt_frontier.PromptFrontierExhausted):
            plan_round()
        return
    messages, plan, stats = plan_round()
    assert "worldbook_match" in plan.included_tool_names
    assert worker.PROMPT_ESTIMATOR_UTF8_BYTES_PER_TOKEN == 1.0
    text = "\n".join(m["content"] for m in messages if isinstance(m, dict))
    assert "REQUIRED_SYSTEM" in text and "REQUIRED_USER" in text and native in messages
    assert context.WORLD_BOOK_TRUNCATION_MARKER in text
    assert stats["worldbook_truncated"]
    with pytest.raises(prompt_frontier.PromptFrontierExhausted):
        builder.plan_provider_round(
            transcript=[native, {"content": "REQUIRED" * 10000}],
            tools=tools,
            required_tool_names=("worldbook_match",),
            model_limit=_limit(window),
            output_reserve_tokens=worker.PROMPT_OUTPUT_RESERVE_TOKENS,
            safety_margin_tokens=worker.PROMPT_SAFETY_MARGIN_TOKENS,
            utf8_bytes_per_token=worker.PROMPT_ESTIMATOR_UTF8_BYTES_PER_TOKEN,
            image_reserve_tokens=worker.PROMPT_IMAGE_RESERVE_TOKENS,
        )


def _signal(uid, mid, text, **metadata):
    doc = {
        "id": mid,
        "owner_user_id": uid,
        "body": text,
        "role": "user",
        "source": "model_api",
        **metadata,
    }
    db.chat_append_strict(uid, mid, 10.0, doc, 10000)
    return db.chat_max_seq(uid)


@pytest.mark.parametrize("role", ["user", "human", "assistant", "openclaw", "agent"])
def test_keyword_window_is_seq_ordered_independent_of_summary_and_does_not_duplicate_current(
    monkeypatch,
    role,
):
    uid = _owner("u_worldbook_signal_window")
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    _entry(uid, "RECENT_KEYWORD_LORE", always_on=False)
    _signal(uid, "keyword", "moon-library", role=role)
    for i in range(4):
        upper = _signal(uid, f"same-{i}", "same repeated message")
    signals = db.world_book_chat_signals(uid, upper)
    assert len(signals) == 5
    assert [r["id"] for r in signals] == [
        "keyword",
        "same-0",
        "same-1",
        "same-2",
        "same-3",
    ]
    read = lambda seq: serve_worker._read_worldbook_context(
        uid, [], runtime_token="", through_seq=seq
    )
    assert "RECENT_KEYWORD_LORE" in read(upper)["block"]
    later = _signal(uid, "newest", "same repeated message")
    assert read(later)["block"] == ""
    assert "RECENT_KEYWORD_LORE" in read(upper)["block"]  # Frozen boundary.


@pytest.mark.parametrize("source", ["model_api", "chat", "verify_ping"])
def test_verify_literal_in_real_chat_is_not_synthetic_metadata(monkeypatch, source):
    uid = _owner("u_worldbook_verify_literal")
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    _entry(uid, "GENUINE_TEXT_LORE", always_on=False)
    upper = _signal(
        uid,
        "literal",
        "Explain __VERIFY_PING__ in the moon-library.",
        source=source,
    )
    signals = db.world_book_chat_signals(uid, upper)
    expected_real_chat = source != "verify_ping"
    assert bool(signals) == expected_real_chat
    block = serve_worker._read_worldbook_context(
        uid,
        [],
        runtime_token="",
        through_seq=upper,
    )["block"]
    assert ("GENUINE_TEXT_LORE" in block) == expected_real_chat


@pytest.mark.parametrize(
    "metadata",
    [
        {"role": "tool"},
        {"role": "system"},
        {"source": "verify_ping"},
        {"source": "resident_maintenance"},
        {"source": "voice_call_transcript"},
        {"source": "screen_watch"},
        {"source": "tool_result"},
        {"content_type": "image"},
        {"content_type": "file"},
    ],
)
def test_untrusted_or_synthetic_rows_do_not_select_keywords(monkeypatch, metadata):
    uid = _owner("u_worldbook_filtered")
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    _entry(uid, "FORBIDDEN_KEYWORD_LORE", always_on=False)
    _signal(uid, "real", "hello")
    upper = _signal(uid, "excluded", "moon-library", **metadata)
    signals = db.world_book_chat_signals(uid, upper)
    assert [r["id"] for r in signals] == ["real"]
    assert (
        serve_worker._read_worldbook_context(
            uid, [], runtime_token="", through_seq=upper
        )["block"]
        == ""
    )


def test_signal_plaintext_and_sealed_reader_preserve_owner_and_runtime_auth(
    monkeypatch,
):
    uid = _owner("u_worldbook_signal_auth")
    other = _owner("u_worldbook_signal_foreign")
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    _entry(uid, "AUTHENTICATED_LORE", always_on=False)
    _signal(other, "foreign", "moon-library")
    assert (
        serve_worker._read_worldbook_context(
            uid, [], runtime_token="", through_seq=db.chat_max_seq(other)
        )["block"]
        == ""
    )
    upper = _signal(
        uid, "sealed", "unused stale plaintext", body_ct="sealed", K_enclave="key"
    )
    observed = []

    def decrypt(row, api_key, **kwargs):
        observed.append((row, api_key, kwargs))
        return b"moon-library"

    monkeypatch.setattr(envelope.enclave, "_decrypt_envelope_via_enclave", decrypt)
    result = serve_worker._read_worldbook_context(
        uid, [], runtime_token="runtime-secret", through_seq=upper
    )
    assert "AUTHENTICATED_LORE" in result["block"]
    assert len(observed) == 1
    assert observed[0][2]["caller_user_id"] == uid
    assert observed[0][2]["runtime_token"] == "runtime-secret"
