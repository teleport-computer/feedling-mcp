"""PostgreSQL regressions for hosted plaintext Genesis memory imports."""
from __future__ import annotations

import json
import sys
import types
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
    monkeypatch.setattr(
        plaintext,
        "_write_back_plaintext_user_name",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(plaintext, "_trace_genesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "load_genesis_checkpoint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "write_genesis_checkpoint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "delete_genesis_checkpoint", lambda *_args, **_kwargs: None)
    # 新 job 走 memgarden 导入会话：模型端给出包的回复形状（长期记忆档案 → curated_archive
    # 单段式写卡），其余（动作校验、执行器、PostgreSQL）全是真的。
    monkeypatch.setenv("FEEDLING_GARDEN_IMPORT_STRATEGY", "single_pass")

    class _FakeLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, **_kwargs):
            reply = json.dumps({"cards": [{
                "action": "add", "type": "fact", "bucket": "饮食", "threads": ["咖啡"],
                "summary": "喜欢手冲咖啡", "content": "明确说过自己喜欢手冲咖啡。",
                "importance": 0.6, "pulse": 0.3, "occurred_at": None,
            }]}, ensure_ascii=False)
            return types.SimpleNamespace(text=reply, stop_reason="stop")

    monkeypatch.setattr(plaintext, "GenesisLLMClient", _FakeLLM)
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
    assert job["status"] == "done", job
    assert job["memory_action_count"] == 1
    moments = db.memory_load(user_id)
    assert len(moments) == 1
    assert moments[0]["source"] == "genesis_import"
    assert json.loads(moments[0]["body_ct"])["summary"] == "喜欢手冲咖啡"


def test_plaintext_genesis_all_rejected_batch_fails_job_instead_of_done_zero(monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    user_id, store = _setup_plaintext_job(monkeypatch, job_id=job_id)

    # Reproduce the legacy/malformed executor shape that previously slipped
    # through as success: HTTP 200 and a rejected row, without aggregate counts.
    # capture_mode_invalid is a host bug (not a bad card), so the batch must fail
    # the job — never commit as "written, 0 cards".
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


# ---------------------------------------------------------------------------
# T750-B: very short material the model finds nothing in (Seven 2026-09-28:
# copy approved verbatim, still a failed import)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from genesis import plaintext_garden  # noqa: E402

APPROVED_ZH = "这份材料太短,没找到可以记下的内容。补充一些细节后再导入试试。"
APPROVED_EN = (
    "This material is too short — nothing worth remembering was found. "
    "Add some detail and import it again."
)


def _run_add_memory_with(monkeypatch, *, text: str, reply: str, strategy: str = "single_pass"):
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    user_id, store = _setup_plaintext_job(monkeypatch, job_id=job_id)
    if strategy == "two_pass":
        # Production default: the fixture pins single_pass, undo it.
        monkeypatch.delenv("FEEDLING_GARDEN_IMPORT_STRATEGY", raising=False)
    calls = []

    class _ReplyLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, **kwargs):
            calls.append(kwargs.get("idempotency_key"))
            return types.SimpleNamespace(text=reply, stop_reason="stop")

    monkeypatch.setattr(plaintext, "GenesisLLMClient", _ReplyLLM)
    plaintext._run_plaintext_genesis_job(
        store, "api_key", job_id, mode="add_memory",
        source_groups=[{
            "source_kind": "memory_summary_import",
            "source_family": "memory_summary",
            "chunk_texts": [text],
        }],
    )
    assert calls, "the model was never called"
    return user_id, db.genesis_get_job(user_id, job_id)


_EMPTY = {"single_pass": json.dumps({"cards": []}), "two_pass": json.dumps({"candidates": []})}
_MALFORMED = {"single_pass": json.dumps({"cards": [17]}),
              "two_pass": json.dumps({"candidates": [17]})}


@pytest.mark.parametrize("strategy", ["single_pass", "two_pass"])
def test_short_material_with_nothing_to_remember_fails_with_the_approved_copy(
    monkeypatch, strategy
):
    text = "我喜欢猫。"
    assert len(text) <= plaintext_garden.SHORT_MATERIAL_MAX_CHARS
    user_id, job = _run_add_memory_with(
        monkeypatch, text=text, reply=_EMPTY[strategy], strategy=strategy)

    assert job["status"] == "failed", job
    assert job["memory_action_count"] == 0
    assert db.memory_load(user_id) == []
    assert "distill_material_too_short" in job["error"]
    assert service.classify_genesis_error(job["error"]) == "distill_material_too_short"
    copy = service.genesis_failure_required_text(job["error"], ingest="plaintext")
    assert APPROVED_ZH in copy
    assert APPROVED_EN in copy
    # The zh-only error_hint field (read by the app when friendly_copy is absent)
    # carries the same approved sentences, the final full stop left to the UI.
    assert service.GENESIS_ERROR_HINTS["distill_material_too_short"] + "。" == APPROVED_ZH
    assert service.GENESIS_ERROR_HINTS_EN["distill_material_too_short"] + "." == APPROVED_EN


@pytest.mark.parametrize("strategy", ["single_pass", "two_pass"])
def test_short_material_with_a_malformed_nonempty_list_is_not_too_short(monkeypatch, strategy):
    """T752 review: the parser silently skips malformed entries, so this also ends
    with zero cards and nothing dropped — but the model did not say "nothing"."""
    _user_id, job = _run_add_memory_with(
        monkeypatch, text="我喜欢猫。", reply=_MALFORMED[strategy], strategy=strategy)

    assert job["status"] == "failed", job
    assert service.classify_genesis_error(job["error"]) != "distill_material_too_short"


@pytest.mark.parametrize("strategy", ["single_pass", "two_pass"])
def test_long_material_with_zero_cards_keeps_the_switch_model_copy(monkeypatch, strategy):
    text = "我喜欢猫。" * (plaintext_garden.SHORT_MATERIAL_MAX_CHARS // 5 + 1)
    assert len(text) > plaintext_garden.SHORT_MATERIAL_MAX_CHARS
    _user_id, job = _run_add_memory_with(
        monkeypatch, text=text, reply=_EMPTY[strategy], strategy=strategy)

    assert job["status"] == "failed", job
    assert service.classify_genesis_error(job["error"]) == "distill_empty_output"
    copy = service.genesis_failure_required_text(job["error"], ingest="plaintext")
    assert APPROVED_ZH not in copy and APPROVED_EN not in copy


def test_short_material_where_the_model_failed_keeps_the_switch_model_copy(monkeypatch):
    """Nothing usable because the model's output was broken is not "too short"."""
    _user_id, job = _run_add_memory_with(
        monkeypatch, text="我喜欢猫。", reply="这不是 JSON")

    assert job["status"] == "failed", job
    assert service.classify_genesis_error(job["error"]) != "distill_material_too_short"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"cards": []}', True),
        ('{"candidates": []}', True),
        ('<think>先想想 {"cards":[1]}</think>{"cards": []}', True),
        ('{"cards": [17]}', False),
        ('{"cards": [], "extra": [1]}', False),
        ('{"note": "nothing"}', False),    # no list at all
        ("[]", False),                     # not an object
        ("这不是 JSON", False),
        ("", False),
    ],
)
def test_explicit_empty_reply_rule(reply, expected):
    assert plaintext_garden._reply_is_explicit_empty(reply) is expected


@pytest.mark.parametrize(
    ("chars", "dropped", "skipped", "replies", "empty", "expected"),
    [
        (1, 0, 0, 1, 1, True),
        (plaintext_garden.SHORT_MATERIAL_MAX_CHARS, 0, 0, 2, 2, True),
        (plaintext_garden.SHORT_MATERIAL_MAX_CHARS + 1, 0, 0, 1, 1, False),
        (10, 1, 0, 1, 1, False),   # a candidate was proposed and dropped
        (10, 0, 1, 1, 1, False),   # a batch failed and was skipped
        (0, 0, 0, 1, 1, False),    # no material at all is not "too short"
        (10, 0, 0, 0, 0, False),   # no model reply seen: no evidence
        (10, 0, 0, 2, 1, False),   # one reply was not an explicit empty list
    ],
)
def test_too_short_rule(chars, dropped, skipped, replies, empty, expected):
    source = types.SimpleNamespace(family="memory_summary", windows=["字" * chars])
    other = types.SimpleNamespace(family="history", windows=["字" * 10_000])
    result = types.SimpleNamespace(dropped=dropped, batches_skipped=skipped)
    importer = types.SimpleNamespace(model_replies=replies, explicit_empty_replies=empty)
    assert plaintext_garden._too_short_and_nothing_proposed(
        [source, other], result, importer) is expected


# ---------------------------------------------------------------------------
# T758: a completion writes its final stage + materials in the same statement
# that marks the job done (T757: a status read between genesis_complete_job and
# the following publish saw done with materials=[]).
# ---------------------------------------------------------------------------

def test_add_memory_completion_already_carries_stage_and_materials(monkeypatch):
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    user_id, store = _setup_plaintext_job(monkeypatch, job_id=job_id)
    counter = {"value": 0}

    def fake_envelope(actual_store, inner, *, item_id=None):
        counter["value"] += 1
        memory_id = item_id or f"mom_genesis_{counter['value']}"
        return ({
            "id": memory_id, "body_ct": json.dumps(inner, ensure_ascii=False),
            "nonce": f"nonce_{memory_id}", "K_user": f"ku_{memory_id}",
            "K_enclave": f"ke_{memory_id}", "enclave_pk_fpr": "test_fpr",
            "visibility": "shared", "owner_user_id": actual_store.user_id,
        }, "")

    monkeypatch.setattr(memory_actions, "_build_memory_envelope_for_store", fake_envelope)
    monkeypatch.setattr(memory_actions.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)
    seen = []
    real_complete = db.genesis_complete_job

    def complete_and_read_back(uid, jid, **kwargs):
        completed = real_complete(uid, jid, **kwargs)
        # What any status read sees right after the completing statement.
        seen.append(db.genesis_get_job(uid, jid))
        return completed

    monkeypatch.setattr(db, "genesis_complete_job", complete_and_read_back)

    _run_add_memory(store, job_id)

    assert len(seen) == 1
    row = seen[0]
    assert row["status"] == "done"
    assert row["output"]["stage"] == "plaintext_add_memory_done"
    materials = row["output"]["materials"]
    assert [m["kind"] for m in materials] == ["memory_summary"]
    assert materials[0]["status"] == "done" and materials[0]["cards"] == 1
    assert service.public_materials_for_job(row) == materials


def test_run_full_completion_carries_stage_materials_and_applied_fields(monkeypatch):
    """_run_full used to complete with only _apply_non_memory's result doc."""
    from genesis import plaintext_garden as pg

    materials = [{"kind": "chat_history", "status": "done", "windows_done": 1,
                  "windows_total": 1, "cards": 9}]

    class _Progress:
        def publish(self, **_k):
            pass

        def mark_identity_ready(self):
            pass

        def materials(self, **_k):
            return list(materials)

        done_output = plaintext._PlaintextCheckpointProgress.done_output

    class _Runner:
        state: dict = {}

        def run(self, *_a, **_k):
            return types.SimpleNamespace(cards_written=9, dropped=0, batches_skipped=0)

    applied = {"memory_action_count": 9, "identity_status": "written",
               "persona_ref": "p", "persona_sha256": "ps"}
    captured = {}
    monkeypatch.setattr(pg, "_finish_output", lambda *_a, **_k: {})
    monkeypatch.setattr(pg, "_apply_non_memory", lambda *_a, **_k: dict(applied))
    monkeypatch.setattr(pg, "_emit_partial", lambda *_a, **_k: None)
    monkeypatch.setattr(pg.notices_core, "resolve", lambda *_a, **_k: None)
    monkeypatch.setattr(pg.service, "write_genesis_state", lambda *_a, **_k: None)
    monkeypatch.setattr(pg.pt, "_write_back_plaintext_user_name", lambda *_a, **_k: None)
    monkeypatch.setattr(pg, "_sources", lambda *_a, **_k: [])
    monkeypatch.setattr(pg.db, "genesis_complete_job",
                        lambda _u, _j, **kwargs: captured.update(kwargs) or {"status": "done"})

    pg._run_full(
        types.SimpleNamespace(user_id="u1"), "api_key", "job_full", runtime=object(),
        source_groups=[], relationship_anchor=None, msgs=[], user_name="TA", llm=object(),
        progress=_Progress(), runner=_Runner(), language="zh")

    output = captured["output"]
    assert output["stage"] == "plaintext_reducer_done"
    assert output["materials"] == materials
    assert output["identity_ready"] is True
    for key, value in applied.items():
        assert output[key] == value


def test_done_output_has_the_publish_shape():
    fake = types.SimpleNamespace(materials=lambda **_k: [{"kind": "memory_summary"}])
    out = plaintext._PlaintextCheckpointProgress.done_output(fake, "some_done", extra_key=1)
    assert out == {"extra_key": 1, "stage": "some_done",
                   "materials": [{"kind": "memory_summary"}], "identity_ready": True}
    # A stale key passed through cannot override the fresh materials.
    out = plaintext._PlaintextCheckpointProgress.done_output(fake, "done_stage", materials=["stale"])
    assert out["materials"] == [{"kind": "memory_summary"}]
