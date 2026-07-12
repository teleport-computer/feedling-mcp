"""classify_agent_error: 三层错误来源 → (error_class, blame, 话术) 分类。

用例全部取自 prod 真实报错串（spec §测试）。
Run:  python -m pytest tests/test_consumer_error_classify.py -q
"""
import os
import subprocess
import sys
import types
from pathlib import Path

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


def test_foreground_transient_wave_merges_to_one_banner(monkeypatch):
    # 同一波抖动打出两个瞬时/系统类（429 + unknown）→ 只弹第一条；
    # 同类在窗口内再失败 → 也不再弹。
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=True)
    crc._notify_agent_turn_failure(RuntimeError("totally opaque failure"), foreground=True)  # unknown
    crc._notify_agent_turn_failure(RuntimeError("cli agent exited 1: 429"), foreground=True)
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
        crc.httpx, "post",
        lambda url, **kw: calls.append(kw.get("json")) or _FakeResp(200))
    crc._runtime_error_reported = True
    crc._note_agent_turn_success()
    assert calls == [{"error": "", "error_class": ""}]
    assert crc._runtime_error_reported is False


def test_clear_failure_keeps_flag_and_retries_next_success(monkeypatch):
    # Codex P2：清空 POST 失败不许翻标记——否则过期错误滞留设置页且永不重试。
    # 序列：传输异常 → 5xx → 200；前两次后标记必须仍为 True，第三次成功后 False，
    # 之后的成功回合不再发请求。
    calls = []
    outcomes = [RuntimeError("connect timeout"), _FakeResp(503), _FakeResp(200)]

    def fake_post(url, **kw):
        calls.append(kw.get("json"))
        r = outcomes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(crc.httpx, "post", fake_post)
    crc._runtime_error_reported = True
    crc._note_agent_turn_success()   # 传输失败
    assert crc._runtime_error_reported is True
    crc._note_agent_turn_success()   # 5xx
    assert crc._runtime_error_reported is True
    crc._note_agent_turn_success()   # 成功
    assert crc._runtime_error_reported is False
    crc._note_agent_turn_success()   # 已清，不再发
    assert len(calls) == 3


def test_report_404_counts_as_settled(monkeypatch):
    # 404=用户无 model_api profile，无可清内容——视为已了结，别让每个成功回合
    # 都对着 404 重试。
    monkeypatch.setattr(crc.httpx, "post", lambda url, **kw: _FakeResp(404))
    crc._runtime_error_reported = True
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
    # 靠模块级标记让前台调用方知道要补发 reply_parse_failed 通知（spec §组件2）
    crc._turn_reply_parse_failed = False
    assert hasattr(crc, "_turn_reply_parse_failed")


def test_parse_failed_marker_consumed_not_dangling():
    # 串扰修复：标记是 call_agent 多车道共享的，谁调用谁消费——绝不允许悬挂到
    # 别的车道/回合（proactive/verify_probe 置位后前台却读到旧值的 bug）。
    crc._turn_reply_parse_failed = True
    assert crc._consume_reply_parse_failed() is True
    assert crc._turn_reply_parse_failed is False
    assert crc._consume_reply_parse_failed() is False


def test_provider_incompatible_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("400 invalid_request_error: unsupported tool 'x'"))
    assert n.error_class == "provider_incompatible" and n.blame == "user_provider"


def test_context_overflow_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("prompt is too long: 210000 tokens > maximum context length"))
    assert n.error_class == "context_overflow" and n.blame == "user_provider"


def test_content_filtered_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("response was blocked by content_filter policy"))
    assert n.error_class == "content_filtered" and n.blame == "provider_transient"
