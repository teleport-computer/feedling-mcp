from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from conftest import seed_user
from model_api_runtime.v2 import jobs_store
from workspace.backends import PostgresWorkspaceBackend, WorkspaceConflict


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="workspace DB tests require PostgreSQL",
)


class _Codec:
    def seal(self, path: str, plaintext: bytes) -> dict:
        # Deliberately opaque-ish test envelope. The backend must pass only this
        # object to jobs_store, never the plaintext sibling.
        return {"id": path, "body_ct": plaintext.hex(), "owner_user_id": "test"}

    def open(self, path: str, envelope: dict) -> bytes:
        assert envelope["id"] == path
        return bytes.fromhex(envelope["body_ct"])


@pytest.fixture
def clean_workspace():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE v2_sandbox_usage_events, v2_workspace_entries, users CASCADE")
    yield


def test_postgres_workspace_encrypts_content_and_enforces_revision_cas(clean_workspace):
    uid = "u_workspace_db"
    seed_user(uid)
    backend = PostgresWorkspaceBackend(uid, _Codec())

    first = backend.write("/workspace/plan.md", "secret plan", expected_revision=0)
    assert first.revision == 1
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT content_envelope::text FROM v2_workspace_entries "
            "WHERE user_id=%s AND path=%s",
            (uid, "/workspace/plan.md"),
        ).fetchone()
    assert "secret plan" not in row[0]
    assert backend.read("/workspace/plan.md").content == "secret plan"

    with pytest.raises(WorkspaceConflict):
        backend.write("/workspace/plan.md", "stale", expected_revision=0)
    second = backend.write(
        "/workspace/plan.md", "next", expected_revision=first.revision,
    )
    assert second.revision == 2
    backend.delete("/workspace/plan.md", expected_revision=second.revision)


def test_sandbox_acquisition_is_append_only_billable_usage(clean_workspace):
    uid = "u_workspace_billing"
    seed_user(uid)
    first = jobs_store.record_sandbox_acquisition(
        uid, provider="cvm", purpose="materialize_artifact",
    )
    second = jobs_store.record_sandbox_acquisition(
        uid, provider="cvm", purpose="shell",
    )
    assert second > first
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT provider,purpose FROM v2_sandbox_usage_events "
            "WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [("cvm", "materialize_artifact"), ("cvm", "shell")]


def test_nonrecursive_listing_is_not_starved_by_nested_entries(clean_workspace):
    uid = "u_workspace_listing"
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_workspace_entries "
            "(user_id,path,kind,content_envelope,mime_type,source_ref,revision) "
            "SELECT %s, '/workspace/dir/' || lpad(i::text, 4, '0') || '/nested.md', "
            "'workspace', '{\"body_ct\":\"opaque\"}'::jsonb, 'text/markdown', '', 1 "
            "FROM generate_series(1, 600) AS i",
            (uid,),
        )
        conn.execute(
            "INSERT INTO v2_workspace_entries "
            "(user_id,path,kind,content_envelope,mime_type,source_ref,revision) "
            "VALUES (%s,'/workspace/dir/zzzz.md','workspace',"
            "'{\"body_ct\":\"opaque\"}'::jsonb,'text/markdown','',1)",
            (uid,),
        )

    rows = jobs_store.list_workspace_entries(
        uid, prefix="/workspace/dir", recursive=False, limit=100,
    )
    assert [row["path"] for row in rows] == ["/workspace/dir/zzzz.md"]
