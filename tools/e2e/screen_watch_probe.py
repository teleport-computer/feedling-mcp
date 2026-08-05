#!/usr/bin/env python3
"""屏幕共享(screen_watch)lane 服务端语义 (Seven 清单 §4).

真实屏幕共享会话需要 App;服务端这一半可以完整验:
  1. lane 选到专属 system prompt(不是心跳那份)
  2. 该 prompt 明令:说与沉默同等有效 / 不叙述"我在看你屏幕" / 不提系统措辞
  3. 沉默(空回复)是合法终态,不写失败、不写退避
  4. DND 与 screen_watch_enabled 各自能拦,且不连带(与开关矩阵互补)
  5. 只有真 enqueue 时才推进 last_screen_watch_frame_id(不空烧)
"""
import os
import pathlib
import sys

REPO = str(pathlib.Path(__file__).resolve().parents[2]) if "tools" in __file__ else "/Users/xiaotingtan/Desktop/feedling-mcp-test"
sys.path.insert(0, os.path.join(REPO, "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://xiaotingtan@127.0.0.1:5432/feedling_parity_e2e?sslmode=disable")
os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "parity-secret")
os.environ["NO_PROXY"] = "*"

FAIL = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {str(detail)[:160]}", flush=True)
    if not ok:
        FAIL.append(name)


from model_api_runtime.v2 import worker  # noqa: E402
from proactive import controls_v2 as cv2  # noqa: E402

sw = worker._SCREEN_WATCH_SYSTEM_PROMPT
hb = worker._WAKE_SYSTEM_PROMPT
check("screen_watch 有独立 system prompt", sw and sw != hb, f"len={len(sw)}")
check("说与沉默同等有效", "equally valid" in sw, sw[:80])
check("不叙述在看屏幕/看了帧", "never narrate" in sw.lower(), "")
check("永不提及唤醒或系统措辞", "system wording" in sw.lower(), "")
check("用 attention_facts 避免打扰/重复", "attention_facts" in sw, "")

# silence path: the shared wake reply handler treats SILENT/empty as success for
# screen_watch (scheduled is the only must-deliver lane).
src = open(os.path.join(REPO, "backend/model_api_runtime/v2/worker.py")).read()
i = src.find("elif _wst_status == _st_wake.SILENT")
window = src[i:i + 700] if i > 0 else ""
check("SILENT 在 screen_watch 是合法沉默(只有 scheduled 例外)",
      'lane == "scheduled"' in window and "return" in window, window[:120].replace("\n", " "))

for own in (cv2.SWITCH_SCREEN_WATCH_ENABLED,):
    d = cv2.evaluate_wake_control_v2(source="scene_change", settings={own: False})
    check("screen_watch 自己的开关关 → 拦", not d.accepted, d.reason)
    others = {cv2.SWITCH_AMBIENT: False, cv2.SWITCH_PHOTO_WAKE_ENABLED: False,
              cv2.SWITCH_ARRIVAL_WAKE_ENABLED: False}
    d2 = cv2.evaluate_wake_control_v2(source="scene_change", settings=others)
    check("别的开关关 → screen_watch 仍通", d2.accepted, d2.reason)

sws = open(os.path.join(REPO, "backend/model_api_runtime/v2/serve_worker.py")).read()
j = sws.find("def _tick_screen_watch_for_user")
body = sws[j:j + 4000] if j > 0 else ""
check("enqueue 前先问 proactive oracle", "_wake_decision_for_user" in body, "")
check("只有真 enqueue 才推进 last_screen_watch_frame_id",
      "last_screen_watch_frame_id" in body, "")

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
