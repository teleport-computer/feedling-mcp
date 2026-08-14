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

import re
import sys
from pathlib import Path

# conftest 的 autouse fixture 会 import 后端模块,所以即使本文件只读文本,
# 也必须自带这行引导 —— 否则收集期就 ModuleNotFoundError。
# (`tests/conftest.py` 的 `_PURE_UNIT` 注释专门提醒过这一点。)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".github" / "pytest-uncovered-baseline.txt"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# 豁免名单允许的最大条数。
#
# ⚠️ 这个数**只许往下改**。调大它意味着又有一个测试文件退出了 CI ——
# 那需要在 PR 里说明理由,而不是顺手 +1。缩小名单时请一并把这个数改小,
# 否则棘轮会松掉。
MAX_EXEMPTED = 467


def _baseline_entries() -> list[str]:
    return [
        line.strip()
        for line in BASELINE.read_text().splitlines()
        if line.strip()
    ]


def _ci_named_tests() -> set[str]:
    """ci.yml 里显式点名的测试文件 —— 与守卫用的正则保持一致。"""
    return set(
        re.findall(r"tests/test_[A-Za-z0-9_]+\.py", CI_WORKFLOW.read_text())
    )


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
