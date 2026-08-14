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
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
_KERNEL_ROOT = _BACKEND_ROOT / "memory_garden"

# 允许的非标准库依赖。
#
# ``agent_protocol_core`` 是模型协议层的纯共享判据（思维链剥离、协议残片识别），
# 与「记忆」无关 —— 聊天主链路、工具循环、主动唤醒都在用。把它单独成包是因为
# 第一版把它塞进了 memory_garden，导致**普通聊天反向依赖记忆包**
# （codex review 2026-08-14 指出）。它自己同样不 import 任何 io 模块。
_ALLOWED_THIRD_PARTY: frozenset[str] = frozenset({"agent_protocol_core"})


def _backend_top_level_names() -> frozenset[str]:
    """动态枚举 backend/ 下的所有顶层模块与包 —— 这些都是 io 侧代码。

    用枚举而不是黑名单：黑名单只挡得住写它的时候想到的那几个，
    ``from genesis import ...`` / ``from proactive import ...`` /
    ``from model_api_runtime import ...`` 全会漏过去 —— 而批 7 接 genesis 时，
    那正是最容易出现的倒挂（codex code_review 2026-08-14 指出）。
    """
    names: set[str] = set()
    # 这两个包本身就是「非 io」的纯包，不算在禁止集合里。
    _NOT_IO = {"memory_garden", "agent_protocol_core"}
    for entry in _BACKEND_ROOT.iterdir():
        if entry.name.startswith((".", "_")) or entry.name in _NOT_IO:
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            names.add(entry.stem)
    return frozenset(names)


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
    """内核不得绝对 import backend/ 下的任何模块 —— 包内引用一律相对 import。"""
    io_names = _backend_top_level_names()
    assert "genesis" in io_names and "db" in io_names, (
        "backend/ 枚举结果不对，守卫会失效：" + ", ".join(sorted(io_names))[:200]
    )

    offenders: list[str] = []
    for path in _kernel_files():
        rel = path.relative_to(_KERNEL_ROOT)
        for root in sorted(_imported_roots(path)):
            if root in io_names:
                offenders.append(f"{rel}: import {root}（io 模块）")
            elif root == "memory_garden":
                offenders.append(f"{rel}: import memory_garden（包内请用相对 import）")
    assert not offenders, (
        "内核里出现了 io 依赖（包内引用请改成相对 import）:\n  "
        + "\n  ".join(offenders)
    )


def test_kernel_imports_only_stdlib_and_allowlisted_third_party():
    """白名单口径：除标准库与显式登记的第三方外，不许有别的绝对 import。

    比「黑名单 io 模块」更严 —— 悄悄引入一个新依赖也会被这条挡下来。
    """
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in _kernel_files():
        rel = path.relative_to(_KERNEL_ROOT)
        for root in sorted(_imported_roots(path)):
            if root in stdlib or root in _ALLOWED_THIRD_PARTY:
                continue
            offenders.append(f"{rel}: import {root}")
    assert not offenders, (
        "内核出现了非标准库依赖。若确实需要，请登记进 _ALLOWED_THIRD_PARTY:\n  "
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


# --------------------------------------------------------------------------- #
# agent_protocol_core 同样必须是纯的
# --------------------------------------------------------------------------- #

_PROTOCOL_ROOT = _BACKEND_ROOT / "agent_protocol_core"


def _protocol_files() -> list[pathlib.Path]:
    return sorted(p for p in _PROTOCOL_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_protocol_core_is_not_empty():
    assert _protocol_files(), f"agent_protocol_core 为空：{_PROTOCOL_ROOT}"


def test_protocol_core_imports_only_stdlib():
    """中立包只许依赖标准库 —— 它被聊天和记忆两边共用，不能反向依赖任何一边。"""
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in _protocol_files():
        for root in sorted(_imported_roots(path)):
            if root in stdlib:
                continue
            offenders.append(f"{path.name}: import {root}")
    assert not offenders, (
        "agent_protocol_core 出现了非标准库依赖：\n  " + "\n  ".join(offenders)
    )


def test_protocol_core_does_not_depend_on_memory_garden():
    """方向必须是单向的：memory_garden → agent_protocol_core，不能反过来。"""
    for path in _protocol_files():
        assert "memory_garden" not in _imported_roots(path), (
            f"{path.name} 反向依赖了 memory_garden"
        )
