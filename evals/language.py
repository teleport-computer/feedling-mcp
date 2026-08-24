"""花园语言判定 eval（宿主侧）—— 跑**内核那份语料**，验 io 自己的取证。

## 为什么它还留在 io，而语料不在

2026-08-24 之后，判据本身搬进了内核（``memgarden.garden_language``），因为**错的
是算法，不是 io 的数据** —— 任何接入方都要做同一件判断，判据留在宿主等于让每个
接入方各踩一遍那次事故。

留在 io 的是**取证**：桶名从哪张表读、没有桶时看身份卡还是 locale、证据门槛多少。
这些是宿主的数据形状，内核碰不到。

所以这条 eval 的形状是：**语料用内核的，判定器用 io 的。**

    内核语料 (memgarden/evals/corpus/gardens.jsonl)
        │
        └──→ io 的 garden_language_decision()   ←── 这条 eval 测的是它

这样两边守的是同一条线。要是 io 哪天在取证层面又自己发明了一套判据（比如在读桶名
之前先做了个"归一化"把英文桶全折叠掉），内核的语料会立刻把它照出来 —— 而只跑
内核自己的 eval 是照不出来的，那边用的是内核自己的判定器。

跑法：

    python3 evals/language.py

## 事故那条在语料里

内核语料里 ``g_incident_0824`` 就是事故当天真实的桶构成。它再红一次时，报告会
单独喊「**曾经真的发生过的事故，回归了**」。
"""
from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "backend"))

from chat.reply_language import garden_language_decision  # noqa: E402


def _kernel_evals_dir() -> pathlib.Path | None:
    """找到内核那份语料。

    两种情形：装成依赖时（.deps/memgarden 或 site-packages 旁）语料可能不随 wheel
    走 —— evals/ 不在包里，是仓库的东西。所以按仓库找，找不到就明说跳过，
    **不静默通过**：静默通过等于这条防线消失了而没人知道。
    """
    for base in (
        _REPO.parent.parent.parent / "memory-garden",   # worktrees/<repo>/<branch>/ → io/
        _REPO.parent / "memory-garden",
        pathlib.Path.home() / "Projects" / "io" / "memory-garden",
    ):
        d = base / "evals"
        if (d / "corpus" / "gardens.jsonl").exists():
            return d
    return None


def _decider(buckets, fallbacks):
    """把内核语料的输入形状，翻译成 io 的取证入口。

    语料给的 ``fallbacks`` 是"没有桶时按优先级看的其它信号"。io 这边第一级是
    ``identity.language_preference``，所以接在那儿。
    """
    identity = {}
    rest = list(fallbacks)
    if rest and rest[0]:
        identity = {"language_preference": rest[0]}
    locale = next((f for f in rest[1:] if f), "")
    d = garden_language_decision(identity, existing_buckets=buckets, locale=locale)
    # io 的依据名更细（``reply_language:<source>``）。语料只严格比对
    # ``existing_buckets`` 那一档（事故就出在那档）；兜底档只看"是不是走了兜底"，
    # 不比序号 —— io 的证据链顺序本来就跟内核不一样，比序号是在测无关的东西。
    basis = d["basis"]
    if basis.startswith("reply_language:"):
        basis = "default" if basis.endswith(":default") else "fallback"
    return {"locale": d["locale"], "basis": basis}


def main() -> int:
    ev = _kernel_evals_dir()
    if ev is None:
        print("⚠️  找不到内核仓库的 evals/corpus/gardens.jsonl —— **跳过，不等于通过**。")
        print("    这条 eval 需要 memgarden 的源码仓库（语料不随 wheel 分发）。")
        print("    本地把 memory-garden clone 到 io/ 下面即可。")
        return 0
    sys.path.insert(0, str(ev))
    import garden_language as kernel_eval  # noqa: E402

    print(f"语料：{ev / 'corpus' / 'gardens.jsonl'}")
    print("判定器：io 的 chat.reply_language.garden_language_decision\n")
    return kernel_eval.run(decider=_decider)


if __name__ == "__main__":
    raise SystemExit(main())
