"""落卡窗口游标要出现在 admin 诊断里 —— 但**不许**带对话原文。

## 为什么需要它（2026-09-12 事故的后半段）

prod 上 58 个用户连续两天零落卡。诊断里只看得到「每天失败几次」，
看不出「是不是同一批消息在反复失败」—— 而这两件事处置完全相反：

    偶发失败      等重试就好
    队头阻塞      一条毒消息把这个用户永久锁死，必须跳过

当时这条因果链只能靠读代码推断，没法用数据证实。加上游标之后，
连续几个失败任务的 ``after_*`` 一比就知道。

## 硬约束

游标字段**只能是 id / seq / 条数**。这个接口是诊断面，带上正文就等于
把用户的对话内容摊在运维视图里。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR",
                      tempfile.mkdtemp(prefix="feedling-cursor-diag-"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin.data_track import _capture_window_cursor  # noqa: E402

WINDOW = {
    "after_message_id": "msg_a", "until_message_id": "msg_c",
    "after_seq": 100, "through_seq": 120, "message_count": 8,
    # 下面这些**绝不能**出现在输出里
    "text": "用户说了一些很私密的事",
    "messages": [{"role": "user", "content": "我对青霉素过敏"}],
    "window_text": "- 用户: 今天被领导批了",
}


def test_the_cursor_is_exposed():
    out = _capture_window_cursor({"capture_window": WINDOW})
    assert out["after_message_id"] == "msg_a"
    assert out["until_message_id"] == "msg_c"
    assert out["after_seq"] == 100
    assert out["through_seq"] == 120
    assert out["message_count"] == 8


def test_no_conversation_content_ever_leaks():
    """🔴 这条是硬约束：输出里不许有任何对话原文。"""
    out = _capture_window_cursor({"capture_window": WINDOW})
    blob = repr(out)
    for secret in ("青霉素", "领导", "私密", "用户说"):
        assert secret not in blob, f"诊断里漏出了对话内容: {secret}"
    assert set(out) <= {"after_message_id", "until_message_id",
                        "after_seq", "through_seq", "message_count"}


def test_head_of_line_blocking_is_visible_from_the_cursors():
    """连续失败任务的 after_* 相同 → 一眼看出游标没推进。"""
    stuck = [_capture_window_cursor({"capture_window": {
        **WINDOW, "until_message_id": end, "through_seq": seq}})
        for end, seq in (("msg_c", 120), ("msg_d", 130), ("msg_e", 140))]
    # 起点一直没动 —— 这就是队头阻塞的指纹
    assert len({w["after_message_id"] for w in stuck}) == 1
    # 终点在往前走（新消息在进来），所以不能只看终点判断
    assert len({w["until_message_id"] for w in stuck}) == 3


def test_the_legacy_window_key_is_accepted():
    """老任务把窗口放在 ``window`` 而不是 ``capture_window``。"""
    assert _capture_window_cursor({"window": WINDOW})["after_message_id"] == "msg_a"


def test_missing_or_malformed_windows_degrade_quietly():
    """拿不到窗口就返回空 —— 诊断接口不该因为一条脏数据 500。"""
    for job in (None, {}, {"capture_window": None}, {"capture_window": "坏数据"},
                {"capture_window": []}):
        assert _capture_window_cursor(job) == {}
    # 数值字段是脏的就跳过那一个，不整条报废
    out = _capture_window_cursor({"capture_window": {
        "after_message_id": "msg_a", "through_seq": "不是数字"}})
    assert out == {"after_message_id": "msg_a"}
