"""管理端 debug 页面:单次事件完整视图(T219)。

这批断言全部**两个方向一起钉**。只断言「该看见的看见了」是不够的:
把脱敏整个删掉,那一半照样绿。所以每条安全码的可见性断言旁边,
都配一条「用户原话仍然看不见」。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track  # noqa: E402


def _text(html_str: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_str)


# --------------------------------------------------------------------------- #
# 1. 脱敏:按产生方闭集放行,不按键名/形状放行
# --------------------------------------------------------------------------- #

def _public(etype: str, detail: dict) -> dict:
    return data_track._debug_event_public_json({"type": etype, "detail": detail})["detail"]


def test_declared_fields_are_readable_on_their_own_event():
    """2026-08-21 那批失败原因,在**声明过它们的事件上**必须是原文。

    改之前它们全部落在键白名单之外,查案的人看到的是
    `<redacted string len=25>` —— 真值是 `wake_failed:providererror`。
    **那不是空,是一条写着「这里有敏感信息被保护了」的假线索。**
    """
    out = _public("agent.job.terminal", {
        "lane": "heartbeat", "outcome": "failed",
        "error_code": "quota_insufficient",
    })
    assert out["lane"] == "heartbeat"
    assert out["outcome"] == "failed"
    assert out["error_code"] == "quota_insufficient"
    # 视觉那组码在 `notices.catalog.ERROR_CLASSES` 里,**可见**。
    # (此处原本写着「同样没有导出、属已知缺口」—— 那是我抄表抄漏造出来的假缺口,
    #  同一文件下方 343 行已给出相反结论。搭档指出「别让同一文件同时给两种事实」,已删。)
    assert _public("vision.provider.completed", {
        "error_class": "vision_model_unavailable", "status_code": 408,
    })["status_code"] == 408


def test_the_same_key_stays_redacted_on_an_undeclared_event():
    """⭐ 放行是**按事件**的,不是按键名全局的。

    我第一版做成了全局形状检查,被既有用例
    `test_provider_roundtrip_trace_closed_enums_are_admin_readable` 挡下 ——
    它同时要求 `lane` 在某个事件上看得见、在 `mcp.surface.*` 上**仍被遮住**。
    **一个全局规则会把这个刻意的 fail-closed 默认在整页掀翻。**
    这条用例把那个边界钉在我自己这边,免得下次又想「顺手放开一点」。
    """
    out = _public("mcp.surface.registered", {"lane": "chat"})
    assert out["lane"].startswith("<redacted")


def test_a_declared_key_still_rejects_values_outside_its_set():
    """键被声明了不等于值可以随便填。"""
    assert _public("agent.job.terminal", {"lane": "not_a_real_lane"})["lane"].startswith("<redacted")


def test_anything_a_person_said_is_still_redacted():
    """公开键仍只接受产生方闭集；同一个键塞进人话照样遮住。

    这条是可读性那条的另一半。**没有它,把整套放行删掉也会全绿。**
    """
    for value in (
        "我今天心情不太好,你能陪我聊聊吗",
        "the user said hello there",
        "User@Example.com",
        "a" * 120,
    ):
        out = _public("agent.job.terminal", {"error_code": value})
        assert out["error_code"].startswith("<redacted"), f"未被遮住: {value!r}"


def test_lane_values_come_from_the_producer_not_a_local_copy():
    """取值集合从 jobs_store 读,不在管理端抄一份。

    抄一份的失败方式是**静默失配**:别人改了 LANES,页面上那个字段忽然消失,
    而没有任何人会因此收到告警。
    """
    from model_api_runtime.v2 import jobs_store
    for lane in sorted(jobs_store.LANES):
        assert _public("agent.job.terminal", {"lane": lane})["lane"] == lane


# --------------------------------------------------------------------------- #
# 2. 回合抬头
# --------------------------------------------------------------------------- #

def _turn(**over):
    base = {
        "lane": "heartbeat", "job_id": "6791",
        "outcome_class": "safety_suppression", "terminal_status": "error",
        "queue_wait_ms": 2100.0, "exec_ms": 1100.0,
    }
    base.update(over)
    return base


def test_turn_header_carries_lane_and_job():
    body = _text(data_track._render_turn_identity(_turn()))
    assert "heartbeat" in body
    assert "6791" in body


def test_queue_wait_and_execution_are_shown_apart():
    """终态事件的 dur_ms **只含执行时长**,却挂在 enqueue->terminal 这对的终态半边。

    合并成一个总耗时,等于把「这是从入队算起吗」这个误读留给下一个人 ——
    与 T197 在失败率上踩的是同一个坑:**一个值脱离它的前提被展示。**
    """
    body = _text(data_track._render_turn_identity(_turn()))
    assert "排队" in body and "执行" in body
    assert "2100" in body and "1100" in body


def test_a_healthy_turn_never_shows_a_failure_class():
    """`outcome_class` 没有「成功」这一档,ok 事件带的是**默认值**。

    2026-08-21 实弹:132 行里 111 行是 ok 却带着 `operational_failure`。
    无条件渲染 = 页面上凭空多出一批看起来在报错的行,
    而**「看起来像失败」会让人去追一个不存在的故障**。
    """
    body = _text(data_track._render_turn_identity(
        _turn(terminal_status="ok", outcome_class="operational_failure")
    ))
    assert "运行故障" not in body
    assert "operational_failure" not in body


def test_a_failed_turn_does_show_its_class():
    body = _text(data_track._render_turn_identity(_turn()))
    assert "安全抑制" in body


# --------------------------------------------------------------------------- #
# 3. 事件标签 —— 四个语音出口必须彼此可分
# --------------------------------------------------------------------------- #

def test_voice_exits_are_distinguishable_and_say_they_returned_200():
    """这四个出口在页面上本来长得一模一样。

    尤其「仍返回 200」必须写进标签:那是这条道最反直觉的地方 ——
    返回 4xx 会让 ElevenLabs 拆掉整通电话,所以失败是「说出来」的,
    HTTP 层看是成功。**不写出来,读的人会把失败读成正常。**
    """
    labels = {
        t: data_track._debug_friendly_step({"type": t})[1]
        for t in (
            "voice.gateway.turn.runtime_rejected",
            "voice.gateway.turn.timed_out",
            "voice.gateway.turn.not_accepted",
            "voice.gateway.turn.superseded",
        )
    }
    assert len(set(labels.values())) == 4, f"出口彼此不可分: {labels}"
    assert "200" in labels["voice.gateway.turn.runtime_rejected"]
    assert "200" in labels["voice.gateway.turn.timed_out"]
    assert "502" in labels["voice.gateway.turn.not_accepted"]


def test_every_event_shipped_on_20260821_has_a_human_label():
    """没有条目时它们退化成通用的「• 某某」—— 而这批事件被记录下来的
    全部理由,就是让人一眼分得出发生了什么。"""
    for t in (
        "agent.job.enqueued", "agent.job.terminal",
        "vision.provider.called", "vision.provider.completed",
        "agent.image.generate.start", "agent.image.generate.done",
        "agent.image.generate.failed",
        "voice.gateway.turn.started", "voice.gateway.runtime.selected",
        "voice.gateway.reply.received",
    ):
        assert t in data_track._DEBUG_STEP_LABELS, f"{t} 在页面上没有标签"


# --------------------------------------------------------------------------- #
# 4. 聚合:身份从事件读出,而不是在这里重新推导
# --------------------------------------------------------------------------- #

def test_turn_aggregation_reads_identity_off_the_events():
    turns = data_track._debug_trace_group_turns([
        {"ts": 100.0, "type": "agent.job.enqueued", "status": "ok",
         "trace_id": "t1", "user_id": "u1", "lane": "heartbeat",
         "outcome_class": "operational_failure"},
        {"ts": 103.2, "type": "agent.job.terminal", "status": "error",
         "trace_id": "t1", "user_id": "u1", "lane": "heartbeat",
         "job_id": "6791", "dur_ms": 1100.0,
         "outcome_class": "safety_suppression"},
    ])
    turn = turns[0]
    assert turn["lane"] == "heartbeat"
    assert turn["job_id"] == "6791"
    # 取自 error 那条,**不是** ok 那条上的默认值
    assert turn["outcome_class"] == "safety_suppression"
    # 3.2s 跨度 - 1.1s 执行 = 2.1s 排队
    assert turn["queue_wait_ms"] == 2100.0
    assert turn["exec_ms"] == 1100.0


def test_turn_with_only_ok_events_has_no_outcome_class():
    turns = data_track._debug_trace_group_turns([
        {"ts": 1.0, "type": "agent.job.enqueued", "status": "ok",
         "trace_id": "t2", "user_id": "u1",
         "outcome_class": "operational_failure"},
    ])
    assert turns[0]["outcome_class"] == ""


# --------------------------------------------------------------------------- #
# 5. ⭐ 整页渲染 —— 不许只测函数
# --------------------------------------------------------------------------- #
#
# 第一版我只测了 `_debug_event_public_json`,10 条全绿,而**页面上一个字都没变**:
# 页面默认详情走的是另一条路(`_debug_redact_value`),根本不经过我加的投影。
# 是搭档实跑整页才发现的。
# ⇒ **单测绿 = 代码对,证不了页面看得见。** 这一组断言必须打在渲染产物上。

def _rendered_page(monkeypatch, events, users=None) -> str:
    from accounts import registry
    from admin import admin_core

    with registry._users_lock:
        registry._users[:] = list(users or [{"user_id": "u1", "principal_id": "p1"}])
    monkeypatch.setattr(
        data_track.db, "query_trace_events", lambda **kw: list(events)
    )
    monkeypatch.setattr(
        data_track.db, "get_blob",
        lambda uid, kind: {"enabled": True} if kind == "v1_flow_trace_enabled" else None,
    )
    return admin_core.page_html("view=debug&mode=timeline&user_id=u1")


def _event(**over):
    base = {
        "ts": 100.0, "user_id": "u1", "subsystem": "agent",
        "type": "agent.job.terminal", "status": "error", "actor": "hosted_v2",
        "trace_id": "t-page", "turn_id": "t-page", "job_id": "6791",
        "dur_ms": 1100.0, "detail": {}, "content_excerpt": {},
        "summary": "", "explain": "",
    }
    base.update(over)
    return base


def test_a_registered_failure_code_is_visible_in_the_rendered_page(monkeypatch):
    """页面上肉眼能看到**已登记的**失败码,而不是 `<redacted ...>`。

    ⚠️ 这条用例的名字和说明改过一次,原因值得写下来:
    原本写的是「能看到 `wake_failed:providererror`」,而我把测试值偷换成了
    `quota_insufficient` —— **说明与断言相反**,读的人会以为拼装码已经可见。
    搭档查出来的。**测试的说明必须描述它实际验证的东西,不是我希望它验证的东西。**

    **这条是整个 PR 的主诉。** 只测投影函数会漏掉它 —— 第一版就是这么漏的。
    """
    html_out = _rendered_page(monkeypatch, [
        _event(detail={"lane": "heartbeat", "outcome": "failed",
                       "error_code": "quota_insufficient"}),
    ])
    visible = re.sub(r"<[^>]+>", " ", html_out)
    assert "quota_insufficient" in visible
    assert "heartbeat" in visible
    assert "<redacted string len=18>" not in visible


def test_user_text_never_reaches_the_rendered_page(monkeypatch):
    """另一半:同样打在渲染产物上,而不是投影函数上。

    搭档构造的三个反例 —— 一个用户名、一个疑似令牌、一个疑似病历号 ——
    它们**都能通过字符形状检查**。所以放行判据改成了「产生者侧登记过的码」,
    形状证明不了来源。
    """
    for value in ("alice", "secret_token", "patient_12345",
                  "我今天心情不太好"):
        html_out = _rendered_page(monkeypatch, [
            _event(detail={"error_code": value}),
        ])
        assert value not in re.sub(r"<[^>]+>", " ", html_out), f"泄漏: {value!r}"


def test_turn_header_rejects_forged_identity_in_the_rendered_page(monkeypatch):
    """抬头是**第三条**输出通路,它既不过脱敏也不过投影。

    实跑 lane=alice / job_id=secret_token / outcome_class=patient_12345
    在上一版会三项全显示 —— 我一边问搭档「有没有别的出口」,一边自己新开了一个。
    """
    html_out = _rendered_page(monkeypatch, [
        _event(lane="alice", job_id="secret_token",
               outcome_class="patient_12345", detail={}),
    ])
    visible = re.sub(r"<[^>]+>", " ", html_out)
    for forged in ("alice", "secret_token", "patient_12345"):
        assert forged not in visible, f"抬头泄漏: {forged}"


def test_undecidable_queue_wait_is_not_rendered_as_zero(monkeypatch):
    """时钟回拨时,抬头**不许**出现「排队 0ms」。

    「我不知道」和「确实是 0」在页面上必须长得不一样 ——
    否则读的人会拿一个不存在的事实去下判断。
    """
    html_out = _rendered_page(monkeypatch, [
        _event(ts=100.0, type="agent.job.enqueued", status="ok", detail={}),
        _event(ts=99.0),   # terminal 早于 enqueue:时钟回拨
    ])
    visible = re.sub(r"<[^>]+>", " ", html_out)
    # ⚠️ 这里改过一次断言,理由要写下来,免得看起来像「改测试让它变绿」:
    # 原断言是「不许出现『排队』二字」—— 那是对真意图的**粗糙代理**。
    # 真意图是「不许编一个排队时长」。而实现给出的
    # 「(排队时长不可判定)」**比不显示更好**:它把「我不知道」明说出来,
    # 而不是让读的人以为这个字段不存在。
    # 所以断言改成直接表达意图:**不许出现任何排队数值,且必须出现不可判定标记。**
    assert not re.search(r"排队\s+[\d.]+\s*ms", visible), visible[:200]
    assert "排队时长不可判定" in visible


def test_v2_composed_failure_codes_come_from_the_worker_export():
    """正向遍历产生方导出；管理端抄表或漏接新增值都会直接变红。"""
    from model_api_runtime.v2 import worker

    assert "wake_failed:providererror" in worker.PUBLIC_FAILURE_CODES
    for code in sorted(worker.PUBLIC_FAILURE_CODES):
        assert _public("agent.job.terminal", {"error_code": code})["error_code"] == code


def test_admin_failure_allowlist_tracks_a_producer_export_mutation(monkeypatch):
    """突变守卫：若 admin 偷抄当前表，产生方新增值时这条会红。"""
    from model_api_runtime.v2 import worker

    probe = "runtime_failed:producer_export_probe"
    monkeypatch.setattr(
        worker,
        "PUBLIC_FAILURE_CODES",
        worker.PUBLIC_FAILURE_CODES | {probe},
    )
    monkeypatch.setattr(data_track, "_KNOWN_FAILURE_CODES_CACHE", None)
    try:
        assert _public("agent.job.terminal", {"error_code": probe})["error_code"] == probe
    finally:
        data_track._KNOWN_FAILURE_CODES_CACHE = None


def test_composed_failure_prefix_does_not_authorize_an_unknown_suffix():
    """泄漏侧独立断言：合法前缀不能替任意后半段证明来源。"""
    hidden = _public("agent.job.terminal", {
        "error_code": "turn_failed:secret_token",
    })["error_code"]
    assert hidden.startswith("<redacted")


def test_enqueue_reasons_come_from_the_jobs_store_export():
    from model_api_runtime.v2 import jobs_store

    assert {"scheduled_wake", "manual_tick"} <= jobs_store.ENQUEUE_REASON_CODES
    for reason in sorted(jobs_store.ENQUEUE_REASON_CODES):
        assert _public("agent.job.enqueued", {"reason": reason})["reason"] == reason
    assert _public("agent.job.enqueued", {
        "reason": "patient_12345",
    })["reason"].startswith("<redacted")


def test_admin_enqueue_allowlist_tracks_a_producer_export_mutation(monkeypatch):
    from model_api_runtime.v2 import jobs_store

    probe = "producer_enqueue_probe"
    monkeypatch.setattr(
        jobs_store,
        "ENQUEUE_REASON_CODES",
        jobs_store.ENQUEUE_REASON_CODES | {probe},
    )
    monkeypatch.setattr(data_track, "_TRACE_PUBLIC_FIELDS_CACHE", None)
    try:
        assert _public("agent.job.enqueued", {"reason": probe})["reason"] == probe
    finally:
        data_track._TRACE_PUBLIC_FIELDS_CACHE = None


def test_voice_codes_cover_both_runtime_paths_from_the_producer_export():
    from voice import error_codes

    assert {
        "voice_turn_not_accepted",
        "runtime_control_unavailable",
    } <= error_codes.VOICE_GATEWAY_ERROR_CODES
    for code in sorted(error_codes.VOICE_GATEWAY_ERROR_CODES):
        assert _public("voice.gateway.turn.runtime_rejected", {
            "error_code": code,
        })["error_code"] == code


def test_v1_model_error_classes_use_the_resident_catalog_contract():
    """V1 resident classifications are catalog-parity tested separately."""
    from notices import catalog

    for code in sorted(catalog.ERROR_CLASSES):
        assert _public("agent.model.call.error", {
            "error_class": code,
        })["error_class"] == code
    assert _public("agent.model.call.error", {
        "error_class": "SecretTokenError",
    })["error_class"].startswith("<redacted")


def test_error_classes_come_from_the_notices_catalog(monkeypatch):
    """正向:权威导出里的每一个码都必须可见 —— 由**产生方**驱动,不是我列的清单。

    我先前在管理端手抄了一份表,还在注释里写「从产生方读」。
    抄漏的直接后果:`provider_timeout` / `provider_empty_reply` 都判 False。
    这条用例让**抄表这件事本身**变得会红:表一旦不是从 catalog 来的,它就挂。
    """
    from notices import catalog
    assert len(catalog.ERROR_CLASSES) >= 20
    for code in sorted(catalog.ERROR_CLASSES):
        assert _public("agent.job.terminal", {"error_code": code})["error_code"] == code


def test_non_finite_durations_are_never_rendered(monkeypatch):
    """NaN / ±Inf 不许渲染成「执行 nanms」。

    上一版只验了跨度有限、**没验执行时长本身** —— 不可判定要两端都验。
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        html_out = _rendered_page(monkeypatch, [
            _event(ts=100.0, type="agent.job.enqueued", status="ok", detail={}),
            _event(ts=103.2, dur_ms=bad),
        ])
        visible = re.sub(r"<[^>]+>", " ", html_out)
        for token in ("nan", "inf", "-inf"):
            assert token not in visible.lower(), f"{bad} 渲染出了 {token}"


def test_job_id_must_be_a_bounded_ascii_integer(monkeypatch):
    """`str.isdigit()` 接受全角 １２３ / 上标 ² / 200 位数字 —— DB 的 id 不是那样。"""
    for forged in ("１２３", "²", "9" * 200):
        html_out = _rendered_page(monkeypatch, [_event(job_id=forged, detail={})])
        assert forged not in re.sub(r"<[^>]+>", " ", html_out)


# --------------------------------------------------------------------------- #
# 6. ⭐ 端点级 —— 渲染层不是唯一出口
# --------------------------------------------------------------------------- #

def test_non_finite_or_negative_durations_never_reach_the_json_endpoint(monkeypatch):
    """`/v1/admin/data-track/debug` 是**第四条**出口,它绕过 HTML 格式化。

    我在上一轮说过「挡在渲染层就所有出口一次覆盖」——**那句是错的**,
    今天第三次把「我希望它是这样」说成「它是这样」。
    Starlette 对 NaN/±Inf 抛 `ValueError: Out of range float values are not
    JSON compliant` ⇒ **整个端点 500**,而 HTML 页面看起来一切正常。

    ⇒ 归一化挪到**数据边界**(聚合与事件投影的构造点),渲染层只是最后一道。
    """
    import json as _json
    from accounts import registry
    from admin import admin_core

    for bad in (float("nan"), float("inf"), float("-inf"), -1.0, -5.0):
        with registry._users_lock:
            registry._users[:] = [{"user_id": "u1", "principal_id": "p1"}]
        monkeypatch.setattr(data_track.db, "query_trace_events", lambda **kw: [
            _event(ts=100.0, type="agent.job.enqueued", status="ok", detail={}),
            _event(ts=103.2, dur_ms=bad),
        ])
        monkeypatch.setattr(
            data_track.db, "get_blob",
            lambda uid, kind: {"enabled": True} if kind == "v1_flow_trace_enabled" else None,
        )
        payload = admin_core.debug_payload("mode=timeline&user_id=u1")
        # 这一步就是端点真正会做的事 —— 它以前会抛 ValueError
        body = _json.dumps(payload, allow_nan=False)
        assert "NaN" not in body and "Infinity" not in body
        # ⚠️ 负时长与非有限值**同形**:渲染层查了 `< 0`、数据边界没查,
        # 于是 HTML 空而 JSON 公开 -5.0,并把总耗时拉成负数。
        # 判据两半必须都在数据边界。
        for turn in payload.get("turns") or []:
            tot = turn.get("total_dur_ms")
            assert tot is None or tot >= 0, turn
            for row in turn.get("rows") or []:
                assert row.get("dur_ms") is None or row["dur_ms"] >= 0, row
        for ev in payload.get("events") or []:
            assert ev.get("dur_ms") is None or ev["dur_ms"] >= 0, ev


def test_aggregate_overflow_is_caught_even_when_every_input_is_finite():
    """⚠️ **每个值都有限,不代表和有限。**

    两个 `1e308` 相加就是 `inf` —— 我上一轮的守卫只看**单个输入**,
    于是端点照样 500。**守住每个输入 != 守住输出。**
    这是「守卫放对了位置但仍漏」的另一形态:位置对、覆盖面漏了一维(聚合)。
    """
    import json as _json
    big = 1e308
    turn = data_track._debug_trace_group_turns([
        {"ts": 1.0, "type": "a.b", "status": "ok", "trace_id": "x",
         "user_id": "u", "dur_ms": big},
        {"ts": 2.0, "type": "a.c", "status": "ok", "trace_id": "x",
         "user_id": "u", "dur_ms": big},
    ])[0]
    # ⚠️ 这条断言**改过一次**:上一版我断言 `== 0.0`,而那是我自己伪造的值。
    # 真实总时长「不可表示」,不是零 —— **用一个精确的假值替换一个错误,
    # 比留着错误更坏**:它看起来是个答案。
    assert turn["total_dur_ms"] is None
    _json.dumps(turn["total_dur_ms"], allow_nan=False)


def test_an_integer_too_large_for_float_does_not_explode():
    """`float(10**400)` 抛的是 **OverflowError** —— 既不是 TypeError 也不是 ValueError。

    只抓那两种的话,一个超大整数就能把端点打 500。
    """
    assert data_track._finite_ms(10 ** 400) is None
    assert data_track._finite_ms(-(10 ** 400)) is None
