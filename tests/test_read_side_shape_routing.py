"""读侧按行形状路由（Phase 2 Task 2.3）。

写侧的对偶：明文行**本地直读、绝不打 enclave**，信封行仍走
`_decrypt_envelope_via_enclave`。这不是删 enclave——加密档用户的读路径完全不变。

`read_envelope_body` 是 Task 1.1 的 `decrypt_provider_key_envelope` 的泛化：
唯一区别是 purpose 可传。后者保留为薄 wrapper，所以 1.1 的调用点逐字不变。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core import envelope as core_envelope  # noqa: E402


@pytest.fixture()
def spy(monkeypatch):
    """记录 enclave 是否被调用，以及用什么参数。"""
    calls = []

    def fake(envelope, api_key, *, purpose, **kw):
        calls.append({"envelope": envelope, "api_key": api_key,
                      "purpose": purpose, **kw})
        return b"decrypted"

    monkeypatch.setattr(core_envelope.enclave,
                        "_decrypt_envelope_via_enclave", fake)
    return calls


def test_sealed_row_goes_through_enclave_with_purpose(spy):
    env = {"body_ct": "Y3Q=", "nonce": "bm9uY2U=", "K_user": "a3U="}

    out = core_envelope.read_envelope_body(env, "ak", purpose="memory_read")

    assert out == b"decrypted"
    assert len(spy) == 1 and spy[0]["purpose"] == "memory_read"


def test_plaintext_row_never_touches_enclave(spy):
    """v6 的核心兑现点：明文档的读路径不依赖 enclave，故 enclave 故障不连坐。"""
    out = core_envelope.read_envelope_body(
        {"body": "hello", "id": "i", "owner_user_id": "u"}, "ak",
        purpose="memory_read")

    assert out == b"hello"
    assert spy == [], "明文行不应打 enclave"


def test_body_ct_wins_when_both_present(spy):
    """迁移中间态密文是真源；反过来会读到过期的明文残留。"""
    out = core_envelope.read_envelope_body(
        {"body_ct": "Y3Q=", "body": "stale"}, "ak", purpose="p")

    assert out == b"decrypted" and len(spy) == 1


def test_runtime_token_is_only_forwarded_when_present(spy):
    """api-key 调用方的下游入参必须逐字不变——无脑传空串会打破既有契约
    （Task 1.1 就是这样造出过一个回归）。"""
    core_envelope.read_envelope_body({"body_ct": "x"}, "ak", purpose="p")
    assert "runtime_token" not in spy[0]

    core_envelope.read_envelope_body({"body_ct": "x"}, None, purpose="p",
                                     runtime_token="rt")
    assert spy[1]["runtime_token"] == "rt"


def test_unrecognized_shape_raises(spy):
    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        core_envelope.read_envelope_body({"id": "i"}, "ak", purpose="p")
    assert spy == []


def test_non_dict_raises(spy):
    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        core_envelope.read_envelope_body(None, "ak", purpose="p")


def test_empty_plaintext_body_reads_as_empty_bytes(spy):
    """空正文是合法内容。真值判会把它当成「形状不认识」而抛错。"""
    assert core_envelope.read_envelope_body(
        {"body": ""}, "ak", purpose="p") == b""
    assert spy == []


def test_provider_key_wrapper_pins_its_purpose(spy):
    """Task 1.1 的 wrapper 保留，purpose 钉死——它的调用点不该因泛化而改动。"""
    core_envelope.decrypt_provider_key_envelope({"body_ct": "x"}, "ak")
    assert spy[0]["purpose"] == "model_api_provider_key"
