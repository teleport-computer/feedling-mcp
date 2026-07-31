"""P0 gate: no message lost / double-run across a runtime flip, both directions.

机制断言（不是端到端 LLM 回合）：
1. flip 前 send 的消息带旧 generation 入库/入队；
2. flip 期间（draining 窗口）send 得到 503 runtime_switching，不产生半状态行；
3. flip 后 send 的消息带新 generation；
4. 全程 chat 行数 == 成功 send 数（无丢失），每条消息 enqueue 恰好 ≤1 次（无双投，
   靠 agent_jobs.id 唯一性 + per-user/lane 单飞唯一索引证明）；
5. generation 严格递增，旧 generation 的 job 不会被新运行时按旧 tuple 认领
   （real schema note: agent_jobs 没有 ``expected_runtime_mode``/``dedupe_key``
   列——真正落在 job 行上的是 ``expected_runtime_generation``（0026 迁移）+
   ``trace_id``（信封 id）。用这两个真实字段断言 job 归属哪次 send、
   pin 在哪个 generation，而不是brief 伪代码里假想的字段名）。

Reuses the idioms from ``tests/test_chat_send_v2_enqueue.py`` (seed user +
route, drive ``chat_send_core.model_api_chat_send_core`` directly, query
``agent_jobs`` for the real effect) and ``tests/test_v2_send_enqueue_atomic.py``
(DB-backed, no HTTP layer). The draining-fence helper mirrors
``tests/test_dual_runtime_db.py``'s ``_force_fence`` — writing the fence
directly is the only way to observe ``draining`` from a test, since
``db.patch_blob_strict``'s ``runtime_state_target`` commits ``draining`` and
the target state in the SAME transaction (never externally observable via the
real control-plane writers).
"""
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import chat_send_core, config_store as hosted_config_store  # noqa: E402
from hosted import runtime_reconciler  # noqa: E402

from conftest import configure_model_api_route  # noqa: E402

POLICY_ENV = hosted_config_store.HOSTED_RUNTIME_POLICY_ENV


def _seed(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    configure_model_api_route(
        uid, provider="anthropic", model="m", test_status="ok",
        envelope={"body_ct": "x", "nonce": "n", "K_user": "k"})


def _flip(uid: str, desired: str) -> None:
    """One fenced transition via the real production path (Task 8):
    ``runtime_reconciler._flip_user`` routes through
    ``admin_core.set_runtime_mode`` (wake-seed-before-persist ordering)."""
    runtime_reconciler._flip_user(uid, desired)


# ---------------------------------------------------------------------------
# Fixtures — thin wrappers over test_chat_send_v2_enqueue.py /
# test_v2_send_enqueue_atomic.py idioms. No new harness invented.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _dual_policy(monkeypatch):
    # Flips only ever route under the dual per-user fence; pin it explicitly
    # rather than depend on the env default, since other test modules pin
    # v2_only (test_chat_send_v2_enqueue.py) or vary it per-test.
    monkeypatch.setenv(POLICY_ENV, "dual")


@pytest.fixture()
def dual_user():
    """A fresh hosted user with an active tested route, in the default
    (resident) state — no fence row written yet."""
    uid = f"u_flip_{uuid.uuid4().hex[:12]}"
    _seed(uid)
    yield uid
    # Runtime V2 entry now deliberately leaves a background profile refresh
    # pending. Keep this module's real enqueue assertions while preventing that
    # job from being claimed by later tests that exercise the global worker
    # queue in the same throwaway database.
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))


@pytest.fixture()
def dual_user_v2(dual_user):
    """Same as ``dual_user`` but already flipped to v2 via the real
    reconciler path before the test body runs."""
    _flip(dual_user, "v2")
    return dual_user


@pytest.fixture()
def _wired_sends(monkeypatch):
    """Wires the collaborators ``chat_send_core`` needs around real DB
    routing + persistence (mirrors test_chat_send_v2_enqueue.py's V2 mocks
    plus test_dual_runtime_send_routing.py's resident-live mocks): fake
    envelope build (threads the plaintext straight into ``body_ct`` so
    ``chat_rows`` can assert on message content), fake driver resolution,
    a live V2 worker pool, a live resident supervisor, and a no-op resident
    turn handoff. Routing (``get_hosted_runtime_control_strict``) and
    persistence (``store.append_chat`` / ``db.chat_append_and_enqueue``)
    stay REAL — that's the whole point of this gate."""

    def _fake_envelope(store, pt, **kw):
        text = pt.decode("utf-8", "replace") if isinstance(pt, (bytes, bytearray)) else str(pt)
        return {"id": uuid.uuid4().hex, "body_ct": text, "nonce": "n", "K_user": "k"}, ""

    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store", _fake_envelope)
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(chat_send_core.core_wake_bus, "notify", lambda *a, **k: None)
    monkeypatch.setattr(
        hosted_config_store, "_load_runtime_provider_config",
        lambda *a, **k: types.SimpleNamespace(provider="anthropic"))
    # NOTE: _ensure_model_api_runtime_profile is intentionally left REAL (unlike
    # test_dual_runtime_send_routing.py's resident-live test, which no-ops it).
    # That module-level function is also called internally by
    # config_store._set_hosted_runtime_mode_for_user_id_locked (the real flip
    # path this file drives via runtime_reconciler._flip_user) to compute the
    # persisted runtime-profile blob; no-oping it there makes the flip's
    # "persisted" value None, which set_hosted_runtime_mode misreports as
    # "user has no model_api config" (400) even though a route exists.
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "check_supervisor_live", lambda **kw: (True, ""))
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "handle_send",
        lambda s, row, driver, **kw: (
            {"status": "processing", "reply_ready": False, "runtime": {"driver": driver}}, 202))


@pytest.fixture()
def send_raw(_wired_sends):
    def _send(uid: str, text: str):
        store = core_store.get_store(uid)
        return chat_send_core.model_api_chat_send_core(
            store, api_key="key", runtime_tok="", payload={"message": text})
    return _send


@pytest.fixture()
def send(send_raw):
    def _send(uid: str, text: str) -> bool:
        body, status = send_raw(uid, text)
        assert status == 202, (status, body)
        return True
    return _send


@pytest.fixture()
def jobs_for():
    def _jobs(uid: str) -> list[dict]:
        with db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, lane, status, reason, trace_id, expected_runtime_generation "
                "FROM agent_jobs WHERE user_id=%s ORDER BY id",
                (uid,),
            ).fetchall()
        return [
            {
                "id": r[0], "lane": r[1], "status": r[2], "reason": r[3],
                "trace_id": r[4], "expected_runtime_generation": r[5],
            }
            for r in rows
        ]
    return _jobs


@pytest.fixture()
def chat_rows():
    def _rows(uid: str) -> list[dict]:
        with db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM chat_messages WHERE user_id=%s ORDER BY seq",
                (uid,),
            ).fetchall()
        return [
            {"id": doc.get("id"), "role": doc.get("role"), "body": doc.get("body_ct")}
            for (doc,) in rows
        ]
    return _rows


@pytest.fixture()
def force_state():
    """Write the hosted-runtime fence directly, bypassing the guarded
    resident<->draining<->v2 transition machinery — replicates
    test_dual_runtime_db.py's ``_force_fence`` (see that helper's docstring
    for why ``draining`` is otherwise unobservable from a test)."""
    def _force(uid: str, state: str, mode: str = hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2):
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO user_blobs (user_id, kind, doc) "
                "VALUES (%s, 'model_api_runtime', %s::jsonb) "
                "ON CONFLICT (user_id, kind) DO UPDATE SET "
                "doc = user_blobs.doc || EXCLUDED.doc",
                (uid, json.dumps({"hosted_runtime_mode": mode})),
            )
            conn.execute(
                "INSERT INTO v2_runtime_state (user_id, hosted_runtime_state) "
                "VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "hosted_runtime_state = EXCLUDED.hosted_runtime_state, "
                "runtime_generation = v2_runtime_state.runtime_generation + 1, "
                "updated_at = now()",
                (uid, state),
            )
    return _force


# ---------------------------------------------------------------------------
# The three behavior specs
# ---------------------------------------------------------------------------

def test_flip_resident_to_v2_no_loss(dual_user, send, jobs_for, chat_rows):
    uid = dual_user  # starts resident (no fence row written yet)
    gen_before = db.get_runtime_generation(uid)

    assert send(uid, "before-flip")  # resident path: append_chat only, no job
    # Mark it answered before flipping. Otherwise config_store's real
    # _recover_cutover_chat_if_needed (fired for every resident->v2 flip,
    # db.reconcile_unenqueued_v2_message_for_user) eagerly recovers this still
    # -unanswered resident row into the v2 queue itself — a real, deliberate,
    # and separately-tested no-loss mechanism (tests/test_v2_reconcile.py,
    # tests/test_hosted_runtime_policy.py). Leaving it wired here would
    # conflate that recovery path with the routing/atomicity behavior this
    # gate targets, and would coalesce the "after-flip" send's job onto the
    # recovery job instead of a fresh one — masking the very generation-pin
    # assertion below.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE chat_messages SET doc = doc || '{\"reply_status\": \"replied\"}'::jsonb "
            "WHERE user_id=%s AND doc->>'body_ct'=%s",
            (uid, "before-flip"),
        )

    _flip(uid, "v2")
    gen_after = db.get_runtime_generation(uid)
    assert gen_after > gen_before  # generation strictly increases across the flip

    assert send(uid, "after-flip")  # v2 path: append_chat + enqueue, one transaction

    rows = chat_rows(uid)
    assert len(rows) == 2  # chat row count == successful send count, nothing lost
    assert {r["body"] for r in rows if r["role"] == "user"} == {"before-flip", "after-flip"}

    jobs = jobs_for(uid)
    # Entering v2 schedules one profile refresh; only the v2-mode send enqueues
    # a chat job. The resident send before the flip never enqueues chat work.
    assert len(jobs) == 2
    profile_jobs = [job for job in jobs if job["lane"] == "profile"]
    chat_jobs = [job for job in jobs if job["lane"] == "chat"]
    assert len(profile_jobs) == 1
    assert profile_jobs[0]["reason"] == "runtime_enabled"
    assert len(chat_jobs) == 1
    assert chat_jobs[0]["reason"] == "chat_send"
    chat_job = chat_jobs[0]
    # Pinned to the POST-flip generation — proves the job was admitted under
    # the new runtime's ownership, not stale-pinned to the old one.
    assert all(job["expected_runtime_generation"] == gen_after for job in jobs)
    after_id = next(r["id"] for r in rows if r["body"] == "after-flip")
    assert chat_job["trace_id"] == after_id
    # No double-enqueue: every admitted unit has a distinct job id.
    assert len({j["id"] for j in jobs}) == len(jobs)


def test_flip_v2_to_resident_no_loss(dual_user_v2, send, jobs_for, chat_rows):
    uid = dual_user_v2  # fixture already flipped resident -> v2
    gen_v2 = db.get_runtime_generation(uid)

    assert send(uid, "before-flip-back")  # v2 path: enqueues, pinned to gen_v2

    _flip(uid, "resident")
    gen_resident = db.get_runtime_generation(uid)
    assert gen_resident > gen_v2  # generation strictly increases across the flip

    assert send(uid, "after-flip-back")  # resident path: append_chat only, no job

    rows = chat_rows(uid)
    assert len(rows) == 2  # chat row count == successful send count, nothing lost
    assert {r["body"] for r in rows if r["role"] == "user"} == {
        "before-flip-back", "after-flip-back"}

    jobs = jobs_for(uid)
    # The initial resident->v2 transition enqueued the profile refresh and the
    # pre-flip v2 send enqueued one chat job. The post-flip resident send must
    # never reach the v2 queue.
    assert len(jobs) == 2
    assert {job["lane"] for job in jobs} == {"profile", "chat"}
    assert all(job["expected_runtime_generation"] == gen_v2 for job in jobs)
    assert all(job["expected_runtime_generation"] != gen_resident for job in jobs)
    after_id = next(r["id"] for r in rows if r["body"] == "after-flip-back")
    assert all(job["trace_id"] != after_id for job in jobs)


def test_send_during_draining_is_clean_503(dual_user, send_raw, force_state, jobs_for, chat_rows):
    force_state(dual_user, "draining")

    body, status = send_raw(dual_user, "mid-drain")

    assert status == 503
    assert body["error"] == "runtime_switching"
    # Fail-closed BEFORE any persistence: no chat row, no job row.
    assert chat_rows(dual_user) == []
    assert jobs_for(dual_user) == []
