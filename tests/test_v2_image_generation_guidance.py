"""没配生图模型时,用户必须拿到「去设置里加一个」这句可行动的话。

usr_7001b1df80e2024d(2026-08-10,test/V2,聊天模型 deepseek-v4-flash via
openrouter)让 AI 画一张海报,AI 回的是「模型那边暂时没接上」—— 一句听起来像
"稍后重试"的话,而真相是**他压根没配生图模型**,重试一万次也不会成。

机制:没有专用生图路由时,`_generate_image_for_chat` 不会直说"你需要配一个",
而是**拿聊天模型去赌一把**(`config = main_provider_config`)。赌输之后能不能给出
正确引导,取决于 provider 那次的报错**恰好**被 classify 成 config/incompatible。
文案(`image_generation_model_required` → 「当前模型不能生成图片,请到设置里添加
生图模型。」)一直存在,只是没人走得到。

契约:在**没有专用生图路由**的那条分支里,
  - 非瞬时失败(配置类/找不到模型/兜底) → `image_generation_model_required`
  - 瞬时失败(限流/上游不可用/超时/额度) → 保留各自的码
后者是有意的:主模型本身能生图的用户(Gemini 那种形状)被限流时,不该被告知
"去加个模型"。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker  # noqa: E402


def _cfg(model="deepseek/deepseek-v4-flash-0731", provider="openrouter"):
    return SimpleNamespace(provider=provider, model=model)


def _run(monkeypatch, *, classified, exc_text="boom", has_route=False):
    """跑一次生图,返回它抛出的 error_code。"""
    monkeypatch.setattr(
        serve_worker.db, "model_api_image_generation_route",
        lambda _uid: ({"id": "r1", "image_generation_test_status": "ok",
                       "api_key_envelope": {"ciphertext": "x"}} if has_route else None),
    )
    monkeypatch.setattr(serve_worker.db, "model_api_active_route", lambda _uid: None)
    monkeypatch.setattr(
        serve_worker.db, "model_api_route_mark_image_generation_test",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(serve_worker.core_store, "get_store",
                        lambda _uid: SimpleNamespace(user_id=_uid))
    monkeypatch.setattr(serve_worker, "_emit_v2_debug_trace",
                        lambda *a, **k: None)
    # 有专用路由时会先去 enclave 解开凭证;本测试只关心错误码映射,
    # 把这一跳桩掉,否则所有 has_route 用例都会先炸成 auth_invalid。
    monkeypatch.setattr(serve_worker.core_enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **k: b"sk-test")   # 真实实现返回 bytes,下游会 .decode

    async def boom(*_a, **_k):
        raise RuntimeError(exc_text)

    monkeypatch.setattr(serve_worker.provider_client, "generate_image_async", boom)
    monkeypatch.setattr(serve_worker.provider_client, "classify_provider_error",
                        lambda _exc: classified)

    with pytest.raises(serve_worker.v2_worker.ImageGenerationUnavailable) as caught:
        asyncio.run(serve_worker._generate_image_for_chat(
            "usr_test", "画一张海报",
            main_provider_config=_cfg(), api_key=None, runtime_token="tok",
        ))
    return caught.value.error_code


# --------------------------------------------------------------------------- #
# 没配生图路由 —— 这是线上那一条
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("classified", [
    "provider_error",      # 兜底那一类:线上就落在这里
    "model_not_found",
    "provider_config",
    "provider_incompatible",
    "",                    # 分类不出来
])
def test_without_an_image_route_every_real_failure_says_add_a_model(monkeypatch, classified):
    code = _run(monkeypatch, classified=classified)

    assert code == "image_generation_model_required", (
        f"classified={classified!r} 时用户拿到的是 {code!r},"
        "那句话没有告诉他该干什么"
    )


@pytest.mark.parametrize("classified,expected", [
    ("rate_limited", "image_generation_rate_limited"),
    ("upstream_unavailable", "image_generation_unavailable"),
    ("turn_timeout", "image_generation_unavailable"),
    ("quota_insufficient", "image_generation_quota_insufficient"),
])
def test_transient_failures_keep_their_own_code(monkeypatch, classified, expected):
    """主模型本身能生图的用户被限流时,不该被赶去"加个模型"。"""
    assert _run(monkeypatch, classified=classified) == expected


# --------------------------------------------------------------------------- #
# 配了专用路由 —— 行为不许被上面那条改掉
# --------------------------------------------------------------------------- #

def test_with_a_dedicated_route_incompatible_still_says_incompatible(monkeypatch):
    code = _run(monkeypatch, classified="provider_incompatible", has_route=True)

    assert code == "image_generation_model_incompatible", (
        "配了生图模型却让人再去加一个,是把用户支使错方向"
    )


def test_with_a_dedicated_route_generic_failure_is_not_rewritten(monkeypatch):
    """有专用路由时,兜底失败保持原样 —— 这条分支不归本次改动管。"""
    assert _run(monkeypatch, classified="provider_error", has_route=True) == "image_generation_failed"


def test_the_guidance_copy_actually_exists_and_is_actionable():
    """错误码必须有人认领,而且那句话得说清楚"去哪做什么"。

    码存在、文案不存在的话,用户看到的就是一串裸码;文案存在但只说"失败了",
    等于没修 —— 这条同时钉住"有文案"和"文案里有动作"。
    """
    from notices import catalog

    zh = catalog.user_text_for("image_generation_model_required")

    assert zh, "image_generation_model_required 没有中文文案"
    assert "设置" in zh and ("添加" in zh or "加" in zh), (
        f"文案没告诉用户去哪、做什么:{zh!r}"
    )
