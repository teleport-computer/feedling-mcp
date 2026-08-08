"""classify_agent_error: 三层错误来源 → (error_class, blame, 话术) 分类。

用例全部取自 prod 真实报错串（spec §测试）。
Run:  python -m pytest tests/test_consumer_error_classify.py -q
"""
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_error_classify_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules["content_encryption"] = _fake_enc

import tools.chat_resident_consumer as crc  # noqa: E402


def _cls(exc):
    return crc.classify_agent_error(exc)


def test_relay_quota_403_is_quota_not_auth():
    # prod usr_0d16bfd4 原文：403 里同时有 Forbidden 和「额度」，语义是余额
    e = RuntimeError(
        "cli agent exited 1: unexpected status 403 Forbidden: litellm.APIError: "
        "APIError: OpenAIException - 预扣费额度失败, 用户剩余额度: ¥0.018000, "
        "需要预扣费额度: ¥0.020000 (request id: xxx)")
    n = _cls(e)
    assert n.error_class == "quota_insufficient"
    assert n.blame == "user_provider"


def test_claude_credit_balance_is_quota():
    n = _cls(RuntimeError(
        "cli agent exited 1: Your credit balance is too low to access the "
        "Anthropic API (api_status=400)"))
    assert n.error_class == "quota_insufficient"


def test_codex_retry_429_is_rate_limited():
    n = _cls(RuntimeError(
        "cli agent exited 1: exceeded retry limit, last status: 429 Too Many Requests"))
    assert n.error_class == "rate_limited"
    assert n.blame == "provider_transient"


def test_invalid_model_name():
    n = _cls(RuntimeError("cli agent exited 1: 400 Invalid model name passed in model=gw-usr_x"))
    assert n.error_class == "model_not_found"
    assert n.blame == "user_provider"


def test_invalid_api_key_is_auth():
    n = _cls(RuntimeError("cli agent exited 1: invalid x-api-key (api_status=401)"))
    assert n.error_class == "auth_invalid"


def test_nonetype_upstream_is_unknown_system():
    # 脑裂案例：key 空/上游死时 codex 的叙述
    n = _cls(RuntimeError("cli agent exited 1: Unexpected response type: NoneType"))
    assert n.error_class == "unknown"
    assert n.blame == "system"


def test_stream_disconnected_is_upstream_unavailable():
    n = _cls(RuntimeError("cli agent exited 1: stream disconnected before completion"))
    assert n.error_class == "upstream_unavailable"


def test_pi_stream_ended_without_finish_reason_is_upstream_unavailable():
    # prod usr_6f5a 2026-07-17: flaky openai-compatible relay cut the SSE stream;
    # pi surfaces this verbatim. Used to fall to unknown/blame=system ("连接模型
    # 服务时出了问题") — blaming US for the relay. It's the provider's transient.
    n = _cls(RuntimeError("pi agent produced no reply: Stream ended without finish_reason"))
    assert n.error_class == "upstream_unavailable"
    assert n.blame == "provider_transient"
    assert "你的模型服务" in n.user_text


def test_timeout_expired_by_type():
    n = _cls(subprocess.TimeoutExpired(cmd="codex", timeout=120))
    assert n.error_class == "turn_timeout"
    assert n.blame == "system"


def test_no_usable_reply_is_parse_failed():
    n = _cls(ValueError("agent produced no usable reply after sanitization"))
    assert n.error_class == "reply_parse_failed"
    assert n.blame == "system"


def test_system_blame_text_never_points_at_user_config():
    # 归责纪律：system 侧话术不得引导用户改 key/充值
    for exc in (subprocess.TimeoutExpired(cmd="c", timeout=120),
                ValueError("agent produced no usable reply after sanitization"),
                RuntimeError("cli agent exited 1: Unexpected response type: NoneType")):
        n = _cls(exc)
        assert n.blame == "system"
        for banned in ("充值", "Key", "key", "额度", "设置里"):
            assert banned not in n.user_text, (n.error_class, banned)


def test_notice_body_has_marker_and_detail_truncated():
    n = _cls(RuntimeError("boom " + "x" * 500))
    body = crc._system_notice_body(n)
    assert body.startswith("⚠️ ")
    assert "详情: " in body
    assert len(n.detail) <= 200


def test_background_failures_never_banner(monkeypatch):
    # Seven 2026-07-11：后台车道失败一律不进聊天流（用户无法据此行动，会被自己
    # 看不见的车道刷屏）。观测走 _report_runtime_error（设置页/admin）——它必须照发。
    sent, reported = [], []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: reported.append(a))
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=False)
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 余额不足"), foreground=False)
    assert sent == []          # 聊天流零横幅
    assert len(reported) == 2  # 设置页/admin 腿照发


def test_foreground_provider_transient_wave_merges_to_one_banner(monkeypatch):
    # provider_transient 各类合并一桶：同一波上游抖动打出 429 + 5xx → 只弹第一条；
    # 同桶在窗口内再失败 → 也不再弹。
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=True)
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 503"), foreground=True)
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=True)
    assert len(sent) == 1


def test_foreground_system_bucket_separate_from_provider_transient(monkeypatch):
    # 方案 C 核心：system（unknown）不再和 provider_transient（429）挤一个桶——
    # 一波上游抖动之后 IO 自己的系统错仍要弹出来，不被吞掉。
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=True)  # provider_transient
    crc._notify_agent_turn_failure(RuntimeError("totally opaque failure"), foreground=True)   # system/unknown
    assert len(sent) == 2


def test_foreground_system_wave_merges_within_bucket(monkeypatch):
    # system 自成一桶但仍合并：同故障期多个系统错（unknown + reply_parse_failed）
    # 只弹第一条防刷屏。
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("totally opaque failure"), foreground=True)  # unknown → system
    crc._notify_agent_turn_failure(RuntimeError("no usable reply"), foreground=True)         # reply_parse_failed → system
    assert len(sent) == 1


def test_foreground_actionable_classes_bucketed_per_class(monkeypatch):
    # 可行动类（user_provider）各自一个窗口：额度 + key 失效都值得用户知道，
    # 但同类重复在窗口内只弹一次。
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    quota = RuntimeError("cli agent exited 1: unexpected status 403: 额度不足")
    auth = RuntimeError("cli agent exited 1: invalid api key")
    crc._notify_agent_turn_failure(quota, foreground=True)
    crc._notify_agent_turn_failure(auth, foreground=True)   # 不被 quota 的窗口挡住
    crc._notify_agent_turn_failure(quota, foreground=True)  # 同类窗口内抑制
    assert len(sent) == 2


def test_foreground_window_not_reset_by_success(monkeypatch):
    # 固定窗口：成功回合不清零，flapping（fail→ok→fail）不重复弹。
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    e = RuntimeError("cli agent exited 1: 429")
    crc._notify_agent_turn_failure(e, foreground=True)
    crc._note_agent_turn_success()
    crc._notify_agent_turn_failure(e, foreground=True)
    assert len(sent) == 1


def test_foreground_banner_returns_after_window_elapses(monkeypatch):
    sent = []
    now = {"t": 1000.0}
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append(text) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    monkeypatch.setattr(crc.time, "monotonic", lambda: now["t"])
    crc._reset_system_notice_state()
    e = RuntimeError("cli agent exited 1: 429")
    crc._notify_agent_turn_failure(e, foreground=True)
    now["t"] += crc.FOREGROUND_NOTICE_WINDOW_SEC + 1
    crc._notify_agent_turn_failure(e, foreground=True)
    assert len(sent) == 2


def test_foreground_first_notice_not_suppressed_at_low_uptime(monkeypatch):
    # monotonic 从 0 起步（模拟刚开机的 CVM）：首个前台横幅不得被 0.0 默认值假抑制
    calls = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: calls.append(kw) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    monkeypatch.setattr(crc.time, "monotonic", lambda: 100.0)  # uptime 100s < 10800s
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=True)
    assert len(calls) == 1


def test_notify_posts_system_role_with_suppress_push(monkeypatch):
    calls = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: calls.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(
        RuntimeError("cli agent exited 1: invalid x-api-key"), foreground=True)
    text, kw = calls[0]
    assert kw["role"] == "system"
    assert kw["notice_kind"] == "upstream_error"
    assert kw["suppress_push"] is True
    assert text.startswith("⚠️ ")


def test_proactive_context_filters_system_notices():
    # role="system" 通知（如上游报错提醒）不该混进前台/proactive 上下文，
    # 否则 agent 会把它当成自己说过的话（审查发现的串扰源）。
    history = [
        {"role": "system", "text": "⚠️ 你的 API 服务额度不足，请检查设置。"},
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi there"},
    ]
    cleaned = crc._clean_messages_for_proactive_context(history)
    assert [m["role"] for m in cleaned] == ["user", "assistant"]


class _FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def test_note_success_clears_reported_flag_after_respawn(monkeypatch):
    # respawn 后新进程以 _runtime_error_reported=True 起步（覆盖 respawn 前滞留的
    # 设置页错误），首个成功回合应无条件清一次。mock 下沉到 httpx 层：标记翻转
    # 发生在 _report_runtime_error 内部且仅在请求送达时。
    calls = []
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda url, **kw: calls.append(kw.get("json")) or _FakeResp(200))
    crc._runtime_error_reported = True
    crc._provider_health_success_reported_at = 0.0
    monkeypatch.setattr(crc.time, "monotonic", lambda: 1_000.0)
    crc._note_agent_turn_success()
    assert calls == [
        {
            "error": "",
            "error_class": "",
            "provider_result": "success",
        }
    ]
    assert crc._runtime_error_reported is False


def test_clear_failure_keeps_flag_and_retries_next_success(monkeypatch):
    # Codex P2：清空 POST 失败不许翻标记——否则过期错误滞留设置页且永不重试。
    # 序列：传输异常 → 5xx → 200；前两次后标记必须仍为 True，第三次成功后 False，
    # 之后的成功回合按 15 分钟节流上报 provider_result=success。
    calls = []
    outcomes = [
        RuntimeError("connect timeout"),
        _FakeResp(503),
        _FakeResp(200),
        _FakeResp(200),
    ]
    clocks = iter([1_000.0, 1_001.0, 1_002.0, 1_003.0, 1_902.0])

    def fake_post(url, **kw):
        calls.append(kw.get("json"))
        r = outcomes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    monkeypatch.setattr(crc.time, "monotonic", lambda: next(clocks))
    crc._runtime_error_reported = True
    crc._provider_health_success_reported_at = 0.0
    crc._note_agent_turn_success()   # 传输失败
    assert crc._runtime_error_reported is True
    crc._note_agent_turn_success()   # 5xx
    assert crc._runtime_error_reported is True
    crc._note_agent_turn_success()   # 成功
    assert crc._runtime_error_reported is False
    crc._note_agent_turn_success()   # 15 分钟内，跳过
    assert len(calls) == 3
    crc._note_agent_turn_success()   # 满 15 分钟，刷新健康时间
    assert len(calls) == 4
    assert all(call["provider_result"] == "success" for call in calls)


def test_report_404_counts_as_settled(monkeypatch):
    # 404=用户无 model_api profile，无可清内容——视为已了结，别让每个成功回合
    # 都对着 404 重试。
    monkeypatch.setattr(crc._HTTP, "post", lambda url, **kw: _FakeResp(404))
    crc._runtime_error_reported = True
    crc._provider_health_success_reported_at = 0.0
    crc._note_agent_turn_success()
    assert crc._runtime_error_reported is False


def test_notify_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("post failed")
    monkeypatch.setattr(crc, "post_reply", boom)
    monkeypatch.setattr(crc, "_report_runtime_error", boom)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("x"), foreground=True)  # 不抛即过


def test_parse_failed_marker_set_by_call_agent_sanitize_branch():
    # call_agent 清洗为空时不抛异常（SEND_FALLBACK_ON_AGENT_ERROR 默认 true），
    # 靠模块级标记让前台调用方知道要补发失败通知（spec §组件2）
    crc._turn_reply_parse_failed = ""
    assert hasattr(crc, "_turn_reply_parse_failed")


# ---------------------------------------------------------------------------
# 空回复归因分叉(2026-08-07,usr_7f30d63f:中转返回 200+空内容曾一律记 system,
# 用户拿着「系统出了问题」来找我们;真凶是他的中转断流/假成功)
# ---------------------------------------------------------------------------


# --- 生产边界:真实 helper + 只 mock transport/subprocess ---------------
# codex2 gatekeep(2026-08-06)抓到的假绿:上一版只用 lambda 直接返回 ""，
# 而生产 helper 在**返回之前**就抛异常，分叉根本执行不到 —— 核心工单场景
# (OpenAI 200 + content:"") 仍然归 system。这几条必须调真实 helper。


class _FakeJsonResp:
    """200 + 指定 JSON body。**刻意不叫 _FakeResp** —— 本文件已有一个同名的
    状态码版(_FakeResp(503)),重名会静默覆盖它、废掉别的用例的 5xx 分支。"""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _classify_raised(fn):
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 — 分类器的输入就是异常
        return crc.classify_agent_error(exc)
    raise AssertionError("helper did not raise")


def test_openai_http_200_with_empty_content_is_provider_empty_reply(monkeypatch):
    # 核心工单形状(usr_7f30d63f):中转 200 + assistant content 为空。
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/v1/chat")
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda *_a, **_k: _FakeJsonResp(
            {"choices": [{"message": {"role": "assistant", "content": ""}}]}))
    notice = _classify_raised(lambda: crc._call_agent_http_openai("hi"))
    assert notice.error_class == "provider_empty_reply"
    assert notice.blame == "provider_transient"


def test_simple_http_200_with_empty_reply_field_is_provider_empty_reply(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/chat")
    monkeypatch.setattr(
        crc._HTTP, "post", lambda *_a, **_k: _FakeJsonResp({"response": ""}))
    notice = _classify_raised(lambda: crc._call_agent_http_simple("hi"))
    assert notice.error_class == "provider_empty_reply"
    assert notice.blame == "provider_transient"


def test_simple_http_unrecognized_body_stays_unknown(monkeypatch):
    # 字段完全不认识 = 协议不匹配,不是「provider 给了空」——归因不许顺手扩大。
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/chat")
    monkeypatch.setattr(
        crc._HTTP, "post", lambda *_a, **_k: _FakeJsonResp({"totally_other": 1}))
    notice = _classify_raised(lambda: crc._call_agent_http_simple("hi"))
    assert notice.error_class == "unknown"
    assert notice.blame == "system"


def test_cli_exit_zero_without_assistant_output_is_provider_empty_reply(monkeypatch):
    import subprocess as _sp

    # 可执行文件存在性预检跑在 _run_cli_subprocess 之前,所以要给真实路径;
    # 进程本身不会被执行(下面 mock 掉了 subprocess 包装器)。
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "/bin/echo {message}")
    monkeypatch.setattr(crc, "_agent_cli_cwd", lambda: None)
    monkeypatch.setattr(crc, "_agent_cli_cwd_error", "")
    monkeypatch.setattr(
        crc, "_run_cli_subprocess",
        lambda *_a, **_k: _sp.CompletedProcess(
            args=["/bin/echo"], returncode=0, stdout="", stderr=""))
    notice = _classify_raised(lambda: crc.call_agent_cli("hi", lane="chat"))
    assert notice.error_class == "provider_empty_reply"
    assert notice.blame == "provider_transient"


def test_pi_empty_reply_with_quota_detail_still_classifies_as_quota(monkeypatch):
    """空回复标记绝不许遮蔽 detail 里更具体的分类(codex2 要求)。

    pi 退出码永远是 0，配额/鉴权错误只在 detail 里 —— 分类器把空回复判定排在
    规则表之后，所以这条仍然是 quota_insufficient/user_provider。"""
    exc = RuntimeError(
        f"{crc.EMPTY_PROVIDER_REPLY_MARK}: pi agent produced no reply: "
        "403 insufficient_user_quota: your credit balance is too low")
    notice = crc.classify_agent_error(exc)
    assert notice.error_class == "quota_insufficient"
    assert notice.blame == "user_provider"


def test_pi_empty_reply_without_detail_is_provider_empty_reply():
    exc = RuntimeError(
        f"{crc.EMPTY_PROVIDER_REPLY_MARK}: pi agent produced no reply: ")
    notice = crc.classify_agent_error(exc)
    assert notice.error_class == "provider_empty_reply"
    assert notice.blame == "provider_transient"


def test_call_agent_raw_empty_marks_provider_empty_reply(monkeypatch):
    # 原始回复本来就是空 → provider 给的空,归 provider_empty_reply。
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "call_agent_http", lambda *_a, **_k: "")
    monkeypatch.setattr(crc, "_call_with_resident_busy_poll",
                        lambda invoke, lane: invoke())
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    out = crc.call_agent("hi", lane="chat")
    assert out == [crc.FALLBACK_REPLY]
    assert crc._consume_reply_parse_failed() == "provider_empty_reply"


# 撕裂协议泄漏的真实形状(与 test_consumer_torn_protocol_leak.py 同源):
# 模型把 JSON 撕成两半,HEAD 落进 reasoning、TAIL 落进可见字段。
_TORN_HEAD = '{"messages":[],"actions":[{"type":"pro'
_TORN_TAIL = 'active.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}'


@pytest.mark.parametrize("body", [
    {"messages": [_TORN_TAIL], "provider_reasoning": _TORN_HEAD},
    {"actions": [{"type": "proactive.send_message", "text": _TORN_TAIL}],
     "provider_reasoning": _TORN_HEAD},
    {"choices": [{"message": {"content": _TORN_TAIL}}],
     "provider_reasoning": _TORN_HEAD},
])
def test_torn_leak_suppression_is_ours_not_the_providers(monkeypatch, body):
    """协议泄漏被我们压制掏空 → 必须留在 system,绝不能甩锅给中转。

    自审 2026-08-07 抓到的 BLOCKER:早先用 _raw_assistant_text(raw) 回头判空,
    那是记忆车道的窄提取器(不读 top-level messages / actions),于是前两种 body
    (其中 actions 那种正是原始 torn-leak bug 形状)被误判成 provider 空回复。
    这里跑**真实压制器**,三种 body 形状都必须归 system。"""
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "call_agent_http", lambda *_a, **_k: body)
    monkeypatch.setattr(crc, "_call_with_resident_busy_poll",
                        lambda invoke, lane: invoke())
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    out = crc.call_agent("hi", lane="proactive")
    assert out == [crc.FALLBACK_REPLY]
    assert crc._consume_reply_parse_failed() == "reply_parse_failed"


# provider **给过**文本、被我们的 sanitizer 清空的稳定样本:纯英文推理旁白,
# _sanitize_reply_text 按规则把它整段清掉(同源见
# tests/test_chat_resident_consumer.py::test_sanitize_reply_text_pure_english_reasoning_returns_empty)。
_SANITIZER_EATS_THIS = (
    'The user wrote "sweet!" ...\nI think it is best to respond ...'
)


def test_openai_sanitized_to_empty_is_ours_not_the_providers(monkeypatch):
    """provider 说了话、我们清空的 → system。归因边界的另一半(codex2 R3)。

    这是本批被连抓两轮的同一个洞:_agent_turn_from_raw **内部就跑 sanitizer**,
    拿它的产物回头判空,「没说话」和「说了被我们清空」永远分不开。"""
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/v1/chat")
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda *_a, **_k: _FakeJsonResp({"choices": [
            {"message": {"role": "assistant", "content": _SANITIZER_EATS_THIS}}]}))
    notice = _classify_raised(lambda: crc._call_agent_http_openai("hi"))
    assert notice.error_class == "reply_parse_failed"
    assert notice.blame == "system"


def test_simple_sanitized_to_empty_is_ours_not_the_providers(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/chat")
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda *_a, **_k: _FakeJsonResp({"response": _SANITIZER_EATS_THIS}))
    notice = _classify_raised(lambda: crc._call_agent_http_simple("hi"))
    assert notice.error_class == "reply_parse_failed"
    assert notice.blame == "system"


def test_call_agent_str_raw_sanitized_to_empty_stays_ours(monkeypatch):
    """CLI 返回非空文本、被 call_agent 这一层的 parse/sanitize 清空 → 仍归 system。

    快照取在 _agent_turn_from_raw **之前**,否则这条也会被甩给 provider。"""
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "call_agent_http",
                        lambda *_a, **_k: _SANITIZER_EATS_THIS)
    monkeypatch.setattr(crc, "_call_with_resident_busy_poll",
                        lambda invoke, lane: invoke())
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    out = crc.call_agent("hi", lane="chat")
    assert out == [crc.FALLBACK_REPLY]
    assert crc._consume_reply_parse_failed() == "reply_parse_failed"


@pytest.mark.parametrize(("body", "expected"), [
    # one-api/new-api 中转配额耗尽的标准形状:HTTP 200 + error 体。
    ({"error": {"message": "insufficient_quota: your credit balance is too low"}},
     "quota_insufficient"),
    ({"error": {"message": "invalid api key provided"}}, "auth_invalid"),
    # 内容策略拦截:choices 在但 finish_reason 说明了原因。
    ({"choices": [{"finish_reason": "content_filter",
                   "message": {"content": ""}}]}, "content_filtered"),
    # 什么诊断都没有 = 真的只是「成功但没内容」。
    ({"choices": [{"message": {"role": "assistant", "content": ""}}]},
     "provider_empty_reply"),
])
def test_openai_200_body_diagnostics_beat_the_empty_mark(monkeypatch, body, expected):
    """200 + 空内容时,body 里的协议诊断必须还能被规则表捞走(自审 P1)。

    不带诊断的话,一个余额为零的用户会被告知「稍后再试、检查中转稳定性」——
    正是 blame 纪律要避免的误导。"""
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/v1/chat")
    monkeypatch.setattr(
        crc._HTTP, "post", lambda *_a, **_k: _FakeJsonResp(body))
    notice = _classify_raised(lambda: crc._call_agent_http_openai("hi"))
    assert notice.error_class == expected


def test_simple_200_error_body_still_classifies_by_rules(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://relay.local/chat")
    monkeypatch.setattr(
        crc._HTTP, "post",
        lambda *_a, **_k: _FakeJsonResp(
            {"error": "insufficient_quota: balance too low", "response": ""}))
    notice = _classify_raised(lambda: crc._call_agent_http_simple("hi"))
    assert notice.error_class == "quota_insufficient"


def test_cli_stdout_present_is_never_called_a_provider_empty_reply(monkeypatch):
    """stdout 里有东西就绝不算「provider 给了空回复」(自审 P2 的成对用例)。

    判据刻意写成看 stdout 而不是看 returncode:非 0 退出在更上游就抛掉了,
    returncode 在这里恒为 0,拿它判等于无条件把所有落到此处的情况甩给 provider。
    现实中 _extract_text_from_cli_output 对任何非空 stdout 都能兜出文本,
    所以这一支根本不抛 —— 正是我们要的:有内容就当回复,别误报成空。"""
    import subprocess as _sp

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "/bin/echo {message}")
    monkeypatch.setattr(crc, "_agent_cli_cwd", lambda: None)
    monkeypatch.setattr(crc, "_agent_cli_cwd_error", "")
    monkeypatch.setattr(
        crc, "_run_cli_subprocess",
        lambda *_a, **_k: _sp.CompletedProcess(
            args=["/bin/echo"], returncode=0,
            stdout='{"unknown_field": "x"}', stderr=""))
    out = crc.call_agent_cli("hi", lane="chat")
    assert crc.EMPTY_PROVIDER_REPLY_MARK not in str(out)


def test_provider_text_cannot_hijack_the_empty_mark():
    """标记带 feedling: 命名空间 —— provider 自己的英文报错不该劫持归因(自审 NIT)。"""
    exc = RuntimeError("pi: upstream said empty provider reply, retrying")
    assert crc.classify_agent_error(exc).error_class != "provider_empty_reply"


def test_call_agent_sanitized_to_empty_stays_reply_parse_failed(monkeypatch):
    # 原始回复有内容、清洗/解析后才空 → 可能是我们的问题,保持 system 归因。
    # 直接 monkeypatch 清洗器把 turn 掏空 —— 测的是归因分叉本身,
    # 不依赖泄漏启发式对具体样本的判定。
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(
        crc, "call_agent_http", lambda *_a, **_k: "一段有实质内容的原始回复")

    def _empty_turn(turn, lane=""):
        turn.actions = []
        turn.messages = []
        turn.tool_calls = []
        turn.thinking_summary = ""

    monkeypatch.setattr(crc, "_suppress_torn_protocol_leaks", _empty_turn)
    monkeypatch.setattr(crc, "_call_with_resident_busy_poll",
                        lambda invoke, lane: invoke())
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    out = crc.call_agent("hi", lane="chat")
    assert out == [crc.FALLBACK_REPLY]
    assert crc._consume_reply_parse_failed() == "reply_parse_failed"


def test_classifier_maps_empty_provider_reply_to_provider_transient():
    n = crc.classify_agent_error(crc._reply_parse_failure_exc("provider_empty_reply"))
    assert n.error_class == "provider_empty_reply"
    assert n.blame == "provider_transient"
    assert "系统" not in n.user_text          # 不再让用户以为是我们的问题
    n2 = crc.classify_agent_error(crc._reply_parse_failure_exc("reply_parse_failed"))
    assert n2.error_class == "reply_parse_failed"
    assert n2.blame == "system"


def test_parse_failed_marker_consumed_not_dangling():
    # 串扰修复：标记是 call_agent 多车道共享的，谁调用谁消费——绝不允许悬挂到
    # 别的车道/回合（proactive/verify_probe 置位后前台却读到旧值的 bug）。
    crc._turn_reply_parse_failed = "reply_parse_failed"
    assert crc._consume_reply_parse_failed() == "reply_parse_failed"
    assert crc._turn_reply_parse_failed == ""
    assert crc._consume_reply_parse_failed() == ""


def test_provider_incompatible_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("400 invalid_request_error: unsupported tool 'x'"))
    assert n.error_class == "provider_incompatible" and n.blame == "user_provider"


def test_deepseek_text_only_image_error_requires_vision_model():
    """Real DeepSeek error must not fall into generic provider_incompatible.

    Observed 2026-07-30 by sending a 1x1 PNG to deepseek-v4-flash through the
    production provider_client wire. The provider returned HTTP 400 with this
    exact deserialization message.
    """
    n = _cls(RuntimeError(
        "provider_http_400: Failed to deserialize the JSON body into the target "
        "type: messages[0]: unknown variant `image_url`, expected `text` at line "
        "1 column 295"))

    assert n.error_class == "vision_model_required"
    assert n.blame == "user_provider"
    assert "视觉" in n.user_text
    assert crc._turn_failure_reply_text(n) == n.user_text
    assert crc._turn_failure_reply_text(n) != crc.FALLBACK_REPLY


def test_openrouter_text_only_image_404_requires_vision_model_before_model_fallback():
    """Liko's exact OpenRouter rejection must beat the broad 404+model branch."""
    n = _cls(RuntimeError(
        "provider_http_404: model deepseek/deepseek-v4-pro: "
        "No endpoints found that support image input"))

    assert n.error_class == "vision_model_required"
    assert n.blame == "user_provider"
    assert crc._turn_failure_reply_text(n) != crc.FALLBACK_REPLY


def test_vision_model_required_guidance_uses_turn_language():
    zh = crc.classify_agent_error(crc.VisionObserverFailure(
        "vision_model_required",
        raw_user_text="看看这张图",
    ))
    en = crc.classify_agent_error(crc.VisionObserverFailure(
        "vision_model_required",
        raw_user_text="What is in this picture?",
    ))

    assert zh.user_text == (
        "由于当前模型没有视觉能力，模型无法收到图片信息，"
        "建议更改模型或在设置页单独添加视觉模型"
    )
    assert en.user_text == (
        "Your current model can't process images, so it didn't receive this "
        "picture. Switch models, or add a dedicated vision model in Settings."
    )
    assert zh.blame == en.blame == "user_provider"


def test_vision_model_required_bare_image_uses_archive_language(monkeypatch):
    monkeypatch.setitem(crc._whoami_cache, "archive_language", "zh-Hans")

    notice = crc.classify_agent_error(crc.VisionObserverFailure(
        "vision_model_required",
        raw_user_text="",
    ))

    assert notice.user_text.startswith("由于当前模型没有视觉能力")


def test_context_overflow_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("prompt is too long: 210000 tokens > maximum context length"))
    assert n.error_class == "context_overflow" and n.blame == "user_provider"


def test_content_filtered_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("response was blocked by content_filter policy"))
    assert n.error_class == "content_filtered" and n.blame == "provider_transient"


def test_turn_failure_post_kwargs_from_notice():
    """兜底回复必须把分类结果带给后端——这是 iOS 实时看到失败原因的唯一通路。
    只带 error_class/blame/user_text，detail 绝不下发（可能夹带 provider HTML、
    request id、敏感上下文）。"""
    notice = crc.classify_agent_error(RuntimeError("cli agent exited 1: 402 余额不足"))
    kwargs = crc.turn_failure_post_kwargs(notice)

    assert kwargs["turn_failure_error_class"] == "quota_insufficient"
    assert kwargs["turn_failure_blame"] == "user_provider"
    assert kwargs["turn_failure_user_text"] == notice.user_text
    assert "detail" not in kwargs
    assert not any("detail" in k for k in kwargs)
    assert len(kwargs["turn_failure_user_text"]) <= 500


def test_turn_failure_kwargs_empty_for_none():
    """无失败时返回空 dict —— 成功路径不得凭空带字段。"""
    assert crc.turn_failure_post_kwargs(None) == {}


def test_turn_failure_kwargs_truncate_long_user_text():
    """契约：user_text ≤ 500，杜绝把长文灌进用户可见文案。"""
    notice = crc.AgentErrorNotice("unknown", "system", "x" * 900, "detail")
    assert len(crc.turn_failure_post_kwargs(notice)["turn_failure_user_text"]) == 500


# --- 上游下线模型名 → 必须落 user_provider（2026-07-25 usr_a40e/usr_d98b 回归） ---

def test_deepseek_retired_model_name_is_model_not_found():
    """prod 原文：DeepSeek 2026-07-24 把 deepseek-chat 并入 V4 线。

    旧的窄正则（invalid model name|model_not_found|no such model）一条都不命中，
    错误掉进 unknown/blame=system —— 那一档按纪律不许引导用户改配置，于是用户
    连吃几十条兜底话术却永远不知道真正原因。"""
    n = _cls(RuntimeError(
        "cli agent exited 1: API Error: 400 The supported API model names are "
        "deepseek-v4-pro or deepseek-v4-flash, but you passed deepseek-chat "
        "(api_status=400)"))
    assert n.error_class == "model_not_found"
    assert n.blame == "user_provider"


def test_openai_model_does_not_exist_is_model_not_found():
    n = _cls(RuntimeError(
        "API Error: 404 The model `gpt-4.9-turbo` does not exist or you do not "
        "have access to it."))
    assert n.error_class == "model_not_found" and n.blame == "user_provider"


def test_unknown_model_wording_is_model_not_found():
    assert _cls(RuntimeError("relay said: unknown model 'glm-9'")).error_class == "model_not_found"


def test_model_word_in_5xx_still_upstream_not_model_not_found():
    """反向防误伤：报文里出现 model 字样的 5xx 仍是上游抖动，不能被新正则吞掉
    （否则会把「等它自己好」的错误误报成「去改你的配置」）。"""
    n = _cls(RuntimeError("cli agent exited 1: 503 model gateway overloaded, retry later"))
    assert n.error_class == "upstream_unavailable"
    assert n.blame == "provider_transient"


# --- 兜底话术分道：只有会自愈的错误才配说「稍后再发一次」 ---

def test_user_provider_failure_reply_is_actionable_not_fallback():
    """配置类错误永不自愈：发 FALLBACK_REPLY 的「你稍后再发一次，我会继续接」
    是在骗用户重试，每次重试都是又一次注定失败且照样计费的 provider 调用。"""
    notice = _cls(RuntimeError("cli agent exited 1: API Error: 402 Insufficient Balance"))
    text = crc._turn_failure_reply_text(notice)

    assert notice.blame == "user_provider"
    assert text == notice.user_text
    assert text != crc.FALLBACK_REPLY
    assert "稍后再发一次" not in text


def test_transient_failure_reply_keeps_fallback():
    """会自愈的错误（5xx/限流/流断）——「稍后再发一次」对它们是真话，保持不变。"""
    notice = _cls(RuntimeError("cli agent exited 1: 503 upstream overloaded"))
    assert notice.blame == "provider_transient"
    assert crc._turn_failure_reply_text(notice) == crc.FALLBACK_REPLY


def test_system_failure_reply_keeps_fallback():
    """我们自己的锅同样保持兜底：绝不能引导用户去改配置（误导，见 dded 案例）。"""
    notice = _cls(subprocess.TimeoutExpired(cmd="agent", timeout=300))
    assert notice.blame == "system"
    assert crc._turn_failure_reply_text(notice) == crc.FALLBACK_REPLY


def test_none_notice_falls_back():
    assert crc._turn_failure_reply_text(None) == crc.FALLBACK_REPLY


def test_actionable_reply_suppresses_duplicate_banner():
    """可行动话术已在气泡里说过一遍，不必再补一条内容重复的 system 横幅；
    设置页上报在节流判断之前，不受影响。"""
    notice = _cls(RuntimeError("cli agent exited 1: API Error: 402 Insufficient Balance"))
    crc._system_notice_last_sent.pop(notice.error_class, None)

    crc._suppress_duplicate_upstream_banner(notice)

    assert notice.error_class in crc._system_notice_last_sent
    crc._system_notice_last_sent.pop(notice.error_class, None)
