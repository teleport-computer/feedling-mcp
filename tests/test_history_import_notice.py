"""history_import 失败/卡死/恢复 → user_notices（spec Phase C / C2）。

Run:  python -m pytest tests/test_history_import_notice.py -q
"""
from __future__ import annotations

import pytest
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from hosted import history_import as hi  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _notices(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def test_top_level_failure_emits_import_failed():
    """_run_history_import_job's top-level except (history_import.py:3205) fires
    naturally for an empty payload: _process_history_import_sync raises ValueError
    (no history_messages, no support_messages, no fresh_start) before it ever
    reaches provider/runtime setup — no monkeypatching needed to reach the except."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]

    hi._run_history_import_job(store, None, job_id, {})

    n = _notices(uid)
    key = f"history_import:{job_id}"
    assert key in n
    row = n[key]
    assert row["source"] == "history_import"
    assert row["error_class"] == "import_failed"
    assert row["blame"] == "system" and row["severity"] == "error"

    job = db.get_blob(uid, hi._history_job_kind(job_id))
    assert job["status"] == "failed"


@pytest.mark.parametrize(
    "exc,label",
    [
        (RuntimeError("model_api_key_decrypt_failed enclave_http_401:unauthorized"),
         "enclave 401"),
        (RuntimeError("connection to server failed: FATAL: password authentication failed for user"),
         "postgres 密码错"),
        (RuntimeError("connection refused"), "DB 连不上"),
        (ValueError("no_history_messages"), "普通内部错"),
    ],
)
def test_internal_failures_never_blamed_on_user(monkeypatch, exc, label):
    """归责红线的反向守卫：不是用户的错，绝不能赖给他。

    顶层 except 捕获整条流水线的异常（DB / enclave / 信封 / 配置）。这些文本里
    含 'unauthorized' / 'authentication failed' / 'connection refused' 等词，
    若直接跑 classify_upstream 会被正则命中、误归成 user_provider —— 我们数据库
    密码错了却让用户去重存 API Key。故只有 ProviderError 才进分类器。"""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]

    def _boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(hi, "_process_history_import_sync", _boom)

    hi._run_history_import_job(store, None, job_id, {"fresh_start": True})

    row = _notices(uid)[f"history_import:{job_id}"]
    assert row["error_class"] == "import_failed", f"{label} 被误分类成 {row['error_class']}"
    assert row["blame"] == "system", f"{label} 被甩锅给 {row['blame']}"


def test_unclassified_provider_error_stays_with_provider(monkeypatch):
    """ProviderError 但认不出具体形态 → upstream_unavailable/provider_transient。
    它确实是用户的 provider 挂了，归 system 会变成我们替他背锅的漏报。"""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]

    def _boom(*_a, **_k):
        raise hi.provider_client.ProviderError("provider network error: ConnectError")
    monkeypatch.setattr(hi, "_process_history_import_sync", _boom)

    hi._run_history_import_job(store, None, job_id, {"fresh_start": True})

    row = _notices(uid)[f"history_import:{job_id}"]
    assert row["error_class"] == "upstream_unavailable"
    assert row["blame"] == "provider_transient"


def test_top_level_provider_402_emits_quota_insufficient(monkeypatch):
    """When a provider failure (out-of-credits) bubbles to the job's top-level
    except, the notice must blame the user's provider (actionable: 充值), not us.
    provider_client raises ProviderError carrying a provider_http_402 slug;
    classify_upstream maps it to quota_insufficient/user_provider."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]

    def _boom(*_a, **_k):
        raise hi.provider_client.ProviderError("provider_http_402: 预扣费额度失败")
    monkeypatch.setattr(hi, "_process_history_import_sync", _boom)

    hi._run_history_import_job(store, None, job_id, {"fresh_start": True})

    row = _notices(uid)[f"history_import:{job_id}"]
    assert row["error_class"] == "quota_insufficient"
    assert row["blame"] == "user_provider" and row["severity"] == "error"


def test_top_level_provider_401_emits_auth_invalid(monkeypatch):
    """Same top-level path, bad key: provider_http_401 → auth_invalid/user_provider
    (actionable: 重新保存 Key), not the generic import_failed/system."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]

    def _boom(*_a, **_k):
        raise hi.provider_client.ProviderError("provider_http_401: invalid api key")
    monkeypatch.setattr(hi, "_process_history_import_sync", _boom)

    hi._run_history_import_job(store, None, job_id, {"fresh_start": True})

    row = _notices(uid)[f"history_import:{job_id}"]
    assert row["error_class"] == "auth_invalid"
    assert row["blame"] == "user_provider"


def test_stale_job_emits_import_stale_and_marks_failed():
    """_history_import_find_reusable_job's stale branch (history_import.py:188)
    driven directly: seed a 'processing' job blob whose updated_at is older than
    HISTORY_IMPORT_STALE_SEC, then call the reuse lookup with a matching
    client_job_id so the stale branch fires."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]
    old_ts = (datetime.now() - timedelta(seconds=hi.HISTORY_IMPORT_STALE_SEC + 120)).isoformat()
    db.set_blob(uid, hi._history_job_kind(job_id), {
        "job_id": job_id,
        "status": "processing",
        "client_job_id": "cj1",
        "created_at": old_ts,
        "updated_at": old_ts,
    })

    result = hi._history_import_find_reusable_job(store, client_job_id="cj1", input_hash="")

    assert result is None  # stale job is marked failed, not returned as reusable
    n = _notices(uid)
    key = f"history_import:{job_id}"
    assert key in n
    row = n[key]
    assert row["source"] == "history_import"
    assert row["error_class"] == "import_stale"
    assert row["blame"] == "system" and row["severity"] == "error"


def test_completion_resolves_prior_failure_notice(monkeypatch):
    """A prior failure notice must clear once _process_history_import_sync reaches its
    completion update (history_import.py:3174-3184), via notices.resolve(store,
    'history_import:') keyed off the shared 'history_import:' prefix. Drive completion
    with a fresh_start payload so no history parsing is needed, and monkeypatch the
    provider/envelope seams (runtime config, candidate extraction, card append,
    identity derive/store, greeting) so the run reaches 'completed' without any live
    upstream or encryption."""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job_id = "job_" + uuid.uuid4().hex[:10]

    # seed a prior failure notice under a *different* job's key sharing the same
    # 'history_import:' prefix, confirming resolve() is a prefix-wide clear, not
    # scoped to this job_id specifically (matches the spec's per-domain resolve).
    other_job_id = "job_other"
    notices_core.emit(
        store, source="history_import", error_class="import_failed", blame="system",
        severity="error", user_text="x", detail="prior",
        dedupe_key=f"history_import:{other_job_id}",
    )
    assert _notices(uid)[f"history_import:{other_job_id}"]["resolved"] is False

    monkeypatch.setattr(hi.hosted_config_store, "_load_runtime_provider_config",
                        lambda *_a, **_k: object())
    monkeypatch.setattr(hi, "_extract_memory_candidates_with_provider",
                        lambda *_a, **_k: ([], []))
    monkeypatch.setattr(hi, "_ensure_import_minimum_cards", lambda *_a, **_k: [])
    monkeypatch.setattr(hi, "_append_import_memory_cards", lambda *_a, **_k: [])
    monkeypatch.setattr(hi, "_derive_identity_with_provider", lambda *_a, **_k: ({}, []))
    monkeypatch.setattr(hi, "_store_identity_payload", lambda *_a, **_k: {})
    monkeypatch.setattr(hi, "_generate_model_api_onboarding_greeting",
                        lambda *_a, **_k: ("", []))

    job = {"job_id": job_id, "status": "queued", "created_at": hi.core_util._now_iso()}
    payload = {"fresh_start": True}
    hi._process_history_import_sync(store, None, job, payload)

    assert job["status"] == "completed"
    n = _notices(uid)
    assert n[f"history_import:{other_job_id}"]["resolved"] is True
