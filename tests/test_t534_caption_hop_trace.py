"""T534:图片轮 caption 链路的 content-free 观测。

这条 trace 存在的唯一理由是回答「用户随图发的文字在哪一跳丢的」。
因此它必须满足两件事,缺一不可:
  1. **能分辨**:分支名 + 在不在 + 长度,足以把「进来就没有」与「装配时掉的」分开;
  2. **不带内容**:caption/observation 的字面**一个字都不能进 payload** ——
     否则这条诊断事件本身就成了用户消息的副本。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# consumer 在模块作用域读 env,必须在 import 之前设好(与 test_chat_resident_consumer_image.py 同款引导)
for _k, _v in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_t534_checkpoint.json",
}.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import chat_resident_consumer as crc  # noqa: E402


@pytest.fixture
def emitted(monkeypatch):
    calls: list[dict] = []

    def _capture(subsystem, type, **kwargs):
        calls.append({"subsystem": subsystem, "type": type, **kwargs})

    monkeypatch.setattr(crc, "_emit_debug_trace", _capture)
    return calls


SECRET = "这张图怎么了 TOKEN42"


def test_hop_records_presence_and_length_without_the_caption_text(emitted):
    crc._emit_caption_hop("intake", content_type="image", caption=SECRET, payload=SECRET, message_id="m1")
    assert len(emitted) == 1
    ev = emitted[0]
    assert (ev["subsystem"], ev["type"]) == ("chat", "chat.image_caption.hop")
    detail = ev["detail"]
    assert detail["branch"] == "intake"
    assert detail["caption_present"] is True
    assert detail["caption_len"] == len(SECRET)
    # 内容不得外泄:整个事件序列化后不含 caption 任何片段
    blob = repr(ev)
    assert SECRET not in blob
    assert "TOKEN42" not in blob
    assert "这张图怎么了" not in blob


def test_absent_caption_is_reported_as_absent(emitted):
    crc._emit_caption_hop("native_image", content_type="image", caption="   ", payload="一段图片观察", message_id="m2")
    detail = emitted[0]["detail"]
    assert detail["caption_present"] is False
    assert detail["branch"] == "native_image"


@pytest.mark.parametrize("branch", sorted(crc._CAPTION_TRACE_BRANCHES))
def test_known_branches_pass_through(branch, emitted):
    crc._emit_caption_hop(branch, content_type="image", caption="x", payload="x", message_id="m3")
    assert emitted[0]["detail"]["branch"] == branch


def test_unknown_branch_is_closed_to_a_sentinel(emitted):
    # 分支名是闭集:未知值不得原样带出(否则调用点笔误会变成一个新的、无人认识的标签)
    crc._emit_caption_hop("../../etc/passwd", content_type="image", caption="x", payload="x")
    assert emitted[0]["detail"]["branch"] == "unknown"


def test_emit_never_raises_into_the_turn(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("trace backend down")

    monkeypatch.setattr(crc, "_emit_debug_trace", _boom)
    crc._emit_caption_hop("intake", content_type="image", caption="x", payload="x")  # 不抛即通过


# ---------------------------------------------------------------------------
# ⭐ 判别力:载荷非空 ≠ caption 还在(codex3 r2 指出的反例)
# ---------------------------------------------------------------------------

def test_empty_caption_with_nonempty_observation_is_not_reported_present(emitted):
    """装配后的载荷永远非空(有图片观察/占位符)。

    若判据写成「载荷非空」,caption 为空也会被报成 present —— 这条 trace 明天
    就会把「配文丢了」读成「配文还在」,整件事白做。
    """
    crc._emit_caption_hop(
        "dedicated_vision", content_type="image",
        caption="", payload="Image 1:\na visual observation", message_id="m9",
    )
    detail = emitted[0]["detail"]
    assert detail["caption_present"] is False
    assert detail["caption_in_payload"] is False
    assert detail["payload_len"] > 0  # 载荷确实非空,但那不构成 caption 还在


def test_caption_lost_during_assembly_is_visible(emitted):
    crc._emit_caption_hop(
        "dedicated_vision", content_type="image",
        caption="这张图怎么了", payload="Image 1:\n一段观察", message_id="m10",
    )
    detail = emitted[0]["detail"]
    assert detail["caption_present"] is True      # 用户确实发了文字
    assert detail["caption_in_payload"] is False  # 但它没进最终载荷 ⇒ 就丢在这一跳


def test_caption_surviving_is_visible(emitted):
    crc._emit_caption_hop(
        "dedicated_vision", content_type="image",
        caption="这张图怎么了", payload="Image 1:\n一段观察\n\n这张图怎么了", message_id="m11",
    )
    assert emitted[0]["detail"]["caption_in_payload"] is True


def test_trace_id_binds_to_the_message(emitted):
    crc._emit_caption_hop("intake", content_type="image", caption="x", payload="x", message_id="msg-77")
    assert emitted[0]["trace_id"] == "msg-77"


# ---------------------------------------------------------------------------
# ⭐ 接线守卫:拔掉任何一个生产调用点,下面必须红
#    (codex3 r2:原来 8 条全是直接调 helper,把两个调用点删掉测试还全绿 = 零判别力)
# ---------------------------------------------------------------------------

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def _image_msg(msg_id: str, caption: str = "", ts: float = 9500.0) -> dict:
    import base64
    return {
        "id": msg_id,
        "ts": ts,
        "role": "user",
        "content_type": "image",
        "content": caption,
        "image_b64": base64.b64encode(_JPEG).decode("ascii"),
        "image_mime": "image/jpeg",
    }


def _run_image_turn(monkeypatch, tmp_path, msg):
    """跑一次真实的 _process_messages 图片轮,返回 (hops, 交给 agent 的最终文本)。"""
    hops: list[dict] = []
    carrier: dict = {}
    monkeypatch.setattr(
        crc, "_emit_debug_trace",
        lambda subsystem, type, **kw: hops.append({"type": type, **kw}),
    )

    def fake_call(message, images=None, image_paths=None, trace_id=None, **kwargs):
        carrier["message"] = message
        return {"messages": ["ok"]}

    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    monkeypatch.setattr(crc, "IMAGE_TEMP_DIR", tmp_path)
    monkeypatch.setattr(crc, "call_agent", fake_call)
    monkeypatch.setattr(crc, "post_reply", lambda *a, **k: {"id": "r1"})
    crc._process_messages([msg])
    return [h for h in hops if h["type"] == "chat.image_caption.hop"], carrier.get("message", "")


def test_wiring_intake_and_assembly_hops_fire_on_a_real_image_turn(monkeypatch, tmp_path):
    hops, _ = _run_image_turn(monkeypatch, tmp_path, _image_msg("wire-1", caption="这张图怎么了"))
    branches = [h["detail"]["branch"] for h in hops]
    assert "intake" in branches, f"intake 跳没打,拔掉调用点也不会红;实际 ={branches}"
    assert any(b in {"dedicated_vision", "native_image", "image_placeholder"} for b in branches), (
        f"装配跳没打;实际 ={branches}"
    )


def test_wiring_agent_carrier_hop_fires_and_matches_what_agent_received(monkeypatch, tmp_path):
    caption = "这张图怎么了"
    hops, carrier = _run_image_turn(monkeypatch, tmp_path, _image_msg("wire-2", caption=caption))
    agent_hops = [h for h in hops if h["detail"]["branch"] == "agent_carrier"]
    assert agent_hops, f"agent_carrier 跳没打;实际 ={[h['detail']['branch'] for h in hops]}"
    d = agent_hops[-1]["detail"]
    # trace 说的「还在/不在」必须与 agent 实际收到的文本一致 —— 否则它在说谎
    assert d["caption_in_payload"] is (caption in carrier)


def test_wiring_hops_report_absence_when_the_user_sent_no_text(monkeypatch, tmp_path):
    hops, _ = _run_image_turn(monkeypatch, tmp_path, _image_msg("wire-3", caption=""))
    for h in hops:
        assert h["detail"]["caption_present"] is False
        assert h["detail"]["caption_in_payload"] is False


def test_no_caption_bytes_leak_from_a_real_turn(monkeypatch, tmp_path):
    caption = "绝密配文 LEAK42"
    hops, _ = _run_image_turn(monkeypatch, tmp_path, _image_msg("wire-4", caption=caption))
    blob = repr(hops)
    assert "LEAK42" not in blob and "绝密配文" not in blob


# ---------------------------------------------------------------------------
# ⭐ 作用域:一轮结束后不得留残影(codex3 r4 用真实 harness 实测到过)
# ---------------------------------------------------------------------------

def test_context_is_empty_before_and_after_a_successful_image_turn(monkeypatch, tmp_path):
    assert crc._caption_hop_current() == ("", "")
    _run_image_turn(monkeypatch, tmp_path, _image_msg("ctx-1", caption="CAPTION_STAYS_IN_GLOBAL"))
    assert crc._caption_hop_current() == ("", ""), (
        "回合结束后 caption 仍留在进程里 —— 之后任何 CLI 准备都会打出幽灵事件"
    )


def test_context_is_restored_when_the_agent_call_raises(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("driver exploded")

    crc._seen_ids.clear(); crc._seen_ids_order.clear()
    monkeypatch.setattr(crc, "_emit_debug_trace", lambda *a, **k: None)
    monkeypatch.setattr(crc, "IMAGE_TEMP_DIR", tmp_path)
    monkeypatch.setattr(crc, "call_agent", boom)
    monkeypatch.setattr(crc, "post_reply", lambda *a, **k: {"id": "r1"})
    try:
        crc._process_messages([_image_msg("ctx-2", caption="RAISE_PATH")])
    except Exception:
        pass
    assert crc._caption_hop_current() == ("", ""), "异常路径没还原 ⇒ finally 没兜住"


def test_later_unrelated_cli_preparation_emits_no_ghost(monkeypatch, tmp_path):
    _run_image_turn(monkeypatch, tmp_path, _image_msg("ctx-3", caption="GHOST_CANDIDATE"))
    hops: list[dict] = []
    monkeypatch.setattr(
        crc, "_emit_debug_trace",
        lambda subsystem, type, **kw: hops.append({"type": type, **kw}),
    )
    crc._prepare_cli_command("一条与图片无关的后台消息")
    assert not [h for h in hops if h["type"] == "chat.image_caption.hop"], (
        f"上一轮的 caption 漏进了无关调用;实际 ={hops}"
    )


# ---------------------------------------------------------------------------
# ⭐ cli_carrier 必须打在**真正离开进程的那份文本**上(pi / claude 两种 stdin 形状)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("driver", ["pi", "claude"])
def test_cli_carrier_hop_measures_the_real_driver_payload(monkeypatch, tmp_path, driver):
    """cli_carrier 必须打在**真正离开进程的那份文本**上(pi / claude 两种 stdin 形状)。

    这两个正是验收要跑的驱动;探针里用真实可执行的 shim,否则 CLI 存在性检查
    会在到达该跳之前就抛错,测试变成「没打也过」。
    """
    import subprocess as _sp

    shim = tmp_path / driver
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)

    caption = "CARRIER_PROBE_TEXT"
    composed = f"Image 1:\n一段观察\n\n{caption}"
    hops: list[dict] = []
    seen: dict = {}

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", f"{shim} --print {{message}}")
    monkeypatch.setattr(
        crc, "_emit_debug_trace",
        lambda subsystem, type, **kw: hops.append({"type": type, **kw}),
    )
    monkeypatch.setattr(crc, "_auto_memory_arrival", lambda *a, **k: None)

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs.get("input")
        seen["cmd"] = cmd
        return _sp.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(crc.subprocess, "run", fake_run)
    token = crc._CAPTION_HOP_CTX.set((caption, "carrier-msg-1"))
    try:
        crc._call_agent_cli_impl(composed)
    except Exception:
        pass  # 驱动细节不重要,只看 hop 打在哪份文本上
    finally:
        crc._CAPTION_HOP_CTX.reset(token)

    carrier_hops = [h for h in hops
                    if h["type"] == "chat.image_caption.hop"
                    and h["detail"]["branch"] == "cli_carrier"]
    assert carrier_hops, f"{driver}:cli_carrier 跳没打;seen={seen.get('cmd')}"
    detail = carrier_hops[-1]["detail"]
    actual_payload = seen.get("input") or " ".join(str(x) for x in seen.get("cmd", []))
    assert detail["caption_in_payload"] is (caption in (actual_payload or "")), (
        f"{driver}:trace 报的『在/不在』与真正交给驱动的文本不一致 "
        f"(trace={detail['caption_in_payload']}, 实际含={caption in (actual_payload or '')})"
    )
    # ⭐ 光比「在/不在」不够:message 与真正载体通常都含 caption,布尔一样,
    #    把 payload 换成 message 也不会红(实测 M6 全绿)。用长度钉住「量的是哪一份文本」。
    assert detail["payload_len"] == len(actual_payload or ""), (
        f"{driver}:payload_len={detail['payload_len']} 与真正载体长度 "
        f"{len(actual_payload or '')} 不符 ⇒ 这一跳量的不是离开进程的那份文本"
    )
    # 只查本探针自己的事件:别的既有 trace 事件是否带正文不在本单范围
    assert caption not in repr(carrier_hops), "载体探针泄漏了 caption 字面"
