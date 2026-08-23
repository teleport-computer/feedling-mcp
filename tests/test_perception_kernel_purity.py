"""内核纯度守卫：``perception_kernel`` 不得 import 任何 io 模块。

结构判据（AST 里有没有这个 import），不判语义、不判风格：误伤为零。
照抄 tests/test_memory_garden_purity.py 的做法。

★ 禁止名单不止 backend/ 下的顶层模块——tools/（例如
  ``chat_resident_consumer``）也是 io 侧代码，内核同样不许 import。
  两边各扫一遍顶层名字，合并成一个禁止集合。
"""
from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
_TOOLS_ROOT = _REPO_ROOT / "tools"
_KERNEL_ROOT = _BACKEND_ROOT / "perception_kernel"

_ALLOWED_THIRD_PARTY: frozenset[str] = frozenset({"core"})
_NOT_IO = {"perception_kernel", "memory_garden", "core"}


def _top_level_names(root: pathlib.Path) -> frozenset[str]:
    names: set[str] = set()
    for entry in root.iterdir():
        if entry.name.startswith((".", "_")) or entry.name in _NOT_IO:
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            names.add(entry.stem)
    return frozenset(names - _ALLOWED_THIRD_PARTY)


def _backend_top_level_names() -> frozenset[str]:
    return _top_level_names(_BACKEND_ROOT) | _top_level_names(_TOOLS_ROOT)


def _imported_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # 相对导入，包内自引用
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_kernel_does_not_import_io_modules():
    forbidden = _backend_top_level_names()
    offenders: list[str] = []
    for path in sorted(_KERNEL_ROOT.rglob("*.py")):
        bad = _imported_roots(path) & forbidden
        if bad:
            offenders.append(f"{path.relative_to(_REPO_ROOT)}: {sorted(bad)}")
    assert not offenders, "内核 import 了 io 模块：\n" + "\n".join(offenders)
