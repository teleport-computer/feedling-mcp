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
  7. **唤醒道也认得世界书**(2026-08-10 新增,本探针的主体之一):种一个已到期的
     定时提醒并立即 fire,断言到点那条主动消息里 ① alwaysOn 的世界常数出现;
     ② 提醒正文里提到的关键词条目被命中;③ 关掉的条目即使触发词命中也不出现。

**覆盖边界(别含糊)**:只有 `scheduled` 有强制触发端点
(`POST /v1/proactive/scheduled/fire`,绕过 30s 调度器)。heartbeat / manual_wake /
screen_watch **没有 live 覆盖** —— 它们与 scheduled 走 `_run_wake` 里**同一段**取用
代码,差别只在传给匹配器的 messages,那一段由单测四条 lane 全参数化锁死
(`tests/test_v2_wake_worker.py::test_only_scheduled_wake_demands_a_reply` 同族)。

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
    # ⚠️ 必须**等 converged**,不能只 sleep。V2 调度器的 `eligible_users` 只认已经
    # 收敛到 db_action_v2 的用户(`_build_scheduler_deps`),没收敛就整条唤醒道跳过
    # ——聊天照常работа(走的是另一条路),于是探针会以为一切正常,却永远等不到
    # 主动消息。2026-08-10 我第一版只 sleep(20),唤醒断言拿到空回复。
    converged = False
    for _ in range(30):
        with httpx.Client(timeout=40, verify=False) as h:
            al = h.get("https://test-api.feedling.app/v1/admin/runtime-allowlist",
                       headers={"X-Admin-Token": tok})
        try:
            rows = [e for e in al.json().get("allowlist", [])
                    if e.get("user_id") == c.user_id]
        except Exception:
            rows = []
        if rows and rows[0].get("converged"):
            converged = True
            print("   v2 converged after", _ * 5, "s", flush=True)
            break
        time.sleep(5)
    check("账号已收敛到 Runtime V2", converged,
          "没收敛 = 调度器整条唤醒道跳过,后面的唤醒断言无意义")

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

    # ── 断言 7:唤醒道也认得世界书(本轮改动的主体)
    #
    # ⚠️ **不要用 `POST /v1/proactive/scheduled/fire`**。它是 V1/resident 形状:
    # `proactive_core.scheduled_fire` 无条件 `append_proactive_job(legacy_job_...)`,
    # 把 job 塞进 **V2 下无人排空的旧流**(`serve_worker._fire_scheduled_for_user`
    # 注释里的 BUG-3),同时把 timer 标记 fired —— 于是 V2 用户既拿不到消息,timer
    # 也被消耗掉。2026-08-10 我的第一版探针正是这么写的,拿到一个**空的**唤醒回复。
    # (该端点的真实调用方是 resident consumer,iOS 不调,所以线上用户碰不到;
    #  但探针必须走用户真实走的那条路。)
    #
    # 正路:把 timer 排在**过去**,然后等 V2 调度器那 30s 一跳自己取走
    # (`due_scheduled_users` → `_fire_scheduled_for_user` → agent_jobs)。
    import datetime as _dt
    c.post("/v1/proactive/state", json={"scheduled": True, "timezone": "UTC"})
    past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)).isoformat()
    # 提醒正文里同时提到 HIT(该命中)与 OFF(关着,即使命中也不该出现)的触发词。
    # ⚠️ 提醒正文必须让**这次提醒的任务本身**需要那份世界知识。
    # 世界书进了 prompt ≠ 模型一定会复述它:一条「该去上课了」的提醒没有任何理由
    # 背诵历法。2026-08-10 第一版就是这么写的,唤醒消息**确实来了**(证明唤醒道
    # 端到端是通的),但不含世界书内容,于是断言红得莫名其妙。
    # e2e 只看得见模型输出,所以要把「需要用到它」写进任务里。
    note = ("提醒他去青岚学院上课;跟他说一句院训,再按这个世界的历法告诉他今天是哪个月,"
            "顺便提一下幽兰剑冢")
    t_wake = time.time()
    sch = c.post("/v1/proactive/scheduled/actions", json={"actions": [
        {"type": "schedule_wake", "at": past, "tz": "UTC", "note": note}]})
    print("   schedule(past):", sch.status_code, sch.text[:140], flush=True)
    check("定时器排定成功", sch.status_code == 200 and
          "schedule_wake_result" in sch.text, sch.text[:160])

    # V2 调度器 30s 一跳 + 一次真实模型回合,给足余量。
    m_wake = c.wait_reply(t_wake, timeout=300)
    wake_reply = c.decrypt_reply(m_wake) if m_wake else ""
    print("wake reply:", wake_reply[:400], flush=True)

    # ⚠️ 先证明**有正文**,再谈里面有没有东西。空回复会让下面所有「不该出现」的
    # 断言恒真——2026-08-10 第一版就这么假 PASS 了一条(唤醒回复为空,而
    # 「enabled=false 不出现」照样绿)。
    check("唤醒道真的产出了一条主动消息", bool(wake_reply.strip()),
          "空回复:下面的负向断言全部无意义")
    if wake_reply.strip():
        check("唤醒道拿到了 alwaysOn 世界常数",
              MARK["ALWAYS"] in wake_reply or "影月" in wake_reply, wake_reply[:200])
        check("唤醒道按提醒正文命中了关键词条目",
              MARK["HIT"] in wake_reply or "观星祭" in wake_reply, wake_reply[:200])
        check("唤醒道上 enabled=false 依旧不出现",
              MARK["OFF"] not in wake_reply and "血誓碑" not in wake_reply,
              wake_reply[:200])

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
