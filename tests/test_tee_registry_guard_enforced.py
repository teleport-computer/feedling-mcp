"""元守卫：保证注册表守卫在 CI 上真的跑了。

tests/conftest.py 在连不上 Postgres 时会 collect_ignore 掉所有非纯单元的测试
模块，且零 skipped 计数——本仓库有过"391 passed 全绿"其实少跑约 2000 个用例的
先例（memory pytest-silently-skips-db-modules-without-pg）。

test_tee_table_registry.py 需要 PG，因此在无 PG 时会被静默忽略。本文件不需要
PG，所以永远会被收集；它断言"在 CI 上 PG 必须可用"，把静默跳过变成红灯。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import conftest  # noqa: E402  — tests/conftest.py，取其 provisioning 结果


def test_guard_module_exists():
    """守卫文件不能被误删——删了它，漏登记的表就再也没人拦。"""
    guard = Path(__file__).parent / "test_tee_table_registry.py"
    assert guard.is_file(), "注册表守卫文件不存在；它是 TEE 同步机制的唯一拦截点"


def test_postgres_is_provisioned_on_ci():
    """CI 上没有 PG = 注册表守卫被静默跳过 = 机制失效。直接红。

    本地开发机没起 PG 时不红（只是那趟跑不到守卫），但 CI 必须硬性保证。
    """
    if os.environ.get("CI", "").lower() not in ("1", "true"):
        return
    assert conftest._provisioned, (
        "CI 上 Postgres 未就绪，TEE 注册表守卫（及约 2000 个 DB 用例）被静默跳过。"
        f"provisioning 错误：{conftest._PROVISION_ERROR!r}"
    )
