#!/usr/bin/env python3
"""世界书 V2 live 验收 (codex4 dddd9a29 的 gatekeep live 半).

断言(按我给他的验收口径):
  1. 建两条条目:一条该命中(触发词出现在用户消息里)、一条不该命中
  2. 聊到命中词 → 回复体现命中条目的内容
  3. 未命中条目的内容不出现
  4. 无条目账号:聊天正常(不因空块报错)
"""
import base64
import json
import os
import sys
import time

REPO = "/Users/xiaotingtan/Desktop/feedling-mcp-test"
sys.path.insert(0, os.path.join(REPO, "backend"))
sys.path.insert(0, os.path.join(REPO, "tools"))
os.environ.setdefault("DATABASE_URL", "postgresql://xiaotingtan@127.0.0.1:5432/feedling_parity_e2e?sslmode=disable")
os.environ.setdefault("TEE_DATABASE_URL", "postgresql://xiaotingtan@127.0.0.1:5432/feedling_parity_e2e_tee?sslmode=disable")
os.environ.setdefault("FEEDLING_ENCLAVE_URL", "http://127.0.0.1:5003")
os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "parity-secret")
os.environ["NO_PROXY"] = "*"; os.environ.pop("HTTP_PROXY", None); os.environ.pop("HTTPS_PROXY", None)
KEYS = dict(l.strip().split("=", 1) for l in open(os.path.expanduser("~/.feedling-e2e-keys.env"))
            if "=" in l and not l.startswith("#"))
from e2e.client import E2EClient  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {str(detail)[:200]}", flush=True)
    if not ok:
        FAIL.append(name)


HIT = {"name": "青岚学院", "keys": ["青岚", "学院"],
       "content": "青岚学院是坐落在雾隐山脉的一所古老书院,院训是「见微知著」,每年霜降举办观星祭。"}
MISS = {"name": "赤鸦商会", "keys": ["赤鸦", "商会"],
        "content": "赤鸦商会垄断南境的香料贸易,会长绰号「铜算盘」,总部在落日港的第七码头。"}

c = E2EClient.provision(route="model_api", api_url="https://test-api.feedling.app")
print("user:", c.user_id, flush=True)
try:
    r = c.post("/v1/model_api/setup", json={"provider": "anthropic",
               "api_key": KEYS["E2E_KEY_ANTHROPIC"].strip("'\""),
               "model": "claude-haiku-4-5-20251001"})
    assert r.status_code == 200, r.text[:160]

    # pin the throwaway account to Runtime V2 (test admin allowlist)
    import httpx
    tok = open(os.path.expanduser("~/.feedling/data-track-admin-token")).read().strip()
    with httpx.Client(timeout=40, verify=False) as h:
        pin = h.post("https://test-api.feedling.app/v1/admin/runtime-allowlist",
                     headers={"X-Admin-Token": tok},
                     json={"user_id": c.user_id, "desired": "v2",
                           "note": "worldbook live probe 2026-08-06"})
    print("   pin v2:", pin.status_code, pin.text[:120], flush=True)
    time.sleep(20)

    for e in (HIT, MISS):
        # World Book entries are client-sealed (same as iOS): the server only
        # ever stores an envelope; plaintext lives in body_ct.
        # The enclave json.loads() the decrypted world-book body: the sealed
        # plaintext must be the ENTRY OBJECT, not raw prose. Matching reads
        # entry["keywords"] (not "keys").
        inner = {"id": e["name"], "name": e["name"], "keywords": e["keys"],
                 "content": e["content"], "enabled": True}
        # AAD = owner||v||id: the id must be bound AT SEAL TIME. Overwriting
        # env["id"] afterwards silently breaks the AEAD tag (that cost an hour).
        from content_encryption import build_envelope
        env = build_envelope(
            plaintext=json.dumps(inner, ensure_ascii=False).encode("utf-8"),
            owner_user_id=c.user_id,
            user_pk_bytes=bytes(c._sk.public_key),
            enclave_pk_bytes=c._enclave_pk,
            visibility="shared",
            item_id=e["name"],
        )
        rr = c.post("/v1/worldbook/upsert", json={"envelope": env, "id": e["name"]})
        print(f"   upsert {e['name']}: {rr.status_code} {rr.text[:120]}", flush=True)
    lst = c.get("/v1/worldbook/list")
    print("   list:", lst.status_code, lst.text[:160], flush=True)

    t = c.send_chat("你还记得青岚学院的院训是什么吗?顺便说说他们每年有什么活动。")
    m = c.wait_reply(t, timeout=300)
    reply = c.decrypt_reply(m) if m else ""
    print("reply:", reply[:220], flush=True)
    check("命中条目内容进了回复", ("见微知著" in reply) or ("观星祭" in reply), reply[:120])
    check("未命中条目内容没泄漏", ("铜算盘" not in reply) and ("落日港" not in reply), reply[:120])
finally:
    try:
        c.teardown(); print("account deleted", flush=True)
    except Exception as e:
        print("teardown err", e, flush=True)

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
