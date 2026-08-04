import asyncio
from contextlib import contextmanager
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import config_store
from model_api_runtime.v2 import jobs_store, profile, profile_store, worker


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)


def _deps(cards=("cards", 1)):
    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_profile_cards=lambda _uid: cards,
    )


def _cas_result(document):
    return profile_store.ProfileCasResult(
        status="written",
        document=document,
        cas_attempts=1,
        recomputations=1,
    )


def test_profile_is_registered_at_background_priority():
    assert "profile" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["profile"] == 10
    assert jobs_store.LANE_PRIORITY["profile"] == jobs_store.LANE_PRIORITY["dream"]


def test_profile_shape_reject_writes_pending_metadata_and_fails_silently(
    monkeypatch,
):
    captured = {}

    async def _generate(**_kwargs):
        return profile.ProfileGenerationResult(
            fields=None,
            reject_code="reply_not_json",
            overlap=None,
            provider_calls=1,
        )

    async def _cas(_uid, recompute):
        document = await recompute({})
        captured["document"] = document
        return _cas_result(document)

    failed = []
    monkeypatch.setattr(profile, "generate_profile", _generate)
    monkeypatch.setattr(profile_store, "update_profile_cas_async", _cas)
    monkeypatch.setattr(worker.db, "memory_profile_source_stats", lambda _uid: (1, "u1"))
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_failed",
        lambda job_id, code, **_kw: failed.append((job_id, code)) or True,
    )
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_completed",
        lambda *_args, **_kwargs: pytest.fail("rejected profile cannot complete"),
    )
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda *_args, **_kwargs: pytest.fail("profile must never emit an error chip"),
    )
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda *_args, **_kwargs: pytest.fail("profile must never write a bubble"),
    )

    status = asyncio.run(
        worker._run_profile(
            7,
            "u",
            _deps(),
            object(),
            asyncio.Semaphore(1),
        )
    )

    assert status == "failed"
    assert failed == [(7, "reply_not_json")]
    assert captured["document"]["state"] == "pending"
    assert captured["document"]["last_attempt"]["reject_code"] == "reply_not_json"


def test_profile_budget_exception_message_is_persisted_as_reject_code(monkeypatch):
    calls = 0
    captured = {}

    async def _generate(**_kwargs):
        raise profile.ProfileGenerationExhausted(
            "profile_source_exceeds_budget:900001"
        )

    async def _cas(_uid, recompute):
        nonlocal calls
        calls += 1
        document = await recompute({})
        captured["document"] = document
        return _cas_result(document)

    failed = []
    monkeypatch.setattr(profile, "generate_profile", _generate)
    monkeypatch.setattr(profile_store, "update_profile_cas_async", _cas)
    monkeypatch.setattr(worker.db, "memory_profile_source_stats", lambda _uid: (1, "u1"))
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_failed",
        lambda job_id, code, **_kw: failed.append((job_id, code)) or True,
    )

    status = asyncio.run(
        worker._run_profile(
            8,
            "u",
            _deps(),
            object(),
            asyncio.Semaphore(1),
        )
    )

    assert status == "failed"
    assert calls == 2
    assert failed == [(8, "profile_source_exceeds_budget:900001")]
    assert captured["document"]["last_attempt"]["reject_code"] == (
        "profile_source_exceeds_budget:900001"
    )


def test_profile_provider_setup_failure_persists_degraded_backoff(monkeypatch):
    previous = profile_store.build_profile_document(
        "u",
        state="ok",
        source={
            "card_count": 1,
            "max_updated_at": "u1",
            "generated_at": "2026-07-31T00:00:00Z",
        },
        last_attempt={
            "at": "2026-07-31T00:00:00Z",
            "reject_code": "",
            "attempts": 1,
            "retry_not_before": 0,
        },
        memory_text="事实",
        user_text="方式",
        seal_text=lambda _uid, _text: {"body_ct": "ct", "nonce": "n"},
    )
    captured = {}

    async def _cas(_uid, recompute):
        document = await recompute(previous)
        captured["document"] = document
        return _cas_result(document)

    failed = []
    monkeypatch.setattr(profile_store, "update_profile_cas_async", _cas)
    monkeypatch.setattr(worker.db, "memory_profile_source_stats", lambda _uid: (1, "u1"))
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_failed",
        lambda job_id, code, **_kw: failed.append((job_id, code)) or True,
    )

    deps = _deps()
    deps.read_profile_cards = lambda _uid: pytest.fail(
        "provider setup failure must not decrypt Garden cards"
    )
    status = asyncio.run(
        worker._run_profile(
            9,
            "u",
            deps,
            worker._ProfileProviderSetupFailure(
                "profile_provider_unavailable:runtimeerror"
            ),
            asyncio.Semaphore(1),
        )
    )

    assert status == "failed"
    assert failed == [(9, "profile_provider_unavailable:runtimeerror")]
    assert captured["document"]["state"] == "degraded"
    assert captured["document"]["last_attempt"]["reject_code"] == (
        "profile_provider_unavailable:runtimeerror"
    )
    assert captured["document"]["last_attempt"]["retry_not_before"] > 0


def test_run_turn_routes_profile_provider_resolution_failure_to_handler(monkeypatch):
    deps = _deps()
    deps.resolve_provider = lambda _uid: (_ for _ in ()).throw(
        RuntimeError("secret detail")
    )
    deps.mint_enclave_token = lambda _uid: pytest.fail(
        "profile does not use the generic runtime token"
    )
    captured = {}

    async def _process(job, _deps, **kwargs):
        captured["job"] = job
        captured.update(kwargs)
        return "failed"

    monkeypatch.setattr(worker, "process_job", _process)

    status = asyncio.run(
        worker._run_turn(
            {
                "id": 10,
                "user_id": "u",
                "lane": "profile",
                "claimed_by": "worker",
            },
            deps,
        )
    )

    assert status == "failed"
    assert isinstance(
        captured["provider_config"],
        worker._ProfileProviderSetupFailure,
    )
    assert str(captured["provider_config"]) == (
        "profile_provider_unavailable:runtimeerror"
    )
    assert captured["runtime_token"] == ""


def test_empty_garden_completes_with_zero_provider_calls(monkeypatch):
    captured = {}

    async def _cas(_uid, recompute):
        document = await recompute({})
        captured["document"] = document
        return _cas_result(document)

    completed = []
    monkeypatch.setattr(
        profile,
        "generate_profile",
        lambda **_kwargs: pytest.fail("empty Garden must not call provider"),
    )
    monkeypatch.setattr(profile_store, "update_profile_cas_async", _cas)
    monkeypatch.setattr(worker.db, "memory_profile_source_stats", lambda _uid: (0, ""))
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_completed",
        lambda job_id, **_kw: completed.append(job_id) or True,
    )

    status = asyncio.run(
        worker._run_profile(
            9,
            "u",
            _deps(cards=("", 0)),
            object(),
            asyncio.Semaphore(1),
        )
    )

    assert status == "completed"
    assert completed == [9]
    assert captured["document"]["state"] == "empty"


def test_empty_eligible_garden_persists_raw_source_witness(monkeypatch):
    captured = {}

    async def _cas(_uid, recompute):
        document = await recompute({})
        captured["document"] = document
        return _cas_result(document)

    monkeypatch.setattr(
        profile,
        "generate_profile",
        lambda **_kwargs: pytest.fail("empty eligible Garden must not call provider"),
    )
    monkeypatch.setattr(profile_store, "update_profile_cas_async", _cas)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (4, "2026-07-31T00:00:00Z"),
    )
    monkeypatch.setattr(worker.jobs_store, "mark_completed", lambda *_a, **_kw: True)

    status = asyncio.run(
        worker._run_profile(
            10,
            "u",
            _deps(cards=("", 0)),
            object(),
            asyncio.Semaphore(1),
        )
    )

    assert status == "completed"
    assert captured["document"]["state"] == "empty"
    assert captured["document"]["source"] == {
        "card_count": 4,
        "max_updated_at": "2026-07-31T00:00:00Z",
        "generated_at": captured["document"]["source"]["generated_at"],
    }


def test_profile_roll_back_after_generation_blocks_profile_cas(monkeypatch):
    async def _generate(**_kwargs):
        return profile.ProfileGenerationResult(
            fields={"memory": "事实", "user": "方式"},
            reject_code="",
            overlap=None,
            provider_calls=1,
        )

    async def _cas(_uid, recompute):
        await recompute({})
        pytest.fail("runtime rollback must prevent a profile candidate")

    monkeypatch.setattr(profile, "generate_profile", _generate)
    monkeypatch.setattr(profile_store, "update_profile_cas_async", _cas)
    monkeypatch.setattr(worker.db, "memory_profile_source_stats", lambda _uid: (1, "u1"))
    failed = []
    monkeypatch.setattr(
        worker.jobs_store,
        "mark_failed",
        lambda job_id, code, **_kw: failed.append((job_id, code)) or True,
    )

    deps = _deps()
    deps.runtime_mode_enabled = lambda _uid: False

    status = asyncio.run(
        worker._run_profile(
            11,
            "u",
            deps,
            object(),
            asyncio.Semaphore(1),
        )
    )

    assert status == "failed"
    assert failed == [(11, "runtimemodechanged")]


def test_cutover_profile_enqueue_occurs_after_config_lock_release(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_PROFILE_ENABLED", "1")
    inside = {"lock": False}

    @contextmanager
    def _lock(_uid):
        inside["lock"] = True
        try:
            yield
        finally:
            inside["lock"] = False

    monkeypatch.setattr(config_store.db, "hosted_runtime_config_mutation_lock", _lock)
    monkeypatch.setattr(
        config_store.db,
        "get_hosted_runtime_control_strict",
        lambda _uid: ("resident", "resident", 1),
    )
    monkeypatch.setattr(
        config_store,
        "_set_hosted_runtime_mode_for_user_id_locked",
        lambda *_args, **_kwargs: config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )
    calls = []

    def _enqueue(uid, lane, **kwargs):
        assert inside["lock"] is False
        calls.append((uid, lane, kwargs["reason"]))
        return 11, False

    monkeypatch.setattr(jobs_store, "enqueue_job", _enqueue)
    monkeypatch.setattr(
        "core.wake_bus.notify",
        lambda topic, user_id: calls.append((topic, user_id)),
    )

    result = config_store._set_hosted_runtime_mode_for_user_id(
        "u",
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )

    assert result == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    assert calls[0] == ("u", "profile", "runtime_v2_cutover")


def test_cutover_profile_enqueue_is_absent_when_profile_disabled(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_PROFILE_ENABLED", raising=False)

    @contextmanager
    def _lock(_uid):
        yield

    monkeypatch.setattr(config_store.db, "hosted_runtime_config_mutation_lock", _lock)
    monkeypatch.setattr(
        config_store.db,
        "get_hosted_runtime_control_strict",
        lambda _uid: ("resident", "resident", 1),
    )
    monkeypatch.setattr(
        config_store,
        "_set_hosted_runtime_mode_for_user_id_locked",
        lambda *_args, **_kwargs: config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )
    monkeypatch.setattr(
        jobs_store,
        "enqueue_job",
        lambda *_args, **_kwargs: pytest.fail(
            "default-off cutover must not enqueue profile"
        ),
    )

    assert config_store._set_hosted_runtime_mode_for_user_id(
        "u",
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    ) == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2


def test_profile_output_budget_is_raised_for_full_cjk_json():
    seen = {}

    async def _llm(_config, _messages, **kwargs):
        seen.update(kwargs)
        return {"reply": '{"memory":"事实","user":"方式"}'}

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards="cards",
            llm=_llm,
        )
    )

    assert result.fields == {"memory": "事实", "user": "方式"}
    assert seen["max_tokens"] == 8000


def test_compose_memory_lane_defaults_match_environment_policy():
    root = Path(__file__).parent.parent
    for relative in (
        "deploy/docker-compose.phala.yaml",
        "deploy/docker-compose.phala.pre.yaml",
        "deploy/docker-compose.phala.test.yaml",
    ):
        text = (root / relative).read_text()
        backend_block, worker_block = text.split("  serve-worker:", 1)
        assert (
            'FEEDLING_V2_CAPTURE_ENABLED: "${FEEDLING_V2_CAPTURE_ENABLED:-1}"'
            in backend_block
        )
        assert (
            'FEEDLING_V2_PROFILE_ENABLED: "${FEEDLING_V2_PROFILE_ENABLED:-0}"'
            in backend_block
        )
        assert (
            'FEEDLING_V2_CAPTURE_ENABLED: "${FEEDLING_V2_CAPTURE_ENABLED:-1}"'
            in worker_block
        )
        expected_dream = (
            'FEEDLING_V2_DREAM_ENABLED: "${FEEDLING_V2_DREAM_ENABLED:-0}"'
            if relative.endswith(".test.yaml")
            else 'FEEDLING_V2_DREAM_ENABLED: "${FEEDLING_V2_DREAM_ENABLED:-1}"'
        )
        assert expected_dream in worker_block
        assert (
            'FEEDLING_V2_PROFILE_ENABLED: "${FEEDLING_V2_PROFILE_ENABLED:-0}"'
            in worker_block
        )
        assert "# DO NOT set to 1 before M5 MEMORY/USER prompt injection" in worker_block


@pytest.mark.parametrize(
    ("job", "next_job", "prefix"),
    (
        ("deploy-cvm", "deploy-test-cvm", "PROD"),
        ("deploy-test-cvm", "deploy-test-runner-cvm", "TEST"),
        ("deploy-pre-cvm", "deploy-pre-runner-cvm", "PRE"),
    ),
)
def test_profile_flag_is_wired_through_each_phala_deploy_job(job, next_job, prefix):
    workflow = (Path(__file__).parent.parent / ".github/workflows/ci.yml").read_text()
    deploy = workflow.split(f"\n  {job}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[0]
    mapping = (
        "FEEDLING_V2_PROFILE_ENABLED:  "
        f"${{{{ vars.{prefix}_FEEDLING_V2_PROFILE_ENABLED || '0' }}}}"
    )
    injection = '-e "FEEDLING_V2_PROFILE_ENABLED=$FEEDLING_V2_PROFILE_ENABLED"'
    assert mapping in deploy
    assert deploy.count(injection) == 1
