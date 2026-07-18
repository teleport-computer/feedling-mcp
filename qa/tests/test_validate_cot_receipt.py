from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from qa import cot_delivery_probe as probe
from qa import validate_cot_receipt as validator
from tools.provider_smoke.client import Session


PROFILE_ID = "official-gemini"


def _passing_receipt() -> dict[str, object]:
    return {
        "schema_version": 2,
        "profile_id": PROFILE_ID,
        "request_id": "request-1",
        "turn_id": "request-1",
        "trace_id": "request-1",
        "reply_message_id": "reply-1",
        "status": "PASS",
        "failure_code": "NONE",
        "requested_effort": "medium",
        "configured_effort": "medium",
        "configured_effort_attested": True,
        "configured_route_matches_manifest": True,
        "effective_effort": "unknown",
        "effective_effort_attested": False,
        "release_qualified": False,
        "delivery_qualified": True,
        "final_answer_correct": True,
        "ack_latency_ms": 25.0,
        "reply_latency_ms": 800.0,
        "model_duration_ms": 700.0,
        "provider_api_duration_ms": 650.0,
        "trace_dropped": False,
        "model_call_count": 1,
        "agent_reply_count": 1,
        "chat_response_count": 1,
        "chat_response_match_count": 1,
        "model_thinking_present": True,
        "model_thinking_len": 42,
        "reasoning_event_count": 1,
        "model_thinking_source": "pi_thinking",
        "agent_reply_thinking_kind": "provider_reasoning_summary",
        "delivered_thinking_present": True,
        "delivered_thinking_len": 24,
        "delivered_thinking_kind": "provider_reasoning_summary",
        "delivered_thinking_source": "pi_thinking",
        "delivered_thinking_model": "gemini-2.5-flash",
        "delivered_thinking_native": True,
        "metadata_present": True,
        "user_visible_disclosure_present": True,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
        "raw_reply_stored": False,
        "raw_thinking_stored": False,
        "raw_trace_stored": False,
    }


def _write_receipt(
    tmp_path: Path, receipt: dict[str, object], *, pretty: bool = False
) -> Path:
    path = tmp_path / "receipt.json"
    serialized = json.dumps(receipt, indent=2 if pretty else None)
    path.write_text(serialized, encoding="utf-8")
    path.chmod(0o600)
    return path


def _default_delivery(receipt: dict[str, object]) -> None:
    receipt.update(
        {
            "delivered_thinking_present": False,
            "delivered_thinking_len": 0,
            "delivered_thinking_kind": "",
            "delivered_thinking_source": "",
            "delivered_thinking_model": "",
            "delivered_thinking_native": None,
            "metadata_present": False,
            "user_visible_disclosure_present": False,
        }
    )


def _zero_trace(receipt: dict[str, object]) -> None:
    receipt.update(
        {
            "trace_dropped": False,
            "model_call_count": 0,
            "agent_reply_count": 0,
            "chat_response_count": 0,
            "chat_response_match_count": 0,
            "model_thinking_present": False,
            "model_thinking_len": 0,
            "reasoning_event_count": 0,
            "model_thinking_source": "",
            "agent_reply_thinking_kind": "",
            "model_duration_ms": None,
            "provider_api_duration_ms": None,
        }
    )


def test_accepts_probe_contract_and_returns_canonical_sha256(tmp_path):
    receipt = _passing_receipt()
    path = _write_receipt(tmp_path, receipt, pretty=True)

    parsed, digest = validator.validate_cot_receipt(path, PROFILE_ID)

    canonical = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    assert parsed == receipt
    assert digest == hashlib.sha256(canonical).hexdigest()
    assert len(digest) == 64


def test_decoded_receipt_validator_matches_file_validator(tmp_path):
    receipt = _passing_receipt()

    decoded, decoded_digest = validator.validate_cot_receipt_document(
        receipt, PROFILE_ID
    )
    loaded, loaded_digest = validator.validate_cot_receipt(
        _write_receipt(tmp_path, receipt), PROFILE_ID
    )

    assert decoded == loaded == receipt
    assert decoded_digest == loaded_digest


@pytest.mark.parametrize("count", (0, 192))
def test_accepts_explicit_reasoning_token_metadata(tmp_path, count):
    receipt = _passing_receipt()
    receipt.update(
        token_metadata_status="PRESENT",
        reasoning_token_count=count,
    )

    parsed, _ = validator.validate_cot_receipt(
        _write_receipt(tmp_path, receipt), PROFILE_ID
    )

    assert parsed["token_metadata_status"] == "PRESENT"
    assert parsed["reasoning_token_count"] == count


@pytest.mark.parametrize("count", (None, -1, True, 1.5))
def test_present_token_metadata_requires_nonnegative_integer(tmp_path, count):
    receipt = _passing_receipt()
    receipt.update(
        token_metadata_status="PRESENT",
        reasoning_token_count=count,
    )

    with pytest.raises(validator.CotReceiptError, match="token count"):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


def test_accepts_bounded_printable_unicode_model_label(tmp_path):
    receipt = _passing_receipt()
    receipt["delivered_thinking_model"] = "[特价纯血] Claude Sonnet 4.5"

    parsed, _ = validator.validate_cot_receipt(
        _write_receipt(tmp_path, receipt), PROFILE_ID
    )

    assert parsed["delivered_thinking_model"] == "[特价纯血] Claude Sonnet 4.5"


def test_accepts_receipt_emitted_by_current_probe(tmp_path, monkeypatch):
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    output = private_root / "cot-receipt.json"

    monkeypatch.setattr(
        probe,
        "load_profile_context",
        lambda *_args, **_kwargs: (
            {
                "profile_id": PROFILE_ID,
                "provider": "gemini",
                "configured_model": "gemini-2.5-flash",
                "configured_base_url": "",
                "reasoning_effort": "medium",
            },
            probe.LOCKED_BASE_URL,
            Session(
                user_id="synthetic-user",
                api_key="private-key",
                sk=b"s" * 32,
                pk=b"p" * 32,
            ),
        ),
    )

    class FakeClient:
        def _req(self, method, path, *, api_key, attempts, read_timeout):
            assert (method, path) == ("GET", "/v1/model_api/get")
            assert api_key == "private-key"
            assert (attempts, read_timeout) == (2, 15)
            return 200, {
                "config": {
                    "configured": True,
                    "provider": "gemini",
                    "model": "gemini-2.5-flash",
                    "base_url": "",
                    "reasoning_effort": "medium",
                }
            }

        def send(self, _session, _text, *, read_timeout):
            assert read_timeout == probe.CHAT_SEND_TIMEOUT_SECONDS
            return {"user_message": {"id": "request-1", "ts": 100.0}}

        def poll_reply_record(self, *_args, **_kwargs):
            return {"reply": "323", "message": {"id": "reply-1"}}

    monkeypatch.setattr(
        probe,
        "decrypt_reply_record",
        lambda *_args: {
            "thinking_present": True,
            "thinking": "sanitized by the probe before persistence",
            "thinking_kind": "provider_reasoning_summary",
            "thinking_source": "pi_thinking",
            "thinking_model": "gemini-2.5-flash",
            "thinking_native": True,
        },
    )
    monkeypatch.setattr(
        probe,
        "_poll_trace",
        lambda *_args, **_kwargs: {
            "dropped": False,
            "model_call_count": 1,
            "agent_reply_count": 1,
            "chat_response_count": 1,
            "chat_response_match_count": 1,
            "model_thinking_present": True,
            "model_thinking_len": 42,
            "model_thinking_source": "pi_thinking",
            "agent_reply_thinking_kind": "provider_reasoning_summary",
            "model_duration_ms": 700.0,
            "provider_api_duration_ms": 650.0,
        },
    )

    emitted = probe.run_probe(
        tmp_path / "unused-manifest.json",
        output,
        nonce="nonce_123456",
        expected_profile_id=PROFILE_ID,
        client=FakeClient(),
    )
    parsed, _ = validator.validate_cot_receipt(output, PROFILE_ID)

    assert set(emitted) == validator.RECEIPT_KEYS
    assert parsed == emitted


@pytest.mark.parametrize(
    ("status", "code", "mutate"),
    [
        ("PASS", "NONE", lambda _receipt: None),
        (
            "FAIL",
            "FINAL_ANSWER_WRONG",
            lambda receipt: receipt.update(final_answer_correct=False),
        ),
        (
            "FAIL",
            "DOWNSTREAM_PARSE_DROPPED_REASONING",
            lambda receipt: receipt.update(agent_reply_thinking_kind=""),
        ),
        (
            "FAIL",
            "THINKING_ENVELOPE_NOT_DELIVERED",
            _default_delivery,
        ),
        ("FAIL", "THINKING_ENVELOPE_UNREADABLE", _default_delivery),
        (
            "FAIL",
            "THINKING_METADATA_INVALID",
            lambda receipt: receipt.update(
                delivered_thinking_native=False, metadata_present=False
            ),
        ),
        (
            "UNVERIFIED",
            "MODEL_REASONING_NOT_OBSERVED",
            lambda receipt: receipt.update(
                model_thinking_present=False,
                model_thinking_len=0,
                reasoning_event_count=0,
                model_thinking_source="",
            ),
        ),
        (
            "UNVERIFIED",
            "TRACE_AMBIGUOUS",
            lambda receipt: receipt.update(
                trace_dropped=True,
                model_thinking_present=False,
                model_thinking_len=0,
                reasoning_event_count=0,
                model_thinking_source="",
            ),
        ),
        ("UNVERIFIED", "TRACE_UNAVAILABLE", _zero_trace),
    ],
)
def test_accepts_fixed_status_failure_pairs(tmp_path, status, code, mutate):
    receipt = _passing_receipt()
    receipt.update(status=status, failure_code=code, delivery_qualified=status == "PASS")
    mutate(receipt)

    parsed, _ = validator.validate_cot_receipt(
        _write_receipt(tmp_path, receipt), PROFILE_ID
    )

    assert parsed["status"] == status
    assert parsed["failure_code"] == code


def test_accepts_chat_timeout_and_request_failure_states(tmp_path):
    timeout = _passing_receipt()
    timeout.update(
        status="UNVERIFIED",
        failure_code="CHAT_TIMEOUT",
        delivery_qualified=False,
        final_answer_correct=False,
        reply_message_id="",
        reply_latency_ms=None,
    )
    _zero_trace(timeout)
    _default_delivery(timeout)
    validator.validate_cot_receipt(_write_receipt(tmp_path, timeout), PROFILE_ID)

    failed = _passing_receipt()
    failed.update(
        status="UNVERIFIED",
        failure_code="CHAT_REQUEST_FAILED",
        delivery_qualified=False,
        final_answer_correct=False,
        request_id="",
        turn_id="",
        trace_id="",
        reply_message_id="",
        ack_latency_ms=None,
        reply_latency_ms=None,
    )
    _zero_trace(failed)
    _default_delivery(failed)
    validator.validate_cot_receipt(_write_receipt(tmp_path, failed), PROFILE_ID)

    failed_after_ack = dict(failed)
    failed_after_ack.update(
        request_id="request-1",
        turn_id="request-1",
        trace_id="request-1",
        ack_latency_ms=25.0,
    )
    validator.validate_cot_receipt(
        _write_receipt(tmp_path, failed_after_ack), PROFILE_ID
    )


@pytest.mark.parametrize("raw_key", ["thinking", "reply", "raw_trace", "secret"])
def test_rejects_extra_or_raw_text_fields(tmp_path, raw_key):
    receipt = _passing_receipt()
    receipt[raw_key] = "raw private text"

    with pytest.raises(validator.CotReceiptError, match="shape"):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


def test_rejects_missing_and_duplicate_keys(tmp_path):
    missing = _passing_receipt()
    missing.pop("trace_id")
    with pytest.raises(validator.CotReceiptError, match="shape"):
        validator.validate_cot_receipt(_write_receipt(tmp_path, missing), PROFILE_ID)

    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(validator.CotReceiptError, match="duplicate"):
        validator.validate_cot_receipt(path, PROFILE_ID)


def test_rejects_profile_mismatch(tmp_path):
    with pytest.raises(validator.CotReceiptError, match="profile assignment"):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, _passing_receipt()), "official-openai"
        )


@pytest.mark.parametrize(
    ("configured_effort", "attested", "route_matches"),
    [
        ("off", True, True),
        ("medium", True, False),
        ("unknown", False, False),
    ],
)
def test_accepts_configuration_context_without_changing_delivery_status(
    tmp_path, configured_effort, attested, route_matches
):
    receipt = _passing_receipt()
    receipt.update(
        configured_effort=configured_effort,
        configured_effort_attested=attested,
        configured_route_matches_manifest=route_matches,
    )

    parsed, _ = validator.validate_cot_receipt(
        _write_receipt(tmp_path, receipt), PROFILE_ID
    )

    assert parsed["status"] == "PASS"
    assert parsed["failure_code"] == "NONE"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"requested_effort": "low"}, "requested effort"),
        ({"effective_effort": "medium"}, "effective effort"),
        ({"effective_effort_attested": True}, "effective effort"),
        (
            {
                "configured_effort": "unknown",
                "configured_effort_attested": True,
            },
            "configured effort",
        ),
        (
            {
                "configured_effort": "off",
                "configured_effort_attested": False,
                "configured_route_matches_manifest": False,
            },
            "configured effort",
        ),
        (
            {
                "configured_effort": "unknown",
                "configured_effort_attested": False,
                "configured_route_matches_manifest": True,
            },
            "configured route",
        ),
    ],
)
def test_rejects_untruthful_or_inconsistent_effort_evidence(
    tmp_path, changes, message
):
    receipt = _passing_receipt()
    receipt.update(changes)

    with pytest.raises(validator.CotReceiptError, match=message):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        ("FAIL", "REASONING_CONFIGURATION_MISMATCH"),
        ("UNVERIFIED", "REASONING_CONFIGURATION_UNAVAILABLE"),
    ],
)
def test_rejects_configuration_as_delivery_status(tmp_path, status, failure_code):
    receipt = _passing_receipt()
    receipt.update(
        status=status,
        failure_code=failure_code,
        delivery_qualified=False,
    )

    with pytest.raises(validator.CotReceiptError, match="status and failure code"):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_qualified", True),
        ("schema_version", 1.0),
        ("raw_reply_stored", True),
        ("raw_thinking_stored", True),
        ("raw_trace_stored", True),
        ("ack_latency_ms", -1),
        ("reply_latency_ms", float("inf")),
        ("model_duration_ms", float("nan")),
        ("provider_api_duration_ms", True),
        ("model_call_count", -1),
        ("agent_reply_count", 1.5),
        ("reasoning_event_count", True),
        ("delivered_thinking_native", 1),
        ("reasoning_token_count", 10),
        ("token_metadata_status", "AVAILABLE"),
    ],
)
def test_rejects_unsafe_boolean_numeric_and_token_values(tmp_path, field, value):
    receipt = _passing_receipt()
    receipt[field] = value

    with pytest.raises(validator.CotReceiptError):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.update(turn_id="another-turn"),
        lambda receipt: receipt.update(delivery_qualified=False),
        lambda receipt: receipt.update(final_answer_correct=False),
        lambda receipt: receipt.update(chat_response_match_count=2),
        lambda receipt: receipt.update(reasoning_event_count=0),
        lambda receipt: receipt.update(metadata_present=False),
        lambda receipt: receipt.update(delivered_thinking_present=False),
        lambda receipt: receipt.update(reply_latency_ms=1.0),
        lambda receipt: receipt.update(status="FAIL", failure_code="NONE"),
        lambda receipt: receipt.update(
            status="UNVERIFIED",
            failure_code="TRACE_AMBIGUOUS",
            delivery_qualified=False,
        ),
    ],
)
def test_rejects_internally_inconsistent_receipts(tmp_path, mutate):
    receipt = _passing_receipt()
    mutate(receipt)

    with pytest.raises(validator.CotReceiptError):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


@pytest.mark.parametrize(
    "model",
    ["model\nraw-text", "model\x00raw-text", "x" * 257],
)
def test_rejects_control_characters_and_oversized_model_labels(tmp_path, model):
    receipt = _passing_receipt()
    receipt["delivered_thinking_model"] = model

    with pytest.raises(validator.CotReceiptError, match="model"):
        validator.validate_cot_receipt(
            _write_receipt(tmp_path, receipt), PROFILE_ID
        )


def test_requires_absolute_owner_only_regular_single_link_file(tmp_path, monkeypatch):
    receipt = _passing_receipt()
    path = _write_receipt(tmp_path, receipt)

    path.chmod(0o644)
    with pytest.raises(validator.CotReceiptError, match="unsafe"):
        validator.validate_cot_receipt(path, PROFILE_ID)
    path.chmod(0o600)

    symlink = tmp_path / "receipt-link.json"
    symlink.symlink_to(path)
    with pytest.raises(validator.CotReceiptError, match="unsafe"):
        validator.validate_cot_receipt(symlink, PROFILE_ID)

    hardlink = tmp_path / "receipt-hardlink.json"
    os.link(path, hardlink)
    with pytest.raises(validator.CotReceiptError, match="unsafe"):
        validator.validate_cot_receipt(path, PROFILE_ID)
    hardlink.unlink()

    monkeypatch.chdir(tmp_path)
    with pytest.raises(validator.CotReceiptError, match="unsafe"):
        validator.validate_cot_receipt(Path("receipt.json"), PROFILE_ID)


def test_rejects_empty_oversized_and_non_json_files(tmp_path):
    path = tmp_path / "receipt.json"
    path.touch(mode=0o600)
    with pytest.raises(validator.CotReceiptError, match="unsafe"):
        validator.validate_cot_receipt(path, PROFILE_ID)

    path.write_bytes(b"x" * (validator.MAX_RECEIPT_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(validator.CotReceiptError, match="unsafe"):
        validator.validate_cot_receipt(path, PROFILE_ID)

    path.write_text("not json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(validator.CotReceiptError, match="JSON"):
        validator.validate_cot_receipt(path, PROFILE_ID)
