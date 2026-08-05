"""入住/记忆「处理」管线(estimate → commit → status)的真跑探针。

为什么存在:2026-08-03/04 这批(P0 止血 + estimate/commit 新流程)上线前,
本探针的前身在本机全栈真跑里抓出四个单测一个都没发现的问题 ——

  1. 分支基点缺 test 的 capture_mode 白名单 → apply_outputs 100% 挂,
     且报错(`memory_actions_failed:capture_mode_invalid`)完全不指向真因;
  2. combined_map 提前 return → 24 窗素材只蒸 8 窗就 done(用户 2/3 的历史被
     静默丢弃),而 windows_total 仍报 24,进度条永远到不了头;
  3. 后端重启后 job 卡在 processing、per-user 排他锁不放行 → 用户被锁 30 分钟
     (每次部署必现);
  4. status 帧里 materials 时有时无(3→0→3)→ 客户端横幅计数闪烁。

共同点:都只在「真 provider + 真管线 + 多帧观察」下暴露。单测和契约测试全绿。

用法:
    python3 -m tools.e2e.processing_probe                 # 全部已配 key 的 provider
    python3 -m tools.e2e.processing_probe --only hojimi   # 指定格子
    python3 -m tools.e2e.processing_probe --large         # 加跑多窗大素材(慢,~3 分钟)

目标环境由 FEEDLING_E2E_API 决定(默认 test)。client 硬拒 prod。
每个格子用完即删账号(test-account-hygiene)。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.client import E2EClient  # noqa: E402
from tools.e2e.config import HOSTED_CELLS, HostedCell, load_keys  # noqa: E402
from tools.e2e.probe_common import (  # noqa: E402
    BLOCKED_EVIDENCE, PASS, PRODUCT_FAIL, Probe, worst,
)

# 期望的材料顺序 —— spec §2-9,后端按此序处理(身份类先行,聊天史最后)。
EXPECTED_KINDS = ["ai_persona", "user_profile", "memory_summary", "chat_history"]
MATERIAL_STATUSES = {"queued", "processing", "failed", "done"}

PERSONA = "角色卡:名字叫小满。性格温柔耐心,喜欢在傍晚散步,说话带一点点笨拙的幽默感。"
PROFILE = "个人档案:独立插画师,住在杭州,最近在赶一本绘本的截稿。"
SUMMARY = "长期记忆摘要:我们认识于 2025 年秋天。赶稿时容易忘记吃饭;每年冬至一定要吃汤圆。"
HISTORY_SMALL = "\n".join([
    "User: 今天又画到凌晨,手腕疼,医生说是腱鞘炎。",
    "Assistant: 腱鞘炎不能硬扛,画一小时就得歇十分钟。",
    "User: 年糕又踩我的数位板了,这只橘猫一点不见外。",
    "Assistant: 年糕大概只是想让你抬头看看它。",
])


def _large_history(target_chars: int = 380_000) -> str:
    """多窗素材:后端切窗 max_chars=18000,38 万字符 → 20+ 窗,足以复刻
    usr_e8fe / usr_9601 那类大导入(采样 + 后台补全两段都会被走到)。"""
    scenes = [("十月", "赶绘本初稿"), ("十一月", "改第三版"), ("十二月", "冬至"),
              ("一月", "交稿"), ("二月", "回老家")]
    lines: list[str] = []
    i = 0
    while sum(len(x) for x in lines) < target_chars:
        month, work = scenes[i % len(scenes)]
        lines.append(f"User: {month}第{i}天,今天{work},画到第{i % 60}页,手腕有点酸。")
        lines.append(f"Assistant: {month}辛苦了,{work}你已经坚持很久。手腕酸就歇十分钟。")
        i += 1
    return "\n".join(lines)


def _estimate_payload(history: str, client_job_id: str) -> dict:
    return {
        "content": history, "format": "auto", "fresh_start": False,
        "client_job_id": client_job_id,
        "ai_persona_content": PERSONA, "character_content": PERSONA,
        "personal_profile_content": PROFILE,
        "memory_summary_content": SUMMARY, "support_material_content": SUMMARY,
        "relationship_started_at": "2025-10-01", "mode": "onboarding",
    }


def _job_id(body: dict) -> str:
    return str((body.get("job") or {}).get("job_id") or body.get("job_id") or "")


def _history_material(job: dict) -> dict:
    for m in job.get("materials") or []:
        if m.get("kind") == "chat_history":
            return m
    return {}


def _poll(c: E2EClient, job_id: str, *, timeout: float, frames: list[dict]) -> dict:
    """轮询到终态,把每一帧收集起来 —— 抖动/单调性判据要看帧序列,不能只看终态。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = c.get(f"/v1/genesis/imports/{job_id}")
        if r.status_code != 200:
            # 探针铁律:非 200 硬失败,不能吞成"再试一次"(会把产品错误藏十几分钟)
            raise RuntimeError(f"status poll HTTP {r.status_code}: {r.text[:160]}")
        last = r.json()
        frames.append(last)
        if last.get("status") in ("done", "failed"):
            return last
        time.sleep(3)
    return last


def run_processing_cell(cell: HostedCell, pool: dict[str, str], *, large: bool = False) -> dict:
    """一个 provider 格子跑完整处理管线。返回 probe_common 的 result 结构。"""
    p = Probe(f"processing:{cell.name}")
    key = cell.key(pool)
    if not key:
        p.blocked("key", f"no {cell.key_env} in pool")
        return p.result()
    models = cell.models or [m for m in [pool.get("E2E_RELAY_MODEL", "")] if m]
    if not models:
        p.blocked("model", "no model candidates configured")
        return p.result()

    with E2EClient.provision(route="model_api") as c:
        # -- provider 配置(用自检那一次证明 key 可用)---------------------
        configured = ""
        for model in models:
            payload = {"provider": cell.provider, "model": model, "api_key": key}
            base = cell.base_url(pool)
            if base:
                payload["base_url"] = base
            r = c.post("/v1/model_api/setup", json=payload)
            if r.status_code == 200:
                body = r.json()
                if ((body.get("config") or {}).get("test_status") or body.get("status")) in ("ok",):
                    configured = model
                    break
        if not p.ok("setup", bool(configured), f"model={configured or models}"):
            return p.result()

        history = _large_history() if large else HISTORY_SMALL
        job_id = ""
        staged_id = ""
        recommended = None

        # -- estimate:契约 + 推荐链路 ------------------------------------
        def _estimate():
            nonlocal staged_id, recommended
            r = c.post("/v1/genesis/imports/plaintext/estimate",
                       json=_estimate_payload(history, f"probe-{cell.name}-{time.time()}"))
            if r.status_code not in (200, 201):
                return PRODUCT_FAIL, f"HTTP {r.status_code}: {r.text[:120]}"
            body = r.json()
            staged_id = str(body.get("staged_id") or "")
            recommended = body.get("recommended_model")
            kinds = [m.get("kind") for m in body.get("materials") or []]
            total = int(body.get("est_total_tokens") or 0)
            if not staged_id:
                return PRODUCT_FAIL, "no staged_id"
            if kinds != EXPECTED_KINDS:
                return PRODUCT_FAIL, f"material order {kinds} != {EXPECTED_KINDS}"
            if total <= 0:
                return PRODUCT_FAIL, f"est_total_tokens={total}"
            return PASS, f"kinds ok, est={total} tokens, recommended={recommended}"
        p.guard("estimate_contract", _estimate)
        if not staged_id:
            return p.result()

        # 推荐模型:每个 provider 家族都该给得出一个快模型(§2-10)。
        # bedrock 目前设计上返回 null,其余为 null 即回归。
        p.ok("recommends_fast_model", bool(recommended),
             f"recommended_model={recommended!r} provider={cell.provider}",
             fail=PRODUCT_FAIL)

        # -- commit ---------------------------------------------------------
        def _commit():
            nonlocal job_id
            r = c.post("/v1/genesis/imports/plaintext/commit",
                       json={"staged_id": staged_id,
                             **({"distill_model": recommended} if recommended else {})})
            if r.status_code >= 300:
                return PRODUCT_FAIL, f"HTTP {r.status_code}: {r.text[:120]}"
            job_id = _job_id(r.json())
            return (PASS, f"job={job_id}") if job_id else (PRODUCT_FAIL, "no job_id")
        p.guard("commit", _commit)
        if not job_id:
            return p.result()

        # -- 防重:同 stage 再提交 / 处理中另开一单 --------------------------
        def _double_commit():
            r = c.post("/v1/genesis/imports/plaintext/commit", json={"staged_id": staged_id})
            body = r.json() if r.status_code < 500 else {}
            err = body.get("error")
            # 4f9e3d1d 起 staged 只在 DONE 时 consume(失败重试要复用材料),
            # 处理中的重复提交撞活跃 job 闸;更早的部署则撞 consumed。两者都算防住。
            ok = r.status_code == 409 and (
                (err == "import_job_active" and body.get("active_job_id") == job_id)
                or err == "staged_import_consumed")
            return (PASS if ok else PRODUCT_FAIL), f"HTTP {r.status_code} {str(body)[:90]}"
        p.guard("double_commit_rejected", _double_commit)

        def _concurrent_commit():
            r1 = c.post("/v1/genesis/imports/plaintext/estimate",
                        json=_estimate_payload(history, f"probe-2nd-{time.time()}"))
            if r1.status_code not in (200, 201):
                return BLOCKED_EVIDENCE, f"second estimate HTTP {r1.status_code}"
            r2 = c.post("/v1/genesis/imports/plaintext/commit",
                        json={"staged_id": r1.json().get("staged_id")})
            body = r2.json() if r2.status_code < 500 else {}
            ok = (r2.status_code == 409 and body.get("error") == "import_job_active"
                  and body.get("active_job_id") == job_id)
            return (PASS if ok else PRODUCT_FAIL), f"HTTP {r2.status_code} {str(body)[:110]}"
        p.guard("concurrent_commit_409", _concurrent_commit)

        # -- 轮询到终态,收集所有帧 ------------------------------------------
        frames: list[dict] = []
        try:
            final = _poll(c, job_id, timeout=1800 if large else 600, frames=frames)
        except RuntimeError as e:
            p.blocked("poll", str(e))
            return p.result()

        p.ok("job_done", final.get("status") == "done",
             f"status={final.get('status')} error_class={final.get('error_class')} "
             f"copy={str(final.get('friendly_copy'))[:80]}")

        # -- 身份先行:identity_ready 必须早于 done -------------------------
        # 修复前(combined_map 提前 return)二者同秒,节点页「先进家、TA 在后台
        # 继续想起来」是句假话。大素材下应差几十秒到几分钟。
        def _identity_first():
            ready_at = next((i for i, f in enumerate(frames) if f.get("identity_ready")), None)
            done_at = next((i for i, f in enumerate(frames)
                            if f.get("status") in ("done", "failed")), None)
            if ready_at is None:
                return PRODUCT_FAIL, "identity_ready never became true"
            if done_at is None:
                return BLOCKED_EVIDENCE, "job never reached terminal state"
            gap = done_at - ready_at
            return ((PASS if gap >= 1 else PRODUCT_FAIL),
                    f"identity_ready frame={ready_at} done frame={done_at} gap={gap} frames")
        p.guard("identity_ready_before_done", _identity_first)

        # -- 分母诚实:done 时每份材料 windows_done == windows_total ---------
        def _windows_complete():
            bad = [f"{m.get('kind')}={m.get('windows_done')}/{m.get('windows_total')}"
                   for m in final.get("materials") or []
                   if int(m.get("windows_done") or 0) != int(m.get("windows_total") or 0)]
            return ((PASS if not bad else PRODUCT_FAIL),
                    "all complete" if not bad else f"incomplete: {bad} ← 采样后未补全,进度条到不了头")
        p.guard("all_windows_processed", _windows_complete)

        # -- 帧稳定性:materials 长度单调非减(修复前 3→0→3 会让横幅闪) -----
        def _materials_monotonic():
            lens = [len(f.get("materials") or []) for f in frames]
            drops = [(i, lens[i - 1], lens[i]) for i in range(1, len(lens)) if lens[i] < lens[i - 1]]
            first = lens[0] if lens else 0
            if drops:
                return PRODUCT_FAIL, f"materials 回退 {drops[:3]} (序列={lens[:12]})"
            if first == 0:
                return PRODUCT_FAIL, f"首帧 materials 为空 → 客户端会谎报份数 (序列={lens[:12]})"
            return PASS, f"首帧={first},单调非减 (序列={lens[:12]})"
        p.guard("materials_frames_stable", _materials_monotonic)

        # -- 材料状态枚举必须在客户端认识的集合内 ---------------------------
        def _status_enum():
            seen = {str(m.get("status")) for f in frames for m in (f.get("materials") or [])}
            bad = seen - MATERIAL_STATUSES
            return ((PASS if not bad else PRODUCT_FAIL),
                    f"seen={sorted(seen)}" + (f" 越界={sorted(bad)} ← 客户端会归 queued" if bad else ""))
        p.guard("material_status_enum", _status_enum)

        # -- 真的落卡了 -----------------------------------------------------
        cards = sum(int(m.get("cards") or 0) for m in final.get("materials") or [])
        p.ok("cards_written", cards > 0, f"cards={cards}")

    return p.result()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的格子名")
    ap.add_argument("--large", action="store_true",
                    help="用多窗大素材(复刻大导入事故场景,慢约 3 分钟/格)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    pool = load_keys()
    cells = [cell for cell in HOSTED_CELLS if cell.provider != "openai"]  # openai 走同族逻辑,默认略过
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        cells = [cell for cell in HOSTED_CELLS if cell.name in only]
        unknown = only - {cell.name for cell in HOSTED_CELLS}
        if unknown:
            ap.error(f"unknown cell(s): {', '.join(sorted(unknown))}")

    if args.list:
        for cell in HOSTED_CELLS:
            print(f"  {cell.name:26} key={'ok' if cell.key(pool) else 'MISSING'}")
        return 0

    icon = {PASS: "✅", PRODUCT_FAIL: "❌", BLOCKED_EVIDENCE: "⏭️"}
    results = []
    for cell in cells:
        print(f"\n── {cell.name} " + "─" * 40, flush=True)
        res = run_processing_cell(cell, pool, large=args.large)
        results.append(res)
        for case in res["cases"]:
            print(f"  {icon.get(case['result'], case['result'])} {case['name']}  {case['detail']}",
                  flush=True)

    print("\n==== SUMMARY ====")
    hard_fail = False
    for res in results:
        statuses = [c["result"] for c in res["cases"]]
        overall = worst(statuses)
        hard_fail = hard_fail or overall == PRODUCT_FAIL
        print(f"  {icon.get(overall, overall)} {res['area']}  "
              f"({sum(1 for s in statuses if s == PASS)}/{len(statuses)} pass)")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
