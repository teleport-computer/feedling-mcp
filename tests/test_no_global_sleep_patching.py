"""守卫:测试不得篡改**进程全局**的 stdlib ``time.sleep``。

`monkeypatch.setattr(某模块.time, "sleep", …)` 改的不是那个模块的属性,而是
**整个进程共享的 stdlib time 模块**。后果有两种,都不确定、都难归因:

  · 替换成"记录到列表":后台线程(如 wake-bus 重连)睡的那一下会被追加进本测试的
    断言列表 —— 2026-08-22 的 `assert 1.0 == 30.0` 就是这么来的,同一个 commit
    推到两条分支一红一绿。
  · 替换成 no-op:后台线程的 sleep 立刻返回,变成空转,同样扰动时序。
    **它不比前者安全,只是坏得更安静。**

正路是 `conftest.capture_sleeps(monkeypatch, module)`:只替换该模块自己的 `time`
引用,其余属性委托真模块,并自验进程全局未被触碰。

## ⚠️ 为什么是 AST 而不是正则

第一版按**行**跑正则。`tests/test_io_cli_auth.py:220` 是同一个调用拆成四行写的:

    monkeypatch.setattr(
        io_cli.time,
        "sleep",
        …)

⇒ **真 offender 在场,而守卫是绿的**(codex 2026-08-23 r3 查出)。
同一条正则对拼起来的完整串能匹配,逐行匹配就是 False —— 换句话说,
**它守的不是「有没有这种写法」,是「有没有人把它写在一行里」。**

⭐ 一个只在源码某种排版下才成立的检查等于没有检查:它给出的绿与「确实没有
offender」完全同形,而 `black` 随手换个行宽就能让它失明。
⇒ 判据换成语法结构:AST 不关心换行、缩进与注释。

## ⚠️ 这条守卫是写死的字面量锚

它**不从被测集合派生**。若改成「扫描现有站点再断言数量」,它会随着站点被删除
而自我抹除 —— 那时它仍然是绿的,但什么都不再守。
判据:**撤销这次改动,这条测试是会变红,还是会不存在?** 必须是前者。
"""
from __future__ import annotations

import ast
import pathlib
from typing import Optional

_TESTS = pathlib.Path(__file__).resolve().parent


def _dotted(node: ast.AST) -> Optional[str]:
    """把 `a.b.c` 还原成字符串;不是纯点号链就返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_time_module_expr(node: ast.AST) -> bool:
    """第一个实参是不是「某个 time 模块」:`x.time` / `a.b.time` / 裸 `time`。"""
    if isinstance(node, ast.Attribute):
        return node.attr == "time"
    if isinstance(node, ast.Name):
        return node.id == "time"
    return False


def _is_sleep_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "sleep"


def _offenders_in(tree: ast.AST) -> list:
    """返回篡改了进程全局 time.sleep 的调用所在行号。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _dotted(node.func) if isinstance(node.func, ast.Attribute) else None
        is_setattr = (
            callee is not None and callee.split(".")[-1] == "setattr"
        ) or (isinstance(node.func, ast.Name) and node.func.id == "setattr")
        if not is_setattr or not node.args:
            continue
        # 形态一(两段式):setattr(<某>.time, "sleep", …)
        if (
            len(node.args) >= 2
            and _is_time_module_expr(node.args[0])
            and _is_sleep_literal(node.args[1])
        ):
            hits.append(node.lineno)
            continue
        # 形态二(点号字符串):monkeypatch.setattr("mod.time.sleep", …)
        # ⚠️ 这一支不是"顺手补全",是因为 monkeypatch **真支持**这种写法;
        # 漏掉它等于在守卫旁边留了一条并行的路。
        #
        # ⚠️ 必须按**点号段**判,不能用 `endswith("time.sleep")`:
        # 后者没有段边界,会把 `runtime.sleep` / `sometime.sleep` / `my_runtime.sleep`
        # 全判成 offender —— 那是某个普通模块自己的 sleep 属性,隔离 patch 它完全正当。
        # (2026-08-23 codex r4 查出;这条误判**直接违反本文件下面自己写的负向原则**
        #  「只守 sleep;扩大射程会让守卫失去可信度」—— 守卫误伤会逼人绕开它,
        #  而被绕开的守卫比没有守卫更坏:它还在,所以没人再去补。)
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            target = first.value
            if target == "time.sleep" or target.endswith(".time.sleep"):
                hits.append(node.lineno)
    return sorted(set(hits))


def test_no_test_patches_the_process_global_sleep():
    offenders = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        source = path.read_text()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # 语法坏了必须出声,不能静默当成"没有 offender"
            raise AssertionError(f"{path.relative_to(_TESTS)}: 无法解析,守卫在此失明: {exc}")
        lines = source.splitlines()
        for lineno in _offenders_in(tree):
            offenders.append(
                f"{path.relative_to(_TESTS)}:{lineno}: {lines[lineno - 1].strip()}"
            )
    assert not offenders, (
        "these tests mutate the process-global time.sleep; use "
        "conftest.capture_sleeps(monkeypatch, module) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_sees_through_line_breaks():
    """⭐ 这条守的是**守卫自己**:第一版逐行正则正是在这个形状上失明的。

    没有它,把 `_offenders_in` 退回逐行正则之后,上面那条测试**仍然是绿的** ——
    因为仓里此刻已经没有单行 offender 了。**一个检查失去能力却不变红**,
    正是它被悄悄挖空的样子。
    """
    multiline = ast.parse(
        "monkeypatch.setattr(\n"
        "    io_cli.time,\n"
        '    "sleep",\n'
        "    lambda _s: None,\n"
        ")\n"
    )
    assert _offenders_in(multiline), "守卫必须看穿换行,否则换个行宽就能让它失明"

    single = ast.parse('monkeypatch.setattr(io_cli.time, "sleep", lambda _s: None)')
    assert _offenders_in(single), "单行形态必须仍然咬得住"

    dotted = ast.parse('monkeypatch.setattr("io_cli.time.sleep", lambda _s: None)')
    assert _offenders_in(dotted), "点号字符串是 monkeypatch 支持的并行路径"

    bare = ast.parse('monkeypatch.setattr("time.sleep", lambda _s: None)')
    assert _offenders_in(bare), "裸 time.sleep 是最直接的那条路"

    # 负向:正路写法不许被误报 —— 守卫误伤会逼人绕开它。
    ok = ast.parse("capture_sleeps(monkeypatch, io_cli)")
    assert not _offenders_in(ok), "capture_sleeps 是正路,不能被判为 offender"

    # 负向:替换同一模块的**别的**属性不在射程内。
    other = ast.parse('monkeypatch.setattr(io_cli.time, "monotonic", lambda: 0.0)')
    assert not _offenders_in(other), "只守 sleep;扩大射程会让守卫失去可信度"

    # 负向:字符串形态必须按**点号段**判。`endswith("time.sleep")` 没有段边界,
    # 会把这三个普通模块自己的 sleep 属性一并打成 offender —— 隔离 patch 它们完全正当。
    # ⭐ 这三条是本文件里唯一咬得住那个 bug 的东西:去掉它们,把段判定退回
    # `endswith("time.sleep")`,上面所有正例**依然全绿**(误判只在负向侧显形)。
    for name in ("runtime.sleep", "sometime.sleep", "my_runtime.sleep"):
        false_positive = ast.parse(f'monkeypatch.setattr("{name}", lambda _s: None)')
        assert not _offenders_in(false_positive), (
            f"{name} 是某个普通模块自己的 sleep 属性,不是进程全局 time.sleep;"
            "按点号段判,别用 endswith"
        )
