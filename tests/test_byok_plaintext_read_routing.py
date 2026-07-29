"""BYOK 凭证读侧按形状路由（Phase 1 Task 1.1）。

cutover 后 TEE 主库里的 model_api_credentials.api_key_envelope 是
{body, id, owner_user_id, visibility} 明文形状（表同步工作流在复制时已解密），
没有 body_ct。读侧若无条件打 enclave，hosted 线会全线
model_api_key_decrypt_failed。
"""
from __future__ import annotations

import pytest

from core import envelope as core_envelope


def _plaintext_row(body: str = "sk-plain-123") -> dict:
    """TEE 主库形态：实测 key 集合 = body,id,owner_user_id,visibility。"""
    return {"body": body, "id": "cred-1", "owner_user_id": "usr_x",
            "visibility": "shared"}


def _envelope_row(body_ct: str = "CIPHER") -> dict:
    """RDS 现状形态：双收件人信封。"""
    return {"body_ct": body_ct, "nonce": "n", "K_user": "ku", "K_enclave": "ke",
            "id": "cred-1", "owner_user_id": "usr_x", "visibility": "shared",
            "v": 1}


def test_plaintext_row_is_read_locally_without_touching_enclave(monkeypatch):
    """明文行必须本地直读——不得打 enclave（cutover 后 enclave 可能已不在读路径上）。"""
    called = []
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: called.append(1) or b"WRONG")

    out = core_envelope.decrypt_provider_key_envelope(_plaintext_row(), "api-key")

    assert out == b"sk-plain-123"
    assert called == [], "明文行不应触发任何 enclave 调用"


def test_envelope_row_still_goes_through_enclave(monkeypatch):
    """信封行维持现状路径，且 purpose 必须仍是 model_api_provider_key。"""
    seen = {}

    def fake(envelope, api_key, *, purpose, runtime_token=""):
        seen.update(envelope=envelope, api_key=api_key, purpose=purpose,
                    runtime_token=runtime_token)
        return b"sk-from-enclave"

    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave", fake)

    out = core_envelope.decrypt_provider_key_envelope(
        _envelope_row(), "api-key", runtime_token="rt-1")

    assert out == b"sk-from-enclave"
    assert seen["purpose"] == "model_api_provider_key"
    assert seen["runtime_token"] == "rt-1"


def test_absent_runtime_token_is_not_forwarded(monkeypatch):
    """没有 runtime_token 时**不得**把 runtime_token="" 传下去。

    原调用点用 `**decrypt_kwargs`（`{"runtime_token": rt} if rt else {}`），
    注释写明「Only pass runtime_token through when present, so api-key callers
    are unchanged」。helper 若无脑透传空串，api-key 调用方的入参就变了——
    tests/test_model_api_profiles_config_store.py 正是断言这一点。
    """
    seen_kwargs = {}

    def fake(envelope, api_key, *, purpose, **kwargs):
        seen_kwargs.update(kwargs)
        return b"sk-from-enclave"

    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave", fake)

    core_envelope.decrypt_provider_key_envelope(_envelope_row(), "api-key")

    assert seen_kwargs == {}, "空 runtime_token 不应出现在下游入参里"


def test_unrecognized_shape_raises(monkeypatch):
    """既无 body_ct 也无 body：必须显式报错，不能静默返回空 key。

    静默返回空会让上游拿着空 key 去打 provider，错误信息变成 provider 侧的
    401，排查时完全看不出根因在这里。
    """
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: b"SHOULD-NOT-BE-CALLED")

    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        core_envelope.decrypt_provider_key_envelope({"id": "cred-1"}, "api-key")


def test_body_ct_wins_when_both_present(monkeypatch):
    """两个字段同时存在（迁移中间态）时以 body_ct 为准——密文是真源。

    反过来优先明文会在「已写新密文、旧明文残留」的窗口里读到过期的 key。
    """
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: b"sk-from-enclave")
    row = _envelope_row()
    row["body"] = "sk-stale-plaintext"

    assert core_envelope.decrypt_provider_key_envelope(row, "k") == b"sk-from-enclave"


def test_non_string_body_is_rejected(monkeypatch):
    """body 不是字符串（脏数据）时按无法识别处理，不要 str() 硬转。"""
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: b"SHOULD-NOT-BE-CALLED")

    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        core_envelope.decrypt_provider_key_envelope({"body": {"nested": 1}}, "k")


def test_supervisor_reads_plaintext_row_without_http_call(monkeypatch):
    """supervisor 遇到明文行必须短路，不发 HTTP。

    它发出去只会拿回 enclave 的 decrypt_failed: envelope missing body_ct
    （与 2026-07-28 verify 瘫痪同因），然后 _decrypt_provider_key 吞掉异常返回
    空串 —— 空 key 会一路带到 provider 那边变成 401，根因彻底看不见。
    """
    from agent_runtime import supervisor

    posted = []
    monkeypatch.setattr(supervisor._ENCLAVE_HTTP, "post",
                        lambda *a, **kw: posted.append(1))

    got = supervisor._decrypt_provider_key(
        "http://enclave.invalid", "api-key", _plaintext_row("sk-plain-9"))

    assert got == "sk-plain-9"
    assert posted == [], "明文行不应发出任何 enclave HTTP 请求"


def test_genesis_worker_reads_plaintext_row_without_http_call(monkeypatch):
    """genesis worker 的 provider key 取用同样要短路。

    它走自己的 _decrypt_envelope(HTTP)，不在 core.envelope 的路由范围内——
    细案初稿漏了这处，靠 grep 'model_api_provider_key' 全仓重扫才发现。
    """
    from genesis import worker as genesis_worker

    called = []
    monkeypatch.setattr(genesis_worker, "_decrypt_envelope",
                        lambda *a, **kw: called.append(1) or b"WRONG")

    got = genesis_worker._provider_key_from_envelope(
        _plaintext_row("sk-plain-7"), enclave_url="http://enclave.invalid",
        runtime_token="rt", store=None, job_id="job-1")

    assert got == "sk-plain-7"
    assert called == [], "明文行不应触发 _decrypt_envelope"


def test_no_unrouted_provider_key_decrypt_sites_remain():
    """守卫：读侧不得再直接调 _decrypt_envelope_via_enclave 解 provider key。

    新增一处「无形状路由」的解密点，cutover 后就是一条静默的
    model_api_key_decrypt_failed。这条守卫让它在 CI 就红。
    允许的例外只有 core/envelope.py 自己（路由函数内部）与 core/enclave.py（实现）。
    """
    import pathlib
    import re

    backend = pathlib.Path(__file__).parent.parent / "backend"
    offenders = []
    pat = re.compile(r"_decrypt_envelope_via_enclave\s*\(")
    for f in sorted(backend.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        if f.relative_to(backend).as_posix() in {"core/envelope.py", "core/enclave.py"}:
            continue
        src = f.read_text(encoding="utf-8")
        if not pat.search(src):
            continue
        # 只关心 provider key 这一类
        if "model_api_provider_key" in src:
            offenders.append(f.relative_to(backend).as_posix())
    assert not offenders, (
        "以下文件仍直接调 _decrypt_envelope_via_enclave 解 provider key，"
        "cutover 后遇到 TEE 的明文行会静默失败：\n  " + "\n  ".join(offenders)
        + "\n改调 core.envelope.decrypt_provider_key_envelope（按行形状路由）。"
    )
