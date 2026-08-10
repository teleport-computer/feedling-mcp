"""V2 screen-watch gate —— **纯**函数，stdlib only。

fresh（共享真的在进行）→ changed（只对新内容动作）。
三条的**顺序**是可观测性契约：reason 告诉运维为什么没醒。

零 I/O：两个输入（最新帧 id/ts、用户上次说话 ts）在服务端都是明文可得，不需要 enclave。
这很重要 —— 这个 gate 每 120s 对每个 db_action_v2 用户跑一次。
"""
from __future__ import annotations

# 一帧比这更新 = 共享此刻确实是活的（iOS 约 30s 一帧）。
FRESH_SEC = 90
# 轮询间隔（scheduler 用它推进 next_screen_watch_at）。
INTERVAL_SEC = 120


def should_watch(
    *,
    latest_frame_id: str,
    latest_ts: float,
    last_frame_id: str,
    last_user_msg_ts: float | None,
    now: float,
) -> tuple[bool, str]:
    """(should, reason)。reason 恒非空。"""
    if not latest_frame_id:
        return False, "no_frames"
    if (now - float(latest_ts or 0.0)) > FRESH_SEC:
        return False, "not_fresh"
    if latest_frame_id == (last_frame_id or ""):
        return False, "unchanged"
    return True, "ok"
