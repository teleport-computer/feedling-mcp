"""一次聊天就把 debug trace 冲干净了 —— 48 小时的保留期形同虚设。

2026-08-10 查 usr_7001b1df80e2024d 的三个问题(生图、世界书、记忆读回)时,
连着三次打不开现场:环里 200 条事件有 182 条是 purpose=v2_chat_read 的
`enclave.call.start`/`.done`,整个 trace 只覆盖 **1 秒**。

机制:`_decrypt_chat_rows` 逐行解密提示窗(上限 60 行),每行两条成功事件 ——
一个回合 ~120 条,而环深 500。**保留期是 48 小时,可用窗口是秒**。

契约:
  - 成功的批量解密折叠成一条 `enclave.call.batch`(带 calls 计数与总耗时);
  - **失败绝不折叠** —— 批次里的错误/超时正是查 trace 的人要找的东西;
  - 作用域是线程局部的,不能串到并发的另一个用户身上;
  - 非批量场景(单次解密)行为一个字不变。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import debug_trace  # noqa: E402
from core import enclave  # noqa: E402


@pytest.fixture
def recorded(monkeypatch):
    """收集实际落进 debug_trace 的事件。"""
    events: list[dict] = []

    def fake_trace_event(_store, **kw):
        events.append(kw)

    monkeypatch.setattr(debug_trace, "trace_event", fake_trace_event)
    return events


def _store(uid="usr_test"):
    return SimpleNamespace(user_id=uid)


def _ok(purpose="v2_chat_read"):
    """模拟一次成功解密发出的那一对事件。"""
    enclave._trace_enclave(_store(), "enclave.call.start", purpose=purpose)
    enclave._trace_enclave(_store(), "enclave.call.done", purpose=purpose)


# --------------------------------------------------------------------------- #
# 折叠
# --------------------------------------------------------------------------- #

def test_a_sixty_row_window_no_longer_floods_the_ring(recorded):
    """线上那个形状:60 行提示窗。修前 120 条,修后 1 条。"""
    with enclave.coalesced_success_trace("v2_chat_read"):
        for _ in range(60):
            _ok()

    assert len(recorded) == 1, (
        f"一个回合仍然写了 {len(recorded)} 条 trace —— 环会被单个回合冲掉"
    )
    assert recorded[0]["type"] == "enclave.call.batch"
    assert recorded[0]["detail"]["calls"] == 60, "批次没记住真实调用次数"


def test_the_rollup_is_emitted_only_after_the_scope_closes(recorded):
    """作用域内不该有半成品事件泄漏出去。"""
    with enclave.coalesced_success_trace("v2_chat_read"):
        _ok()
        assert recorded == [], "作用域没结束就写了事件"

    assert len(recorded) == 1


def test_an_empty_batch_writes_nothing(recorded):
    """没有解密发生就不该凭空多一条事件。"""
    with enclave.coalesced_success_trace("v2_chat_read"):
        pass

    assert recorded == []


# --------------------------------------------------------------------------- #
# 失败绝不折叠 —— 折叠掉就等于把查问题的理由删了
# --------------------------------------------------------------------------- #

def test_a_failure_inside_the_batch_is_still_traced_individually(recorded):
    """批次里第 30 行挂了,那一条必须单独可见。"""
    with enclave.coalesced_success_trace("v2_chat_read"):
        for _ in range(29):
            _ok()
        enclave._trace_enclave(
            _store(), "enclave.call.error", purpose="v2_chat_read",
            status="error", detail={"status_code": 401},
        )
        for _ in range(30):
            _ok()

    types = [e["type"] for e in recorded]
    assert "enclave.call.error" in types, "批次内的失败被折叠掉了 —— 这正是要查的东西"
    errors = [e for e in recorded if e["type"] == "enclave.call.error"]
    assert errors[0]["detail"]["status_code"] == 401, "失败的细节丢了"


def test_a_timeout_inside_the_batch_survives(recorded):
    with enclave.coalesced_success_trace("v2_chat_read"):
        _ok()
        enclave._trace_enclave(
            _store(), "enclave.call.timeout", purpose="v2_chat_read", status="error",
        )

    assert "enclave.call.timeout" in [e["type"] for e in recorded]


# --------------------------------------------------------------------------- #
# 作用域边界
# --------------------------------------------------------------------------- #

def test_a_different_purpose_is_not_swallowed_by_the_scope(recorded):
    """作用域只管自己那个 purpose;顺路发生的别的解密照常单独记。"""
    with enclave.coalesced_success_trace("v2_chat_read"):
        _ok()
        _ok(purpose="v2_workspace_read")

    types = [e["type"] for e in recorded]
    assert "enclave.call.start" in types and "enclave.call.done" in types, (
        "别的 purpose 被这个作用域吞了"
    )


def test_nested_different_purpose_batches_fold_independently(recorded):
    """Chat rows and attachment captions share one loop but keep their purposes."""
    with enclave.coalesced_success_trace("v2_chat_read"):
        with enclave.coalesced_success_trace("v2_caption_read"):
            for _ in range(3):
                _ok("v2_chat_read")
            for _ in range(2):
                _ok("v2_caption_read")

    assert [event["type"] for event in recorded] == [
        "enclave.call.batch",
        "enclave.call.batch",
    ]
    assert {
        event["detail"]["purpose"]: event["detail"]["calls"]
        for event in recorded
    } == {"v2_chat_read": 3, "v2_caption_read": 2}


def test_nested_same_purpose_joins_the_outer_batch(recorded):
    with enclave.coalesced_success_trace("v2_chat_read"):
        _ok()
        with enclave.coalesced_success_trace("v2_chat_read"):
            _ok()

    assert len(recorded) == 1
    assert recorded[0]["type"] == "enclave.call.batch"
    assert recorded[0]["detail"]["calls"] == 2


def test_screen_proxy_successes_use_the_same_batch_mechanism(recorded):
    from screen import screen_read_core

    with enclave.coalesced_success_trace("screen_frame_decrypt"):
        for frame_id in ("frame-a", "frame-b"):
            path = f"/v1/screen/frames/{frame_id}/decrypt"
            screen_read_core._trace_enclave_proxy(
                _store(),
                "enclave.call.start",
                path=path,
                purpose="screen_frame_decrypt",
            )
            screen_read_core._trace_enclave_proxy(
                _store(),
                "enclave.call.done",
                path=path,
                purpose="screen_frame_decrypt",
            )

    assert len(recorded) == 1
    assert recorded[0]["type"] == "enclave.call.batch"
    assert recorded[0]["detail"]["calls"] == 2
    assert recorded[0]["detail"]["path"] == ""
    assert recorded[0]["explain"].startswith("Screen route proxied")


def test_outside_any_scope_behaviour_is_unchanged(recorded):
    """单次解密的事件形状一个字不变。"""
    _ok()

    assert [e["type"] for e in recorded] == [
        "enclave.call.start", "enclave.call.done",
    ]


def test_the_scope_does_not_leak_into_another_thread(recorded):
    """并发用户不能共用一个批次 —— 那会把 A 的计数记到 B 头上。"""
    seen: list[str] = []

    def other_thread():
        _ok()
        seen.extend(e["type"] for e in recorded)

    with enclave.coalesced_success_trace("v2_chat_read"):
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()

    assert "enclave.call.start" in seen, "另一个线程的事件被本线程的作用域吞了"


def test_the_scope_closes_even_when_the_body_raises(recorded):
    """解密中途抛错也不能把作用域永久留在开启状态。"""
    with pytest.raises(RuntimeError):
        with enclave.coalesced_success_trace("v2_chat_read"):
            _ok()
            raise RuntimeError("boom")

    recorded.clear()
    _ok()
    assert len(recorded) == 2, "作用域泄漏了,后续单次解密仍被折叠"


# --------------------------------------------------------------------------- #
# 环深 —— 48 小时的保留期需要装得下 48 小时
# --------------------------------------------------------------------------- #

def test_the_ring_is_deep_enough_to_outlive_a_single_turn():
    """环深必须远大于单个回合的事件量,否则 TTL 写多久都没意义。"""
    assert debug_trace._MAX_EVENTS >= 2000, (
        f"环深 {debug_trace._MAX_EVENTS} 条,一个活跃用户装不下 48 小时"
    )
    assert debug_trace._MAX_EVENTS_VERBOSE >= 1000
    assert debug_trace._TTL_SEC == 48 * 3600, "保留期不再是 48 小时"


def test_the_admin_panel_can_read_the_whole_ring_out():
    """环里存了 2500 条,面板最多只能选 500 的话等于读不到。"""
    import inspect

    from admin import data_track

    src = inspect.getsource(data_track)

    assert '"1000", "2500"' in src, "面板的 page size 选项没跟上环深,读不出整环"


# --------------------------------------------------------------------------- #
# 前缀折叠 —— 感知上报每个字段一个 purpose,精确匹配折不动
#
# 2026-08-12 实测:前面几批折叠都上线之后,活跃 V2 用户环里**仍有 100%**
# 是 `perception:*` 的逐字段事件(actor=backend,一次上报 ~14 条),窗口被压在
# 几小时。前面那些折叠包的都是 serve_worker 里的循环,而这条在 backend 进程。
# --------------------------------------------------------------------------- #

_REPORT_FIELDS = (
    "location_signal", "weather", "motion_state",
    "calendar_next_event", "playback", "audio_route", "battery",
)


def _report_round(purposes=_REPORT_FIELDS):
    for field in purposes:
        _ok(f"perception:{field}")


def test_a_perception_report_folds_into_one_event(recorded):
    """线上那个形状:七个字段 × 六轮上报。"""
    with enclave.coalesced_success_trace("perception:", prefix=True):
        for _ in range(6):
            _report_round()

    assert len(recorded) == 1, (
        f"仍然写了 {len(recorded)} 条(不折叠是 84 条)—— 环还是会被上报冲掉"
    )
    assert recorded[0]["detail"]["calls"] == 42


def test_the_batch_still_says_which_signals_were_in_it(recorded):
    """折叠掉「一条条铺开」,不能折掉「这批里有哪些信号」。

    分不清哪个字段来了、哪个没来,正是排查感知问题时唯一要看的东西。
    """
    with enclave.coalesced_success_trace("perception:", prefix=True):
        _report_round(("weather", "battery", "battery"))

    by_purpose = recorded[0]["detail"]["by_purpose"]

    assert by_purpose == {"perception:weather": 1, "perception:battery": 2}


def test_a_failure_inside_a_prefix_batch_is_still_traced_individually(recorded):
    with enclave.coalesced_success_trace("perception:", prefix=True):
        _ok("perception:weather")
        enclave._trace_enclave(
            _store(), "enclave.call.error", purpose="perception:weather",
            status="error", detail={"status_code": 401},
        )
        _ok("perception:battery")

    assert "enclave.call.error" in [e["type"] for e in recorded]


def test_an_exact_scope_wins_over_an_enclosing_prefix_scope(recorded):
    """更具体者优先:精确作用域自己成批,剩下的才归前缀。

    顺序反过来的话,一个宽前缀会把本该独立成批的 purpose 全吸走,
    那些批次的计数就再也分不出来了。
    """
    with enclave.coalesced_success_trace("perception:", prefix=True):
        with enclave.coalesced_success_trace("perception:weather"):
            for _ in range(3):
                _ok("perception:weather")
        for _ in range(2):
            _ok("perception:battery")

    batches = {e["detail"]["purpose"]: e["detail"] for e in recorded}

    assert batches["perception:weather"]["calls"] == 3
    assert batches["perception:"]["calls"] == 2
    assert batches["perception:"]["by_purpose"] == {"perception:battery": 2}


def test_a_plain_scope_does_not_absorb_other_purposes(recorded):
    """不开 prefix 的老调用方行为一个字不变 —— 别的 purpose 照常单条落地。"""
    with enclave.coalesced_success_trace("v2_chat_read"):
        for _ in range(5):
            _ok("v2_chat_read")
        _ok("v2_caption_read")

    types = [e["type"] for e in recorded]

    assert "enclave.call.start" in types and "enclave.call.done" in types
    assert len([e for e in recorded if e["type"] == "enclave.call.batch"]) == 1


def test_the_perception_ingestion_actually_opens_a_prefix_scope():
    """光有能力不算数 —— 入库那条路必须真的用上它。

    这条是「机制存在」与「机制被接上」之间的差别;今天已经在别处栽过一次
    (工具面 enum 做好了,但没登记进 CI 就等于没有)。
    """
    import inspect

    from perception import service

    src = inspect.getsource(service.ingest_snapshot_v2)

    assert 'coalesced_success_trace("perception:", prefix=True)' in src, (
        "感知入库没有开前缀折叠作用域,环还是会被逐字段事件填满"
    )
