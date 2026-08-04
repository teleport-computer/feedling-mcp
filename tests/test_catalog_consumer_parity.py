"""catalog 覆盖 consumer 分类器的全部 error_class（spec Phase B / B3 一致性纪律）。

consumer 在 tools/ 不能 import backend，catalog 在 backend/——两处各自维护，
用本测试锁一致性：catalog.ERROR_CLASSES ⊇ consumer 的全部 error_class，且
同一 error_class 在两处的 blame 一字不差（同源纪律的真正锁）。

Run:  python -m pytest tests/test_catalog_consumer_parity.py -q
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

# tools/chat_resident_consumer.py 在 import 时需要一批环境变量默认值（照抄
# tests/test_consumer_error_classify.py 的既有写法，保持两处环境一致）。
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_catalog_parity_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))              # 让 tools 可 import
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))  # 让 notices 可 import

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules["content_encryption"] = _fake_enc

from notices import catalog  # noqa: E402
from notices import core  # noqa: E402
import tools.chat_resident_consumer as crc  # noqa: E402


def test_catalog_covers_all_consumer_error_classes():
    missing = set(crc.CONSUMER_ERROR_CLASSES) - set(catalog.ERROR_CLASSES)
    assert not missing, f"catalog 缺 error_class: {sorted(missing)}"


def test_every_catalog_blame_is_valid():
    for ec in catalog.ERROR_CLASSES:
        assert catalog.blame_for(ec) in core.VALID_BLAME


def test_vision_model_required_catalog_guidance_is_bilingual():
    assert catalog.user_text_for(
        "vision_model_required", language="zh-Hans"
    ) == (
        "由于当前模型没有视觉能力，模型无法收到图片信息，"
        "建议更改模型或在设置页单独添加视觉模型"
    )
    assert catalog.user_text_for(
        "vision_model_required", language="en-US"
    ) == (
        "Your current model can't process images, so it didn't receive this "
        "picture. Switch models, or add a dedicated vision model in Settings."
    )


def test_image_generation_configuration_catalog_is_actionable_and_bilingual():
    classes = {
        "image_generation_model_required",
        "image_generation_model_incompatible",
        "image_generation_auth_invalid",
        "image_generation_quota_insufficient",
        "image_generation_model_not_found",
        "image_generation_model_not_ready",
    }
    assert classes <= catalog.ERROR_CLASSES
    for error_class in classes:
        assert catalog.blame_for(error_class) == "user_provider"
        assert catalog.user_text_for(error_class, language="zh-Hans")
        assert catalog.user_text_for(error_class, language="en-US")
        assert catalog.user_text_for(
            error_class, language="en-US"
        ) != catalog.user_text_for(error_class, language="zh-Hans")


def test_resident_decrypt_maintenance_classes_are_registered_as_user_environment():
    classes = {
        "resident_decrypt_source_unavailable",
        "resident_decrypt_health_unreported",
    }
    assert classes <= catalog.ERROR_CLASSES
    for error_class in classes:
        assert catalog.blame_for(error_class) == "user_environment"
        assert catalog.user_text_for(error_class) != catalog.user_text_for("unknown")


def _consumer_blame_map() -> dict[str, str]:
    """从 consumer 的分类规则表 + classify_agent_error 硬编码分支推导
    error_class -> blame 全集（同源纪律：不重新发明，只是把已知代码路径的
    结果收集成 dict）。"""
    out = {klass: blame for klass, blame, _text, _pat in crc._ERROR_CLASS_RULES}
    # classify_agent_error 里硬编码（非规则表）的几类：
    out.setdefault("turn_timeout", "system")
    out.setdefault("provider_empty_reply", "provider_transient")
    out.setdefault("reply_parse_failed", "system")
    out.setdefault("model_not_found", "user_provider")  # 裸 404+model 分支，和规则表一致
    out.setdefault("unknown", "system")
    return out


def test_catalog_blame_matches_consumer_blame_for_every_class():
    consumer_blame = _consumer_blame_map()
    assert set(consumer_blame) == set(crc.CONSUMER_ERROR_CLASSES)
    for ec, blame in consumer_blame.items():
        assert catalog.blame_for(ec) == blame, (
            f"blame mismatch for {ec}: catalog={catalog.blame_for(ec)!r} "
            f"consumer={blame!r}")


def test_classify_upstream_mirrors_consumer_on_samples():
    """catalog.classify_upstream 是 backend 侧的 consumer 分类器正则副本；
    用代表串锁两者对同一文本给出同一 error_class（防两份正则漂移）。"""
    from notices import catalog
    from tools.chat_resident_consumer import classify_agent_error
    samples = [
        "insufficient_quota: your credit balance is too low",
        "401 invalid x-api-key",
        "429 too many requests",
        "503 overloaded, please retry",
        # Real DeepSeek image rejection observed 2026-07-30. This must stay
        # ahead of the generic "unknown variant" provider_incompatible rule.
        "provider_http_400: Failed to deserialize the JSON body into the target "
        "type: messages[0]: unknown variant `image_url`, expected `text` at line "
        "1 column 295",
        "provider_http_404: model deepseek/deepseek-v4-pro: "
        "No endpoints found that support image input",
        "400 unsupported parameter 'tool_choice'",
        "maximum context length exceeded",
        "blocked by content policy",
        # 上游下线模型名的两种真实措辞（2026-07-25 usr_a40e 回归）：两份正则必须同时认，
        # 否则「改个模型名就好」的错误在某一侧掉进 unknown/blame=system。
        "API Error: 400 The supported API model names are deepseek-v4-pro or "
        "deepseek-v4-flash, but you passed deepseek-chat",
        "404 The model `gpt-4.9-turbo` does not exist or you do not have access to it.",
        # provider_http_<code> slug shape (genesis/import worker errors): the
        # underscore defeats \b<code>\b, so the slug rules must catch these.
        "plaintext_import_failed:ProviderError:provider_http_504: <!DOCTYPE html>",
        "provider_http_402: payment page html",
        "provider_http_401: relay auth html",
        "provider_http_429: relay throttle html",
    ]
    for s in samples:
        assert catalog.classify_upstream(s) == classify_agent_error(RuntimeError(s)).error_class, s


def test_upstream_rule_patterns_match_consumer_rule_patterns():
    """结构锁（样本串锁不住的漂移）：catalog._UPSTREAM_RULES 与 consumer
    ._ERROR_CLASS_RULES 逐 error_class 比对正则 pattern 字符串 + 次序，而不是
    只测几个样本串是否命中一致——consumer 改了正则、catalog 副本漏改，只要
    两边都还命中现有样本串，test_classify_upstream_mirrors_consumer_on_samples
    测不出来，这个测试能。turn_timeout/reply_parse_failed 是 consumer 独有的
    硬编码分支（不在 _ERROR_CLASS_RULES 元组里），catalog 有意不收，排除。"""
    excluded = {"turn_timeout", "reply_parse_failed"}
    consumer_rules = [(k, pat.pattern) for k, _b, _t, pat in crc._ERROR_CLASS_RULES if k not in excluded]
    catalog_rules = [(k, pat.pattern) for k, pat in catalog._UPSTREAM_RULES if k not in excluded]
    assert catalog_rules == consumer_rules, (
        f"catalog._UPSTREAM_RULES 与 consumer._ERROR_CLASS_RULES 的正则或次序不一致:\n"
        f"catalog={catalog_rules}\nconsumer={consumer_rules}")
