"""一等偏好 content_encryption（Phase 2 Task 2.1）。

只验证「偏好被正确存取」，不涉及任何加解密格式路由——那是 Task 2.2/2.3。
偏好交付后暂时没有消费者，这是有意的：让路由改造能独立评审。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from accounts import registry  # noqa: E402
from conftest import seed_user  # noqa: E402


@pytest.fixture()
def uid(backend_env):
    """建一个真用户：seed_user 同时写 users 行与内存 registry，
    registry 的 helper 只认在册用户。"""
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(user_id, api_key_hash="h", doc={})
    return user_id


def test_unset_preference_reads_as_off(uid):
    """用户存在但未设 → "off"（v6 默认明文），**不是** None。

    None 必须专门留给「用户查不到记录」这一态，见下一条测试。
    """
    assert registry._get_user_content_encryption(uid) == "off"


def test_unknown_user_reads_as_none_not_off(backend_env):
    """用户查不到记录 → None，调用方必须 fail-safe 走加密。

    这是写侧的安全边界：若与「未设偏好」一样返回 "off"，任何 registry 未命中
    （新用户竞态、缓存未加载、纯单元测试用户）都会静默降级成明文写入。
    实证样本：test_v2_encrypted_effect_payload 的 u_effect_real_crypto 就是这类
    ——不在 registry 里、只 monkeypatch 了公钥。
    """
    assert registry._get_user_content_encryption("usr_definitely_not_here") is None


def test_set_on_then_read_back(uid):
    assert registry._set_user_content_encryption(uid, "on") is True
    assert registry._get_user_content_encryption(uid) == "on"


def test_set_off_then_read_back(uid):
    assert registry._set_user_content_encryption(uid, "off") is True
    assert registry._get_user_content_encryption(uid) == "off"


def test_value_is_normalized(uid):
    """大小写与空格归一——iOS/io_cli 传 "ON " 不该产生第三种取值。"""
    assert registry._set_user_content_encryption(uid, " ON ") is True
    assert registry._get_user_content_encryption(uid) == "on"


def test_invalid_value_rejected_and_not_written(uid):
    """非法值必须拒绝且不落库：写进去会让下游路由拿到无法判定的第三态。"""
    registry._set_user_content_encryption(uid, "on")
    assert registry._set_user_content_encryption(uid, "maybe") is False
    assert registry._get_user_content_encryption(uid) == "on", "非法写入不得覆盖原值"


def test_empty_value_clears(uid):
    """清除偏好后回到默认档 "off"——用户仍然存在，所以不是 None。

    None 只表示「查不到这个用户」，见 test_unknown_user_reads_as_none_not_off。
    """
    registry._set_user_content_encryption(uid, "on")
    assert registry._set_user_content_encryption(uid, "") is True
    assert registry._get_user_content_encryption(uid) == "off"


def test_unknown_user_returns_false(backend_env):
    assert registry._set_user_content_encryption("usr_nope_nope_nope", "on") is False


def _whoami(uid):
    from accounts import whoami_core
    from core import store as core_store
    return whoami_core.whoami_payload(core_store.UserStore(uid))


def test_whoami_reports_off_by_default(uid):
    """未设置时必须明确下发 "off"，而不是省略该字段。

    省略会让各客户端自己猜默认值；v6 的默认是明文，必须由服务端讲清楚。
    """
    assert _whoami(uid)["content_encryption"] == "off"


def test_whoami_reports_on_when_enabled(uid):
    registry._set_user_content_encryption(uid, "on")
    assert _whoami(uid)["content_encryption"] == "on"


def test_whoami_still_emits_enclave_public_key_for_plaintext_users(uid):
    """⚠️ Phase 2 阶段明文档用户仍要拿到 enclave_content_public_key_hex。

    现役 iOS 用它封双收件人信封，这是它唯一的写入路径。主计划原文说「按偏好
    决定是否下发」，但 Phase 3（iOS 开关发版）之前那样做 = 现役 App 全量写入
    中断。停发推迟到 iOS 发版且旧版本淘汰之后。
    """
    body = _whoami(uid)
    assert body["content_encryption"] == "off"
    # 该字段依赖 enclave 可达；拿不到 enclave 信息时本来就不会下发，
    # 故只在存在时断言「没有因为明文档而被刻意摘掉」。
    if "enclave_content_public_key_hex" in body:
        assert body["enclave_content_public_key_hex"]


def _prefs(uid, payload):
    from accounts import accounts_core
    from core import store as core_store
    return accounts_core.users_set_preferences(core_store.UserStore(uid), payload)


def test_prefs_sets_and_returns_preference(uid):
    body, status = _prefs(uid, {"content_encryption": "on"})
    assert status == 200, body
    assert body["content_encryption"] == "on"
    assert registry._get_user_content_encryption(uid) == "on"


def test_prefs_alone_is_accepted(uid):
    """只传 content_encryption 必须被接受。

    现有入口有 `if not has_lang and not has_tz: return 400`——不把新字段加进这个
    判断，客户端就只能靠捎带一个 archive_language 才能改加密开关。
    """
    body, status = _prefs(uid, {"content_encryption": "off"})
    assert status == 200, body


def test_prefs_rejects_invalid_value(uid):
    _prefs(uid, {"content_encryption": "on"})
    body, status = _prefs(uid, {"content_encryption": "maybe"})
    assert status == 400
    assert registry._get_user_content_encryption(uid) == "on", "非法请求不得改动原值"


def test_prefs_rejects_non_string(uid):
    body, status = _prefs(uid, {"content_encryption": 123})
    assert status == 400


def test_unchanged_value_is_a_noop(uid, monkeypatch):
    """值未变必须 early return，不得触发 persist_user。

    persist_user = users 行 upsert + TEE mirror + 跨 worker 广播，每个 worker
    收到广播会**整表重载**。timezone 正是在这里踩过重载风暴
    （memory users-reload-storm-resident-heartbeat）。
    """
    registry._set_user_content_encryption(uid, "on")

    calls = []
    monkeypatch.setattr(registry, "persist_user", lambda u: calls.append(1))

    assert registry._set_user_content_encryption(uid, "on") is True
    assert calls == [], "值未变时不应 persist（会引发全表重载风暴）"

    assert registry._set_user_content_encryption(uid, "off") is True
    assert len(calls) == 1, "值真的变了才 persist 一次"


# ---------------------------------------------------------------------------
# 生效值（effective）——供客户端**写侧**使用的那一个
#
# 偏好本身是「用户意图」，可以随时被设成 off；但服务端在 Task 2.2 完成前
# **仍然拒收明文写入**（客户端写闸硬校验 K_enclave，见 worldbook_core.
# _validate_envelope:51）。若 iOS 直接按意图走明文，全量写入会 400。
#
# 故 whoami 同时下发两个字段：
#   content_encryption            = 用户意图（设置页开关绑这个）
#   content_encryption_effective  = 客户端写侧必须遵守的形状
#
# 这样 iOS 与后端的发版顺序互不依赖：iOS 先发版也不会写坏，等后端把
# PLAINTEXT_WRITES_ACCEPTED 翻成 True 才真正开始产生明文行。


def test_effective_is_on_while_plaintext_writes_are_rejected(uid, monkeypatch):
    """服务端还不收明文时，即便用户意图 off，生效值也必须是 "on"。"""
    from core import envelope as core_envelope

    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", False)

    registry._set_user_content_encryption(uid, "off")

    assert registry.effective_content_encryption(uid) == "on"


def test_plaintext_write_gate_defaults_closed():
    """Only the exact deployment value 1 opens the fail-safe gate."""
    from core import envelope as core_envelope

    key = "FEEDLING_PLAINTEXT_WRITES_ACCEPTED"
    assert core_envelope._plaintext_writes_accepted({}) is False
    assert core_envelope._plaintext_writes_accepted({key: ""}) is False
    assert core_envelope._plaintext_writes_accepted({key: "true"}) is False
    assert core_envelope._plaintext_writes_accepted({key: "0"}) is False
    assert core_envelope._plaintext_writes_accepted({key: " 1 "}) is True


def test_pre_processes_share_the_plaintext_gate_and_other_envs_stay_closed():
    """Every Pre reply writer must agree with whoami; test/prod stay closed."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    key = "FEEDLING_PLAINTEXT_WRITES_ACCEPTED"
    pre = (root / "deploy/docker-compose.phala.pre.yaml").read_text()
    pre_runner = (root / "deploy/docker-compose.phala.pre.runner.yaml").read_text()
    test = (root / "deploy/docker-compose.phala.test.yaml").read_text()
    prod = (root / "deploy/docker-compose.phala.yaml").read_text()

    pre_compose = yaml.safe_load(pre)
    for service in ("backend", "serve-worker"):
        assert pre_compose["services"][service]["environment"][key] == "1"
    assert pre_compose["services"]["backend"]["environment"][
        "FEEDLING_EXPECTED_RUNNER_COUNT"
    ] == "1"
    runner_compose = yaml.safe_load(pre_runner)
    assert runner_compose["x-agent-runner-env"][key] == "1"
    assert key not in test
    assert key not in prod


def test_effective_follows_intent_once_plaintext_writes_are_accepted(uid, monkeypatch):
    """后端开闸后，off 意图才真正生效成明文。"""
    from core import envelope as core_envelope

    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", True)
    registry._set_user_content_encryption(uid, "off")

    assert registry.effective_content_encryption(uid) == "off"


def test_effective_stays_on_for_opted_in_user_even_after_gate_opens(uid, monkeypatch):
    """开闸不影响显式选择加密的用户。"""
    from core import envelope as core_envelope

    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", True)
    registry._set_user_content_encryption(uid, "on")

    assert registry.effective_content_encryption(uid) == "on"


def test_effective_is_on_for_unknown_user(backend_env, monkeypatch):
    """查不到用户 → 加密。与 _get_user_content_encryption 的 fail-safe 一致。"""
    from core import envelope as core_envelope

    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", True)

    assert registry.effective_content_encryption("usr_definitely_not_here") == "on"
