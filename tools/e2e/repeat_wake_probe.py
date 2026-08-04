"""重复定时提醒的端到端验收（2026-08-03 `d745acc9`）。

单测已经覆盖了续排的算术和取消的 SQL。这个脚本验的是**单测证明不了的那一半**：
在真部署上，一次真实的 fire 之后，下一条真的出现在用户看得到的地方，取消之后
真的不再有下一条。用户对这个功能的验收标准就是"明天真的响、说停真的停"，
而那句话跨越的是调度器 + 存储 + 上下文注入三层，只有真环境能证明。

四格：
  1. 排一条 repeat=daily → fire 一次 → **自动出现下一条 pending，时间 = +24h**
  2. 取消（用**旧的、已 fired 的那个 id**）→ **整串停掉**，不再有 pending
  3. 同一时刻同一 repeat 再排一次 → 去重，不产生第二条
  4. 一次性提醒（无 repeat）fire 之后 → **没有**续排（回归面）

用完即删账号（test-account-hygiene）。只在 test 环境跑，`_refuse_prod` 兜底。

Run:  NO_PROXY='*' python3 -m tools.e2e.repeat_wake_probe
退出码 0 = 通过，1 = 失败。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from .client import E2EClient

_DAY = 86400.0


def _retry(fn, *, action: str):
    """test 网关 TLS/读超时是常态（见 RELEASE_TESTING_PROTOCOL §1.3）。

    这些调用都是幂等的（排定有去重、fire 无副作用重复、debug 是只读），
    所以重试安全；不重试的话探针会被网络抖动判成产品失败。
    """
    last: Exception | None = None
    for attempt in range(3):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — transport-level only
            last = exc
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    raise SystemExit(f"FAIL [{action}] transport: {type(last).__name__}: {last}")


def _post(c, path: str, payload: dict, *, action: str) -> dict:
    r = _retry(lambda: c.post(path, json=payload), action=action)
    if r.status_code != 200:
        raise SystemExit(f"FAIL [{action}] HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _timers(c) -> list[dict]:
    """当前已排定的记录。

    没有 `/scheduled/list` 端点（我先假设有，核实后没有），读的是
    `/v1/proactive/debug` 的 `v2_scheduled_wakes` —— 与注入模型上下文的
    `runtime_data.scheduled_wakes` 同一个 stream（`SCHEDULED_WAKE_STREAM_V2`），
    所以断言它等于断言"模型看得见"。
    """
    r = _retry(lambda: c.get("/v1/proactive/debug"), action="read scheduled")
    if r.status_code != 200:
        raise SystemExit(f"FAIL [read scheduled] HTTP {r.status_code}: {r.text[:200]}")
    rows = (r.json() or {}).get("v2_scheduled_wakes") or []
    return [x for x in rows if isinstance(x, dict)]


def _pending(c) -> list[dict]:
    return [t for t in _timers(c) if str(t.get("status") or "") == "pending"]


def _schedule(c, *, at: str, repeat: str = "", note: str = "") -> str:
    """排一条，返回 timer_id。

    调用返回体给的是 `next_trigger_at`（ISO 串），epoch 形态的 `due_at` 只在
    `/v1/proactive/debug` 的记录里。统一从后者读，省掉两种时间表示的换算。
    """
    action = {"type": "schedule_wake", "at": at, "tz": "UTC", "note": note}
    if repeat:
        action["repeat"] = repeat
    body = _post(c, "/v1/proactive/scheduled/actions", {"actions": [action]},
                 action=f"schedule {repeat or 'one-shot'}")
    results = body.get("results") or []
    row = results[0] if results and isinstance(results[0], dict) else {}
    return str(row.get("timer_id") or "")


def _due_of(c, timer_id: str) -> float:
    for t in _timers(c):
        if str(t.get("timer_id")) == timer_id:
            return float(t.get("due_at") or 0)
    return 0.0


def main() -> int:
    c = E2EClient.provision(route="model_api")
    print(f"account: {c.user_id}")
    failures: list[str] = []
    try:
        _post(c, "/v1/proactive/state", {"scheduled": True, "timezone": "UTC"},
              action="enable scheduled")

        # -- 1) daily 续排 ------------------------------------------------
        # fire 只触发已到期的（`due_at <= now`，scheduled_wake_v2.py:453/699），
        # 所以排在过去，让这一轮立刻可触发——否则 fire 是空转，看起来像"续排没发生"。
        soon = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        first_id = _schedule(c, at=soon, repeat="daily", note="repeat probe")
        first_due = _due_of(c, first_id) if first_id else 0.0
        if not first_id or not first_due:
            failures.append(f"排定失败：timer_id={first_id!r} due_at={first_due}")
            raise _Abort()

        _post(c, "/v1/proactive/scheduled/fire", {}, action="fire")

        nxt = [t for t in _pending(c) if str(t.get("timer_id")) != first_id]
        if len(nxt) != 1:
            failures.append(f"① 续排：fire 后应恰好 1 条新 pending，实际 {len(nxt)}")
        else:
            gap = float(nxt[0].get("due_at") or 0) - first_due
            if abs(gap - _DAY) > 1:
                failures.append(f"① 续排间隔应为 +86400，实际 {gap}")
            else:
                print(f"  ✓ ① 续排出现，+{gap:.0f}s，新 id={nxt[0].get('timer_id')}")

        # -- 2) 用旧 id 取消整串（这是这个功能的下限）---------------------
        _post(c, "/v1/proactive/scheduled/actions",
              {"actions": [{"type": "cancel_wake", "wake_id": first_id,
                            "reason": "probe cancel via stale id"}]},
              action="cancel series by stale id")
        if not nxt:
            failures.append("② 无效：前一步没有续排，此处的「无残留」"
                            "只是因为本来就没东西，不构成取消生效的证据")
        left = _pending(c)
        if left:
            failures.append(
                f"② 取消整串失败：用已 fired 的旧 id 取消后仍有 {len(left)} 条 pending"
                "——用户会永远关不掉这个每日提醒")
        else:
            print("  ✓ ② 旧 id 取消整串，无残留 pending")

        # 取消后再 fire 一次，确认不会又冒出来
        _post(c, "/v1/proactive/scheduled/fire", {}, action="fire after cancel")
        if _pending(c):
            failures.append("② 取消后再次 fire 又续排了——取消没有终止序列")
        else:
            print("  ✓ ② 取消后再 fire 也不复活")

        # -- 3) 同刻同 repeat 去重 ---------------------------------------
        at3 = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        a_id = _schedule(c, at=at3, repeat="daily", note="dedup a")
        b_id = _schedule(c, at=at3, repeat="daily", note="dedup b")
        same = a_id == b_id
        a_due = _due_of(c, a_id)
        dup = [t for t in _pending(c)
               if abs(float(t.get("due_at") or 0) - a_due) < 1]
        if not same or len(dup) != 1:
            failures.append(f"③ 去重失败：同刻同 repeat 产生了 {len(dup)} 条")
        else:
            print("  ✓ ③ 同刻同 repeat 去重")

        # -- 4) 一次性不续排（回归面）------------------------------------
        at4 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        one_id = _schedule(c, at=at4, note="one-shot")
        one_due = _due_of(c, one_id)
        _post(c, "/v1/proactive/scheduled/fire", {}, action="fire one-shot")
        revived = [t for t in _pending(c)
                   if abs(float(t.get("due_at") or 0) - one_due - _DAY) < 1]
        if revived:
            failures.append("④ 一次性提醒被续排了——存量行为被破坏")
        else:
            print(f"  ✓ ④ 一次性提醒不续排（id={one_id}）")
    except _Abort:
        pass
    finally:
        c.teardown()

    if failures:
        print("\n".join(["", "FAIL:"] + [f"  - {f}" for f in failures]))
        return 1
    print("\nOK — 重复定时四格全过")
    return 0


class _Abort(Exception):
    """提前结束但仍走 finally 的 teardown——账号必须删掉。"""


if __name__ == "__main__":
    sys.exit(main())
