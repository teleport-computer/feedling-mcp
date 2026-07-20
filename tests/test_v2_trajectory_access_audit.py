from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from conftest import seed_user, set_v2_runtime_owner
import db
from model_api_runtime.v2 import jobs_store


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="trajectory access audit tests require PostgreSQL",
)


@pytest.fixture(autouse=True)
def clean_tables():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_trajectory_access_audit,agent_jobs,users CASCADE"
        )
    yield


def test_access_audit_is_update_immutable_content_free_and_account_scoped():
    user_id = "u_trajectory_audit"
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    access_id = str(uuid.uuid4())

    jobs_store.append_trajectory_access_audit(
        access_id=access_id,
        phase="requested",
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="incident",
        case_ref="INC-123",
        event_count=None,
        result_code="pending",
    )
    jobs_store.append_trajectory_access_audit(
        access_id=access_id,
        phase="succeeded",
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="incident",
        case_ref="INC-123",
        event_count=3,
        result_code="ok",
    )

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT phase,event_count,result_code FROM "
            "v2_trajectory_access_audit ORDER BY id"
        ).fetchall()
        assert rows == [("requested", None, "pending"), ("succeeded", 3, "ok")]
        assert "private prompt" not in str(rows)
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE v2_trajectory_access_audit SET result_code='debug' "
                "WHERE access_id=%s",
                (access_id,),
            )
        conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_trajectory_access_audit WHERE user_id=%s",
            (user_id,),
        ).fetchone()[0] == 0


def test_source_lookup_never_crosses_user_boundary_and_denials_can_be_audited():
    owner = "u_trajectory_audit_owner"
    requester = "u_trajectory_audit_requester"
    for user_id in (owner, requester):
        seed_user(user_id)
        set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(owner, "chat")

    assert jobs_store.get_trajectory_source_job(job_id, owner)["id"] == job_id
    assert jobs_store.get_trajectory_source_job(job_id, requester) is None
    jobs_store.append_trajectory_access_audit(
        access_id=str(uuid.uuid4()),
        phase="failed",
        user_id=requester,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="support",
        case_ref="SUP-456",
        event_count=None,
        result_code="trajectory_source_not_found",
    )


def test_success_authorization_is_fenced_by_live_source_and_frontier():
    user_id = "u_trajectory_audit_fence"
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    jobs_store.append_trajectory_event(
        job_id,
        user_id,
        event_kind="provider_request",
        idempotency_key="inspect.request.0",
        payload_envelope={
            "v": 1,
            "id": "inspect-event-0",
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": base64.b64encode(b"ciphertext").decode(),
            "nonce": "nonce",
            "K_user": "wrapped-user",
            "K_enclave": "wrapped-enclave",
        },
        payload_bytes=10,
    )
    access_id = str(uuid.uuid4())
    jobs_store.append_trajectory_access_audit(
        access_id=access_id,
        phase="requested",
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="incident",
        case_ref="INC-777",
        event_count=None,
        result_code="pending",
    )
    assert jobs_store.authorize_trajectory_inspection_success(
        access_id=access_id,
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="incident",
        case_ref="INC-777",
        event_count=1,
        expected_next_event_index=1,
    ) is True

    denied_access_id = str(uuid.uuid4())
    jobs_store.append_trajectory_access_audit(
        access_id=denied_access_id,
        phase="requested",
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="security",
        case_ref="SEC-777",
        event_count=None,
        result_code="pending",
    )
    jobs_store.append_trajectory_event(
        job_id,
        user_id,
        event_kind="provider_response",
        idempotency_key="inspect.response.1",
        payload_envelope={
            "v": 1,
            "id": "inspect-event-1",
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": base64.b64encode(b"ciphertext-2").decode(),
            "nonce": "nonce",
            "K_user": "wrapped-user",
            "K_enclave": "wrapped-enclave",
        },
        payload_bytes=12,
    )
    assert jobs_store.authorize_trajectory_inspection_success(
        access_id=denied_access_id,
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="security",
        case_ref="SEC-777",
        event_count=1,
        expected_next_event_index=1,
    ) is False


def test_chat_clear_keeps_old_trajectory_authorizable_for_debugging():
    user_id = "u_trajectory_audit_after_clear"
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    jobs_store.append_trajectory_event(
        job_id,
        user_id,
        event_kind="provider_request",
        idempotency_key="inspect.after_clear.request.0",
        payload_envelope={
            "v": 1,
            "id": "inspect-after-clear-event-0",
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": base64.b64encode(b"ciphertext").decode(),
            "nonce": "nonce",
            "K_user": "wrapped-user",
            "K_enclave": "wrapped-enclave",
        },
        payload_bytes=10,
    )

    assert db.chat_clear(user_id) == 0

    access_id = str(uuid.uuid4())
    jobs_store.append_trajectory_access_audit(
        access_id=access_id,
        phase="requested",
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="debug",
        case_ref="DBG-909",
        event_count=None,
        result_code="pending",
    )
    assert jobs_store.authorize_trajectory_inspection_success(
        access_id=access_id,
        user_id=user_id,
        job_id=job_id,
        operator_id="alice@example.com",
        reason_code="debug",
        case_ref="DBG-909",
        event_count=1,
        expected_next_event_index=1,
    ) is True
