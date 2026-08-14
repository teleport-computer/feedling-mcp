"""存储 port：能力声明 + 显式降级。

守两件事：
  1. 每一项缺失的能力都产出一条**带后果说明**的记录 —— 不允许静默降级。
  2. IO 自己的后端（全能力）不受这套机制影响，降级列表为空。
"""
from __future__ import annotations

import pytest

from memory_garden.storage import (
    Capabilities,
    Degradation,
    StoragePort,
    describe_degradations,
    plan_degradations,
)


def test_default_capabilities_are_all_supported():
    """默认全支持 —— IO 自己的适配器不需要显式声明每一项。"""
    caps = Capabilities()
    assert plan_degradations(caps) == []


def test_missing_supersede_degrades_explicitly():
    caps = Capabilities(supports_supersede=False)
    degradations = plan_degradations(caps)
    assert len(degradations) == 1
    d = degradations[0]
    assert isinstance(d, Degradation)
    assert d.capability == "supports_supersede"
    assert d.fallback, "必须写明降级成什么"
    assert d.risk, "必须写明后果 —— 这是防静默降级的关键字段"
    assert "不硬删" in d.risk


def test_every_missing_capability_is_reported():
    caps = Capabilities(False, False, False, False)
    degradations = plan_degradations(caps)
    assert len(degradations) == 4
    reported = {d.capability for d in degradations}
    assert reported == {
        "supports_supersede",
        "supports_atomic_batch",
        "supports_custom_fields",
        "supports_metadata_sort",
    }


def test_every_degradation_carries_fallback_and_risk():
    """没有一条降级可以只说「不支持」而不说后果。"""
    for d in plan_degradations(Capabilities(False, False, False, False)):
        assert d.fallback.strip(), f"{d.capability} 缺 fallback"
        assert d.risk.strip(), f"{d.capability} 缺 risk"


def test_describe_is_readable_and_mentions_every_gap():
    caps = Capabilities(supports_atomic_batch=False, supports_metadata_sort=False)
    text = describe_degradations(plan_degradations(caps))
    assert "supports_atomic_batch" in text
    assert "supports_metadata_sort" in text
    assert "supports_supersede" not in text


def test_describe_says_nothing_wrong_when_full():
    assert "无降级" in describe_degradations(plan_degradations(Capabilities()))


def test_capabilities_are_immutable():
    caps = Capabilities()
    with pytest.raises(Exception):
        caps.supports_supersede = False  # type: ignore[misc]


def test_storage_port_is_structural():
    """StoragePort 是结构化协议：鸭子类型对上就算实现，不要求继承。"""

    class FakeAdapter:
        def capabilities(self) -> Capabilities:
            return Capabilities()

        def load(self, tenant: str, **filters):
            return []

        def apply(self, tenant, mutations, *, idempotency_key, expected_revision=None):
            return []

    assert isinstance(FakeAdapter(), StoragePort)


def test_incomplete_adapter_is_not_a_storage_port():
    class MissingApply:
        def capabilities(self) -> Capabilities:
            return Capabilities()

        def load(self, tenant: str, **filters):
            return []

    assert not isinstance(MissingApply(), StoragePort)
