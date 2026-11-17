"""Real PostgreSQL/ASGI export contract, complete archives, and lifecycle races."""
from __future__ import annotations

import concurrent.futures
import io
import asyncio
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import db
import object_storage
from content import export_archive, export_service, export_store
from core import enclave as core_enclave
from core import runtime_token


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPORT_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_EXPORT_MAILER", "log")
    monkeypatch.setattr(object_storage, "exports_enabled", lambda: True)
    monkeypatch.setattr(object_storage, "delete_export", lambda key: None)
    with db.get_pool().connection() as conn:
        previous = [r[0] for r in conn.execute("SELECT job_id FROM user_export_jobs").fetchall()]
    yield monkeypatch
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM user_export_jobs WHERE NOT(job_id=ANY(%s))", (previous,))


def user():
    uid = "usr_" + uuid.uuid4().hex[:16]
    db.insert_user({"user_id": uid, "created_at": "2026-11-17"})
    return uid


def body(uid, text, **extra):
    return {"id": uuid.uuid4().hex, "owner_user_id": uid,
            "visibility": "shared", "body": text, **extra}


def insert_blob(uid, kind, doc):
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO user_blobs(user_id,kind,doc) VALUES(%s,%s,%s) "
                     "ON CONFLICT(user_id,kind) DO UPDATE SET doc=excluded.doc",
                     (uid, kind, Jsonb(doc)))


def archive(uid):
    output = io.BytesIO()
    export_archive.build(uid, "test-token", output)
    return zipfile.ZipFile(io.BytesIO(output.getvalue()))


@pytest.mark.parametrize("email", [None, "", "a", "a@b", "a\r\n@b.com",
                                  "a b@c.com", "x"*255+"@b.com"])
def test_invalid_email(ready, email):
    result, status = export_service.submit(user(), {"email": email})
    assert status == 400 and result["error_code"] == "invalid_email"


def test_http_auth_latest_isolation_and_no_address_or_url(client, ready, monkeypatch):
    import base64
    monkeypatch.setattr(core_enclave, "_get_enclave_info",
                        lambda: {"content_pk_hex": "22"*32, "compose_hash": "test"})
    response = client.post("/v1/users/register",
                           json={"public_key": base64.b64encode(b"1"*32).decode()})
    assert response.status_code == 201
    auth = response.get_json()
    headers = {"X-API-Key": auth["api_key"]}
    assert client.get("/v1/content/export-jobs/latest", headers=headers).status_code == 404
    response = client.post("/v1/content/export-jobs", headers=headers,
                           json={"email": "arbitrary+box@example.net"})
    assert response.status_code == 202
    job = response.get_json()
    assert job["status"] == "queued"
    assert set(job) == {"job_id", "status", "created_at", "expires_at", "error_code"}
    assert "example.net" not in str(job)
    assert client.get("/v1/content/export-jobs/latest", headers=headers).get_json() == job
    assert client.get("/v1/content/export-jobs/" + job["job_id"]).status_code == 401
    token = runtime_token.mint(b"test-runtime-token-secret", user_id=auth["user_id"],
                              runtime_instance_id="untrusted", scope=["envelope_decrypt"])
    response = client.post("/v1/content/export-jobs",
                           headers={"X-Feedling-Runtime-Token": token},
                           json={"email": "exfil@example.com"})
    assert response.status_code in {401, 403}
    assert export_service.status(user(), job["job_id"])[1] == 404


def test_one_active_concurrently_and_daily_limit(ready):
    uid = user()
    def submit():
        return export_service.submit(uid, {"email": "x@example.com"})
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(x[1] for x in results) == [202, 409]
    conflict = next(x[0] for x in results if x[1] == 409)
    job = next(x[0] for x in results if x[1] == 202)
    assert conflict["job_id"] == job["job_id"]
    for _ in range(2):
        with db.get_pool().connection() as conn:
            conn.execute("UPDATE user_export_jobs SET status='failed',email=NULL WHERE user_id=%s",
                         (uid,))
        assert submit()[1] == 202
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE user_export_jobs SET status='failed' WHERE user_id=%s", (uid,))
    assert submit()[1] == 429


def test_full_pg_history_excludes_internal_images_system_and_archive(ready):
    uid, other = user(), user()
    with db.get_pool().connection() as conn:
        # Real PG history over the 5000-row hot-cache cap.
        conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) "
                     "SELECT %s,'m'||n,1700000000+n,jsonb_build_object("
                     "'id','m'||n,'owner_user_id',%s::text,'body','visible-'||n,"
                     "'source','model_api','role','user','content_type','text') "
                     "FROM generate_series(1,5002) n", (uid, uid))
        for source, role, kind in [
            ("verify_ping","user","text"),("verify_ping","openclaw","text"),
            ("resident_maintenance","user","text"),("resident_maintenance","openclaw","text"),
            ("chat","system","text"),("chat","user","image"),("chat","user","file"),
        ]:
            doc = body(uid, "DO-NOT-EXPORT", source=source, role=role, content_type=kind,
                       file_mime="application/pdf", file_name="private.pdf")
            conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s)",
                         (uid, doc["id"],1700000000,Jsonb(doc)))
        doc = body(other, "OTHER-USER", source="chat", role="user", content_type="text")
        conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s)",
                     (other, doc["id"],1700000000,Jsonb(doc)))
        conn.execute("INSERT INTO chat_message_archive(user_id,source_seq,msg_id,ts,doc,clear_generation,cleared_at) "
                     "VALUES(%s,1,'cleared',1700000000,%s,1,now())",
                     (uid,Jsonb(body(uid,"CLEARED-MUST-NOT-RETURN"))))
    with archive(uid) as z:
        names=z.namelist()
        data="".join(z.read(n).decode() for n in names if n.startswith("chat/"))
        assert data.count("visible-") == 5002
        assert "visible-1\n" in data and "visible-5002" in data
        assert "DO-NOT-EXPORT" not in data and "OTHER-USER" not in data
        assert not any("cleared" in n for n in names)


def test_all_owned_md_archived_memory_identity_persona_worldbook_and_profile(ready):
    uid = user()  # No current V2 tuple: still export old owned workspace.
    with db.get_pool().connection() as conn:
        for path in ["/memory/WORKING.md","/workspace/project/notes.md",
                     "/workspace/Canvas.io.html","/artifacts/a.md",
                     "/skills/a.md","/workspace/../secrets.md"]:
            doc = body(uid, "FILE " + path)
            conn.execute("INSERT INTO v2_workspace_entries"
                         "(user_id,path,kind,content_envelope,mime_type,revision) "
                         "VALUES(%s,%s,'workspace',%s,'text/markdown',1)",
                         (uid,path,Jsonb(doc)))
        doc=body(uid,"old-memory",occurred_at="2026-01-01",is_archived=True)
        conn.execute("INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
                     "VALUES(%s,'m','2026-01-01',%s)",(uid,Jsonb(doc)))
        conn.execute("INSERT INTO world_book_entries(user_id,entry_id,updated_at,doc) "
                     "VALUES(%s,'../../evil','2026-01-01',%s)",
                     (uid,Jsonb(body(uid,"my-world"))))
    insert_blob(uid,"identity",body(uid,"my-identity"))
    insert_blob(uid,"genesis_persona",{"content_envelope":body(uid,"my-persona")})
    insert_blob(uid,"v2_agent_profile",{
        "memory":{"envelope":body(uid,"long-term-profile"),"chars":17},
        "style":{"envelope":body(uid,"writing-style"),"chars":13}})
    with archive(uid) as z:
        assert set(n for n in z.namelist() if n.startswith("workspace/")) == {
            "workspace/memory/WORKING.md","workspace/workspace/project/notes.md"}
        assert "已归档" in z.read("memories.md").decode()
        assert b"old-memory" in z.read("memories.md")
        assert z.read("identity.md") == b"my-identity"
        assert z.read("persona.md") == b"my-persona"
        assert b"long-term-profile" in z.read("profile.md")
        assert b"writing-style" in z.read("profile.md")
        assert any(z.read(n) == b"my-world" for n in z.namelist() if n.startswith("world-book/"))
        assert all(".." not in n.split("/") for n in z.namelist())


def test_sealed_decrypt_purpose_and_owner_fail_closed(ready, monkeypatch):
    uid = user()
    seen=[]
    def decrypt(env, api_key, **kwargs):
        seen.append(kwargs)
        return b"decoded-private"
    monkeypatch.setattr(core_enclave,"_decrypt_envelope_via_enclave",decrypt)
    sealed={"id":"identity","body_ct":"opaque","nonce":"n","K_user":"k","K_enclave":"k",
            "owner_user_id":uid,"visibility":"shared","v":1}
    insert_blob(uid,"identity",sealed)
    with archive(uid) as z:
        assert z.read("identity.md") == b"decoded-private"
    assert seen == [{"purpose":"user_export","caller_user_id":uid,"runtime_token":"test-token"}]
    insert_blob(uid,"identity",{**sealed,"owner_user_id":"someone-else"})
    with pytest.raises(ValueError, match="owner"):
        archive(uid)


def test_worker_upload_ttl_log_only_recipient_scrub_and_expiry(ready, monkeypatch, caplog):
    uid = user()
    row = export_store.create(uid,"sensitive-person@example.com",3)
    objects={}
    monkeypatch.setattr(object_storage,"upload_export",
                        lambda key,path: objects.__setitem__(key,Path(path).read_bytes()))
    monkeypatch.setattr(object_storage,"export_download_url",
                        lambda key: "https://private.example/?secret=signed-token")
    monkeypatch.setattr(object_storage,"delete_export",lambda key: objects.pop(key,None))
    import logging
    caplog.set_level(logging.INFO)
    assert export_service.tick()
    finished=export_store.get(uid,row["job_id"])
    assert finished["status"] == "completed" and finished["email"] is None
    ttl=(finished["expires_at"]-datetime.now(timezone.utc)).total_seconds()
    assert 604790 < ttl <= 604800
    with zipfile.ZipFile(io.BytesIO(objects[finished["object_key"]])) as z:
        assert "README.md" in z.namelist()
    assert "export_mail_placeholder" in caplog.text
    for secret in ["sensitive-person","example.com","signed-token","private.example"]:
        assert secret not in caplog.text
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE user_export_jobs SET expires_at=now()-interval '1 second' "
                     "WHERE job_id=%s",(row["job_id"],))
    assert export_service.cleanup_expired() == 1
    assert not objects
    assert export_store.get(uid,row["job_id"])["status"] == "expired"


def test_worker_failed_read_never_emails_partial_zip(ready, monkeypatch):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    insert_blob(uid,"identity",{"id":"bad","body_ct":"bad","owner_user_id":uid})
    sent=[]
    class Mail:
        def send(self,**kwargs): sent.append(kwargs)
    monkeypatch.setattr(core_enclave,"_decrypt_envelope_via_enclave",
                        lambda *a,**k: (_ for _ in ()).throw(RuntimeError("private data")))
    for _ in range(3):
        assert export_service.tick(Mail())
        with db.get_pool().connection() as conn:
            conn.execute("UPDATE user_export_jobs SET available_at=now() WHERE job_id=%s",
                         (row["job_id"],))
    final=export_store.get(uid,row["job_id"])
    assert final["status"] == "failed" and final["email"] is None
    assert final["error_code"] == "export_failed" and not sent


def test_delete_cleans_completed_and_pending_exports_before_cascade(ready,monkeypatch):
    uid=user()
    first=export_store.create(uid,"x@example.com",3)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE user_export_jobs SET status='completed' WHERE job_id=%s",
                     (first["job_id"],))
    second=export_store.create(uid,"y@example.com",3)
    deleted=[]
    monkeypatch.setattr(object_storage,"delete_export",deleted.append)
    assert db.delete_user(uid)
    assert set(deleted)=={first["object_key"],second["object_key"]}
    assert export_store.get(uid) is None


def test_delete_storage_failure_keeps_account_and_cleanup_coordinates(ready,monkeypatch):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    monkeypatch.setattr(object_storage,"delete_export",
                        lambda key: (_ for _ in ()).throw(RuntimeError("storage down")))
    with pytest.raises(db.UserExportCleanupError):
        db.delete_user(uid)
    assert db.user_exists(uid)
    assert export_store.get(uid,row["job_id"])["object_key"] == row["object_key"]


def test_delete_waits_for_upload_and_then_revokes_download(ready,monkeypatch):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    entered,release=threading.Event(),threading.Event()
    objects=set()
    def upload(key,path):
        entered.set()
        assert release.wait(10)
        objects.add(key)
    monkeypatch.setattr(object_storage,"upload_export",upload)
    monkeypatch.setattr(object_storage,"delete_export",lambda key: objects.discard(key))
    monkeypatch.setattr(object_storage,"export_download_url",lambda key:"https://signed.example")
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        work=pool.submit(export_service.tick)
        assert entered.wait(10)
        assert export_store.get(uid,row["job_id"])["status"] == "processing"
        assert export_service.submit(uid,{"email":"second@example.com"})[1] == 409
        deleting=pool.submit(db.delete_user,uid)
        try:
            # Check PostgreSQL lock observation instead of a timing-only sleep.
            deadline = time.monotonic() + 5
            while True:
                with db.get_pool().connection() as conn:
                    held=conn.execute("SELECT count(*) FROM pg_locks WHERE "
                                      "locktype='advisory' AND NOT granted").fetchone()[0]
                if held or time.monotonic() >= deadline:
                    break
                time.sleep(.02)
            assert held >= 1 and not deleting.done()
        finally:
            release.set()
        assert work.result(10)
        assert deleting.result(10)
    assert not objects and export_store.get(uid,row["job_id"]) is None


def test_s3_upload_presigned_seven_days_and_delete(monkeypatch,tmp_path):
    monkeypatch.setattr(object_storage,"exports_enabled",lambda:True)
    import boto3
    from moto import mock_aws
    from urllib.parse import parse_qs,urlparse
    monkeypatch.setenv("R2_EXPORTS_BUCKET","io-export-test")
    with mock_aws():
        client=boto3.client("s3",region_name="us-east-1",
                           aws_access_key_id="test",aws_secret_access_key="test",
                           config=__import__("botocore.config",fromlist=["Config"]).Config(
                               signature_version="s3v4"))
        client.create_bucket(Bucket="io-export-test")
        monkeypatch.setattr(object_storage,"_client",lambda:client)
        path=tmp_path/"archive.zip"
        with zipfile.ZipFile(path,"w") as z:
            z.writestr("README.md","owned account data")
        key="user-exports/usr_one/job.zip"
        object_storage.upload_export(key,str(path))
        meta=client.head_object(Bucket="io-export-test",Key=key)
        assert meta["ContentType"]=="application/zip"
        assert meta["CacheControl"]=="private, no-store"
        url=object_storage.export_download_url(key)
        assert parse_qs(urlparse(url).query)["X-Amz-Expires"]==["604800"]
        with pytest.raises(ValueError):
            object_storage.export_download_url(key,ttl=604801)
        object_storage.delete_export(key)
        assert not client.list_objects_v2(Bucket="io-export-test").get("Contents")


def test_crashed_processing_job_resumes_same_key_with_idempotency(ready,monkeypatch):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE user_export_jobs SET status='processing',attempts=1 "
                     "WHERE job_id=%s",(row["job_id"],))
    sent,keys=[],[]
    class Mail:
        def send(self,**kwargs):sent.append(kwargs)
    monkeypatch.setattr(object_storage,"upload_export",lambda key,path:keys.append(key))
    monkeypatch.setattr(object_storage,"export_download_url",lambda key:"https://signed")
    assert export_service.tick(Mail())
    assert keys==[row["object_key"]]
    assert sent[0]["job_id"]==row["job_id"]
    assert export_store.get(uid,row["job_id"])["attempts"]==2


def test_expiry_cleanup_runs_even_after_new_requests_are_disabled(ready,monkeypatch):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE user_export_jobs SET status='completed',expires_at="
                     "now()-interval '1 second' WHERE job_id=%s",(row["job_id"],))
    deleted=[]
    monkeypatch.setattr(object_storage,"delete_export",deleted.append)
    monkeypatch.setenv("FEEDLING_EXPORT_ENABLED","0")
    assert not export_service.tick()
    assert deleted==[row["object_key"]]


def test_profile_corruption_is_not_exported_as_ciphertext_or_empty_success(ready):
    uid=user()
    insert_blob(uid,"v2_agent_profile",{"memory":{"body_ct":"never-leak-this"}})
    with pytest.raises(ValueError,match="profile_shape"):
        archive(uid)


def test_log_mail_failure_retries_same_key_without_half_package(ready,monkeypatch):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    keys,deleted=[],[]
    class Mail:
        def send(self,**kwargs):raise RuntimeError("recipient and url must never be logged")
    monkeypatch.setattr(object_storage,"upload_export",lambda key,path:keys.append(key))
    monkeypatch.setattr(object_storage,"export_download_url",lambda key:"https://signed")
    monkeypatch.setattr(object_storage,"delete_export",deleted.append)
    assert export_service.tick(Mail())
    after=export_store.get(uid,row["job_id"])
    assert after["status"]=="queued" and after["attempts"]==1
    assert keys==deleted==[row["object_key"]]


def test_zip_text_payload_is_markdown_not_ciphertext_json(ready):
    uid=user()
    insert_blob(uid,"identity",body(uid,'{"name":"Alex","preferences":["tea","books"]}'))
    with archive(uid) as z:
        text=z.read("identity.md").decode()
        assert "## name\n\nAlex" in text
        assert "## preferences" in text and "tea" in text


def test_async_worker_loop_hosts_actual_queue_and_stops(ready,monkeypatch):
    uid=user()
    job=export_store.create(uid,"x@example.com",3)
    monkeypatch.setattr(object_storage,"upload_export",lambda key,path:None)
    monkeypatch.setattr(object_storage,"export_download_url",lambda key:"https://signed")
    original=export_service.tick
    async def drive():
        stop=asyncio.Event()
        loop=asyncio.get_running_loop()
        def tick():
            result=original()
            loop.call_soon_threadsafe(stop.set)
            return result
        monkeypatch.setattr(export_service,"tick",tick)
        await asyncio.wait_for(export_service.run(stop),timeout=10)
    asyncio.run(drive())
    assert export_store.get(uid,job["job_id"])["status"]=="completed"


def test_delete_during_build_discards_result_without_upload_or_email(ready,monkeypatch,tmp_path):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    entered,release=threading.Event(),threading.Event()
    uploads,sent=[],[]
    def build(*args):
        entered.set()
        assert release.wait(10)
    class Mail:
        def send(self,**kwargs):sent.append(kwargs)
    monkeypatch.setattr(export_archive,"build",build)
    monkeypatch.setattr(object_storage,"upload_export",lambda *args:uploads.append(args))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        generating=pool.submit(export_service.tick,Mail())
        assert entered.wait(10)
        deleting=pool.submit(db.delete_user,uid)
        try:
            # Deletion must finish before build is allowed to finish.
            assert deleting.result(3)
            assert export_store.get(uid,row["job_id"]) is None
        finally:
            release.set()
        assert generating.result(10)
    assert not uploads and not sent


def test_account_reset_does_not_hold_registry_lock_during_database_delete(
    ready,monkeypatch,tmp_path
):
    from types import SimpleNamespace
    from accounts import registry
    from content import content_core
    uid=user()
    entered,release=threading.Event(),threading.Event()
    original=db.delete_user
    def deleting(user_id):
        entered.set()
        assert release.wait(10)
        return original(user_id)
    monkeypatch.setattr(db,"delete_user",deleting)
    def take_registry():
        acquired=registry._users_lock.acquire(timeout=2)
        if acquired:
            registry._users_lock.release()
        return acquired
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        reset=pool.submit(content_core.account_reset,
                          SimpleNamespace(user_id=uid,dir=tmp_path/"absent"),
                          {"confirm":"delete-all-data"},purge_archives=lambda uid:None)
        assert entered.wait(10)
        try:
            assert pool.submit(take_registry).result(3)
        finally:
            release.set()
        assert reset.result(10)[1]==200


def test_device_only_sealed_records_are_counted_but_plain_local_records_export(
    ready,monkeypatch
):
    uid=user()
    sealed={"id":"private","owner_user_id":uid,"visibility":"local_only",
            "v":1,"body_ct":"opaque-ciphertext","nonce":"n","K_user":"k"}
    with db.get_pool().connection() as conn:
        for index,doc in enumerate([
            {**sealed,"source":"chat","role":"user","content_type":"text"},
            body(uid,"PLAINTEXT-LOCAL",visibility="local_only",
                 source="chat",role="user",content_type="text"),
        ]):
            conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s)",
                         (uid,"m"+str(index),1700000000+index,Jsonb(doc)))
        conn.execute("INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
                     "VALUES(%s,'private','2026-01-01',%s)",(uid,Jsonb(sealed)))
    monkeypatch.setattr(core_enclave,"_decrypt_envelope_via_enclave",
                        lambda *a,**k:pytest.fail("device-only content must not call enclave"))
    with archive(uid) as z:
        text="".join(z.read(n).decode() for n in z.namelist())
        assert "PLAINTEXT-LOCAL" in text and "opaque-ciphertext" not in text
        readme=z.read("README.md").decode()
        assert "- Chat messages: 1" in readme and "- Memory cards: 1" in readme
        assert "Private to your device" in readme


def test_sealed_local_only_chat_pointer_is_not_downloaded(ready,monkeypatch):
    uid=user()
    doc={"id":"private","owner_user_id":uid,"visibility":"local_only",
         "body_key":"chatfiles/"+uid+"/private","source":"chat","role":"user",
         "content_type":"file","file_mime":"text/plain","file_name":"private.txt"}
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,'p',%s,%s)",
                     (uid,1700000000,Jsonb(doc)))
    monkeypatch.setattr(db,"hydrate_chat_file_body",
                        lambda *a,**k:pytest.fail("private pointer must not be hydrated"))
    with archive(uid) as z:
        assert "- Chat messages: 1" in z.read("README.md").decode()


def test_image_retains_caption_and_readable_speaker_without_fetching_image(
    ready,monkeypatch
):
    uid=user()
    with db.get_pool().connection() as conn:
        for i,role in enumerate(["user","openclaw"]):
            doc=body(uid,"BINARY-IMAGE-MUST-NOT-BE-READ",
                     source="chat",role=role,content_type="image",
                     body_key="chatimages/"+uid+"/image",caption_body="MY-CAPTION",
                     caption_owner_user_id=uid,caption_id="caption",caption_v=1,
                     caption_visibility="shared")
            conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s)",
                         (uid,doc["id"],1700000000+i,Jsonb(doc)))
    monkeypatch.setattr(db,"hydrate_chat_file_body",
                        lambda *a,**k:pytest.fail("must not hydrate image"))
    with archive(uid) as z:
        text="".join(z.read(n).decode() for n in z.namelist() if n.startswith("chat/"))
        assert text.count("[图片 / Image]")==2
        assert text.count("MY-CAPTION")==2
        assert "BINARY-IMAGE" not in text
        assert "我 / Me" in text and "TA / Agent" in text
        assert "openclaw" not in text and " — user" not in text
        assert "UTC" in z.read("README.md").decode()


def test_archived_reason_uses_shared_memory_predicate(ready):
    uid=user()
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
                     "VALUES(%s,'reason','2026-01-01',%s)",
                     (uid,Jsonb(body(uid,"retired",archive_reason="superseded_by:new"))))
    with archive(uid) as z:
        assert "已归档" in z.read("memories.md").decode()


def test_device_private_records_do_not_prevent_completed_worker_job(ready,monkeypatch):
    uid=user()
    insert_blob(uid,"identity",{"id":"private","owner_user_id":uid,"visibility":"local_only",
                               "body_ct":"sealed","nonce":"n","K_user":"k","v":1})
    row=export_store.create(uid,"x@example.com",3)
    uploaded=[]
    monkeypatch.setattr(object_storage,"upload_export",
                        lambda key,path:uploaded.append(Path(path).read_bytes()))
    monkeypatch.setattr(object_storage,"export_download_url",lambda key:"https://signed")
    monkeypatch.setattr(core_enclave,"_decrypt_envelope_via_enclave",
                        lambda *a,**k:pytest.fail("private record must not reach enclave"))
    assert export_service.tick()
    assert export_store.get(uid,row["job_id"])["status"]=="completed"
    with zipfile.ZipFile(io.BytesIO(uploaded[0])) as z:
        assert "identity.md" not in z.namelist()
        assert "- Identity: 1" in z.read("README.md").decode()


def test_device_private_caption_is_counted_image_body_is_never_read(ready,monkeypatch):
    uid=user()
    doc=body(uid,"IMAGE-BINARY",source="chat",role="user",content_type="image",
             caption_body_ct="opaque",caption_visibility="local_only",
             caption_K_user="key",caption_nonce="nonce",caption_id="caption",
             caption_owner_user_id=uid,caption_v=1)
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s)",
                     (uid,doc["id"],1700000000,Jsonb(doc)))
    monkeypatch.setattr(core_enclave,"_decrypt_envelope_via_enclave",
                        lambda *a,**k:pytest.fail("private caption must not decrypt"))
    with archive(uid) as z:
        text="".join(z.read(n).decode() for n in z.namelist())
        assert "[图片 / Image]" in text and "opaque" not in text
        assert "- Image captions: 1" in z.read("README.md").decode()


def test_deleted_expired_objects_do_not_require_storage_again_to_delete_account(
    ready,monkeypatch
):
    uid=user()
    row=export_store.create(uid,"x@example.com",3)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE user_export_jobs SET status='expired',email=NULL,expires_at=NULL "
                     "WHERE job_id=%s",(row["job_id"],))
    monkeypatch.setattr(object_storage,"delete_export",
                        lambda *a:pytest.fail("already-cleaned historic key must not incur R2 I/O"))
    assert db.delete_user(uid)
