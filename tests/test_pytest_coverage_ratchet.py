"""豁免名单只许缩小 —— 否则「测试没跑」会永远长得像「测试通过」。

`.github/workflows/ci.yml` 的 "Guard top-level pytest discovery coverage" 只拦
一件事:**新测试文件不在任何清单里**。它拦不住相反的做法 ——
把新文件写进 `.github/pytest-uncovered-baseline.txt`,守卫当场闭嘴,
而那个文件从此在 CI 里一行都不跑。

2026-08-10 一天之内,这个口子以三种形态各咬了一次:

- 一个 173 行的新测试被直接写进豁免名单(整批断言从未执行过)
- `tests/test_capabilities_tool_schema.py` 新增的 39 行断言同理
- 我自己把 `debug_trace` 的 verbose 环深从 200 提到 1000,撞上
  `test_verbose_ring_cap` 里写死的 `== 200` —— 那条红**带着上线**,
  因为 `test_debug_trace.py` 也在豁免名单里

单个失败都不起眼,共同点是**红了但没有任何人看得见**。所以这里给名单装一个棘轮:
条数只许往下走,想加豁免就得改下面那个常量,在 review 里是一行显眼的 diff,
而不是名单里悄悄多出来的一行。

写成 pytest 而不是再加一段 shell,是顺着仓库已有的先例
(`test_ci_image_tag_width.py` / `test_deploy_yaml_strict.py`):本机能跑、
报错能说人话。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# conftest 的 autouse fixture 会 import 后端模块,所以即使本文件只读文本,
# 也必须自带这行引导 —— 否则收集期就 ModuleNotFoundError。
# (`tests/conftest.py` 的 `_PURE_UNIT` 注释专门提醒过这一点。)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
# 守卫的判据实现住在 tools/ 里,测试与守卫共用它(见 _ci_named_tests 的注释)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ci_executed_tests  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".github" / "pytest-uncovered-baseline.txt"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# 豁免名单允许的最大条数。
#
# ⚠️ 这个数**只许往下改**。调大它意味着又有一个测试文件退出了 CI ——
# 那需要在 PR 里说明理由,而不是顺手 +1。缩小名单时请一并把这个数改小,
# 否则棘轮会松掉。
MAX_EXEMPTED = 279


def _baseline_entries() -> list[str]:
    return [
        line.strip()
        for line in BASELINE.read_text().splitlines()
        if line.strip()
    ]


def _ci_named_tests() -> set[str]:
    """CI 真的执行到的测试文件 —— 与守卫共用同一份实现。

    ⚠️ 这里曾经是 `re.findall(..., CI_WORKFLOW.read_text())`,即「文件名在
    ci.yml 文本里出现过」,并且注释还写着「与守卫用的正则保持一致」——
    守卫和它的测试一起走在**同一个错判据**上,所以谁都发现不了对方错了。
    现在两边都 import `tools/ci_executed_tests.py`:判据只有一处,改了必须一起改。
    """
    return ci_executed_tests.executed_test_files(CI_WORKFLOW)


def test_the_exemption_list_only_ever_shrinks():
    entries = _baseline_entries()

    assert len(entries) <= MAX_EXEMPTED, (
        f"豁免名单从 {MAX_EXEMPTED} 涨到了 {len(entries)} 条。\n"
        "每多一条 = 又一个测试文件在 CI 里不跑了,而且守卫不会再为它报警。\n"
        "如果确实要加,请连同 MAX_EXEMPTED 一起改,并在 PR 里写明为什么。"
    )


def test_the_exemption_list_has_no_stale_entries():
    """名单里的文件必须真的存在。

    陈旧条目会掩护一次真正的回归:删掉某个测试文件后名单里仍留着它的名字,
    守卫的差集算出来就少一项,新问题反而更不容易被发现。
    """
    missing = [e for e in _baseline_entries() if not (ROOT / e).exists()]

    assert missing == [], f"名单里这些文件已不存在,请删掉:{missing}"


def test_no_file_is_both_run_by_ci_and_exempted_from_it():
    """同一个文件不能既被 CI 点名跑、又列在「不跑」的名单里。

    两边同时写着的时候,读名单的人会以为它没在跑而不去管它 ——
    也可能相反。任一方向都会让人对覆盖面做出错误判断。
    """
    both = sorted(set(_baseline_entries()) & _ci_named_tests())

    assert both == [], f"这些文件同时出现在 CI 清单和豁免名单里:{both}"


def test_the_ratchet_constant_matches_reality():
    """常量不许比实际条数大太多,否则棘轮是松的。

    留 0 的余量:MAX_EXEMPTED 必须等于当前条数。这样任何增加都要显式改这一行,
    任何减少也会被这条提醒着一起改小 —— 棘轮才咬得住。
    """
    actual = len(_baseline_entries())

    assert MAX_EXEMPTED == actual, (
        f"MAX_EXEMPTED={MAX_EXEMPTED} 与实际 {actual} 条不符。\n"
        "名单缩小之后请把这个常量一起改小,否则等于给未来预留了免检额度。"
    )


# --------------------------------------------------------------------------- #
# 判据本身的回归:「覆盖」必须是「真的被执行」,不是「名字出现过」。
#
# 用合成 workflow 而不是真 ci.yml —— 真文件此刻两种判据同解(452 = 452,零幽灵),
# 拿它做断言等于零判别力:旧判据也会全绿。合成用例把两者分开。
#
# 下面每一行都对应一个真实踩过的形状:前五条是审计里实测出的假绿(命令边界、
# 管道、行尾注释、echo 里的文件名),F/K/L/M 是修那五条时**自己引入**的假红 ——
# `\` 续行被当成命令分隔符,真 workflow 的覆盖集从 452 塌到 10。
# 两个方向都要钉住,只钉一边下次就会从另一边漏。
# --------------------------------------------------------------------------- #

_GHOST = "tests/test_ghost.py"
_REAL = "tests/test_real.py"

# 每一行都对应一个真实踩过的形状。前五组是审计一轮报的假绿(命令边界/管道/行尾
# 注释/echo 里的文件名);重定向、`|&`/`;&`、heredoc 体、跨行引号、--ignore、
# --collect-only 是审计二轮报的;续行那几条是我**自己修一轮时引入的假红**
# (`\` 续行被当成命令分隔符,真 workflow 覆盖集从 452 塌到 10)。
# 两个方向都要钉:只钉一边,下次就从另一边漏。
_SHAPES = [
    # —— 真执行,必须算覆盖 ——
    ("pytest 参数", f"pytest {_REAL}", {_REAL}),
    ("python -m pytest", f"python -m pytest {_REAL}", {_REAL}),
    ("env 前缀", f"PYTHONPATH=backend python -m pytest {_REAL} -v", {_REAL}),
    ("脚本直跑(自定义 runner)",
     "python tests/test_api.py http://127.0.0.1:5001 --multi-tenant",
     {"tests/test_api.py"}),
    ("续行", f"python -m pytest \\\n  {_REAL} \\\n  -v", {_REAL}),
    ("续行 + tee",
     f"python -m pytest \\\n  {_REAL} \\\n  -v | tee /tmp/x.log", {_REAL}),
    ("行尾管道续行", f"cat x |\n  python -m pytest {_REAL}", {_REAL}),
    ("管道后接 pytest", f"cat x | python -m pytest {_REAL}", {_REAL}),
    ("heredoc 之后照常解析",
     f"cat <<EOF\npytest {_GHOST}\nEOF\npytest {_REAL}", {_REAL}),

    # —— 只是被提到 / 明确不执行,不能算覆盖 ——
    # 这两条原本期望 {_REAL}(只把 ghost 排除掉)。审计四轮指出 &&/|| 是**条件**
    # 分隔符,`true || pytest x` 里 pytest 根本不执行 —— 可达性没建模就不能只排 ghost。
    # 于是行为改成:条件命令列里出现 pytest ⇒ 放弃整段。期望随之改成 set()。
    ("&& 之前的 echo(条件列 ⇒ 放弃)", f"echo {_GHOST} && pytest {_REAL}", set()),
    ("&& 之后的 echo(条件列 ⇒ 放弃)", f"pytest {_REAL} && echo {_GHOST}", set()),
    ("换行分隔的另一条命令", f"echo {_GHOST}\npytest {_REAL}", {_REAL}),
    ("重定向目标 >", f"pytest {_REAL} > {_GHOST}", {_REAL}),
    ("重定向目标 2>", f"pytest {_REAL} 2> {_GHOST}", {_REAL}),
    ("重定向目标 &>", f"pytest {_REAL} &> {_GHOST}", {_REAL}),
    ("|& 复合操作符", f"pytest {_REAL} |& echo {_GHOST}", {_REAL}),
    (";& 复合操作符", f"pytest {_REAL} ;& echo {_GHOST}", {_REAL}),
    ("--ignore 的操作数", f"pytest {_REAL} --ignore {_GHOST}", {_REAL}),
    ("--ignore= 的操作数", f"pytest {_REAL} --ignore={_GHOST}", {_REAL}),
    ("--deselect 的操作数", f"pytest {_REAL} --deselect {_GHOST}", {_REAL}),
    ("--collect-only 不执行", f"pytest --collect-only {_GHOST}", set()),
    ("--co 不执行", f"pytest --co {_GHOST}", set()),
    ("heredoc 体里的 pytest", f"cat <<EOF\npytest {_GHOST}\nEOF", set()),
    ("跨行引号中间那行", f'echo "start\npytest {_GHOST}\nend"', set()),
    ("行尾注释里的 pytest", f"echo ok # pytest {_GHOST}", set()),
    ("echo 里的 python 调用", f"echo python {_GHOST}", set()),
    ("引号里的文件名", f'echo "grep {_GHOST}"', set()),
    # 未知包装器不认 ⇒ 判未覆盖(假红)。方向刻意:漏判只会逼作者去看,
    # 误判成已覆盖才会让「测试没在跑」永远无人发现。
    ("未知包装器 xargs(刻意假红)",
     f"printf {_GHOST} | xargs pytest {_REAL}", set()),
    # —— 审计三轮报的:选项元数 / 重定向文法 / 多 heredoc ——
    # `python -X x.py y.py` 真正跑的是 y:-X 把 x 当成了自己的选项值
    # (拿 python 自己验过:sys._xoptions == {'tests/test_ghost.py': True})。
    # 「跳过 - 开头的、取第一个非选项」这套是不成立的,所以遇到非零元数/未知选项
    # 一律放弃整段。
    ("python -X 吞掉后一个参数(放弃整段)", f"python -X {_GHOST} {_REAL}", set()),
    ("python 零元数选项照常识别", f"python -u {_REAL}", {_REAL}),
    (">& 重定向目标", f"pytest {_REAL} >& {_GHOST}", {_REAL}),
    (">| 重定向目标", f"pytest {_REAL} >| {_GHOST}", {_REAL}),
    ("<> 重定向目标", f"pytest {_REAL} <> {_GHOST}", {_REAL}),
    ("一条命令上的多个 heredoc",
     f"cat <<A <<B\nbody-a\nA\npytest {_GHOST}\nB", set()),
    ("--setup-only 不执行用例", f"pytest --setup-only {_GHOST}", set()),
    ("--setup-plan 不执行用例", f"pytest --setup-plan {_GHOST}", set()),
    # —— 审计四轮报的:短路求值 / heredoc 定界符精确性 / pytest 选项白名单 ——
    # `true || pytest x` 与 `false && pytest x || true` 都以 0 退出,而 pytest
    # 一次都没跑。把 &&/|| 当无条件分隔符 = 把「证明没执行」的文件算成已覆盖。
    # 可达性没有建模,所以条件命令列里出现 pytest ⇒ 放弃整段(真 workflow 实测 0 处)。
    ("|| 短路:pytest 不会执行", f"true || pytest {_GHOST}", set()),
    ("&& 短路后接 ||", f"false && pytest {_GHOST} || true", set()),
    # bash 只对 `<<-` 剥**制表符**,`<<` 要求定界符独占一行且不缩进。
    # 用 .strip() 会在缩进的 EOF 处提前结束,把后面的数据当命令。
    # (两条都拿 bash 实跑对过:`<<` + 空格缩进不结束;`<<-` + tab 缩进结束。)
    ("<< 的定界符必须不缩进", f"cat <<EOF\n  EOF\npytest {_GHOST}\nEOF", set()),
    ("<<- 剥 tab 后定界符成立",
     f"cat <<-EOF\n\tEOF\npytest {_GHOST}\nEOF", {_GHOST}),
    ("--fixtures 只显示不执行", f"pytest --fixtures {_GHOST}", set()),
    ("--fixtures-per-test 只显示不执行",
     f"pytest --fixtures-per-test {_GHOST}", set()),
    # 选项走白名单:真 CI 只用到 `-v`(实测)。未知选项语义没人建模 ⇒ 放弃。
    ("未知 pytest 选项 ⇒ 放弃整段", f"pytest --wat {_REAL}", set()),
    ("-v 在白名单里", f"pytest {_REAL} -v", {_REAL}),
    ("; 是无条件分隔符,照常算", f"echo hi ; pytest {_REAL}", {_REAL}),
    # —— 审计五轮报的:多行控制流 / 反斜杠 heredoc 定界符 ——
    # `if false; then pytest x; fi` 与未命中的 case 分支都以 0 退出且 pytest 没跑。
    # &&/|| 只堵住了单行那一种条件语法。可达性仍不建模,于是**从第一个控制关键字起
    # 往后一律不计**(前向截断而不是整段放弃:真 workflow 唯一含控制关键字的 step
    # 其 pytest 在第 1 行、`if` 在第 4 行,实测过,截断后 452 一个不少)。
    ("if false 分支里的 pytest", f"if false; then\n  pytest {_GHOST}\nfi", set()),
    ("未命中的 case 分支", f"case x in\n  y) pytest {_GHOST} ;;\nesac", set()),
    ("for 循环体", f"for f in x; do pytest {_GHOST}; done", set()),
    ("控制结构**之前**的 pytest 仍保留",
     f"pytest {_REAL}\nif true; then pytest {_GHOST}; fi", {_REAL}),
    # `<<\EOF` 与 `<<'EOF'` 一样是带引的定界符(拿 bash 实跑对过:体内那行原样打印,
    # 说明是数据)。定界符形式认不出来时不再猜,直接放弃整段。
    ("反斜杠引的 heredoc 定界符",
     f"cat <<\\EOF\npytest {_GHOST}\nEOF", set()),
]


@pytest.mark.parametrize(
    "label,script,expected", _SHAPES, ids=[s[0] for s in _SHAPES]
)
def test_only_actually_executed_files_count_as_covered(label, script, expected):
    assert ci_executed_tests.executed_in_script(script) == expected, label


def test_the_real_workflow_is_not_shredded_by_the_parser():
    """真 ci.yml 必须解析出成百个文件。

    修假绿时我把 `\\` 续行当成命令分隔符,真 workflow 的覆盖集从 452 塌到 10,
    而合成用例全绿 —— 因为它们都是单行。这条守着「别把真文件解析碎了」。
    """
    executed = ci_executed_tests.executed_test_files(CI_WORKFLOW)

    assert len(executed) > 300, (
        f"只从 ci.yml 解析出 {len(executed)} 个被执行的测试文件 —— "
        "多半是命令切分把真实调用拆碎了,而不是 CI 真的只跑这么几个。"
    )


def test_real_workflow_coverage_stays_within_the_workflow_text():
    """弱不变量:解析结果是文本里出现过的名字的子集,且文件真实存在。

    ⚠️ 名字和文档都是被审计纠正过的。它**证明不了**真 workflow 上没有假阳性:
    `executed <= mentioned` 按构造恒真(名字本来就是从这份文本里抽的),
    而「文件存在」也分不开「被执行」与「仅被提到」—— 真实的假阳性用的就是存在的路径。
    留着它只是为了拦住「凭空造出仓库里没有的名字」这种解析垃圾,不承担更多。
    真正拦假阳性的是下面那条对真 ci.yml 做变异的用例。
    """
    executed = ci_executed_tests.executed_test_files(CI_WORKFLOW)
    mentioned = ci_executed_tests.mentioned_test_files(CI_WORKFLOW)

    assert executed <= mentioned
    missing = sorted(name for name in executed if not (ROOT / name).exists())
    assert missing == [], f"判为「被执行」但仓库里不存在的文件:{missing}"


def test_real_workflow_stops_covering_a_file_moved_into_a_comment(tmp_path):
    """真·假阳性守卫:拿真 ci.yml 做变异。

    合成用例只能证明「这种写法我处理对了」,证明不了在**真文件**这么复杂的
    上下文里也对。这里把一个真的在跑的测试从它的 pytest 命令里删掉、把名字挪进
    注释 —— 覆盖集必须**少掉这一个**。旧判据(文本 grep)在同一份变异上仍判它已覆盖,
    这正是本单要堵的洞。
    """
    original = CI_WORKFLOW.read_text()
    victim = "tests/test_pytest_coverage_ratchet.py"
    argument = f"            {victim} \\\n"
    assert original.count(argument) == 1, "变异锚点不唯一,请更新这条用例"

    mutated = original.replace(argument, "")
    mutated = mutated.replace(
        "      - name: Guard top-level pytest discovery coverage",
        f"      # TODO: 恢复 {victim}\n"
        "      - name: Guard top-level pytest discovery coverage",
    )
    workflow = tmp_path / "ci.yml"
    workflow.write_text(mutated)

    executed = ci_executed_tests.executed_test_files(workflow)
    mentioned = ci_executed_tests.mentioned_test_files(workflow)

    assert victim not in executed, "从命令里删掉后仍被判已覆盖 —— 假阳性回来了"
    assert victim in mentioned, "旧判据(文本出现过)在同一变异上仍是绿的"
    assert ci_executed_tests.executed_test_files(CI_WORKFLOW) - executed == {victim}, (
        "变异只应影响这一个文件;影响面不同说明解析被这次改动带偏了"
    )


def test_old_criterion_would_have_missed_the_ghosts(tmp_path):
    """把「旧判据会放行」钉住,免得将来有人悄悄换回文本 grep。"""
    script = (
        "echo tests/test_ghost_in_echo.py\n"
        "# pytest tests/test_ghost_in_comment.py\n"
        "pytest tests/test_real.py"
    )
    body = "\n".join("          " + line for line in script.split("\n"))
    workflow = tmp_path / "ci.yml"
    workflow.write_text("jobs:\n  j:\n    steps:\n      - run: |\n" + body + "\n")

    mentioned = ci_executed_tests.mentioned_test_files(workflow)
    executed = ci_executed_tests.executed_test_files(workflow)

    assert mentioned - executed == {
        "tests/test_ghost_in_echo.py",
        "tests/test_ghost_in_comment.py",
    }


def test_step_file_count_labels_match_the_actual_command():
    """step 名字里的「(N files)」必须等于该 step 真的传给 pytest 的文件数。

    这类数字是装饰性的、没人校验,于是会静静地漂:本次修复时 tier 3 的标签写着
    71,`run:` 块里实际列着 76 个(差 5,历史累积)。读的人会拿它当清单长度的判据,
    所以让它自己变红,而不是靠人去数。
    """
    import re

    import yaml

    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    mismatched = []
    for job in (workflow.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            if not isinstance(step, dict) or not step.get("run"):
                continue
            name = step.get("name") or ""
            label = re.search(r"\((\d+)\s+files\)", name)
            if not label:
                continue
            actual = ci_executed_tests.executed_in_script(step["run"])
            if int(label.group(1)) != len(actual):
                mismatched.append((name, int(label.group(1)), len(actual)))

    assert mismatched == [], (
        "这些 step 的文件数标签与实际不符(标签, 实际):\n"
        + "\n".join(f"  {n}: 标签={l} 实际={a}" for n, l, a in mismatched)
    )
