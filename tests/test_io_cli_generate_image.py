"""`generate-image` 必须真的能把图交到 send-image 手上。

这个文件存在的理由,是一次**契约靠脑补写、单测全绿照样必失败**的事故:

- 后端 `/v1/image-generation/generate` 返回 `images[].data_base64/mime_type/name`
  (hosted/image_generator.py:162),第一版实现照 `media[].data_b64/mime` 读,
  **任何一次真实调用都会停在 "returned no media"** —— 而伴侣看到的是「画不出来」,
  会以为是模型不支持,根本追不到这里。
- 落盘目录写的是 `FEEDLING_OUTBOUND_FILE_DIR`(全仓无人设置)→ 退到 `/tmp`;
  而 `send-image` 经 IPC 只接受 `$FEEDLING_HOME/outbound-files`
  (consumer 的 `path_outside_outbound_dir` 闸)。**图生成了也交付不出去。**

两个错误都不是逻辑错,是**没有对着真实契约核对**。所以这里的断言全部锚在真实
形状上:响应字段名、send-image 认的目录、以及两个动词之间的路径可衔接性。
"""
import base64
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import io_cli  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\nFAKEPNGBYTES"

# 后端真实成功体(hosted/image_generator.py:150-162)。别改成"看起来差不多"的形状 ——
# 这个 fixture 就是契约本身。
REAL_SUCCESS_BODY = {
    "images": [
        {
            "mime_type": "image/png",
            "data_base64": base64.b64encode(_PNG).decode("ascii"),
            "name": "generated_1.png",
        }
    ]
}


class _Args:
    def __init__(self, prompt):
        self.prompt = prompt


def _run(monkeypatch, tmp_path, status, body, prompt="一只在窗台上打盹的橘猫"):
    """跑一次 cmd_generate_image,把它 _emit 出来的 JSON 和退出码抓回来。"""
    monkeypatch.setenv("FEEDLING_HOME", str(tmp_path))
    monkeypatch.setattr(io_cli, "_require_backend", lambda: ("https://api.test", "k"))
    monkeypatch.setattr(io_cli, "_http_json", lambda *a, **kw: (status, body))

    captured = {}

    def _fake_emit(payload, code=0):
        captured["payload"] = payload
        captured["code"] = code
        raise SystemExit(code)

    monkeypatch.setattr(io_cli, "_emit", _fake_emit)
    with pytest.raises(SystemExit):
        io_cli.cmd_generate_image(_Args(prompt))
    return captured["payload"], captured["code"]


def test_real_backend_shape_produces_a_file_send_image_would_accept(monkeypatch, tmp_path):
    """真实响应形状 → 落盘 → 且落在 send-image 认的目录里。

    这一条同时锁住两个曾经的错误:字段名、目录。
    """
    payload, code = _run(monkeypatch, tmp_path, 200, REAL_SUCCESS_BODY)

    assert code == 0, f"真实契约形状必须成功,却失败了: {payload}"
    assert payload["ok"] is True
    paths = payload["paths"]
    assert len(paths) == 1

    written = Path(paths[0])
    assert written.read_bytes() == _PNG, "写进去的必须是解码后的真实字节"

    # send-image(io_cli.cmd_send_image)只把相对路径拼到这个目录,并且 consumer
    # 侧的闸也只认它。生成的图必须正好落在这里,否则交付必被拒。
    accepted_dir = Path(io_cli._resident_ipc_home()) / "outbound-files"
    assert written.parent == accepted_dir, (
        f"图落在 {written.parent},但 send-image 只接受 {accepted_dir}"
    )
    assert written.suffix == ".png"


def test_two_calls_do_not_overwrite_each_other(monkeypatch, tmp_path):
    """连续两次生图必须是两个文件。

    原实现用秒级时间戳做文件名:同一秒内的两次生图会互相覆盖,hosted 多用户
    共享目录时还会串图 —— 把别人的图发给这个用户。
    """
    first, _ = _run(monkeypatch, tmp_path, 200, REAL_SUCCESS_BODY)
    second, _ = _run(monkeypatch, tmp_path, 200, REAL_SUCCESS_BODY)
    assert first["paths"][0] != second["paths"][0]


def test_failure_is_handed_back_honestly_and_never_claims_success(monkeypatch, tmp_path):
    """失败要如实交回,并且明确叮嘱不要谎报。

    这是产品红线:失败是伴侣该知道的事实,不能被 runtime 吞掉,更不能让它以为
    图已经存在。
    """
    payload, code = _run(
        monkeypatch, tmp_path, 400, {"error": "image_generation_model_incompatible"}
    )
    assert code == 1
    assert payload["ok"] is False
    assert "不要声称图已经生成" in payload["hint"]
    # 失败体里不能出现任何可被误读成"图在这儿"的路径
    assert "paths" not in payload


def test_empty_images_list_is_a_failure_not_a_silent_success(monkeypatch, tmp_path):
    """200 但没有图 —— 必须报失败。

    静默成功会让伴侣发一句「图画好了」而实际什么都没有,正好撞上谎报检测,
    把 provider 的问题伪装成模型在撒谎。
    """
    payload, code = _run(monkeypatch, tmp_path, 200, {"images": []})
    assert code == 1
    assert payload["ok"] is False
    assert "不要声称图已经生成" in payload["hint"]


def test_next_step_points_at_send_image(monkeypatch, tmp_path):
    """成功后要告诉伴侣下一步 —— 生成不等于交付,这两步是分开的。"""
    payload, _ = _run(monkeypatch, tmp_path, 200, REAL_SUCCESS_BODY)
    assert "send-image" in payload["next"]


def test_blank_prompt_is_rejected_before_spending_money(monkeypatch, tmp_path):
    """空 prompt 不该打到 provider —— 那是一次纯浪费的付费调用。"""
    monkeypatch.setenv("FEEDLING_HOME", str(tmp_path))
    monkeypatch.setattr(io_cli, "_require_backend", lambda: ("https://api.test", "k"))

    def _boom(*a, **kw):
        raise AssertionError("空 prompt 不该发起请求")

    monkeypatch.setattr(io_cli, "_http_json", _boom)
    captured = {}

    def _fake_emit(payload, code=0):
        captured["payload"] = payload
        raise SystemExit(code)

    monkeypatch.setattr(io_cli, "_emit", _fake_emit)
    with pytest.raises(SystemExit):
        io_cli.cmd_generate_image(_Args("   "))
    assert captured["payload"]["ok"] is False
