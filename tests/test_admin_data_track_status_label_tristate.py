"""T367 ③ 的回归网:吃 snapshot 的函数不许渲染二值 `*_status` 标签。

为什么需要这条(而不是只靠实测夹具):
    实测夹具一次只能压住它恰好种到的那一格。`app_usage.fields_status` 有网了,
    另外五个产生点一处都没有 —— 一个只把 app_usage 三态化、其余原样不动的实现
    会通过整套实测夹具。静态读码结论能证明缺陷存在,但它不是回归网:下次没人重读。

判据(从代码派生,不是写死清单):
    `admin_data_track_snapshot` 是那个会失败的调用。凡是形参里有 `snap` 的函数,
    渲染的都是这次 snapshot 的产物 —— 那么它写的 `*_status` 标签就必须能表达
    「这次没读出来」。二值全字面量三元(如 `"invalid" if invalid_fields else "ok"`)
    **结构上做不到**:读失败 ⇒ 字段为空 ⇒ invalid_fields 为空 ⇒ 落 "ok"。

    所以生产上 100/100 行 snapshot_read_status=timeout 却同时 counts_status="ok",
    不是巧合,是这行代码的必然输出。

这条对新增产生点同样生效:以后谁再加一个吃 snap 的渲染函数并写二值标签,这里红。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_TRACK = ROOT / "backend" / "admin" / "data_track.py"

# 能表达「这次没读出来」的第三态词汇。三态化实现只要落在这里任意一个即可。
UNKNOWN_WORDS = {
    "unknown", "read_error", "timeout", "unread",
    "unavailable", "read_failed", "not_read",
}

# 这四项名字虽以 `_status` 结尾,值却是按业务状态分组的计数字典,不是数据质量
# 标签。豁免判据必须保持窄,成员也必须冻结:新增一项时由人判断,不能自动放行。
COUNT_DICT_LABELS = {
    ("_data_track_memory_from_snapshot", "capture_jobs_by_status", "dict"),
    ("_data_track_proactive_from_snapshot", "jobs_by_status", "dict"),
    ("_data_track_proactive_from_snapshot", "live_activity_status", "dict"),
    ("_data_track_proactive_from_snapshot", "alert_status", "dict"),
}

# 桶 3 是我读不懂的清单,不是我批准的清单。
# 每项都要写明原因。B 类修完会从这里迁进桶 1,清单断言随之红是正确行为,
# 不是回归,此时应重审并更新清单,不能把生产修复回滚掉。
UNREADABLE_STATUS_LABELS = {
    ("_effective_responder", "last_poll_status", "dict"): (
        "值是单条 poll observation 已完成解析后的局部状态变量"
    ),
    ("_build_data_track_user_fast", "consumer_poll_status", "dict"): (
        "值来自 onboarding validation.get 调用,不是 snapshot 读取质量标签"
    ),
}


def _snapshot_consumers(tree: ast.AST) -> list[ast.FunctionDef]:
    """形参里有 snapshot 或其 read status 的函数。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
        if "snap" in params or "snapshot_read_status" in params:
            out.append(node)
    return out


def _ternary_literals(node: ast.AST) -> tuple[str, ...] | None:
    """三元表达式所有可达的字符串字面量分支;有非字面量分支则返回 None。

    必须**递归**下去:三态化后的写法是嵌套三元
    (`"unknown" if failed else ("invalid" if bad else "ok")`),只看一层的话
    修好之后扫描器就什么都找不到了 —— 那样这条守卫会在修复后静默守空气。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if not isinstance(node, ast.IfExp):
        return None
    out: list[str] = []
    for branch in (node.body, node.orelse):
        values = _ternary_literals(branch)
        if values is None:
            return None
        out.extend(values)
    return tuple(out)


def _status_candidates(func: ast.AST) -> list[tuple[str, int, ast.AST, str]]:
    """收集所有 `*_status` 赋值,不因扫描器读不懂值表达式而静默丢弃。"""
    found: list[tuple[str, int, ast.AST, str]] = []

    def consider(key: str, value: ast.AST, shape: str) -> None:
        if key.endswith("_status"):
            found.append((key, value.lineno, value, shape))

    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    consider(k.value, v, "dict")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    consider(target.slice.value, node.value, "subscript")
    return found


def _status_labels(func: ast.AST) -> list[tuple[str, int, tuple[str, ...], str]]:
    """(键名, 行号, 所有可达值, AST 形状) —— 收全字面量标签,不限分支数。

    ⚠️ 不能只收三元:**无条件字面量**(`"counts_status": "ok"`)才是 ③ 最纯粹的形态
    ——「无条件写死的真值标签」。只收 ast.IfExp 会把它整类漏掉。
    """
    found = []
    for key, lineno, value, shape in _status_candidates(func):
        values = _ternary_literals(value)
        if values is None:
            continue
        found.append((key, lineno, values, shape))
    return found


def _scan() -> dict[str, list[tuple[str, int, tuple[str, ...], str]]]:
    tree = ast.parse(DATA_TRACK.read_text(encoding="utf-8"), filename=str(DATA_TRACK))
    return {
        func.name: _status_labels(func) for func in _snapshot_consumers(tree)
    }


def _scan_candidates() -> dict[str, list[tuple[str, int, ast.AST, str]]]:
    tree = ast.parse(DATA_TRACK.read_text(encoding="utf-8"), filename=str(DATA_TRACK))
    return {
        func.name: _status_candidates(func) for func in _snapshot_consumers(tree)
    }


def _is_count_dict(value: ast.AST) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "_data_track_count_dict"
    ) or (
        isinstance(value, ast.Name) and value.id.endswith("_counts")
    )


def test_scanner_finds_its_producers():
    """扫描器必须真的在测量东西。

    派生集为空时「0 条全部合格」恒真,守卫会静默守空气。所以这里钉的是**形状**
    而不只是数量:AST 走法被重构打断时在这里红,而不是等到下次真出事。
    """
    tree = ast.parse(DATA_TRACK.read_text(encoding="utf-8"), filename=str(DATA_TRACK))
    consumers = {f.name for f in _snapshot_consumers(tree)}

    # 具名锚:这两个函数确实吃 snapshot,改名了要在这里红
    assert "_data_track_app_usage_from_snapshot" in consumers, sorted(consumers)
    assert "_data_track_chat_from_snapshot" in consumers, sorted(consumers)
    assert len(consumers) >= 6, sorted(consumers)

    scanned = _scan()
    labels = [item for items in scanned.values() for item in items]
    assert labels, "扫描器一个 *_status 标签都没找到 ⇒ 它在守空气"

    # 两种 AST 形状都要能看见,否则扫描器只守住了一半文件
    shapes = {shape for _, _, _, shape in labels}
    assert shapes == {"dict", "subscript"}, (
        f"扫描器漏了一种写法,只看见 {shapes};"
        " dict 字面量与下标赋值两种产生形状都必须覆盖"
    )


def test_snapshot_consumers_can_express_unread():
    """吃 snapshot 的函数不许把「没读出来」渲染成一个确定标签。"""
    offenders = []
    for func_name, labels in sorted(_scan().items()):
        for key, lineno, values, _shape in labels:
            if set(values) & UNKNOWN_WORDS:
                continue
            offenders.append(
                f"{func_name}() data_track.py:{lineno} {key}={values}"
            )

    assert not offenders, (
        "以下标签描述的是 snapshot 的产物,却只有二值、无法表达「这次没读出来」;\n"
        "读失败 ⇒ 字段空 ⇒ 落到那个真值分支:\n  "
        + "\n  ".join(offenders)
    )


def test_count_dictionary_exemptions_are_narrow_and_frozen():
    actual = set()
    for func_name, labels in _scan_candidates().items():
        for key, _lineno, value, shape in labels:
            if _is_count_dict(value):
                actual.add((func_name, key, shape))

    assert actual == COUNT_DICT_LABELS, (
        "桶 2 的计数字典豁免成员变了,必须逐项人工判断,不能自动放行:\n"
        f"expected={sorted(COUNT_DICT_LABELS)}\nactual={sorted(actual)}"
    )


def test_unreadable_status_labels_match_the_reviewed_inventory():
    actual = set()
    details = []
    for func_name, labels in _scan_candidates().items():
        for key, lineno, value, shape in labels:
            identity = (func_name, key, shape)
            if _ternary_literals(value) is not None or _is_count_dict(value):
                continue
            actual.add(identity)
            details.append(
                f"{func_name}() data_track.py:{lineno} {key}={type(value).__name__}"
            )

    expected = set(UNREADABLE_STATUS_LABELS)
    assert actual == expected, (
        "桶 3 成员变了:扫描器读不懂的表达式必须逐项解释并复审。\n"
        f"expected={sorted(expected)}\nactual={sorted(actual)}\n"
        + "\n".join(details)
    )
