from __future__ import annotations

import base64
import json
import os
import stat
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import genesis_e2e


FORBIDDEN = "QA-PRIVATE-CANARY-7F3A"
RELATIONSHIP_STARTED_AT = (
    datetime.now(timezone.utc).date() - timedelta(days=5)
).isoformat()
ARCHIVE_FILENAMES = (
    "mira-chat-history.txt",
    "mira-ai-persona.md",
    "rowan-personal-profile.txt",
    "mira-memory-summary.md",
)


def _fixture() -> dict:
    return {
        "materials": {
            "format": "auto",
            "chat_history": "User: tea and a walk help me reset.",
            "ai_persona": "Name: Mira. Warm and grounded.",
            "personal_profile": f"The user owns this private marker: {FORBIDDEN}",
            "memory_summary": "The reset ritual is jasmine tea and a walk.",
            "upload_files": {
                "chat_history": {
                    "filename": "mira-chat-history.txt",
                    "content_type": "text/plain",
                },
                "ai_persona": {
                    "filename": "mira-ai-persona.md",
                    "content_type": "text/markdown",
                },
                "personal_profile": {
                    "filename": "rowan-personal-profile.txt",
                    "content_type": "text/plain",
                },
                "memory_summary": {
                    "filename": "mira-memory-summary.md",
                    "content_type": "text/markdown",
                },
            },
        },
        "persona": {
            "agent_name": "Mira",
            "category": "warm",
            "dimensions": [{"name": "grounded"}],
            "self_introduction_keywords": ["Mira"],
        },
        "relationship": {
            "relationship_started_at": RELATIONSHIP_STARTED_AT,
        },
        "ground_truth": {
            "facts": [
                {
                    "id": "reset-ritual",
                    "text": "The reset ritual is jasmine tea and a walk.",
                    "keywords": ["jasmine tea", "walk"],
                }
            ]
        },
        "privacy": {"forbidden_in_agent_identity_or_persona": [FORBIDDEN]},
    }


def _semantic_judgment(evidence_sha256: str, **overrides: bool) -> dict:
    judgment = {
        "schema_version": 1,
        "judge": "qualification_agent",
        "evidence_sha256": evidence_sha256,
        "reviewed_surfaces": ["identity", "persona", "memories"],
        "reviewed_fact_ids": ["reset-ritual"],
        "persona_identity_consistent": True,
        "ground_truth_facts_supported": True,
        "contradictions_absent": True,
    }
    judgment.update(overrides)
    return judgment


def _capture_paths(tmp_path: Path, name: str = "capture") -> dict[str, str]:
    artifact_dir = tmp_path / f"{name}-artifacts"
    artifact_dir.mkdir(exist_ok=True)
    return {
        "private_evidence_path": str(tmp_path / f"{name}-private-evidence.json"),
        "artifact_dir": str(artifact_dir),
    }


def _archive_ok(index: int) -> tuple[int, dict]:
    archive_id = f"{index:032x}"
    return 201, {
        "status": "ok",
        "archive_id": archive_id,
        "key": (
            f"onboarding/user-existing-1/{archive_id}/"
            f"{ARCHIVE_FILENAMES[index - 1]}"
        ),
    }


def test_persona_transport_rejects_redirects_before_forwarding_account_key():
    request = urllib.request.Request(
        "https://test-api.feedling.app/v1/genesis/imports/plaintext",
        headers={"X-API-Key": "feedling-existing-api-key"},
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        genesis_e2e._RejectRedirects().redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            {},
            "https://attacker.invalid/capture",
        )

    assert exc_info.value.code == 307
    assert exc_info.value.url == request.full_url


def test_checked_in_fixture_hydrates_exact_upload_file_bytes():
    fixture_path = (
        Path(genesis_e2e.__file__).resolve().parent.parent
        / "qa"
        / "fixtures"
        / "persona-import-v1.json"
    )
    raw_fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert not any(
        material in raw_fixture["materials"]
        for material, _field in genesis_e2e._UPLOAD_MATERIALS
    )

    fixture = genesis_e2e._load_fixture(str(fixture_path))
    uploads = genesis_e2e._qualification_material_uploads(fixture)

    assert [upload["material"] for upload in uploads] == [
        "chat_history",
        "ai_persona",
        "personal_profile",
        "memory_summary",
    ]
    for upload in uploads:
        spec = fixture["materials"]["upload_files"][upload["material"]]
        exact_bytes = (fixture_path.parent / spec["path"]).read_bytes()
        assert upload["content"] == exact_bytes
        assert upload["content_sha256"] == genesis_e2e._sha256_hex(exact_bytes)


def test_multipart_encoder_preserves_exact_file_content_and_rejects_injection():
    content = b"line one\nline two\n"
    body, content_type = genesis_e2e._multipart_form_body(
        {"client_job_id": "qa-job-fixed", "filename": "persona.md"},
        {
            "field_name": "file",
            "filename": "persona.md",
            "content_type": "text/markdown",
            "content": content,
        },
    )

    assert content_type.startswith("multipart/form-data; boundary=----feedling-qa-")
    assert body.count(content) == 1
    assert b'name="file"; filename="persona.md"' in body
    assert body.endswith(b"--\r\n")

    with pytest.raises(ValueError, match="header injection"):
        genesis_e2e._multipart_form_body(
            {"client_job_id": "qa-job-fixed"},
            {
                "field_name": "file",
                "filename": "persona.md\r\nX-Evil: yes",
                "content_type": "text/plain",
                "content": b"safe",
            },
        )


def test_existing_session_two_phase_flow_reuses_profile_and_sanitizes_output(
    monkeypatch, tmp_path: Path
):
    calls: list[dict] = []

    def fake_request(
        method,
        url,
        api_key,
        *,
        json_body=None,
        multipart_fields=None,
        file_upload=None,
        **_kwargs,
    ):
        assert api_key == "feedling-existing-api-key"
        calls.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "multipart_fields": multipart_fields,
                "file_upload": file_upload,
                "request_options": _kwargs,
            }
        )
        if method == "POST" and url.endswith("/v1/onboarding/archive"):
            archive_index = sum(
                call["url"].endswith("/v1/onboarding/archive") for call in calls
            )
            return _archive_ok(archive_index)
        if method == "POST" and url.endswith("/v1/genesis/imports/plaintext"):
            return 202, {"job_id": "genesis_0123456789abcdef"}
        if method == "GET" and url.endswith(
            "/v1/genesis/imports/genesis_0123456789abcdef"
        ):
            return 200, {
                "job": {
                    "job_id": "genesis_0123456789abcdef",
                    "status": "done",
                    "voice_ref": "voice-existing-1",
                    "metadata": {
                        "client_job_id": "qa-existing-fixed",
                        "file_count": 4,
                        "history_count": 6,
                        "ai_persona_count": 1,
                        "user_profile_count": 1,
                        "memory_summary_count": 1,
                    },
                },
                "persona": {
                    "content_envelope": {
                        "kind": "persona",
                        "body_ct": "ciphertext",
                        "owner_user_id": "user-existing-1",
                    }
                },
            }
        if method == "GET" and url.endswith("/v1/identity/get"):
            return 200, {
                "identity": {
                    "kind": "identity",
                    "body_ct": "ciphertext",
                    "owner_user_id": "user-existing-1",
                    "days_with_user": 5,
                    "relationship_started_at": RELATIONSHIP_STARTED_AT,
                }
            }
        if method == "GET" and "/v1/chat/history" in url:
            return 200, {
                "messages": [
                    {
                        "kind": "greeting",
                        "id": "greeting-existing-1",
                        "role": "agent",
                        "body_ct": "ciphertext",
                        "owner_user_id": "user-existing-1",
                        "content": "PLAINTEXT DECOY MUST BE IGNORED",
                    }
                ]
            }
        if method == "GET" and "/v1/memory/list" in url:
            return 200, {
                "moments": [
                    {
                        "kind": "memory",
                        "id": "memory-reset",
                        "body_ct": "ciphertext",
                        "owner_user_id": "user-existing-1",
                        "description": "PLAINTEXT DECOY MUST BE IGNORED",
                    }
                ]
            }
        if method == "GET" and url.endswith("/v1/onboarding/validate"):
            return 200, {"passing": True}
        raise AssertionError(f"unexpected request: {method} {url}")

    def fake_decrypt(envelope, private_key):
        assert private_key == b"k" * 32
        if envelope.get("kind") == "identity":
            return json.dumps(
                {
                    "agent_name": "Mira",
                    "category": "warm",
                    "dimensions": [
                        {
                            "name": "grounded",
                            "description": "Offers concrete next steps.",
                        }
                    ],
                    "self_introduction": "I am Mira.",
                }
            )
        if envelope.get("kind") == "persona":
            return "Mira is a warm, grounded companion."
        if envelope.get("kind") == "greeting":
            return "Hello."
        if envelope.get("kind") == "memory":
            return json.dumps(
                {"description": "Jasmine tea and a walk are the reset ritual."}
            )
        raise AssertionError("unexpected encrypted envelope")

    monkeypatch.setattr(genesis_e2e, "_decrypt_envelope_user", fake_decrypt)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    private_evidence = tmp_path / "private-evidence.json"
    receipt = genesis_e2e.capture_existing_session_distill_evidence(
        api_url="https://test-api.feedling.app",
        api_key="feedling-existing-api-key",
        user_id="user-existing-1",
        content_private_key=b"k" * 32,
        fixture=_fixture(),
        private_evidence_path=str(private_evidence),
        artifact_dir=str(artifact_dir),
        timeout=1,
        poll=0,
        intro_timeout=1,
        client_job_id="qa-existing-fixed",
        request_fn=fake_request,
    )
    assert receipt["phase"] == "CAPTURED"
    assert stat.S_IMODE(private_evidence.stat().st_mode) == 0o600
    captured_text = private_evidence.read_text(encoding="utf-8")
    assert "Mira is a warm, grounded companion" in captured_text
    assert "Jasmine tea and a walk are the reset ritual" in captured_text

    judgment_path = tmp_path / "semantic-judgment.json"
    judgment_path.write_text(
        json.dumps(_semantic_judgment(receipt["evidence_sha256"])),
        encoding="utf-8",
    )
    judgment_path.chmod(0o600)
    report = genesis_e2e.finalize_existing_session_distill_acceptance(
        private_evidence_path=str(private_evidence),
        semantic_judgment_path=str(judgment_path),
        fixture=_fixture(),
        artifact_dir=str(artifact_dir),
    )

    assert report["ok"] is True
    assert private_evidence.exists() is False
    assert report["evidence"] == {
        "sha256": receipt["evidence_sha256"],
        "semantic_judgment_bound": True,
        "private_evidence_deleted": True,
    }
    assert report["transport"] == {
        "used_existing_session": True,
        "created_user": False,
        "configured_provider": False,
        "job_status": "done",
        "archive_upload_count": 4,
        "archive_receipts": [
            {
                "material": material,
                "filename": spec["filename"],
                "content_type": spec["content_type"],
                "content_sha256": genesis_e2e._sha256_hex(
                    _fixture()["materials"][material].encode("utf-8")
                ),
                "size_bytes": len(_fixture()["materials"][material].encode("utf-8")),
                "http_status": 201,
                "archive_id": f"{index:032x}",
                "upload_accepted": True,
                "storage_key_scope_verified": True,
            }
            for index, (material, spec) in enumerate(
                _fixture()["materials"]["upload_files"].items(), start=1
            )
        ],
        "genesis_upload_metadata": {
            "client_job_id_exposed": True,
            "client_job_id_matched": True,
            "file_count_exposed": True,
            "file_count": 4,
            "source_counts_exposed": True,
            "source_families": [
                "history",
                "ai_persona",
                "user_profile",
                "memory_summary",
            ],
        },
        "upload_http_status": 202,
        "memory_http_status": 200,
        "validate_http_status": 200,
        "memory_decrypt_error_count": 0,
        "chat_decrypt_error_count": 0,
        "decrypted_agent_message_count": 1,
    }
    assert report["checks"]["identity_envelope_decrypted"] is True
    assert report["checks"]["persona_envelope_decrypted"] is True
    assert report["checks"]["memory_envelopes_decrypted"] is True
    assert report["checks"]["chat_envelopes_decrypted"] is True
    assert report["checks"]["archive_receipts_verified"] is True
    assert report["checks"]["genesis_upload_metadata_verified"] is True
    urls = [call["url"] for call in calls]
    assert not any("/v1/users/register" in url for url in urls)
    assert not any("/v1/model_api/setup" in url for url in urls)
    assert not any("/v1/model_api/delete" in url for url in urls)
    assert sum(url.endswith("/v1/onboarding/archive") for url in urls) == 4
    assert sum(url.endswith("/v1/genesis/imports/plaintext") for url in urls) == 1
    archive_calls = [
        call for call in calls if call["url"].endswith("/v1/onboarding/archive")
    ]
    fixture = _fixture()
    for call, (material, spec) in zip(
        archive_calls,
        fixture["materials"]["upload_files"].items(),
        strict=True,
    ):
        assert call["multipart_fields"] == {
            "filename": spec["filename"],
            "content_type": spec["content_type"],
            "client_job_id": "qa-existing-fixed",
        }
        assert call["file_upload"] == {
            "field_name": "file",
            "filename": spec["filename"],
            "content_type": spec["content_type"],
            "content": fixture["materials"][material].encode("utf-8"),
        }
        assert call["request_options"] == {"retries": 1}
    upload_payload = next(
        call["json_body"]
        for call in calls
        if call["url"].endswith("/v1/genesis/imports/plaintext")
    )
    assert upload_payload == {
        "format": "auto",
        "content": "User: tea and a walk help me reset.",
        "fresh_start": False,
        "client_job_id": "qa-existing-fixed",
        "relationship_started_at": RELATIONSHIP_STARTED_AT,
        "ai_persona_content": "Name: Mira. Warm and grounded.",
        "personal_profile_content": f"The user owns this private marker: {FORBIDDEN}",
        "memory_summary_content": "The reset ritual is jasmine tea and a walk.",
        "history_filename": "mira-chat-history.txt",
        "ai_persona_filename": "mira-ai-persona.md",
        "personal_profile_filename": "rowan-personal-profile.txt",
        "memory_summary_filename": "mira-memory-summary.md",
    }
    rendered = json.dumps(report, ensure_ascii=False)
    rendered_receipt = json.dumps(receipt, ensure_ascii=False)
    assert FORBIDDEN not in rendered
    assert "Mira is a warm, grounded companion" not in rendered
    assert "Jasmine tea and a walk are the reset ritual" not in rendered
    assert "onboarding/user-existing-1/" not in rendered
    assert "feedling-existing-api-key" not in rendered
    assert "Mira is a warm, grounded companion" not in rendered_receipt
    assert "Jasmine tea and a walk are the reset ritual" not in rendered_receipt
    assert "onboarding/user-existing-1/" not in rendered_receipt
    assert "feedling-existing-api-key" not in rendered_receipt


def test_privacy_firewall_covers_identity_persona_and_self_introduction_without_echoing_content():
    report = genesis_e2e.evaluate_distill_acceptance(
        _fixture(),
        identity={
            "agent_name": "Mira",
            "category": f"warm {FORBIDDEN}",
            "dimensions": [
                {"name": "grounded", "description": "Offers concrete next steps."}
            ],
            "self_introduction": f"I am Mira. {FORBIDDEN}",
        },
        identity_meta={
            "days_with_user": 5,
            "relationship_started_at": RELATIONSHIP_STARTED_AT,
        },
        memories=[
            {
                "id": "memory-reset",
                "description": "Jasmine tea and a walk are the reset ritual.",
            }
        ],
        validate={"passing": True},
        persona_text=f"Mira is grounded. {FORBIDDEN}",
        voice_text="Warm voice.",
        greeting_messages=[{"role": "agent", "content": "Hello."}],
        job={"job_id": "job-private", "status": "done"},
    )

    assert report["ok"] is False
    assert report["checks"]["privacy_identity_clear"] is False
    assert report["checks"]["privacy_persona_clear"] is False
    assert report["checks"]["privacy_self_introduction_clear"] is False
    assert report["privacy"] == {
        "forbidden_value_count": 1,
        "violation_count": 3,
        "violating_surfaces": ["identity", "persona", "self_introduction"],
    }
    rendered = json.dumps(report, ensure_ascii=False)
    assert FORBIDDEN not in rendered
    assert "warm QA-PRIVATE" not in rendered
    assert "Mira is grounded" not in rendered


def test_relationship_preservation_requires_exact_anchor_and_derived_days(monkeypatch):
    start = datetime.fromisoformat(RELATIONSHIP_STARTED_AT).date()
    monkeypatch.setattr(genesis_e2e, "_utc_today", lambda: start + timedelta(days=5))
    common = {
        "identity": {},
        "memories": [],
        "validate": {},
        "persona_text": "",
        "voice_text": "",
        "greeting_messages": [],
        "job": {},
    }

    for days in (4, 5, 6):
        report = genesis_e2e.evaluate_distill_acceptance(
            _fixture(),
            identity_meta={
                "days_with_user": days,
                "relationship_started_at": RELATIONSHIP_STARTED_AT,
            },
            **common,
        )
        assert report["checks"]["relationship_started_at"] is True
        assert report["checks"]["relationship_days"] is True

    wrong_anchor = genesis_e2e.evaluate_distill_acceptance(
        _fixture(),
        identity_meta={
            "days_with_user": 5,
            "relationship_started_at": "2020-01-01",
        },
        **common,
    )
    wrong_days = genesis_e2e.evaluate_distill_acceptance(
        _fixture(),
        identity_meta={
            "days_with_user": 0,
            "relationship_started_at": RELATIONSHIP_STARTED_AT,
        },
        **common,
    )

    assert wrong_anchor["checks"]["relationship_started_at"] is False
    assert wrong_anchor["checks"]["relationship_days"] is True
    assert wrong_days["checks"]["relationship_started_at"] is True
    assert wrong_days["checks"]["relationship_days"] is False


def test_existing_session_upload_failure_does_not_echo_server_body(tmp_path: Path):
    archive_count = 0

    def rejected(_method, url, *_args, **_kwargs):
        nonlocal archive_count
        if url.endswith("/v1/onboarding/archive"):
            archive_count += 1
            return _archive_ok(archive_count)
        return 400, {"error": f"private server detail: {FORBIDDEN}"}

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path),
            request_fn=rejected,
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "upload",
        "code": "upload_rejected",
        "http_status": 400,
    }
    assert FORBIDDEN not in str(exc_info.value)


def test_existing_session_archive_redirect_is_a_bounded_failure(tmp_path: Path):
    def redirected(*_args, **_kwargs):
        return 307, {"job_id": "genesis_0123456789abcdef"}

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path),
            request_fn=redirected,
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "archive_chat_history",
        "code": "redirect_rejected",
        "http_status": 307,
    }


def test_existing_session_rejects_untrusted_job_id_before_poll_url_interpolation(
    tmp_path: Path,
):
    calls = 0

    def malicious_response(_method, url, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if url.endswith("/v1/onboarding/archive"):
            return _archive_ok(calls)
        return 202, {"job_id": "../../admin/secrets?x=1"}

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path),
            request_fn=malicious_response,
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "upload",
        "code": "job_id_invalid",
        "http_status": 202,
    }
    assert calls == 5


def test_existing_session_rejects_malformed_archive_receipt_before_genesis(
    tmp_path: Path,
):
    calls = 0

    def malformed(_method, _url, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        return 201, {"status": "ok", "archive_id": "not-an-id", "key": "key"}

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path),
            request_fn=malformed,
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "archive_chat_history",
        "code": "archive_receipt_invalid",
        "http_status": 201,
    }
    assert calls == 1


def test_existing_session_rejects_archive_receipt_with_unscoped_storage_key(
    tmp_path: Path,
):
    def wrong_key(_method, _url, *_args, **_kwargs):
        return 201, {
            "status": "ok",
            "archive_id": "1" * 32,
            "key": "onboarding/another-user/" + "1" * 32 + "/mira-chat-history.txt",
        }

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path),
            request_fn=wrong_key,
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "archive_chat_history",
        "code": "archive_receipt_invalid",
        "http_status": 201,
    }


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        ({}, "job_client_job_id_missing"),
        ({"client_job_id": "wrong-job"}, "job_client_job_id_mismatch"),
        (
            {"client_job_id": "qa-existing-fixed"},
            "job_upload_metadata_missing",
        ),
        (
            {"client_job_id": "qa-existing-fixed", "file_count": 4},
            "job_upload_metadata_missing",
        ),
        (
            {"client_job_id": "qa-existing-fixed", "file_count": 3},
            "job_file_count_mismatch",
        ),
        (
            {
                "client_job_id": "qa-existing-fixed",
                "file_count": "not-a-number",
            },
            "job_file_count_invalid",
        ),
        (
            {
                "client_job_id": "qa-existing-fixed",
                "file_count": 4,
                "history_count": 1,
                "ai_persona_count": 1,
            },
            "job_source_counts_incomplete",
        ),
        (
            {
                "client_job_id": "qa-existing-fixed",
                "file_count": 4,
                "history_count": 1,
                "ai_persona_count": 1,
                "user_profile_count": 0,
                "memory_summary_count": 1,
            },
            "job_source_family_missing",
        ),
    ],
)
def test_existing_session_rejects_exposed_bad_genesis_upload_metadata(
    metadata: dict, expected_code: str
):
    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._job_upload_metadata_evidence(
            {"metadata": metadata}, "qa-existing-fixed"
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "distill",
        "code": expected_code,
    }


def test_existing_session_helper_rejects_non_test_target_before_transport(
    tmp_path: Path,
):
    called = False

    def unexpected_request(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport must not run")

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path),
            request_fn=unexpected_request,
        )

    assert exc_info.value.as_result()["code"] == "unsafe_target"
    assert called is False


def test_existing_session_rejects_job_token_genesis_would_normalize(
    tmp_path: Path,
):
    called = False

    def unexpected_request(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport must not run")

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            client_job_id="qa.existing.fixed",
            **_capture_paths(tmp_path),
            request_fn=unexpected_request,
        )

    assert exc_info.value.as_result()["code"] == "client_job_id_invalid"
    assert called is False


def test_existing_session_helper_requires_all_four_materials_before_transport(
    tmp_path: Path,
):
    fixture = _fixture()
    fixture["materials"].pop("memory_summary")

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=fixture,
            **_capture_paths(tmp_path),
            request_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("transport must not run")
            ),
        )

    assert exc_info.value.as_result()["code"] == "four_materials_required"


def test_existing_session_helper_requires_four_upload_file_specs_before_transport(
    tmp_path: Path,
):
    fixture = _fixture()
    fixture["materials"]["upload_files"].pop("memory_summary")

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=fixture,
            **_capture_paths(tmp_path),
            request_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("transport must not run")
            ),
        )

    assert exc_info.value.as_result()["code"] == "four_upload_files_required"


def test_manifest_session_loader_selects_one_existing_profile(tmp_path: Path):
    manifest = tmp_path / "private-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "profile_id": "official-openai",
                        "api_key": "feedling-existing-api-key",
                        "user_id": "user-existing-1",
                        "secret_key_b64": base64.b64encode(b"k" * 32).decode("ascii"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)

    api_key, user_id, private_key = genesis_e2e._load_manifest_session(
        str(manifest), "official-openai"
    )

    assert api_key == "feedling-existing-api-key"
    assert user_id == "user-existing-1"
    assert private_key == b"k" * 32


def _write_manifest(path: Path, *, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "profile_id": "official-openai",
                        "api_key": "feedling-existing-api-key",
                        "user_id": "user-existing-1",
                        "secret_key_b64": base64.b64encode(b"k" * 32).decode("ascii"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)


def _private_evidence_payload() -> dict:
    fixture = _fixture()
    return {
        "schema_version": 1,
        "fixture_sha256": genesis_e2e._sha256_hex(
            genesis_e2e._canonical_json_bytes(fixture)
        ),
        "expected_fact_ids": ["reset-ritual"],
        "identity": {
            "agent_name": "Mira",
            "category": "warm",
            "dimensions": [{"name": "grounded", "description": "Concrete and calm."}],
            "self_introduction": "I am Mira.",
        },
        "identity_meta": {
            "days_with_user": 5,
            "relationship_started_at": RELATIONSHIP_STARTED_AT,
        },
        "persona_text": "Mira is a warm, grounded companion.",
        "memories": [
            {
                "id": "memory-reset",
                "description": "Jasmine tea and a walk are the reset ritual.",
            }
        ],
        "greeting_messages": [{"role": "agent", "content": "Hello."}],
        "validate": {"passing": True},
        "voice_text": "Warm voice.",
        "job": {"job_id": "genesis_0123456789abcdef", "status": "done"},
        "capture_checks": {
            "archive_receipts_verified": True,
            "genesis_upload_metadata_verified": True,
            "identity_envelope_decrypted": True,
            "persona_envelope_decrypted": True,
            "memory_envelopes_decrypted": True,
            "chat_envelopes_decrypted": True,
        },
        "transport": {
            "used_existing_session": True,
            "created_user": False,
            "configured_provider": False,
            "job_status": "done",
            "archive_upload_count": 4,
            "archive_receipts": [
                {
                    "material": material,
                    "filename": spec["filename"],
                    "content_type": spec["content_type"],
                    "content_sha256": genesis_e2e._sha256_hex(
                        fixture["materials"][material].encode("utf-8")
                    ),
                    "size_bytes": len(fixture["materials"][material].encode("utf-8")),
                    "http_status": 201,
                    "archive_id": f"{index:032x}",
                    "upload_accepted": True,
                    "storage_key_scope_verified": True,
                }
                for index, (material, spec) in enumerate(
                    fixture["materials"]["upload_files"].items(), start=1
                )
            ],
            "genesis_upload_metadata": {
                "client_job_id_exposed": True,
                "client_job_id_matched": True,
                "file_count_exposed": True,
                "file_count": 4,
                "source_counts_exposed": True,
                "source_families": [
                    "history",
                    "ai_persona",
                    "user_profile",
                    "memory_summary",
                ],
            },
            "upload_http_status": 202,
            "memory_http_status": 200,
            "validate_http_status": 200,
            "memory_decrypt_error_count": 0,
            "chat_decrypt_error_count": 0,
            "decrypted_agent_message_count": 1,
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["transport"]["archive_receipts"][0].update(
            content_sha256="0" * 64
        ),
        lambda payload: payload["transport"]["archive_receipts"][0].update(
            filename="different-file.txt"
        ),
        lambda payload: payload["transport"]["archive_receipts"][1].update(
            archive_id=payload["transport"]["archive_receipts"][0]["archive_id"]
        ),
        lambda payload: payload["transport"]["archive_receipts"][0].update(
            storage_key_scope_verified=False
        ),
        lambda payload: payload["transport"]["genesis_upload_metadata"].update(
            client_job_id_matched=False
        ),
        lambda payload: payload["identity_meta"].update(
            relationship_started_at="2020-01-01"
        ),
    ],
)
def test_private_evidence_rejects_tampered_archive_bindings(mutate):
    payload = _private_evidence_payload()
    mutate(payload)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._decode_private_evidence(
            genesis_e2e._canonical_json_bytes(payload), _fixture()
        )

    assert exc_info.value.as_result()["code"] == "private_evidence_contract_invalid"


def _write_private_capture(
    tmp_path: Path, name: str = "finalize"
) -> tuple[Path, str, Path]:
    artifacts = tmp_path / f"{name}-artifacts"
    artifacts.mkdir(exist_ok=True)
    evidence_path = tmp_path / f"{name}-private-evidence.json"
    evidence_sha256 = genesis_e2e._write_private_evidence(
        str(evidence_path), str(artifacts), _private_evidence_payload()
    )
    return evidence_path, evidence_sha256, artifacts


def _write_judgment(path: Path, evidence_sha256: str, *, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(_semantic_judgment(evidence_sha256)),
        encoding="utf-8",
    )
    path.chmod(mode)


def test_import_readiness_finalizer_accepts_deterministic_capture_and_deletes_evidence(
    tmp_path: Path,
):
    evidence_path, evidence_sha256, artifacts = _write_private_capture(
        tmp_path, "import-ready"
    )

    receipt = genesis_e2e.finalize_existing_session_import_readiness(
        private_evidence_path=str(evidence_path),
        fixture=_fixture(),
        artifact_dir=str(artifacts),
    )

    assert receipt == {
        "schema_version": 1,
        "kind": "existing_session_import_readiness",
        "ok": True,
        "fixture_sha256": genesis_e2e._sha256_hex(
            genesis_e2e._canonical_json_bytes(_fixture())
        ),
        "evidence_sha256": evidence_sha256,
        "checks": {
            **{
                check: True
                for check in genesis_e2e._IMPORT_READINESS_DETERMINISTIC_CHECKS
            },
            **{
                check: True
                for check in genesis_e2e._IMPORT_READINESS_CAPTURE_CHECKS
            },
        },
        "private_evidence_deleted": True,
    }
    assert evidence_path.exists() is False
    assert FORBIDDEN not in json.dumps(receipt, sort_keys=True)


def test_import_readiness_finalizer_reports_deterministic_failure_without_plaintext(
    tmp_path: Path,
):
    artifacts = tmp_path / "import-failed-artifacts"
    artifacts.mkdir()
    evidence_path = tmp_path / "import-failed-private-evidence.json"
    payload = _private_evidence_payload()
    payload["persona_text"] = f"Mira is a grounded companion. {FORBIDDEN}"
    genesis_e2e._write_private_evidence(
        str(evidence_path), str(artifacts), payload
    )

    receipt = genesis_e2e.finalize_existing_session_import_readiness(
        private_evidence_path=str(evidence_path),
        fixture=_fixture(),
        artifact_dir=str(artifacts),
    )

    assert receipt["ok"] is False
    assert receipt["checks"]["privacy_persona_clear"] is False
    assert evidence_path.exists() is False
    assert FORBIDDEN not in json.dumps(receipt, sort_keys=True)
    assert "persona_text" not in json.dumps(receipt, sort_keys=True)


def test_import_readiness_finalizer_deletes_strictly_invalid_evidence(
    tmp_path: Path,
):
    artifacts = tmp_path / "import-invalid-artifacts"
    artifacts.mkdir()
    evidence_path = tmp_path / "import-invalid-private-evidence.json"
    payload = _private_evidence_payload()
    payload["unexpected_plaintext"] = FORBIDDEN
    genesis_e2e._write_private_evidence(
        str(evidence_path), str(artifacts), payload
    )

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.finalize_existing_session_import_readiness(
            private_evidence_path=str(evidence_path),
            fixture=_fixture(),
            artifact_dir=str(artifacts),
        )

    assert exc_info.value.as_result()["code"] == "private_evidence_contract_invalid"
    assert evidence_path.exists() is False


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_manifest_session_loader_requires_exactly_0600(tmp_path: Path, mode: int):
    manifest = tmp_path / "private-manifest.json"
    _write_manifest(manifest, mode=mode)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._load_manifest_session(str(manifest), "official-openai")

    assert exc_info.value.as_result()["code"] == "manifest_permissions_invalid"


def test_manifest_session_loader_rejects_symlink(tmp_path: Path):
    target = tmp_path / "real-private-manifest.json"
    _write_manifest(target)
    link = tmp_path / "private-manifest.json"
    link.symlink_to(target)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._load_manifest_session(str(link), "official-openai")

    assert exc_info.value.as_result()["code"] == "manifest_not_regular"


def test_manifest_session_loader_requires_current_process_owner(
    tmp_path: Path, monkeypatch
):
    manifest = tmp_path / "private-manifest.json"
    _write_manifest(manifest)
    real_fstat = os.fstat

    def foreign_owner(fd: int):
        values = list(real_fstat(fd))
        values[stat.ST_UID] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(genesis_e2e.os, "fstat", foreign_owner)
    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._load_manifest_session(str(manifest), "official-openai")

    assert exc_info.value.as_result()["code"] == "manifest_owner_mismatch"


@pytest.mark.parametrize(
    "wrong_hash",
    ["0" * 64, "f" * 64],
    ids=["wrong-hash", "stale-hash"],
)
def test_finalize_rejects_wrong_or_stale_evidence_hash_and_cleans_up(
    tmp_path: Path, wrong_hash: str
):
    evidence_path, _actual_hash, artifacts = _write_private_capture(
        tmp_path, wrong_hash[:4]
    )
    judgment_path = tmp_path / f"judgment-{wrong_hash[:4]}.json"
    _write_judgment(judgment_path, wrong_hash)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.finalize_existing_session_distill_acceptance(
            private_evidence_path=str(evidence_path),
            semantic_judgment_path=str(judgment_path),
            fixture=_fixture(),
            artifact_dir=str(artifacts),
        )

    assert (
        exc_info.value.as_result()["code"] == "semantic_judgment_evidence_hash_mismatch"
    )
    assert evidence_path.exists() is False


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_finalize_rejects_unsafe_private_evidence_permissions_and_deletes_file(
    tmp_path: Path, mode: int
):
    evidence_path, evidence_sha256, artifacts = _write_private_capture(
        tmp_path, f"mode-{mode:o}"
    )
    evidence_path.chmod(mode)
    judgment_path = tmp_path / f"judgment-{mode:o}.json"
    _write_judgment(judgment_path, evidence_sha256)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.finalize_existing_session_distill_acceptance(
            private_evidence_path=str(evidence_path),
            semantic_judgment_path=str(judgment_path),
            fixture=_fixture(),
            artifact_dir=str(artifacts),
        )

    assert exc_info.value.as_result()["code"] == "private_evidence_permissions_invalid"
    assert evidence_path.exists() is False


def test_finalize_rejects_private_evidence_symlink_without_deleting_target(
    tmp_path: Path,
):
    target, evidence_sha256, artifacts = _write_private_capture(
        tmp_path, "symlink-target"
    )
    link = tmp_path / "symlink-private-evidence.json"
    link.symlink_to(target)
    judgment_path = tmp_path / "symlink-judgment.json"
    _write_judgment(judgment_path, evidence_sha256)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.finalize_existing_session_distill_acceptance(
            private_evidence_path=str(link),
            semantic_judgment_path=str(judgment_path),
            fixture=_fixture(),
            artifact_dir=str(artifacts),
        )

    assert exc_info.value.as_result()["code"] == "private_evidence_not_regular"
    assert link.exists() is False
    assert target.exists() is True


def test_finalize_rejects_unsafe_judgment_permissions_and_cleans_private_evidence(
    tmp_path: Path,
):
    evidence_path, evidence_sha256, artifacts = _write_private_capture(
        tmp_path, "judgment-mode"
    )
    judgment_path = tmp_path / "unsafe-judgment.json"
    _write_judgment(judgment_path, evidence_sha256, mode=0o644)

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.finalize_existing_session_distill_acceptance(
            private_evidence_path=str(evidence_path),
            semantic_judgment_path=str(judgment_path),
            fixture=_fixture(),
            artifact_dir=str(artifacts),
        )

    assert exc_info.value.as_result()["code"] == "semantic_judgment_permissions_invalid"
    assert evidence_path.exists() is False


def test_capture_refuses_to_write_plaintext_inside_public_artifacts(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._write_private_evidence(
            str(artifacts / "private-evidence.json"),
            str(artifacts),
            _private_evidence_payload(),
        )

    assert exc_info.value.as_result()["code"] == "private_evidence_inside_artifacts"


def test_finalizer_report_is_owner_only_and_outside_public_artifacts(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    private = tmp_path / "private"
    artifacts.mkdir()
    private.mkdir()
    report = private / "persona-report.md"

    genesis_e2e._write_private_report(str(report), str(artifacts), "sanitized\n")

    assert report.read_text(encoding="utf-8") == "sanitized\n"
    assert stat.S_IMODE(report.stat().st_mode) == 0o600


def test_finalizer_report_refuses_public_artifact_destination(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e._write_private_report(
            str(artifacts / "persona-report.md"),
            str(artifacts),
            "sanitized\n",
        )

    assert exc_info.value.as_result() == {
        "ok": False,
        "stage": "report",
        "code": "report_inside_artifacts",
    }


def test_chat_history_uses_decrypted_ciphertext_and_ignores_plaintext_decoy(
    monkeypatch,
):
    decrypt_calls: list[dict] = []

    def decrypt(envelope, private_key):
        decrypt_calls.append(envelope)
        assert private_key == b"k" * 32
        return "decrypted greeting"

    monkeypatch.setattr(genesis_e2e, "_decrypt_envelope_user", decrypt)
    messages, errors, agent_count = genesis_e2e._decrypt_chat_history(
        [
            {
                "id": "greeting-1",
                "role": "agent",
                "body_ct": "ciphertext",
                "owner_user_id": "user-existing-1",
                "content": "malicious plaintext decoy",
            }
        ],
        b"k" * 32,
        "user-existing-1",
    )

    assert messages == [{"role": "agent", "content": "decrypted greeting"}]
    assert errors == 0
    assert agent_count == 1
    assert len(decrypt_calls) == 1


def test_plaintext_only_greeting_is_not_qualification_evidence():
    messages, errors, agent_count = genesis_e2e._decrypt_chat_history(
        [{"id": "greeting-1", "role": "agent", "content": "Hello."}],
        b"k" * 32,
        "user-existing-1",
    )

    assert messages == [
        {
            "role": "agent",
            "content": "",
            "decrypt_error": "chat_ciphertext_missing",
        }
    ]
    assert errors == 1
    assert agent_count == 0


def test_plaintext_only_memory_is_not_qualification_evidence():
    memories = genesis_e2e._decrypt_memory_rows(
        [
            {
                "id": "memory-decoy",
                "description": "Jasmine tea and a walk are the reset ritual.",
            }
        ],
        b"k" * 32,
        "user-existing-1",
    )

    assert memories == [
        {"id": "memory-decoy", "decrypt_error": "memory_ciphertext_missing"}
    ]


@pytest.mark.parametrize("surface", ["chat", "memory"])
def test_envelope_owner_mismatch_is_rejected_before_decrypt(surface: str, monkeypatch):
    monkeypatch.setattr(
        genesis_e2e,
        "_decrypt_envelope_user",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("owner must be checked before decrypt")
        ),
    )
    envelope = {
        "id": "foreign-envelope",
        "role": "agent",
        "body_ct": "ciphertext",
        "owner_user_id": "another-user",
    }

    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        if surface == "chat":
            genesis_e2e._decrypt_chat_history([envelope], b"k" * 32, "user-existing-1")
        else:
            genesis_e2e._decrypt_memory_rows([envelope], b"k" * 32, "user-existing-1")

    assert exc_info.value.as_result()["code"] == f"{surface}_owner_mismatch"


@pytest.mark.parametrize(
    ("surface", "expected_code"),
    [
        ("identity", "identity_owner_mismatch"),
        ("persona", "persona_owner_mismatch"),
    ],
)
def test_existing_session_rejects_foreign_output_envelopes_before_decrypt(
    surface: str, expected_code: str, monkeypatch, tmp_path: Path
):
    archive_count = 0
    submitted_job_token = ""

    def fake_request(method, url, _api_key, *, json_body=None, **_kwargs):
        nonlocal archive_count, submitted_job_token
        if method == "POST" and url.endswith("/v1/onboarding/archive"):
            archive_count += 1
            return _archive_ok(archive_count)
        if method == "POST" and url.endswith("/v1/genesis/imports/plaintext"):
            assert json_body
            submitted_job_token = str(json_body["client_job_id"])
            return 202, {"job_id": "genesis_0123456789abcdef"}
        if url.endswith("/v1/genesis/imports/genesis_0123456789abcdef"):
            return 200, {
                "job": {
                    "job_id": "genesis_0123456789abcdef",
                    "status": "done",
                    "voice_ref": "voice-existing-1",
                    "metadata": {
                        "client_job_id": submitted_job_token,
                        "file_count": 4,
                        "history_count": 1,
                        "ai_persona_count": 1,
                        "user_profile_count": 1,
                        "memory_summary_count": 1,
                    },
                },
                "persona": {
                    "content_envelope": {
                        "kind": "persona",
                        "id": "persona-1",
                        "body_ct": "ciphertext",
                        "owner_user_id": (
                            "foreign-user"
                            if surface == "persona"
                            else "user-existing-1"
                        ),
                    }
                },
            }
        if url.endswith("/v1/identity/get"):
            return 200, {
                "identity": {
                    "kind": "identity",
                    "id": "identity-1",
                    "body_ct": "ciphertext",
                    "owner_user_id": (
                        "foreign-user" if surface == "identity" else "user-existing-1"
                    ),
                    "days_with_user": 5,
                    "relationship_started_at": RELATIONSHIP_STARTED_AT,
                }
            }
        if "/v1/chat/history" in url:
            return 200, {
                "messages": [
                    {
                        "kind": "greeting",
                        "id": "greeting-1",
                        "role": "agent",
                        "body_ct": "ciphertext",
                        "owner_user_id": "user-existing-1",
                    }
                ]
            }
        if "/v1/memory/list" in url:
            return 200, {
                "moments": [
                    {
                        "kind": "memory",
                        "id": "memory-1",
                        "body_ct": "ciphertext",
                        "owner_user_id": "user-existing-1",
                    }
                ]
            }
        if url.endswith("/v1/onboarding/validate"):
            return 200, {"passing": True}
        raise AssertionError(f"unexpected request: {method} {url}")

    def fake_decrypt(envelope, _private_key):
        assert envelope.get("owner_user_id") == "user-existing-1"
        if envelope.get("kind") == "identity":
            return json.dumps(
                {
                    "agent_name": "Mira",
                    "category": "warm",
                    "dimensions": [
                        {"name": "grounded", "description": "Concrete and calm."}
                    ],
                    "self_introduction": "I am Mira.",
                }
            )
        if envelope.get("kind") == "greeting":
            return "Hello."
        if envelope.get("kind") == "memory":
            return json.dumps(
                {"description": "Jasmine tea and a walk are the reset ritual."}
            )
        if envelope.get("kind") == "persona":
            return "Mira is a warm, grounded companion."
        raise AssertionError("unexpected envelope")

    monkeypatch.setattr(genesis_e2e, "_decrypt_envelope_user", fake_decrypt)
    with pytest.raises(genesis_e2e.ExistingSessionDistillError) as exc_info:
        genesis_e2e.capture_existing_session_distill_evidence(
            api_url="https://test-api.feedling.app",
            api_key="feedling-existing-api-key",
            user_id="user-existing-1",
            content_private_key=b"k" * 32,
            fixture=_fixture(),
            **_capture_paths(tmp_path, surface),
            timeout=1,
            poll=0,
            intro_timeout=1,
            request_fn=fake_request,
        )

    assert exc_info.value.as_result()["code"] == expected_code
