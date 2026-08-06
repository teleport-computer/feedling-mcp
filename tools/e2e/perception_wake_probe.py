#!/usr/bin/env python3
"""感知上报链 + 三类场景唤醒 (Seven 清单 §4;志豪 PR#158 根治验证).

用户原始投诉:「没移动却收到『到家』」。PR#158 把进程内内存态 differ 换成
落库 baseline + 原子判定。本探针验四条不变量 + 三类唤醒的开关尊重:

  1. 首次观察只建基线,**绝不唤醒**(除 allow_first_event,如解锁)
  2. 同 source_event_id 重复上报 → duplicate,不唤醒
  3. 时间倒流(旧 ts)→ stale,不唤醒
  4. 值未变 → unchanged,不唤醒;真变化 → 唤醒恰一次
  5. 跨"重启"(换 differ 实例)判定一致 —— 这正是多进程误报的根因
  6. 三类唤醒(到达/解锁/照片)各自尊重自己的开关,互不连带
"""
import os
import pathlib
import sys
import time
import uuid

REPO = str(pathlib.Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(REPO, "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://xiaotingtan@127.0.0.1:5432/feedling_parity_e2e?sslmode=disable")
os.environ.setdefault("TEE_DATABASE_URL", "postgresql://xiaotingtan@127.0.0.1:5432/feedling_parity_e2e_tee?sslmode=disable")
os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "parity-secret")
os.environ["NO_PROXY"] = "*"

import db  # noqa: E402
from perception import ingress_v2  # noqa: E402
from perception.differ_v2 import PerceptionDifferV2  # noqa: E402
from proactive import controls_v2 as cv2  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        FAIL.append(name)


uid = f"usr_perc{uuid.uuid4().hex[:10]}"
with db.get_pool().connection() as conn:
    conn.execute("INSERT INTO users (user_id, created_at, doc) VALUES (%s, now(), '{}'::jsonb) "
                 "ON CONFLICT DO NOTHING", (uid,))
    conn.commit()

wakes = []


def submit(ev):
    wakes.append(ev)


def observe(signal, value, *, ts=None, event_id=None, differ=None, allow_first=False):
    wakes.clear()
    return ingress_v2.observe_signal_v2(
        uid, signal, value, ts=ts, source_event_id=event_id,
        allow_first_event=allow_first, differ=differ, submit_wake=submit,
    )


try:
    now = time.time()
    print("=== 到达唤醒链路(arrived_at_anchor via wifi_anchor)===")
    r = observe("wifi_anchor", "home-wifi", ts=now, event_id="ev1")
    check("首次观察=建基线,不唤醒", len(wakes) == 0, f"outcome={getattr(r,'outcome',r)} wakes={len(wakes)}")

    r = observe("wifi_anchor", "home-wifi", ts=now + 1, event_id="ev1")
    check("同 event_id 重复上报 → 不唤醒", len(wakes) == 0, f"wakes={len(wakes)}")

    r = observe("wifi_anchor", "home-wifi", ts=now + 2, event_id="ev2")
    check("值未变 → 不唤醒", len(wakes) == 0, f"wakes={len(wakes)}")

    r = observe("wifi_anchor", "office-wifi", ts=now + 10, event_id="ev3")
    n_changed = len(wakes)
    check("真变化 → 唤醒恰一次", n_changed == 1,
          f"wakes={n_changed} kinds={[getattr(w,'trigger',getattr(w,'kind','?')) for w in wakes]}")

    r = observe("wifi_anchor", "home-wifi", ts=now + 5, event_id="ev4")
    check("时间倒流(旧 ts)→ 不唤醒", len(wakes) == 0, f"wakes={len(wakes)}")

    print("\n=== 跨进程/重启一致性(误报根因)===")
    fresh = PerceptionDifferV2()  # 模拟另一个 worker / 重启后的新实例
    r = observe("wifi_anchor", "office-wifi", ts=now + 20, event_id="ev5", differ=fresh)
    check("新 differ 实例看同一个值 → 不误报唤醒", len(wakes) == 0,
          f"wakes={len(wakes)} (修复前:内存态 prev=None 会当成变化)")

    print("\n=== 解锁唤醒(allow_first_event 语义)===")
    r = observe("unlock_after_absence", {"absent_sec": 7200}, ts=now + 30,
                event_id="unlock1", allow_first=True)
    check("解锁首次观察 allow_first=True → 允许唤醒", len(wakes) >= 1, f"wakes={len(wakes)}")

    print("\n=== 照片唤醒 ===")
    r = observe("photo_added", {"count": 1, "id": "p1"}, ts=now + 40, event_id="ph1")
    first_photo = len(wakes)
    r = observe("photo_added", {"count": 2, "id": "p2"}, ts=now + 50, event_id="ph2")
    check("照片新增变化 → 唤醒", len(wakes) >= 1, f"first={first_photo} second={len(wakes)}")

    print("\n=== 三类唤醒各自尊重自己的开关(无连带)===")
    for trig, own in (("arrived_at_anchor", cv2.SWITCH_ARRIVAL_WAKE_ENABLED),
                      ("unlock_after_absence", cv2.SWITCH_UNLOCK_WAKE_ENABLED),
                      ("photo_added", cv2.SWITCH_PHOTO_WAKE_ENABLED)):
        off = {own: False}
        d = cv2.evaluate_wake_control_v2(source="perception_event", trigger=trig, settings=off)
        check(f"{trig}: 自己的开关关 → 拦", not d.accepted, d.reason)
        others = {s: False for s in (cv2.SWITCH_ARRIVAL_WAKE_ENABLED,
                                     cv2.SWITCH_UNLOCK_WAKE_ENABLED,
                                     cv2.SWITCH_PHOTO_WAKE_ENABLED) if s != own}
        d2 = cv2.evaluate_wake_control_v2(source="perception_event", trigger=trig, settings=others)
        check(f"{trig}: 别人的开关关 → 仍通", d2.accepted, d2.reason)
finally:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM perception_signal_state_v2 WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        conn.commit()

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
