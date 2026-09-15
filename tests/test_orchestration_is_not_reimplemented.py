"""io 不许再自己拼记忆的编排。

## 守什么

「拼提示词 → 调模型 → 解析 → 过闸 → 重问」这一串是 **GardenComponent 的活**。
io 自己拼的后果不是重复，是**说明书只存在于 io 的代码里**：
Garden 内部改个函数名 io 就编译不过，换一套记忆系统这些调用点全部作废。

capture / dream / migrate 三条路都收口之后，这条守卫防止悄悄退回去 ——
退回去很容易（想加个小功能，顺手 import 一个 prompt builder 就完了），
而且不会有任何测试变红。

## 不在守卫范围里的

io 在自己的边界上用库的**常量和闸门**是正当的，不是坏耦合：

    桶清单 / 写卡规则 / 克制规则   io 注入进自己的提示词（genesis、工具描述）
    card_guard / card_text        落库闸，要管**所有**写入路径（含工具触发的），
                                  不该要求组件参与每一次写
    dream_gates.blast_radius      应用整理结果前的安全闸，是 io 决定要不要应用
    timestamps / observability    纯工具函数

把这些也包进组件反而是错的。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

#: 编排函数 —— io 直接 import 这些，就是又在自己拼了。
ORCHESTRATION = {
    "build_capture_prompt", "parse_capture_cards",
    "build_capture_retry_prompt", "build_capture_semantic_retry_prompt",
    "capture_semantic_retry_reasons",
    "build_dream_prompt", "parse_dream_consolidations", "build_dream_retry_prompt",
    "build_migrate_prompt", "parse_migrated_cards",
    "needs_dream", "dream_snapshot",
}

#: 豁免。理由各不相同，都写清楚：
EXEMPT = {
    # 挂载点本身，import 组件是它的职责
    "backend/memory/garden_component.py",
    # e2e 探针要复用线上同一把尺子来断言，不是产品路径
    "tools/e2e/",
}


def _sources():
    for root in ("backend", "tools"):
        for f in (REPO / root).rglob("*.py"):
            rel = str(f.relative_to(REPO))
            if any(rel.startswith(x) or rel == x for x in EXEMPT):
                continue
            yield rel, f


def test_io_never_imports_the_orchestration_functions() -> None:
    offenders = []
    for rel, f in _sources():
        try:
            tree = ast.parse(f.read_text("utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("memgarden"):
                leaked = {a.name for a in node.names} & ORCHESTRATION
                if leaked:
                    offenders.append(f"{rel}: {sorted(leaked)}")
    assert not offenders, (
        "io 又自己 import 编排函数了：\n  " + "\n  ".join(offenders)
        + "\n\n应该调 GardenComponent 的方法（见 backend/memory/garden_component.py）。"
    )


def test_the_exemptions_are_still_real() -> None:
    """豁免名单不许烂掉 —— 文件没了就该把那条删掉，否则名单会慢慢变成
    「什么都豁免」而没人发现。"""
    missing = [x for x in EXEMPT if not (REPO / x).exists()]
    assert not missing, f"豁免名单里有不存在的路径：{missing}"


#: 记忆 runtime 的入口文件：两条 lane 的落卡 / 整理都经过这里。
#: 2026-09-15 起它们只许 import memgarden 的公开 API（顶层 ``__all__``，或
#: ``memgarden.STABLE_MODULES`` 里模块的 ``__all__``）—— ``prompts.capture`` /
#: ``prompts.dream`` 这类内部零件随时会改名，那正是 ``import *`` 兼容壳被删的原因。
PUBLIC_API_ONLY = (
    "backend/memory/garden_component.py",
    "backend/memory/capture_prompt_v1.py",
    "backend/model_api_runtime/v2/worker.py",
    "backend/model_api_runtime/v2/serve_worker.py",
    "tools/chat_resident_consumer.py",
)


def _is_public(module: str, name: str | None) -> bool:
    import importlib

    import memgarden

    stable = set(memgarden.STABLE_MODULES)
    if name is None:                       # ``import memgarden.x``
        return module == "memgarden" or module in stable
    if module == "memgarden":
        return name in memgarden.__all__ or f"memgarden.{name}" in stable
    if f"{module}.{name}" in stable:       # ``from memgarden.text import card_guard``
        return True
    return module in stable and name in getattr(importlib.import_module(module), "__all__", ())


def test_memory_runtime_entry_files_import_only_public_memgarden_api() -> None:
    offenders = []
    for rel in PUBLIC_API_ONLY:
        tree = ast.parse((REPO / rel).read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("memgarden"):
                if any(alias.name == "*" for alias in node.names):
                    offenders.append(f"{rel}:{node.lineno} from {node.module} import *")
                    continue
                offenders.extend(
                    f"{rel}:{node.lineno} {node.module}.{alias.name}"
                    for alias in node.names if not _is_public(node.module, alias.name)
                )
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{rel}:{node.lineno} import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("memgarden") and not _is_public(alias.name, None)
                )
    assert not offenders, (
        "这些 import 用了 memgarden 的内部零件（不在 __all__ / STABLE_MODULES 里）：\n  "
        + "\n  ".join(offenders)
    )
