#!/usr/bin/env python3
"""消息幂等/重复三族 (Seven 体感清单;deep continuity 报 PRODUCT_FAIL 的复现).

deep 在 test 上报 "same client_msg_id ×2 → 0 user row(s); must be 1"。
0 行有两种可能:①真 bug(去重把两条都吃了)②探针只等 6s 而 test 当时刚过部署窗。
本探针在本地 rig 上给足 60s 并直查库,分辨这两者。

顺带覆盖体感 §4.5「消息重复两族」:
  - 发 1 条 → 恰 1 user row + 恰 1 条回复
  - 同 client_msg_id ×2 → 恰 1 user row
  - 不同 id 连发两条真实消息 → 2 user rows(不许被误合并)
"""
import base64
import os
import pathlib
import sys
import time
import uuid

REPO = str(pathlib.Path(__file__).resolve().parents[2])
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
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {str(detail)[:140]}", flush=True)
    if not ok:
        FAIL.append(name)


c = E2EClient.provision(route="model_api", api_url="http://127.0.0.1:5001")
print("user:", c.user_id, flush=True)
try:
    r = c.post("/v1/model_api/setup", json={"provider": "anthropic",
               "api_key": KEYS["E2E_KEY_ANTHROPIC"].strip("'\""),
               "model": "claude-haiku-4-5-20251001"})
    assert r.status_code == 200, r.text[:160]
    import db
    from core.store import UserStore
    from hosted import config_store as hcs
    hcs.set_hosted_runtime_mode(UserStore(c.user_id), hcs.HOSTED_RUNTIME_MODE_DB_ACTION_V2)

    def rows():
        with db.get_pool().connection() as conn:
            u = conn.execute("SELECT count(*) FROM chat_messages WHERE user_id=%s "
                             "AND doc->>'role'='user'", (c.user_id,)).fetchone()[0]
            a = conn.execute("SELECT count(*) FROM chat_messages WHERE user_id=%s "
                             "AND doc->>'role' IN ('agent','openclaw')", (c.user_id,)).fetchone()[0]
        return u, a

    def wait_rows(target_user, timeout=90):
        end = time.time() + timeout
        last = rows()
        while time.time() < end:
            last = rows()
            if last[0] >= target_user:
                return last
            time.sleep(3)
        return last

    # 1. single send
    cmid1 = str(uuid.uuid4())
    r1 = c.post("/v1/model_api/chat/send", json={"message": f"幂等基线 {cmid1[:8]}", "client_msg_id": cmid1})
    u, a = wait_rows(1)
    check("单发 → 恰 1 user row", u == 1, f"code={r1.status_code} rows={u}")

    # 2. same client_msg_id twice
    base_u = u
    cmid2 = str(uuid.uuid4())
    codes = []
    for _ in range(2):
        rr = c.post("/v1/model_api/chat/send",
                    json={"message": f"幂等重发 {cmid2[:8]}", "client_msg_id": cmid2})
        codes.append(rr.status_code)
        time.sleep(1)
    u2, _ = wait_rows(base_u + 1)
    delta = u2 - base_u
    check("同 client_msg_id ×2 → 恰 1 user row", delta == 1, f"codes={codes} delta={delta}")

    # 3. two genuinely different messages must NOT be merged
    base_u = u2
    for i in range(2):
        c.post("/v1/model_api/chat/send",
               json={"message": f"真连发第{i+1}条 {uuid.uuid4().hex[:6]}",
                     "client_msg_id": str(uuid.uuid4())})
        time.sleep(1)
    u3, _ = wait_rows(base_u + 2)
    check("不同 id 连发两条 → 2 user rows(不被误合并)", u3 - base_u == 2, f"delta={u3 - base_u}")

    # 4. exactly one reply per user turn (no duplicate agent bubbles)
    time.sleep(45)
    uf, af = rows()
    check("回复条数不超过用户轮数(无重复气泡)", af <= uf, f"user={uf} agent={af}")
    print(f"   final rows: user={uf} agent={af}", flush=True)
finally:
    try:
        c.teardown(); print("account deleted", flush=True)
    except Exception as e:
        print("teardown err", e, flush=True)

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
