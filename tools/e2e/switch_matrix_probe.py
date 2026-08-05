#!/usr/bin/env python3
"""开关独立性矩阵 (docs/testing §2 F 行, Seven 2026-07-26 定).

六个主动开关必须互不连带:关掉任一开关,**只有它那条路被拦,其余全通**。
本轮 DND 闸(d8b33a26)动了 due_heartbeat_users / due_screen_watch_users 两个
调度查询,以及 reminders_delivery 与 dnd 的兼容映射 —— 正是最容易引入连带的地方。

两层断言:
  L1 决策层:controls_v2.evaluate_wake_control_v2 对六类唤醒源 × 六个开关的矩阵
  L2 调度层:真实 PG 上 due_heartbeat_users / due_screen_watch_users 对
            dnd / ambient 的响应(dnd=true 排除心跳与屏幕、其余开关不影响它们)
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

from proactive import controls_v2 as cv2  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


# --- L1: decision matrix -----------------------------------------------------
# (label, kwargs to evaluate_wake_control_v2, switch that SHOULD gate it)
SOURCES = [
    ("heartbeat", {"source": "heartbeat"}, cv2.SWITCH_AMBIENT),
    ("photo_added", {"source": "perception_event", "trigger": "photo_added"},
     cv2.SWITCH_PHOTO_WAKE_ENABLED),
    ("arrived_at_anchor", {"source": "perception_event", "trigger": "arrived_at_anchor"},
     cv2.SWITCH_ARRIVAL_WAKE_ENABLED),
    ("unlock_after_absence", {"source": "perception_event", "trigger": "unlock_after_absence"},
     cv2.SWITCH_UNLOCK_WAKE_ENABLED),
    ("screen_watch", {"source": "scene_change"}, cv2.SWITCH_SCREEN_WATCH_ENABLED),
    ("scheduled", {"source": cv2.SCHEDULED_WAKE_SOURCE_V2}, cv2.SWITCH_SCHEDULED),
]
SWITCHES = [s for _, _, s in SOURCES]

print("=== L1 decision matrix (6 sources x 6 switches) ===", flush=True)
for off_switch in SWITCHES:
    settings = {s: True for s in SWITCHES}
    settings[off_switch] = False
    for label, kwargs, owner in SOURCES:
        d = cv2.evaluate_wake_control_v2(settings=settings, **kwargs)
        expected_allowed = (owner != off_switch)
        ok = bool(d.accepted) == expected_allowed
        check(f"off={off_switch:24s} src={label:22s} accepted={d.accepted}",
              ok, "" if ok else f"expected accepted={expected_allowed} reason={d.reason}")

# all-on sanity
settings = {s: True for s in SWITCHES}
for label, kwargs, _ in SOURCES:
    d = cv2.evaluate_wake_control_v2(settings=settings, **kwargs)
    check(f"all-on src={label} allowed", bool(d.accepted), d.reason)

# --- L2: scheduler layer -----------------------------------------------------
print("\n=== L2 scheduler queries vs dnd/ambient ===", flush=True)
import db  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

uid = f"usr_swmatrix{uuid.uuid4().hex[:8]}"
now = time.time()
with db.get_pool().connection() as conn:
    conn.execute("INSERT INTO users (user_id, created_at, doc) VALUES (%s, now(), '{}'::jsonb) "
                 "ON CONFLICT DO NOTHING", (uid,))
    conn.execute(
        "INSERT INTO v2_wake_schedule (user_id, next_heartbeat_at, next_screen_watch_at) "
        "VALUES (%s, now() - interval '1 minute', now() - interval '1 minute') "
        "ON CONFLICT (user_id) DO UPDATE SET next_heartbeat_at=EXCLUDED.next_heartbeat_at, "
        "next_screen_watch_at=EXCLUDED.next_screen_watch_at", (uid,))
    conn.commit()

def due():
    return (uid in jobs_store.due_heartbeat_users(limit=500),
            uid in jobs_store.due_screen_watch_users(limit=500))

try:
    db.set_blob(uid, "proactive_settings", {})
    hb, sw = due()
    check("baseline: heartbeat due", hb)
    check("baseline: screen_watch due", sw)

    db.set_blob(uid, "proactive_settings", {"dnd": True})
    hb, sw = due()
    check("dnd=true excludes heartbeat", not hb)
    check("dnd=true excludes screen_watch", not sw)

    db.set_blob(uid, "proactive_settings", {"dnd": False})
    hb, sw = due()
    check("dnd=false restores heartbeat", hb)
    check("dnd=false restores screen_watch", sw)

    # non-DND switches must NOT affect the scheduler queries (no coupling)
    for s in ("photo_wake_enabled", "arrival_wake_enabled", "unlock_wake_enabled",
              "dream_enabled", "capture_enabled", "scheduled"):
        db.set_blob(uid, "proactive_settings", {s: False})
        hb, sw = due()
        check(f"{s}=false does NOT gate heartbeat", hb)
        check(f"{s}=false does NOT gate screen_watch", sw)

    # ambient=false: product semantics — 心跳开关是 ambient;调度层是否也认?
    db.set_blob(uid, "proactive_settings", {"ambient": False})
    hb, sw = due()
    print(f"[INFO] ambient=false -> heartbeat_due={hb} screen_due={sw} "
          f"(decision layer gates it; scheduler may still list — enqueue-side gate applies)",
          flush=True)
finally:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_wake_schedule WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM user_blobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        conn.commit()

print(f"\nRESULT: {'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
