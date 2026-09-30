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

import re

import httpx
import pytest

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


# --------------------------------------------------------------------------- #
# Path-aware failure copy (usr_3b73f1cb0a9ec975, 2026-08-06).
#
# The generic hints describe the SEALED chunk import, where a dead worker's job
# really is auto-recovered. Plaintext imports have no such path, so the generic
# "已自动重新排队" told that user to sit and wait when the only way forward was
# to tap retry — he sat on it for two days. These pin the split so a later edit
# cannot quietly re-broadcast the sealed wording to plaintext users.
# --------------------------------------------------------------------------- #

_RESTART_ERROR = "genesis_stale_timeout:1800s"


def test_plaintext_restart_copy_does_not_claim_auto_requeue():
    line = service.genesis_failure_required_text(_RESTART_ERROR, ingest="plaintext")

    # The false promise, in both languages.
    assert "已自动重新排队" not in line
    assert "re-queued" not in line.lower()
    # Still names the real cause and stays bilingual.
    assert service.GENESIS_ERROR_HINTS_PLAINTEXT["worker_restarted"] in line
    assert service.GENESIS_ERROR_HINTS_PLAINTEXT_EN["worker_restarted"] in line


def test_sealed_restart_copy_keeps_the_auto_requeue_wording():
    # Not a symmetric edit: the sealed path DOES requeue
    # (db.genesis_reclaim_orphaned_processing_jobs), so its wording is accurate
    # and must not be collateral damage of the plaintext fix.
    line = service.genesis_failure_required_text(_RESTART_ERROR)

    assert service.GENESIS_ERROR_HINTS["worker_restarted"] in line
    assert service.GENESIS_ERROR_HINTS_EN["worker_restarted"] in line
    assert "已自动重新排队" in line


def test_plaintext_override_map_is_key_mirrored_and_scoped():
    assert (set(service.GENESIS_ERROR_HINTS_PLAINTEXT.keys())
            == set(service.GENESIS_ERROR_HINTS_PLAINTEXT_EN.keys()))
    # Overrides may only shadow codes that exist in the generic catalog.
    assert set(service.GENESIS_ERROR_HINTS_PLAINTEXT).issubset(service.GENESIS_ERROR_HINTS)
    # Codes without an override fall through to the shared wording on both paths.
    for error, code in (("provider_http_401", "bad_api_key"),
                        ("mystery blowup ~~ xyz", "internal")):
        assert code not in service.GENESIS_ERROR_HINTS_PLAINTEXT
        assert (service.genesis_failure_required_text(error, ingest="plaintext")
                == service.genesis_failure_required_text(error))


def test_failure_copy_never_promises_materials_are_kept_forever():
    # The old closing clause asserted "已上传的材料不会丢" unconditionally, which
    # inverts into a lie once the staged payload TTL passes and the blob is
    # deleted (retry then answers 410). The line must set a bounded expectation.
    for ingest in ("plaintext", ""):
        line = service.genesis_failure_required_text(_RESTART_ERROR, ingest=ingest)
        assert "已上传的材料不会丢" not in line
        assert "materials are kept." not in line
        assert "过期后需要重新选择文件" in line
        assert "pick the file again" in line.lower()


# T750: the identity profile's validation codes and the provider's empty-reply
# error used to fall through to "internal" ("内部错误,请稍后重试"). Every
# reject code profile.py can produce (every `return None, "<code>"` in
# _validate_profile / map validation), wrapped the way genesis/worker.py raises
# it; the two empty-reply codes are covered separately below.
_PROFILE_BAD_OUTPUT = [
    "reply_not_json", "reply_not_text", "missing_field:memory", "missing_field:style",
    "field_empty:memory", "field_empty:style", "placeholder_detected:memory",
    "memory_chars_over_budget:9999", "style_chars_over_budget:9999",
    "map_reply_not_text", "map_reply_chars_over_budget:9999",
    "map_line_count_over_budget:99", "map_line_not_bullet:3",
    "map_bullet_empty:3", "map_bullet_chars_over_budget:3",
    "map_rendered_chars_over_budget:9999", "unknown",
]


def test_profile_reject_code_list_covers_every_code_profile_py_returns():
    """Derived from the source so a new reject code cannot silently go untested."""
    source = (Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"
              / "profile.py").read_text(encoding="utf-8")
    produced = set(re.findall(r'return None, f?"([a-z_]+)', source))
    covered = {c.split(":")[0] for c in _PROFILE_BAD_OUTPUT} | {"reply_empty", "map_reply_empty"}
    assert produced, "found no reject codes in profile.py"
    assert produced <= covered, sorted(produced - covered)


@pytest.mark.parametrize("code", _PROFILE_BAD_OUTPUT)
@pytest.mark.parametrize("prefix", [
    "plaintext_import_failed:GenesisWorkerError:",
    "genesis_v2_background_failed:GenesisWorkerError:",
    "",
])
def test_classify_profile_invalid_is_model_bad_json(prefix, code):
    error = f"{prefix}genesis_profile_invalid:{code}"
    assert service.classify_genesis_error(error) == "model_bad_json"


@pytest.mark.parametrize("code", ["reply_empty", "map_reply_empty"])
def test_classify_empty_profile_reply_is_model_empty_output(code):
    error = f"plaintext_import_failed:GenesisWorkerError:genesis_profile_invalid:{code}"
    assert service.classify_genesis_error(error) == "model_empty_output"


def test_classify_provider_empty_reply_is_model_empty_output():
    error = (
        "update_identity_failed:provider_identity_failed:ProviderError:"
        "provider response had no usable reply text"
    )
    assert service.classify_genesis_error(error) == "model_empty_output"


@pytest.mark.parametrize("error, expected", [
    # Out of scope for T750 and unchanged: a relay with no channel stays internal.
    ("update_identity_failed:provider_identity_failed:ProviderError:provider_http_503: "
     "No available channel for model x under group auto", "internal"),
    ("plaintext_import_failed:GenesisWorkerError:distill_empty_output:keep_all_nonempty:"
     "zero_memory_cards", "distill_empty_output"),
    ("plaintext_import_failed:RuntimeError:something unexpected", "internal"),
])
def test_t750_neighbours_keep_their_classification(error, expected):
    assert service.classify_genesis_error(error) == expected
