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


def test_unset_preference_reads_as_none(uid):
    assert registry._get_user_content_encryption(uid) is None


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
    registry._set_user_content_encryption(uid, "on")
    assert registry._set_user_content_encryption(uid, "") is True
    assert registry._get_user_content_encryption(uid) is None


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
