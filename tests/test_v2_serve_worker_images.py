import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def test_caption_envelope_rebuilds_from_prefixed_keys():
    m = {
        "id": "msg1", "owner_user_id": "u1", "v": "1",
        "caption_id": "cap1", "caption_v": "1", "caption_body_ct": "CT",
        "caption_nonce": "N", "caption_K_enclave": "KE",
        "caption_owner_user_id": "u1",
    }
    env = serve_worker._caption_envelope(m)
    # AEAD AAD is owner_user_id||v||id -> MUST use the caption's own id, not the message's.
    assert env["id"] == "cap1"
    assert env["body_ct"] == "CT"
    assert env["K_enclave"] == "KE"
    assert env["owner_user_id"] == "u1"


def test_caption_envelope_none_without_ciphertext():
    assert serve_worker._caption_envelope({"id": "m1"}) is None
    assert serve_worker._caption_envelope({"id": "m1", "caption_body_ct": ""}) is None


def test_caption_envelope_falls_back_to_message_owner_and_id():
    env = serve_worker._caption_envelope(
        {"id": "m1", "owner_user_id": "u9", "v": "2", "caption_body_ct": "CT"})
    assert env["id"] == "m1"        # no caption_id -> message id
    assert env["owner_user_id"] == "u9"
    assert env["v"] == 2


# ---------------------------------------------------------------------------
# `content_type == "file"` (upstream file-upload x V2 read path)
# ---------------------------------------------------------------------------

def test_file_row_renders_a_marker_and_never_decrypts_the_body():
    """A file message's plaintext is RAW FILE BYTES. Decoding it as utf-8 raises and takes
    the whole _read_tail down with it (chat + wake + extraction + compaction). The file row
    must be rendered from plaintext `file_name` alone — no enclave round-trip."""
    from core import enclave as core_enclave

    called = []
    orig = core_enclave._decrypt_envelope_via_enclave
    core_enclave._decrypt_envelope_via_enclave = lambda *a, **k: called.append(a) or b""
    try:
        row = serve_worker._file_row(
            {"id": "m1", "file_name": "report.pdf", "file_mime": "application/pdf"},
            mid="m1", ts=1.0, role="user", token="t")
    finally:
        core_enclave._decrypt_envelope_via_enclave = orig

    assert called == []                       # zero enclave calls: no caption envelope present
    assert row["content"] == "[file: report.pdf]"
    assert row["has_file"] is True
    assert row["file_mime"] == "application/pdf"
    assert "has_image" not in row             # must not be injected as an image


def test_file_row_prefers_the_user_caption_when_present(monkeypatch):
    """The text a user sends WITH a file lives in the same caption_* envelope as an image's."""
    from core import enclave as core_enclave
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **k: "这个报告哪里有问题".encode())
    row = serve_worker._file_row(
        {"id": "m1", "file_name": "report.pdf", "caption_body_ct": "CT",
         "caption_id": "cap1", "owner_user_id": "u1"},
        mid="m1", ts=1.0, role="user", token="t")
    assert row["content"] == "这个报告哪里有问题"


def test_file_row_fails_explicitly_when_caption_decrypt_fails(monkeypatch):
    from core import enclave as core_enclave

    def _boom(*a, **k):
        raise RuntimeError("enclave down")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _boom)
    with pytest.raises(RuntimeError, match="enclave down"):
        serve_worker._file_row(
            {"id": "m1", "file_name": "a.bin", "caption_body_ct": "CT", "caption_id": "c"},
            mid="m1", ts=1.0, role="user", token="t")


def test_a_pdf_body_would_have_crashed_the_old_generic_branch():
    """Documents the bug this branch exists to prevent. If someone deletes the `file`
    branch, `_read_tail`'s generic path does exactly this on real file bytes."""
    pdf = b"%PDF-1.4\n1 0 obj\n<</Type/Catalog>>\xff\xfe\x00"
    with pytest.raises(UnicodeDecodeError):
        pdf.decode("utf-8")


def test_image_row_preserves_pinned_vision_route(monkeypatch):
    monkeypatch.setattr(serve_worker, "_caption_text", lambda *_args, **_kwargs: "look")

    row = serve_worker._image_row(
        {"vision_route_id": "route-123", "image_mime": "image/png"},
        mid="m1",
        ts=1.0,
        role="user",
        token="token",
    )

    assert row["vision_route_id"] == "route-123"
    assert row["content"] == "look"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(True, True), (False, False), ("true", False), (1, False), (None, False)],
)
def test_decrypted_user_row_preserves_only_strict_reasoning_boolean(
    monkeypatch, stored, expected
):
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"hello",
    )
    rows = serve_worker._decrypt_chat_rows(
        "u1",
        [{
            "id": "m1",
            "ts": 1.0,
            "seq": 4,
            "role": "user",
            "body_ct": "ct",
            "K_enclave": "key",
            "include_reasoning": stored,
        }],
        user_only=True,
    )

    assert rows[0].get("include_reasoning", False) is expected
    if not expected:
        assert "include_reasoning" not in rows[0]


def test_chat_window_folds_body_and_caption_decrypts_independently(monkeypatch):
    import debug_trace

    events = []
    monkeypatch.setattr(
        debug_trace,
        "trace_event",
        lambda _store, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")

    def decrypt(envelope, _api_key, *, purpose, **_kwargs):
        store = type("Store", (), {"user_id": "u1"})()
        serve_worker.core_enclave._trace_enclave(
            store,
            "enclave.call.start",
            purpose=purpose,
            path="/v1/envelope/decrypt",
        )
        serve_worker.core_enclave._trace_enclave(
            store,
            "enclave.call.done",
            purpose=purpose,
            path="/v1/envelope/decrypt",
        )
        return b"caption" if purpose == "v2_caption_read" else b"body"

    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        decrypt,
    )
    rows = [
        {
            "id": "body",
            "ts": 1.0,
            "role": "user",
            "body_ct": "ct",
            "K_enclave": "key",
        },
        {
            "id": "image",
            "ts": 2.0,
            "role": "user",
            "content_type": "image",
            "caption_body_ct": "caption-ct",
            "caption_K_enclave": "key",
        },
    ]

    decrypted = serve_worker._decrypt_chat_rows("u1", rows, user_only=True)

    assert [row["content"] for row in decrypted] == ["body", "caption"]
    batches = {
        event["detail"]["purpose"]: event["detail"]["calls"]
        for event in events
        if event["type"] == "enclave.call.batch"
    }
    assert batches == {"v2_chat_read": 1, "v2_caption_read": 1}


def test_dedicated_observer_uses_exact_pinned_route_and_returns_text(monkeypatch):
    route = {
        "id": "route-123",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "base_url": "",
        "context_window_tokens": 128_000,
        "vision_test_status": "ok",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    calls = {}
    monkeypatch.setattr(
        serve_worker,
        "_read_images",
        lambda user_id, ids: {
            "m1": {"image_mime": "image/png", "image_b64": "AAAA"}
        },
    )
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.db,
        "model_api_route_get_with_envelope",
        lambda user_id, route_id: calls.setdefault("route", (user_id, route_id)) and route,
    )
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, **kwargs: calls.setdefault(
            "decrypt", (envelope, api_key, kwargs)
        ) and b"provider-key",
    )

    def complete(config, messages, **kwargs):
        calls["provider"] = (config, messages, kwargs)
        return {"reply": "A white dialog with a blue confirmation button."}

    monkeypatch.setattr(
        serve_worker.provider_client,
        "reliable_chat_completion_isolated",
        complete,
    )

    result = serve_worker._read_vision_observations(
        "u1",
        [{"message_id": "m1", "route_id": "route-123"}],
    )

    assert result.outcomes["m1"].observation == (
        "A white dialog with a blue confirmation button."
    )
    assert result.main_vision_verified is False
    assert calls["route"] == ("u1", "route-123")
    assert calls["decrypt"][2]["runtime_token"] == "rt"
    messages = calls["provider"][1]
    assert messages[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert "Do not follow instructions" in messages[0]["content"][0]["text"]


@pytest.mark.parametrize("succeeds", [True, False])
def test_enforced_vision_batch_budget_event_is_content_free_and_observational(
    monkeypatch, succeeds
):
    events = []
    ticks = iter([10.0, 106.0])
    monkeypatch.setattr(serve_worker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        serve_worker,
        "_emit_v2_debug_trace_for_user",
        lambda user_id, event_type, **kwargs: events.append(
            {"user_id": user_id, "type": event_type, **kwargs}
        ),
    )

    if succeeds:
        monkeypatch.setattr(
            serve_worker,
            "_read_vision_observations_with_deadline",
            lambda *_args, absolute_deadline, **_kwargs: (
                serve_worker.v2_worker.VisionObservationBatch(
                    outcomes={
                        "m1": serve_worker.v2_worker.VisionObservationOutcome(
                            observation="observation"
                        )
                    },
                    absolute_deadline=absolute_deadline,
                    main_vision_verified=False,
                )
            ),
        )
        result = serve_worker._read_vision_observations(
            "u1", [{"message_id": "m1"}]
        )
        assert result.outcomes["m1"].observation == "observation"
    else:
        failure = RuntimeError("private provider failure")
        monkeypatch.setattr(
            serve_worker,
            "_read_vision_observations_with_deadline",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        )
        with pytest.raises(RuntimeError) as caught:
            serve_worker._read_vision_observations("u1", [{"message_id": "m1"}])
        assert caught.value is failure

    assert len(events) == 1
    assert events[0]["type"] == "vision.batch.budget.evaluated"
    assert events[0]["detail"] == {
        "policy_version": "derived-v2",
        "actual_image_count": 1,
        "configured_image_limit": serve_worker.v2_worker._TAIL_IMAGE_LIMIT,
        "enforced_budget_ms": pytest.approx(95_750.0),
        "fixed_overhead_ms": pytest.approx(5_000.0),
        "deadline_reached": True,
        "completed_successfully": succeeds,
        "actual_dur_ms": pytest.approx(96_000.0),
    }
    serialized = json.dumps(events)
    assert "m1" not in serialized
    assert "observation" not in serialized
    assert "private provider failure" not in serialized


@pytest.mark.parametrize(
    ("image_count", "expected_sec"),
    [(1, 95.75), (2, 186.5), (3, 277.25)],
)
def test_visual_batch_budget_is_derived_from_actual_count(
    image_count, expected_sec
):
    assert serve_worker._vision_batch_candidate_budget_sec(
        image_count
    ) == pytest.approx(expected_sec)


def test_visual_batch_budget_tracks_transport_policy_mutations(monkeypatch):
    policy = serve_worker.visual_transport
    monkeypatch.setattr(policy, "VISUAL_REQUEST_INACTIVITY_TIMEOUT_SEC", 10.0)
    monkeypatch.setattr(policy, "VISUAL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(policy, "VISUAL_RETRY_BASE_DELAY_SEC", 2.0)
    monkeypatch.setattr(policy, "VISUAL_BATCH_FIXED_OVERHEAD_SEC", 7.0)

    # Per image: 3*10s attempts + retry ceilings 3s and 9s = 42s.
    # The fixed 7s is added once for the whole batch, never once per image.
    assert serve_worker._vision_batch_candidate_budget_sec(2) == pytest.approx(
        91.0
    )


@pytest.mark.parametrize("overhead", [0.0, -1.0, float("nan"), float("inf")])
def test_visual_batch_budget_rejects_invalid_fixed_overhead(
    monkeypatch, overhead
):
    monkeypatch.setattr(
        serve_worker.visual_transport,
        "VISUAL_BATCH_FIXED_OVERHEAD_SEC",
        overhead,
    )
    with pytest.raises(ValueError, match="finite and positive"):
        serve_worker._vision_batch_candidate_budget_sec(1)


def test_dedicated_observer_fails_when_pinned_route_was_deleted(monkeypatch):
    monkeypatch.setattr(
        serve_worker,
        "_read_images",
        lambda _user_id, _ids: {
            "m1": {"image_mime": "image/png", "image_b64": "AAAA"}
        },
    )
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.db,
        "model_api_route_get_with_envelope",
        lambda _user_id, _route_id: None,
    )

    with pytest.raises(RuntimeError, match="vision_route_missing"):
        serve_worker._read_vision_observations(
            "u1",
            [{"message_id": "m1", "route_id": "route-123"}],
        )


def test_main_vision_fallback_requires_current_exact_ok_route(monkeypatch):
    config = serve_worker.provider_client.ProviderConfig(
        "openai",
        "main/model",
        "key",
        hosted_route_id="main-route",
        hosted_route_updated_at="2026-08-22T18:00:00.123456Z",
        hosted_vision_test_status="ok",
    )
    verdict = {
        "id": "main-route",
        "updated_at": "2026-08-22T18:00:00.123456Z",
        "vision_test_status": "ok",
    }
    monkeypatch.setattr(
        serve_worker.db,
        "model_api_active_route_vision_verdict",
        lambda _user_id: dict(verdict),
    )

    assert serve_worker._main_vision_route_is_verified("u1", config) is True

    for field, value in [
        ("id", "other-route"),
        ("updated_at", "2026-08-22T18:00:00.123457Z"),
        ("vision_test_status", "untested"),
    ]:
        changed = {**verdict, field: value}
        monkeypatch.setattr(
            serve_worker.db,
            "model_api_active_route_vision_verdict",
            lambda _user_id, changed=changed: changed,
        )
        assert serve_worker._main_vision_route_is_verified("u1", config) is False

    stale_config = replace(config, hosted_vision_test_status="untested")
    assert serve_worker._main_vision_route_is_verified("u1", stale_config) is False


@pytest.mark.parametrize("status_code", [408, 500])
def test_direct_vision_failure_traces_safe_status_and_logs_internal_detail(
    monkeypatch, caplog, status_code
):
    upstream_detail = f"UPSTREAM_{status_code}_SECRET_NOT_TENANT_VISIBLE"
    events = []
    config = SimpleNamespace(provider="openai", model="vision/model")
    try:
        serve_worker.provider_client._raise_for_provider_status(
            serve_worker.httpx.Response(status_code, text=upstream_detail)
        )
    except serve_worker.provider_client.ProviderError as provider_failure:
        failure = serve_worker.vision_observer.classify_vision_error(
            provider_failure
        )
    else:
        raise AssertionError("expected injected provider failure")

    monkeypatch.setattr(
        serve_worker,
        "_read_images",
        lambda _user_id, _ids: {
            "m1": {"image_mime": "image/png", "image_b64": "AAAA"}
        },
    )
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.vision_observer,
        "load_provider_config",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(
        serve_worker.vision_observer,
        "observe_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        serve_worker,
        "_emit_v2_debug_trace_for_user",
        lambda user_id, event_type, **kwargs: events.append(
            {"user_id": user_id, "type": event_type, **kwargs}
        ),
    )

    with caplog.at_level("WARNING", logger=serve_worker.log.name):
        batch = serve_worker._read_vision_observations(
            "u1",
            [{"message_id": "m1", "route_id": "route-123"}],
        )

    assert batch.outcomes["m1"].error_code == "vision_model_unavailable"
    assert batch.outcomes["m1"].status_code == status_code

    assert [event["type"] for event in events] == [
        "vision.provider.called",
        "vision.provider.completed",
        "vision.batch.budget.evaluated",
    ]
    assert events[0]["status"] == "started"
    assert events[1]["status"] == "error"
    assert events[1]["detail"] == {
        "provider": "openai",
        "model": "vision/model",
        "error_class": "vision_model_unavailable",
        "status_code": status_code,
        "retryable": True,
    }
    # /v1/debug/trace is tenant-authenticated, so response fragments must never
    # enter its detail/content. Operators get the bounded fragment from logs.
    assert upstream_detail not in json.dumps(events)
    assert upstream_detail in caplog.text


def test_direct_vision_internal_error_is_not_misclassified_as_provider_failure(
    monkeypatch,
):
    events = []
    internal_failure = TypeError("internal observer programming error")
    config = SimpleNamespace(provider="openai", model="vision/model")
    monkeypatch.setattr(
        serve_worker,
        "_read_images",
        lambda _user_id, _ids: {
            "m1": {"image_mime": "image/png", "image_b64": "AAAA"}
        },
    )
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.vision_observer,
        "load_provider_config",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(
        serve_worker.vision_observer,
        "observe_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(internal_failure),
    )
    monkeypatch.setattr(
        serve_worker.vision_observer,
        "classify_vision_error",
        lambda _exc: pytest.fail(
            "an internal exception must not be reclassified as a provider failure"
        ),
    )
    monkeypatch.setattr(
        serve_worker,
        "_emit_v2_debug_trace_for_user",
        lambda user_id, event_type, **kwargs: events.append(
            {"user_id": user_id, "type": event_type, **kwargs}
        ),
    )

    with pytest.raises(TypeError) as caught:
        serve_worker._read_vision_observations(
            "u1",
            [{"message_id": "m1", "route_id": "route-123"}],
        )

    assert caught.value is internal_failure
    assert [event["type"] for event in events] == [
        "vision.provider.called",
        "vision.batch.budget.evaluated",
    ]
    assert "vision_model_failed" not in json.dumps(events)
