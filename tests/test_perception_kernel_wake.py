"""叫醒判据 —— 纯函数，不碰 DB、不碰时钟。

★ 语义：should_wake 回答的是「值不值得戳一下 agent」，
  不是「该不该说话」。返回值里不许出现任何跟「说什么」有关的东西。

★ 用词：内核这套叫 PERCEPTION_WAKE_SOURCES（感知叫醒源），刻意不叫 wake_kind
  —— io 里 proactive/gate.py 和 model_api_runtime/v2/effect_outbox.py 各有一套
  含义不同的 wake_kind，三者不可互传。详见 perception_kernel/wake.py 的注释。
"""
from __future__ import annotations

import ast
import pathlib
import sys

# Self-contained sys.path bootstrap (mirrors tests/test_perception_kernel_catalog.py):
# conftest.py only adds backend/ to sys.path inside its DB-provisioning try-block,
# so on a no-Postgres machine this file must add backend/ itself.
_BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import perception_kernel.wake as wake


def test_disabled_source_never_wakes():
    ok, reason = wake.should_wake(
        "photo", enabled_sources=("arrival",), last_wake_ts=0.0, now=1000.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "source_disabled"


def test_debounce_blocks_a_second_wake_inside_the_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "debounced"


def test_wake_passes_outside_the_debounce_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1100.0, debounce_sec=60.0
    )
    assert ok is True
    assert reason == "arrival"


def test_first_ever_wake_has_no_previous_timestamp():
    ok, _ = wake.should_wake(
        "unlock", enabled_sources=wake.PERCEPTION_WAKE_SOURCES, last_wake_ts=None, now=1.0,
        debounce_sec=60.0
    )
    assert ok is True


def test_motion_is_not_a_significant_change():
    # 基线语义：motion 变得太频繁，故意不作为叫醒源。
    assert wake.is_significant_change("motion_state", "still", "walking") is False


def test_place_label_change_is_significant():
    assert wake.is_significant_change("location_signal", "home", "office") is True


def test_same_value_is_never_significant():
    assert wake.is_significant_change("location_signal", "office", "office") is False


def test_kernel_vocabulary_does_not_collide_with_the_two_io_wake_kind_sets():
    """内核这套叫醒源，和 io 里两套同名不同义的 wake_kind 刻意保持区分。

    gate.py 的是「走哪条投递通道」，effect_outbox.py 的是「哪几类要防撞」，
    内核这套是「被什么感知到的」。三者不可互传，名字也不许再撞。
    """
    gate_kinds = {"screen_watch", "screen", "presence"}
    outbox_kinds = {"heartbeat", "manual_wake", "screen_watch"}
    ours = set(wake.PERCEPTION_WAKE_SOURCES)
    assert not hasattr(wake, "WAKE_KINDS"), "别再引入 WAKE_KINDS 这个名字"
    assert ours != gate_kinds and ours != outbox_kinds
    # 唯一的重叠词，含义不同，保留但不代表可互换
    assert ours & gate_kinds == {"screen_watch"}
    assert ours & outbox_kinds == {"screen_watch"}


# ---------------------------------------------------------------------------
# 未接线守卫：PERCEPTION_WAKE_SOURCES / is_significant_change / should_wake
# ---------------------------------------------------------------------------
# 这三个名字目前只被内核自己（wake.py）和本文件引用，io 侧没有任何调用方。
# 只要有人把它们接进 io（哪怕只是 import 一下），下面这个测试就会转红，
# 逼着接线的人先看到这段说明，而不是悄悄地在某条 PR 里改了用户可见的
# reason 字符串。见 wake.py 里 should_wake 上方的大段注释：
#   1. reason 映射待定——内核的 source_disabled/debounced 是合并词，
#      io 现在按 source 分开写（photo_wake_disabled/arrival_wake_disabled/
#      unlock_wake_disabled/screen_watch_disabled、capability_debounce），
#      这些字符串在感知事件流和 admin data_track 里是用户可见的；接线前必须
#      先定「统一成一套」还是「留映射表」，不能悄悄改变已产出的 reason 值。
#   2. is_significant_change 不是 is_wake_worthy_signal 的替代品——它多了
#      一个 `prev == cur` 短路，会让 arrival/unlock/photo 三类真正的叫醒
#      信号被静默吞掉（这三类的「变没变」由 differ_v2 的 HMAC 指纹比对
#      在别处判完，这里传进来的 prev/cur 不构成同一件事）。
_WAKE_UNWIRED_NAMES: tuple[str, ...] = (
    "PERCEPTION_WAKE_SOURCES",
    "is_significant_change",
    "should_wake",
)

_UNWIRED_GUARD_MESSAGE = (
    "{name} 现在被 io 引用了（{hits}），但它还没被判定为「可以直接接线」：\n"
    "wake.py 里 should_wake 上方的注释写了原因——接线前必须先决定 reason 字符串"
    "映射（统一成一套 还是 留映射表，io 现在用 source_disabled/debounced 之外的"
    "分散命名，这些串在感知事件流/admin data_track 里用户可见）；并且"
    "is_significant_change 不是 is_wake_worthy_signal 的替代品（多了"
    "prev == cur 短路，会吞掉 arrival/unlock/photo 三个真实叫醒信号）。"
    "看完这段还要接，再改这个测试。"
)


def _wake_module_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """扫 import 语句，找出「直接绑定了禁用名字」和「绑定了 wake 模块本身」两类。

    - direct: 通过 ``from perception_kernel.wake import <name> [as X]`` 直接
      把某个禁用名字绑到本地——import 这一下本身就算「引用」了。
    - module_aliases: 通过 ``import perception_kernel.wake [as X]`` 或
      ``from perception_kernel import wake [as X]`` 绑定的、指向 wake 模块
      本身的本地名字，用来识别 ``<alias>.should_wake`` 这类属性访问。
    """
    direct: set[str] = set()
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "perception_kernel.wake":
                for alias in node.names:
                    if alias.name in _WAKE_UNWIRED_NAMES:
                        direct.add(alias.asname or alias.name)
            elif node.module == "perception_kernel":
                for alias in node.names:
                    if alias.name == "wake":
                        module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "perception_kernel.wake":
                    module_aliases.add(alias.asname or "perception_kernel")
    return direct, module_aliases


def _attribute_dotted_path(node: ast.Attribute) -> str | None:
    parts = [node.attr]
    cur = node.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


_FULLY_QUALIFIED_WAKE_ATTRS = frozenset(
    f"perception_kernel.wake.{name}" for name in _WAKE_UNWIRED_NAMES
)


def _wake_references_in_file(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    direct, module_aliases = _wake_module_bindings(tree)
    hits: set[str] = set(direct)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in _WAKE_UNWIRED_NAMES):
            continue
        dotted = _attribute_dotted_path(node)
        if dotted is None:
            continue
        root = dotted.split(".")[0]
        if root in module_aliases or dotted in _FULLY_QUALIFIED_WAKE_ATTRS:
            hits.add(node.attr)
    return hits


def test_wake_unwired_names_stay_unreferenced_outside_the_kernel():
    """接线前的哨兵：这三个名字现在只准出现在 wake.py 和本测试文件里。

    实现上按内容扫描 backend/ 和 tools/ 下的源码（AST 解析文本，不 import
    应用模块）——判断标准不是「这个词出现过」（wake.py 的注释里已经解释过
    io 侧另有一套含义不同、但字面上也叫 should_wake 的 dict key，例如
    scheduler.py / serve_worker.py 的 wake_decision 协议），而是「有没有
    真的 import 或属性访问到 perception_kernel.wake 里这三个名字」。
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    kernel_root = repo_root / "backend" / "perception_kernel"
    scan_roots = (repo_root / "backend", repo_root / "tools")

    offenders: list[str] = []
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue
        for path in sorted(scan_root.rglob("*.py")):
            if kernel_root in path.parents or path.parent == kernel_root:
                continue
            hits = _wake_references_in_file(path)
            if hits:
                offenders.append(f"{path.relative_to(repo_root)}: {sorted(hits)}")

    assert not offenders, "\n".join(
        _UNWIRED_GUARD_MESSAGE.format(name=name, hits="; ".join(offenders))
        for name in _WAKE_UNWIRED_NAMES
        if any(name in entry for entry in offenders)
    )
