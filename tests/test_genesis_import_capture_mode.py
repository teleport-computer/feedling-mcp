"""PostgreSQL regressions for hosted plaintext Genesis memory imports."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from genesis import plaintext, service  # noqa: E402
from memory import actions as memory_actions  # noqa: E402


def _setup_plaintext_job(monkeypatch, *, job_id: str):
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id)
    store = get_store(user_id)
    db.genesis_create_job(user_id, {
        "job_id": job_id,
        "status": "created",
        "source_kind": "memory_summary_import",
    })
    monkeypatch.setattr(
        plaintext.hosted_config_store,
        "_load_runtime_provider_config",
        lambda *_args: object(),
    )
    monkeypatch.setattr(plaintext, "_resolve_plaintext_user_name", lambda *_args: "TA")
    monkeypatch.setattr(plaintext, "_write_back_plaintext_user_name", lambda *_args: None)
    monkeypatch.setattr(plaintext, "_trace_genesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        plaintext.worker,
        "build_foreground_output_from_texts",
        lambda **_kwargs: {"all_fact_candidates": [{"summary": "用户喜欢手冲咖啡"}]},
    )
    monkeypatch.setattr(
        plaintext.worker,
        "build_memory_output_from_fact_candidates",
        lambda **_kwargs: {
            "memories": [{
                "type": "fact",
                "summary": "用户喜欢手冲咖啡",
                "content": "用户明确说自己喜欢手冲咖啡。",
                "bucket": "偏好",
                "threads": ["咖啡"],
            }],
        },
    )
    return user_id, store


def _run_add_memory(store, job_id: str) -> None:
    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        job_id,
        mode="add_memory",
        source_groups=[{
            "source_kind": "memory_summary_import",
            "source_family": "memory_summary",
            "chunk_texts": ["我喜欢手冲咖啡。"],
        }],
    )


def test_plaintext_genesis_import_persists_memory_card_in_postgres(monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    user_id, store = _setup_plaintext_job(monkeypatch, job_id=job_id)
    counter = {"value": 0}

    def fake_envelope(actual_store, inner, *, item_id=None):
        counter["value"] += 1
        memory_id = item_id or f"mom_genesis_{counter['value']}"
        return ({
            "id": memory_id,
            "body_ct": json.dumps(inner, ensure_ascii=False),
            "nonce": f"nonce_{memory_id}",
            "K_user": f"ku_{memory_id}",
            "K_enclave": f"ke_{memory_id}",
            "enclave_pk_fpr": "test_fpr",
            "visibility": "shared",
            "owner_user_id": actual_store.user_id,
        }, "")

    # Keep encryption outside this regression; persistence and the complete
    # Genesis -> action-validator -> PostgreSQL write path remain real.
    monkeypatch.setattr(memory_actions, "_build_memory_envelope_for_store", fake_envelope)
    monkeypatch.setattr(memory_actions.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)

    _run_add_memory(store, job_id)

    job = db.genesis_get_job(user_id, job_id)
    assert job["status"] == "done"
    assert job["memory_action_count"] == 1
    moments = db.memory_load(user_id)
    assert len(moments) == 1
    assert moments[0]["source"] == "genesis_import"
    assert json.loads(moments[0]["body_ct"])["summary"] == "用户喜欢手冲咖啡"


def test_plaintext_genesis_all_rejected_batch_fails_job_instead_of_done_zero(monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    user_id, store = _setup_plaintext_job(monkeypatch, job_id=job_id)

    # Reproduce the legacy/malformed executor shape that previously slipped
    # through as success: HTTP 200 and a rejected row, without aggregate counts.
    monkeypatch.setattr(
        service.memory_actions,
        "_execute_memory_actions",
        lambda *_args, **_kwargs: ({
            "status": "failed",
            "results": [{
                "status": "failed",
                "error": "capture_mode_invalid",
                "http_status": 400,
            }],
        }, 200),
    )

    _run_add_memory(store, job_id)

    job = db.genesis_get_job(user_id, job_id)
    assert job["status"] == "failed"
    assert job["memory_action_count"] == 0
    assert "capture_mode_invalid" in job["error"]
    assert db.memory_load(user_id) == []
