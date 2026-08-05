"""T16: genesis distill failure classification + observability fields.

Pure-unit: classify_genesis_error is pure string/exception matching, and the
write_genesis_state / mark_failed tests monkeypatch db.set_blob /
db.genesis_set_job_status the same way tests/test_genesis_service.py already
does, so nothing here touches a real Postgres connection.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client  # noqa: E402
from genesis import service  # noqa: E402


def _store(user_id: str = "usr_genesis_fail"):
    return types.SimpleNamespace(user_id=user_id)


# --- classify_genesis_error: one case per enum value, from REAL raise strings ----

def test_classify_bad_api_key_from_provider_http_401_string():
    assert service.classify_genesis_error(
        "worker_failed:ProviderError:provider_http_401: Invalid API key"
    ) == "bad_api_key"


def test_classify_bad_api_key_from_provider_http_403_string():
    assert service.classify_genesis_error("provider_http_403: forbidden") == "bad_api_key"


def test_classify_bad_api_key_from_exc_status_code():
    exc = provider_client.ProviderError("provider_http_401: bad key", status_code=401)
    # error string alone wouldn't tell us anything useful here; exc carries it.
    assert service.classify_genesis_error("apply_outputs_failed:ProviderError:oops", exc) == "bad_api_key"


def test_classify_provider_quota_from_provider_http_429_string():
    assert service.classify_genesis_error("provider_http_429: rate limited") == "provider_quota"


def test_classify_provider_quota_from_exc_status_code_402():
    exc = provider_client.ProviderError("provider_http_402: out of credits", status_code=402)
    assert service.classify_genesis_error("worker_failed:ProviderError:x", exc) == "provider_quota"


def test_classify_model_bad_json_invalid_json():
    # real worker._json_object raise: f"{task_id}:invalid_json"
    assert service.classify_genesis_error("fact-map-0:invalid_json") == "model_bad_json"


def test_classify_model_bad_json_not_object():
    assert service.classify_genesis_error("voice-reduce-0-1:json_not_object") == "model_bad_json"


def test_classify_model_bad_json_after_repair():
    # real worker._complete_json raise after the repair round also fails
    assert service.classify_genesis_error(
        "worker_failed:GenesisWorkerError:fact-map-0:invalid_json_after_repair"
    ) == "model_bad_json"


def test_classify_model_bad_json_from_non_json_provider_wire_response():
    assert service.classify_genesis_error(
        "plaintext_import_failed:ProviderError:provider returned non-json response"
    ) == "model_bad_json"


def test_classify_model_bad_json_from_non_object_provider_wire_response():
    assert service.classify_genesis_error(
        "plaintext_import_failed:ProviderError:provider returned non-object response"
    ) == "model_bad_json"


def test_classify_model_empty_output_from_all_fact_maps_failed():
    # real _build_reducer_output floor-check raise
    assert service.classify_genesis_error(
        "worker_failed:GenesisWorkerError:all_fact_maps_failed:3/3"
    ) == "model_empty_output"


def test_classify_distill_empty_output_and_friendly_copy():
    error = "plaintext_import_failed:GenesisWorkerError:distill_empty_output:keep_all_nonempty"
    assert service.classify_genesis_error(error) == "distill_empty_output"
    copy = service.genesis_failure_required_text(error)
    assert "换模型" in copy
    assert "switch models" in copy


def test_classify_provider_timeout_from_httpx_exception_type_name():
    # real worker._fetch_provider_key wrap: f"...:{type(e).__name__}"
    assert service.classify_genesis_error(
        "provider_key_envelope_fetch_failed:ReadTimeout"
    ) == "provider_timeout"


def test_classify_distill_model_too_slow_before_generic_timeout():
    assert service.classify_genesis_error(
        "plaintext_import_failed:DistillModelTooSlowError:distill_model_too_slow:ReadTimeout"
    ) == "distill_model_too_slow"
    copy = service.genesis_failure_required_text("distill_model_too_slow")
    assert "换更快的模型" in copy
    assert "switch to a faster model" in copy
    assert "蒸馏" not in copy


def test_classify_provider_timeout_from_live_httpx_exc():
    exc = httpx.ReadTimeout("timed out")
    assert service.classify_genesis_error("some wrapper text", exc) == "provider_timeout"


def test_classify_worker_restarted_from_cloud_stale_reaper():
    # real reap_stale_processing_jobs error string
    assert service.classify_genesis_error("genesis_stale_timeout:1800s") == "worker_restarted"


def test_classify_worker_restarted_from_dead_plaintext_owner():
    assert service.classify_genesis_error("plaintext_worker_restarted") == "worker_restarted"


def test_classify_worker_restarted_from_resident_stale_reaper():
    assert service.classify_genesis_error("resident_stale_timeout:900s") == "worker_restarted"


def test_classify_worker_restarted_from_unclaimed_reaper():
    assert service.classify_genesis_error("resident_never_claimed:86400s") == "worker_restarted"


def test_classify_worker_restarted_not_confused_with_provider_timeout():
    # These strings all contain "timeout" too — must resolve to worker_restarted,
    # not fall into the generic provider_timeout bucket checked later.
    for text in ("genesis_stale_timeout:1800s", "resident_stale_timeout:900s"):
        assert service.classify_genesis_error(text) == "worker_restarted"


def test_classify_decrypt_failed_from_enclave_raise():
    # real worker._decrypt_envelope raise: f"{purpose}:decrypt_failed:{type(e).__name__}"
    assert service.classify_genesis_error(
        "genesis_chunk:decrypt_failed:TimeoutException"
    ) == "decrypt_failed"


def test_classify_internal_fallback_for_unmatched_strings():
    assert service.classify_genesis_error("persona_material_required") == "internal"
    assert service.classify_genesis_error("identity_update_empty") == "internal"
    assert service.classify_genesis_error("") == "internal"


def test_consumer_offline_defined_but_unwired():
    # Enum value exists (contract-complete) but no known raise site produces a
    # matchable string for it yet — classify_genesis_error must never return it
    # from a guess; it's reachable only if/when a real VPS-lane raise site is wired.
    assert "consumer_offline" in service.GENESIS_ERROR_CODES
    assert "consumer_offline" in service.GENESIS_ERROR_HINTS
    assert service.classify_genesis_error("consumer went offline") == "internal"


def test_all_error_codes_have_hints():
    assert set(service.GENESIS_ERROR_CODES) == set(service.GENESIS_ERROR_HINTS.keys())
    for code, hint in service.GENESIS_ERROR_HINTS.items():
        assert isinstance(hint, str) and hint.strip(), code


# --- write_genesis_state: additive blob fields ------------------------------

def test_write_genesis_state_failed_adds_error_code_and_hint_keeps_raw_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db, "set_blob",
        lambda user_id, kind, doc: captured.update(doc),
    )

    state = service.write_genesis_state(
        _store(),
        {"job_id": "job_1", "status": "failed", "error": "fact-map-0:invalid_json"},
        status="failed",
    )

    assert state["error"] == "fact-map-0:invalid_json"  # raw error preserved verbatim
    assert state["error_code"] == "model_bad_json"
    assert state["error_hint"] == service.GENESIS_ERROR_HINTS["model_bad_json"]
    assert captured["error_code"] == "model_bad_json"
    assert captured["error"] == "fact-map-0:invalid_json"


def test_write_genesis_state_failed_uses_exc_for_sharper_classification(monkeypatch):
    monkeypatch.setattr(service.db, "set_blob", lambda *a, **k: None)
    exc = provider_client.ProviderError("provider_http_401: nope", status_code=401)

    state = service.write_genesis_state(
        _store(),
        {"job_id": "job_1", "status": "failed", "error": "worker_failed:ProviderError:provider_http_401: nope"},
        status="failed",
        exc=exc,
    )

    assert state["error_code"] == "bad_api_key"


def test_write_genesis_state_done_status_has_no_error_code(monkeypatch):
    monkeypatch.setattr(service.db, "set_blob", lambda *a, **k: None)
    state = service.write_genesis_state(_store(), {"job_id": "job_1", "status": "done"})
    assert "error_code" not in state
    assert "error_hint" not in state


def test_write_genesis_state_processing_adds_worker_claimed_by_and_claimed_age_sec(monkeypatch):
    import time as time_mod

    monkeypatch.setattr(service.db, "set_blob", lambda *a, **k: None)
    claimed_at_iso = time_mod.strftime("%Y-%m-%dT%H:%M:%S+00:00", time_mod.gmtime(time_mod.time() - 42))

    state = service.write_genesis_state(
        _store(),
        {
            "job_id": "job_1",
            "status": "processing",
            "resident_consumer_id": "vps_abc123",
            "resident_claimed_at": claimed_at_iso,
        },
        status="processing",
    )

    assert state["worker_claimed_by"] == "vps_abc123"
    assert isinstance(state["claimed_age_sec"], int)
    assert 40 <= state["claimed_age_sec"] <= 50


def test_write_genesis_state_processing_omits_worker_fields_when_absent(monkeypatch):
    monkeypatch.setattr(service.db, "set_blob", lambda *a, **k: None)

    state = service.write_genesis_state(
        _store(),
        {"job_id": "job_1", "status": "processing"},
        status="processing",
    )

    # No resident_consumer_id / timestamps on the job dict at all -> nothing to
    # report, and nothing fabricated.
    assert "worker_claimed_by" not in state
    assert "claimed_age_sec" not in state


def test_write_genesis_state_processing_falls_back_to_updated_at_for_age(monkeypatch):
    import time as time_mod

    monkeypatch.setattr(service.db, "set_blob", lambda *a, **k: None)
    updated_at_iso = time_mod.strftime("%Y-%m-%dT%H:%M:%S+00:00", time_mod.gmtime(time_mod.time() - 5))

    state = service.write_genesis_state(
        _store(),
        {"job_id": "job_1", "status": "processing", "updated_at": updated_at_iso},
        status="processing",
    )

    assert "worker_claimed_by" not in state  # cloud worker: no resident_consumer_id
    assert state["claimed_age_sec"] >= 0


# --- mark_failed: forwards exc through to classification --------------------

def test_mark_failed_forwards_exc_to_error_code(monkeypatch):
    job_row = {"job_id": "job_1", "user_id": "usr_genesis_fail", "status": "failed",
               "error": "worker_failed:ProviderError:provider_http_429: slow down"}
    monkeypatch.setattr(service.db, "genesis_set_job_status", lambda *a, **k: dict(job_row))
    captured = {}
    monkeypatch.setattr(service.db, "set_blob", lambda user_id, kind, doc: captured.update(doc))
    monkeypatch.setattr(service.notices, "emit", lambda *a, **k: None)

    exc = provider_client.ProviderError("provider_http_429: slow down", status_code=429)
    service.mark_failed(_store(), "job_1", job_row["error"], exc=exc)

    assert captured["error_code"] == "provider_quota"
    assert captured["error"] == job_row["error"]


def test_mark_failed_without_exc_still_classifies_from_string(monkeypatch):
    job_row = {"job_id": "job_2", "user_id": "usr_genesis_fail", "status": "failed",
               "error": "fact-map-2:json_not_object"}
    monkeypatch.setattr(service.db, "genesis_set_job_status", lambda *a, **k: dict(job_row))
    captured = {}
    monkeypatch.setattr(service.db, "set_blob", lambda user_id, kind, doc: captured.update(doc))
    monkeypatch.setattr(service.notices, "emit", lambda *a, **k: None)

    service.mark_failed(_store(), "job_2", job_row["error"])

    assert captured["error_code"] == "model_bad_json"


def test_error_hints_en_mirror_keys_and_required_text_is_cause_aware():
    # EN mirror must stay key-identical with the zh dict (bilingual contract).
    assert set(service.GENESIS_ERROR_HINTS_EN.keys()) == set(service.GENESIS_ERROR_HINTS.keys())
    for hint in service.GENESIS_ERROR_HINTS_EN.values():
        assert hint.strip()

    timeout_line = service.genesis_failure_required_text(
        "plaintext_import_failed:ProviderError:provider network error: ReadTimeout"
    )
    assert "文件解读失败" in timeout_line
    assert service.GENESIS_ERROR_HINTS["provider_timeout"] in timeout_line
    assert service.GENESIS_ERROR_HINTS_EN["provider_timeout"] in timeout_line
    assert "蒸馏" not in timeout_line
    assert "app build" not in timeout_line.lower()

    # Unknown garbage must fall back to the internal-cause copy, still bilingual.
    unknown_line = service.genesis_failure_required_text("mystery blowup ~~ xyz")
    assert service.GENESIS_ERROR_HINTS["internal"] in unknown_line
    assert service.GENESIS_ERROR_HINTS_EN["internal"] in unknown_line
