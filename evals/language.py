"""花园语言判定 eval（宿主侧）—— 跑**内核随包分发的那份语料**，验 io 自己的取证。

## 分界线

2026-08-24 之后，判据本身搬进了内核（``memgarden.garden_language``），因为**错的是
算法，不是 io 的数据** —— 任何接入方都要做同一件判断，判据留在宿主等于让每个接入方
各踩一遍那次事故。

留在 io 的是**取证**：桶名从哪张表读、没有桶时看身份卡还是 locale、证据门槛多少。
这些是宿主的数据形状，内核碰不到。

所以这条 eval 的形状是：**语料用内核的，判定器用 io 的。**

    memgarden.contract 里的语料（随 wheel 分发）
        │
        └──→ io 的 garden_language_decision()   ←── 这条 eval 测的是它

两边守同一条线。要是 io 哪天在取证层面又发明了一套判据（比如读桶名之前先做个
"归一化"把英文桶全折叠掉），内核的语料会立刻把它照出来 —— 而只跑内核自己的 eval
是照不出来的，那边用的是内核自己的判定器。

## 为什么语料在包里而不在这个仓库

早先这份语料放在内核的**源码仓库**里，io 的 CI 上没有那个仓库，于是这条检查只会
打印「找不到语料，跳过」并返回 0 —— 看起来绿的，其实什么都没验。
**装饰性的检查比没有检查更糟**：它让人以为有防护。

现在语料作为包数据随 wheel 走，``pip install`` 之后就在，CI 上真的会跑。

跑法：``python3 evals/language.py``（需要装好 backend/requirements.txt）
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

from memgarden.contract import run_garden_language_contract  # noqa: E402

from chat.reply_language import garden_language_decision  # noqa: E402


def _decider(buckets, fallbacks):
    """把内核语料的输入形状，翻译成 io 的取证入口。

    语料给的 ``fallbacks`` 是"没有桶时按优先级看的其它信号"。io 这边第一级是
    ``identity.language_preference``，第二级是客户端 locale，所以接在那两处。
    """
    rest = list(fallbacks)
    identity = {"language_preference": rest[0]} if rest and rest[0] else {}
    locale = next((f for f in rest[1:] if f), "")
    d = garden_language_decision(identity, existing_buckets=buckets, locale=locale)
    # io 的依据名更细（``reply_language:<source>``）。契约只严格比对
    # ``existing_buckets`` 那一档（事故就出在那档）；兜底档只看"是不是走了兜底"。
    basis = d["basis"]
    if basis.startswith("reply_language:"):
        basis = "default" if basis.endswith(":default") else "fallback"
    return {"locale": d["locale"], "basis": basis}


def main() -> int:
    print("语料：memgarden.contract（随 wheel 分发）")
    print("判定器：io 的 chat.reply_language.garden_language_decision\n")
    _, fails = run_garden_language_contract(_decider)
    if fails:
        print(f"  失败：{', '.join(fails)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
