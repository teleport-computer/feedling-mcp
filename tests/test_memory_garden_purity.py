"""内核纯度守卫：``memory_garden`` 包不得 import 任何 io 模块。

这是《Memory Garden 内核提取》验收标准第 ② 条的自动化。一旦包里出现
``import db`` 或 ``import identity.user_naming``，「内核可独立发布 / 可被替换」
这条就塌了 —— 用测试钉死，不靠人盯。

两条判据都刻意做成**结构判据**（AST 里有没有这个 import），不判语义、
不判风格：误伤为零，也不需要随实现漂移而维护。
"""
from __future__ import annotations

import ast
import importlib
import pathlib

# io 侧的顶层模块名。内核出现任何一个都是硬伤。
_FORBIDDEN_ROOTS = frozenset({
    "db",
    "identity",
    "accounts",
    "bootstrap",
    "enclave",
    "debug_trace",
    "hosted_runtime",
    "provider_client",
    "core",     # core.store / core.enclave 等；被搬进包的纯模块应改相对引用
    "memory",   # 老的 memory 包；包内引用一律走相对 import
})

_KERNEL_ROOT = pathlib.Path(__file__).resolve().parents[1] / "backend" / "memory_garden"


def _kernel_files() -> list[pathlib.Path]:
    return sorted(p for p in _KERNEL_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(path: pathlib.Path) -> set[str]:
    """这个文件 import 了哪些顶层模块名。相对 import（包内引用）不计。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . import x / from ..y import z —— 包内，放行
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_kernel_package_is_not_empty():
    files = _kernel_files()
    assert files, f"memory_garden 包为空或不存在：{_KERNEL_ROOT}"


def test_kernel_imports_no_io():
    offenders: list[str] = []
    for path in _kernel_files():
        rel = path.relative_to(_KERNEL_ROOT)
        for root in sorted(_imported_roots(path)):
            if root in _FORBIDDEN_ROOTS:
                offenders.append(f"{rel}: import {root}")
    assert not offenders, (
        "内核里出现了 io 依赖（包内引用请改成相对 import）:\n  "
        + "\n  ".join(offenders)
    )


def test_kernel_modules_import_without_side_effects():
    """逐个 import 一遍：任何异常都说明有副作用、漏依赖或相对引用写错。"""
    failures: list[str] = []
    for path in _kernel_files():
        rel = path.relative_to(_KERNEL_ROOT.parent).with_suffix("")
        module_name = ".".join(rel.parts)
        if module_name.endswith(".__init__"):
            module_name = module_name[: -len(".__init__")]
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — 要的就是「任何异常」
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
    assert not failures, "内核模块导入失败:\n  " + "\n  ".join(failures)
