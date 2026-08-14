"""存储 port：能力声明、正确性前置条件、CAS 协议。

守三件事（后两条是 codex code_review 2026-08-14 纠正出来的）：

  1. 每一项可降级能力的缺失都产出带代价说明的记录 —— 不允许静默降级。
  2. **正确性前置条件不可降级** —— 缺 supersede / atomic_batch 时相应操作
     必须被拒绝，而不是「退化后继续」。
  3. 能力声明**必须显式** —— 没有默认值，适配器漏写会直接报错，
     而不是被当成「全支持」。
"""
from __future__ import annotations

import pytest

from memory_garden.storage import (
    CORRECTNESS_CRITICAL,
    FULL_CAPABILITIES,
    ApplyResult,
    Capabilities,
    Degradation,
    Snapshot,
    StoragePort,
    UnsupportedOperation,
    describe_capabilities,
    describe_for_user,
    ensure_supported,
    missing_critical,
    plan_degradations,
)


# --------------------------------------------------------------------------- #
# 能力声明必须显式
# --------------------------------------------------------------------------- #


def test_capabilities_have_no_defaults():
    """漏写任何一项都应该报错 —— 默认全 True 是 fail-open，错在最危险的方向。"""
    with pytest.raises(TypeError):
        Capabilities()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Capabilities(supports_supersede=True)  # type: ignore[call-arg]


def test_io_uses_full_capabilities():
    assert plan_degradations(FULL_CAPABILITIES) == []
    assert missing_critical(FULL_CAPABILITIES) == []


def test_capabilities_are_immutable():
    with pytest.raises(Exception):
        FULL_CAPABILITIES.supports_supersede = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 正确性前置条件：不可降级
# --------------------------------------------------------------------------- #


def test_critical_capabilities_are_the_two_data_integrity_ones():
    assert CORRECTNESS_CRITICAL == {"supports_supersede", "supports_atomic_batch"}


@pytest.mark.parametrize("capability", sorted(CORRECTNESS_CRITICAL))
def test_missing_critical_capability_is_not_a_degradation(capability):
    """缺前置条件时**不能**出现在降级列表里 —— 那会读成「降级后继续跑」。"""
    caps = Capabilities(
        supports_supersede=capability != "supports_supersede",
        supports_atomic_batch=capability != "supports_atomic_batch",
        supports_custom_fields=True,
        supports_metadata_sort=True,
    )
    assert plan_degradations(caps) == []
    assert missing_critical(caps) == [capability]


@pytest.mark.parametrize("capability", sorted(CORRECTNESS_CRITICAL))
def test_operation_needing_missing_capability_is_rejected(capability):
    caps = Capabilities(
        supports_supersede=capability != "supports_supersede",
        supports_atomic_batch=capability != "supports_atomic_batch",
        supports_custom_fields=True,
        supports_metadata_sort=True,
    )
    with pytest.raises(UnsupportedOperation) as excinfo:
        ensure_supported(caps, operation="dream.consolidate", requires=capability)
    assert excinfo.value.capability == capability
    assert excinfo.value.why, "必须说明为什么不能降级"


def test_supported_operation_passes_silently():
    ensure_supported(
        FULL_CAPABILITIES, operation="dream.consolidate", requires="supports_supersede"
    )


def test_unsupported_operation_is_a_runtimeerror():
    assert issubclass(UnsupportedOperation, RuntimeError)


# --------------------------------------------------------------------------- #
# 可降级能力：必须显式上报
# --------------------------------------------------------------------------- #


def test_degradable_capabilities_report_fallback_and_cost():
    caps = Capabilities(
        supports_supersede=True,
        supports_atomic_batch=True,
        supports_custom_fields=False,
        supports_metadata_sort=False,
    )
    degradations = plan_degradations(caps)
    assert len(degradations) == 2
    for d in degradations:
        assert isinstance(d, Degradation)
        assert d.fallback.strip(), f"{d.capability} 缺 fallback"
        assert d.cost.strip(), f"{d.capability} 缺 cost —— 这是防静默降级的关键字段"


def test_describe_mentions_both_kinds_separately():
    caps = Capabilities(
        supports_supersede=False,
        supports_atomic_batch=True,
        supports_custom_fields=False,
        supports_metadata_sort=True,
    )
    text = describe_capabilities(caps)
    assert "会被拒绝" in text, "前置条件缺失要说清是拒绝，不是降级"
    assert "已降级运行" in text
    assert "supports_supersede" in text
    assert "supports_custom_fields" in text
    assert "supports_metadata_sort" not in text


def test_describe_says_nothing_wrong_when_full():
    assert "支持全部能力" in describe_capabilities(FULL_CAPABILITIES)


# --------------------------------------------------------------------------- #
# 给接入方看的说明
# --------------------------------------------------------------------------- #


def test_user_facing_text_is_empty_when_nothing_is_lost():
    assert describe_for_user(FULL_CAPABILITIES) == []


def test_user_facing_text_covers_every_gap():
    caps = Capabilities(
        supports_supersede=False,
        supports_atomic_batch=False,
        supports_custom_fields=False,
        supports_metadata_sort=False,
    )
    lines = describe_for_user(caps)
    assert len(lines) == 4, "每一项缺失都要有一句给用户的说明"


def test_user_facing_text_has_no_field_names():
    """接入方看的是「你会失去什么」，不是内部字段名。"""
    caps = Capabilities(False, False, False, False)
    for line in describe_for_user(caps):
        assert "supports_" not in line, f"话术里漏了字段名: {line}"
        assert line.strip(), "不能有空话术"


def test_user_facing_text_says_what_is_lost_not_just_what_is_missing():
    """每句都要落到后果上，否则用户看了也不知道该不该在意。"""
    caps = Capabilities(
        supports_supersede=True,
        supports_atomic_batch=True,
        supports_custom_fields=True,
        supports_metadata_sort=False,
    )
    (line,) = describe_for_user(caps)
    assert "变慢" in line, "只说「不支持排序」没用，要说清用户会感觉到什么"


# --------------------------------------------------------------------------- #
# CAS 协议闭合
# --------------------------------------------------------------------------- #


def test_snapshot_carries_a_revision():
    """load 必须给出 CAS 凭据，否则调用方无从合法地传 expected_revision。"""
    snap = Snapshot(envelopes=[{"id": "m_1"}], revision="rev-7")
    assert snap.envelopes and snap.revision == "rev-7"


def test_apply_result_returns_the_new_revision():
    """写完要给新版本号，调用方才能接着做下一次 CAS 而不必重读。"""
    res = ApplyResult(results=[{"id": "m_1", "status": "written"}], revision="rev-8")
    assert res.revision == "rev-8"


def test_apply_result_defaults_are_empty_not_none():
    res = ApplyResult()
    assert res.results == []


# --------------------------------------------------------------------------- #
# Port 是结构化协议
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    def capabilities(self) -> Capabilities:
        return FULL_CAPABILITIES

    def load(self, tenant: str, **filters) -> Snapshot:
        return Snapshot(envelopes=[], revision="rev-0")

    def apply(self, tenant, mutations, *, idempotency_key, expected_revision) -> ApplyResult:
        return ApplyResult(results=[], revision="rev-1")


def test_structural_conformance():
    assert isinstance(_FakeAdapter(), StoragePort)


def test_adapter_missing_apply_is_not_a_port():
    class MissingApply:
        def capabilities(self) -> Capabilities:
            return FULL_CAPABILITIES

        def load(self, tenant: str, **filters) -> Snapshot:
            return Snapshot(envelopes=[], revision=None)

    assert not isinstance(MissingApply(), StoragePort)
