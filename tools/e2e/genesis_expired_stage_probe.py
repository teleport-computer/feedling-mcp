"""本机全栈探针:素材过期后「重试」撞到的那堵墙,以及失败文案说的是不是真话。

单测能钉住文案字符串,但钉不住**用户真正会看到的东西** —— 那要穿过路由、job
投影、序列化一整条链路。这个探针走真实 HTTP,断言两件事:

  1. staged 过期后再 commit → **410 `staged_import_expired`**,且 blob 当场被删。
     这正是 usr_3b73f1cb0a9ec975 隔天点「重试」撞上的墙:按钮还在、点了必死。
  2. plaintext 失败 job 的 `friendly_copy` **不再**声称「已自动重新排队」
     (plaintext 路根本没有重排),也**不再**无条件承诺「已上传的材料不会丢」
     (素材过了 TTL 就真的没了)。封装分块路的措辞不在本探针范围 —— 那条路的
     自动重排是真的,由单测钉住。

台子与 `genesis_resume_probe.py` 共用(serve_dev :5001 + dev-seed enclave :5003 +
固定 PG),**enclave 必须带 `NO_PROXY`**,否则 macOS 系统代理会污染回环。
起法见那个文件的 docstring。

    NO_PROXY='*' python3 tools/e2e/genesis_expired_stage_probe.py
"""
import base64
import os
import subprocess
import sys

import httpx

BASE = "http://127.0.0.1:5001"
DB = f"postgresql://{os.environ.get('USER')}@127.0.0.1:5432/feedling_gate_b1"
C = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)

FAILED = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def psql(sql):
    return subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", "5432", "-d", "feedling_gate_b1", "-Atc", sql],
        capture_output=True, text=True).stdout.strip()


# ---- register ------------------------------------------------------------- #
reg = C.post("/v1/users/register", json={
    "public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
    "archive_language": "zh-Hans-CN",
})
assert reg.status_code == 201, reg.text
uid = reg.json()["user_id"]
key = reg.json()["api_key"]
H = {"X-API-Key": key}
print(f"user={uid}")

# ---- stage some material -------------------------------------------------- #
material = "\n".join(f"我: 第{i}条消息,关于骑行和失眠。" for i in range(40))
est = C.post("/v1/genesis/imports/plaintext/estimate", headers=H,
             json={"format": "plaintext", "content": material})
check("estimate accepted", est.status_code in (200, 201), f"{est.status_code} {est.text[:160]}")
if est.status_code not in (200, 201):
    sys.exit(1)
staged_id = est.json().get("staged_id") or ""
check("estimate returns staged_id", bool(staged_id), staged_id)

blob_kind = f"genesis_staged:{staged_id}"
present = psql(f"SELECT count(*) FROM user_blobs WHERE user_id='{uid}' AND kind='{blob_kind}'")
check("staged blob stored", present == "1", f"count={present}")

# ---- force the stage past its TTL ----------------------------------------- #
psql(f"UPDATE user_blobs SET doc = jsonb_set(doc, '{{expires_at}}', to_jsonb(1::int)) "
     f"WHERE user_id='{uid}' AND kind='{blob_kind}'")
print("(forced expires_at into the past)")

# ---- retry-shaped commit on the expired stage ----------------------------- #
commit = C.post("/v1/genesis/imports/plaintext/commit", headers=H,
                json={"staged_id": staged_id})
body = commit.text[:200]
check("expired stage commit answers 410", commit.status_code == 410,
      f"{commit.status_code} {body}")
try:
    err = commit.json().get("error", "")
except Exception:
    err = ""
check("error slug is staged_import_expired", err == "staged_import_expired", err)

gone = psql(f"SELECT count(*) FROM user_blobs WHERE user_id='{uid}' AND kind='{blob_kind}'")
check("expired blob is deleted on load", gone == "0", f"count={gone}")

# ---- failure copy on a plaintext job -------------------------------------- #
job_id = "genesis_e2e_copy"
psql(
    "INSERT INTO genesis_import_jobs (user_id, job_id, status, source_kind, "
    "total_chunks, error, metadata) VALUES "
    f"('{uid}', '{job_id}', 'failed', 'history_import', 3, "
    "'genesis_stale_timeout:1800s', '{\"ingest\":\"plaintext\",\"mode\":\"onboarding\"}'::jsonb)"
)
st = C.get(f"/v1/genesis/imports/{job_id}", headers=H)
check("job status readable", st.status_code == 200, f"{st.status_code} {st.text[:120]}")
copy = (st.json().get("friendly_copy") or "") if st.status_code == 200 else ""
print(f"\ncopy => {copy}\n")
check("plaintext copy drops the false auto-requeue claim", "已自动重新排队" not in copy)
check("english side drops it too", "re-queued" not in copy.lower())
check("no unconditional 'materials are kept' promise", "已上传的材料不会丢" not in copy)
check("sets a bounded expectation instead", "过期后需要重新选择文件" in copy)

# ---- cleanup -------------------------------------------------------------- #
C.post("/v1/account/reset", headers=H, json={"confirm": "delete-all-data"})

print()
if FAILED:
    print(f"E2E FAILED: {FAILED}")
    sys.exit(1)
print("E2E ALL PASS")
