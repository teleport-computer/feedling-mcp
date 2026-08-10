#!/usr/bin/env python3
"""世界书 V2 live 验收 (codex4 dddd9a29 的 gatekeep live 半).

断言:
  1. **无条目账号聊天正常**(不因空块报错)—— 首版 docstring 声明了但没实现,
     2026-08-10 补
  2. 命中条目(触发词出现在用户消息里)的内容进了回复
  3. 未提及的条目内容不出现
  4. **enabled=false 的条目,即使触发词就在消息里也必须不出现** —— 首版缺,
     而这才是真闸:第 3 条只证明「没提到的没进来」,那是匹配器不干活也会通过的
     恒真式;只有第 4 条能区分「按开关过滤」和「压根没读这条」。2026-08-10 补
  5. alwaysOn 条目在没有任何触发词时也必须出现(另一半开关语义)
  6. delete 后 list 里真的没了

覆盖面(读码确认,2026-08-10):世界书**只接前台聊天**——V2 是
`worker.py: if lane == "chat"`,V1 是 `chat_resident_consumer._worldbook_context_
for_foreground`。**两条运行时一致**,唤醒道(心跳/定时/screen_watch)不注入世界书,
不是 V2 的 parity 缺口。本探针因此只打聊天道。
"""
import base64
import json
import os
import sys
import time

# 从 __file__ 推,**不要写死主工作树路径**:写死的话,在任何 worktree 里跑这个
# 探针都会 import 主树的 backend/tools,于是你刚改的代码根本没被测到,而探针照样
# 全绿。2026-08-10 实撞:在 worktree 给 E2EClient 加了 delete(),跑起来仍报
# "no attribute 'delete'" —— 加载的是主树那份。
REPO = os.path.dirname(  # <repo>
    os.path.dirname(     # <repo>/tools
        os.path.dirname(os.path.abspath(__file__))))  # <repo>/tools/e2e
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
# 触发词会**被提到**,但条目关掉了 —— 出现即证明 enabled 没被尊重。
OFF = {"name": "幽兰剑冢", "keys": ["幽兰", "剑冢"], "enabled": False,
       "content": "幽兰剑冢埋着三千柄断剑,守冢人自称「拾荒老鬼」,冢口刻着血誓碑。"}
# 一个触发词都不会被提到,靠 alwaysOn 进场。
ALWAYS = {"name": "历法常识", "keys": [], "alwaysOn": True,
          "content": "此世界通行「墨白历」,一年分十四个月,闰月称为影月。"}
ENTRIES = [HIT, MISS, OFF, ALWAYS]

# 每条条目取一个**只在该条目里出现**的判据词,避免用泛词误判。
MARK = {"HIT": "见微知著", "MISS": "铜算盘", "OFF": "拾荒老鬼", "ALWAYS": "墨白历"}

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

    # ── 断言 1:一条条目都没有时,聊天必须正常(空 world_book 块不能把轮次打挂）
    t0 = c.send_chat("先随便聊一句,你好呀。")
    m0 = c.wait_reply(t0, timeout=300)
    reply0 = c.decrypt_reply(m0) if m0 else ""
    print("reply(空世界书):", reply0[:120], flush=True)
    check("无条目账号聊天正常", bool(reply0.strip()), reply0[:80])

    for e in ENTRIES:
        # World Book entries are client-sealed (same as iOS): the server only
        # ever stores an envelope; plaintext lives in body_ct.
        # The enclave json.loads() the decrypted world-book body: the sealed
        # plaintext must be the ENTRY OBJECT, not raw prose. Matching reads
        # entry["keywords"] (not "keys").
        inner = {"id": e["name"], "name": e["name"], "keywords": e["keys"],
                 "content": e["content"], "enabled": e.get("enabled", True)}
        if e.get("alwaysOn"):
            inner["alwaysOn"] = True
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

    # 同一条消息里**同时提到** HIT 与 OFF 的触发词:两者的匹配条件只差 enabled,
    # 所以 HIT 出现而 OFF 不出现,才唯一地证明是「按开关过滤」而不是「压根没读」。
    t = c.send_chat(
        "你还记得青岚学院的院训是什么吗?顺便说说他们每年有什么活动。"
        "另外幽兰剑冢那边是什么来头?还有你们这儿的历法是怎么算的?")
    m = c.wait_reply(t, timeout=300)
    reply = c.decrypt_reply(m) if m else ""
    print("reply:", reply[:400], flush=True)
    check("命中条目内容进了回复", MARK["HIT"] in reply or "观星祭" in reply, reply[:120])
    check("未提及的条目没泄漏", MARK["MISS"] not in reply and "落日港" not in reply, reply[:120])
    check("enabled=false 的条目即使触发词命中也没出现",
          MARK["OFF"] not in reply and "血誓碑" not in reply, reply[:200])
    check("alwaysOn 条目无触发词也进了回复",
          MARK["ALWAYS"] in reply or "影月" in reply, reply[:200])

    # ── 断言 6:delete 之后 list 里真的没了
    # ⚠️ 是 DELETE + query 参数,不是 POST + body(首版探针在这里拿了 405,
    # 而 405 很容易被读成「删除坏了」——其实是调用方用错了动词)。
    dd = c.delete("/v1/worldbook/delete", params={"id": HIT["name"]})
    lst2 = c.get("/v1/worldbook/list")
    ids_after = [x.get("id") for x in (lst2.json().get("envelopes") or [])]
    check("delete 后 list 不再含该条目",
          dd.status_code == 200 and HIT["name"] not in ids_after,
          f"del={dd.status_code} ids={ids_after}")
finally:
    try:
        c.teardown(); print("account deleted", flush=True)
    except Exception as e:
        print("teardown err", e, flush=True)

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
