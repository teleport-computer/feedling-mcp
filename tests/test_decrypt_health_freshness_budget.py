"""闲置 consumer 的 decrypt-health 必须始终在后端的新鲜度窗口内。

这两个常量分属两个组件，靠注释耦合（consumer 侧写着「kept well under the backend
freshness window」），没有任何东西检查它。踩空的后果不是报错而是**沉默降级**：

    consumer 侧 DECRYPT_HEALTH_REFRESH_SEC 探活间隔
      ↓ 超过
    后端 _DECRYPT_HEALTH_RECENT_SEC(300s) → status 落 "unknown"
      → reason=decrypt_health_stale → decrypt_source_ready=False
      → bootstrap 卡住 / verify 409

预算不只是探活间隔本身：探活挂在**长轮询超时的空闲分支**上，所以最坏情况还要加一整个
轮询周期（POLL_TIMEOUT + 请求耗时）才轮得到下一次。2026-07-22 prod 实测长轮询往返
约 33s（timeout=30 + 后端在负载下的响应时间），所以这里按两个 POLL_TIMEOUT 留裕度。

调大探活间隔是有价值的（每次探活并不便宜：必然 miss 的 whoami 往返 + 200 条记忆拉取），
本测试的作用是**给"调多大"设一个上界**，别再靠人肉记住 300 这个数。

Run:  python -m pytest tests/test_decrypt_health_freshness_budget.py -q
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402
from chat import consumer as backend_consumer  # noqa: E402

# 一次空闲轮询往返的裕度：探活只在长轮询超时后才有机会跑。
_IDLE_CYCLE_ALLOWANCE = 2


def _budget() -> float:
    return backend_consumer._DECRYPT_HEALTH_RECENT_SEC - (
        _IDLE_CYCLE_ALLOWANCE * crc.POLL_TIMEOUT
    )


def test_idle_probe_interval_fits_the_backend_freshness_window():
    assert crc.DECRYPT_HEALTH_REFRESH_SEC <= _budget(), (
        f"探活间隔 {crc.DECRYPT_HEALTH_REFRESH_SEC}s 超出预算 {_budget()}s "
        f"(后端窗口 {backend_consumer._DECRYPT_HEALTH_RECENT_SEC}s − "
        f"{_IDLE_CYCLE_ALLOWANCE}×轮询 {crc.POLL_TIMEOUT}s)；"
        "闲置用户的 decrypt_health 会落 unknown，卡住 bootstrap / verify"
    )


def test_shared_reuse_grace_fits_the_backend_freshness_window():
    """shared 模式下 consumer 复用一份共享读数、钉住它的 checked_at 直到它 REFRESH_SEC
    旧（reuse grace == REFRESH_SEC，无 jitter），所以最坏上报年龄仍是 REFRESH_SEC + 一个
    空闲轮询——和上面的探活间隔预算是同一个约束。这条显式守住「reuse grace 就是
    REFRESH_SEC」这一不变量：若哪天有人重新给复用宽限加了 jitter/常量而没同步进预算，
    这里连同上面的间隔预算一起把它挡红。"""
    reuse_grace = crc.DECRYPT_HEALTH_REFRESH_SEC   # no jitter added on top
    assert reuse_grace <= _budget(), (
        f"reuse grace {reuse_grace}s 超出预算 {_budget()}s；复用中的 checked_at 会漂过"
        "后端 300s 窗口，健康用户被判 unknown、onboarding 反复抖动"
    )


def test_the_budget_is_actually_binding():
    """守卫这个守卫：预算必须真的能拒绝一个过大的值。

    没有这条，把预算写成一个大到没意义的数、或常量改名后静默失效，上面那条
    也会照样绿。
    """
    assert _budget() < backend_consumer._DECRYPT_HEALTH_RECENT_SEC, (
        "预算没有为轮询周期留任何裕度"
    )
    over_budget = _budget() + 1
    assert not (over_budget <= _budget()), "预算不再具有约束力"
