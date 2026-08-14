# Memory Garden 内核提取 实施计划

**Goal:** 把 Memory Garden 的判断力（什么值得记 / 怎么归桶 / 挑哪几张 / 要不要整理 / 怎么整理）
从 io 后端提取成独立包 `backend/memory_garden/`，让 V1 consumer、V2 worker、genesis 三条线
共用同一份判断规则，消除现有的两套落卡实现。

**Architecture:** 内核是纯函数包，不 import 任何 io 模块（db / identity / enclave /
accounts / bootstrap / core.store）。所有外部输入由调用方传参。存储走 port，
适配器实现留在 io。原位置全部保留 re-export，保证现有调用路径零改动。

**Tech Stack:** Python 3.10，无新依赖。`backend/` 已在 sys.path 上
（`tools/chat_resident_consumer.py:132`、`tests/conftest.py:61`），
所以 `backend/memory_garden/` 可直接 `from memory_garden import ...`。

## Global Constraints

- **内核不 import io 模块**：db / identity / accounts / bootstrap / enclave /
  core.store / debug_trace。由 Task 1 的守卫测试自动化。
- **行为逐字节不变**：本计划全部批次都是搬迁与结构调整，不改任何判断逻辑。
  每批以 golden fixtures 对照验收。
- **原位置保留 re-export**：现有 import 路径（`from memory.card_text import ...`、
  `from core.protocol_leak import ...` 等）必须继续可用，含被跨模块引用的私有名。
- **不合并 test 分支**：本分支只推送，合并由 hx 拍板。
- **策略差异不可抹平**：日常聊天 / 历史导入 / 用户整理的档案三把尺子必须保持不同，
  详见 `docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md` 第二节。

---

## 批次总览

| 批 | 内容 | 风险 | 本轮做? |
|---|---|---|---|
| 1 | 包骨架 + 搬 9 个纯模块 + re-export + 守卫测试 | 极低 | ✅ |
| 2 | prompt 三件套进包，identity 依赖改传参 | 低 | ✅ |
| 3 | 策略档位：genesis 与 capture 共用判断规则（消半拟合） | 中 | ✅ |
| 4 | 存储 port + 适配器能力声明 | 中 | ✅ 只加接口，不切流 |
| 5 | 切 V2 read/tool → capture → dream | 高 | ⛔ 等拍板 |
| 6 | 切 V1 resident（hosted/VPS 共用文件） | 高 | ⛔ 等拍板 |
| 7 | 切 genesis（onboarding / add_memory / keep_all / recheck） | 高 | ⛔ 等拍板 |

> **批 7 的一条具体待办（本轮实施中查出来的）**：
> 除了「什么值得记」那把尺子，**结构性规则也重复了一份**。最硬的证据是语言规则——
> 同一条要求在两边各写一遍，连标点风格都不同：
>
> ```
> capture:  语言：所有字段（bucket/threads/summary/content）用 TA 跟你对话的语言记——
>           中文对话就用中文（用「宠物」不是「pets」、「旅行」不是「travel」），英文对话用英文；
>           只有专有名词/品牌名/TA 的原话才保留原文。
> genesis:  语言:bucket/threads/summary/content 用素材原文的语言——中文素材就用中文
>           (用「宠物」不是「pets」),别归成英文桶/线索;专有名词/原话保留原文。
> ```
>
> **尺子本来就该不同，但语言规则应该完全一样。** 这类结构性规则（语言、字段语义、
> 去重口径）才是真正该合并的部分。
>
> ⚠️ **本轮不动**：统一措辞等于改 prompt，而 prompt 行为的 bug 单测抓不到，
> 只有真模型 e2e 能暴露（capture/migrate 的单测都 stub 了 agent）。
> 合并这类规则必须配一次真模型 e2e，放在批 7 一起做。
| 8 | dream_scheduler 拆两半 | 中 | ⛔ 等拍板 |
| 9 | CLI / MCP 壳 | 低 | ⛔ 摸开源时再做 |

批 5-8 会真正切换调用路径，动到写入与 hosted/VPS 共用文件，留待 hx 回来拍板。
批 1-4 全部是「新增结构 + 保持旧路径可用」，正常流程逐字节不变。

---

## 文件结构

```
backend/memory_garden/
├── __init__.py             公开 API 汇总导出
├── types.py                ← memory/source_policy.py（来源与 capture mode 枚举）
├── text/
│   ├── __init__.py
│   ├── protocol_leak.py    ← core/protocol_leak.py（协议泄漏证据原语）
│   ├── self_thinking.py    ← core/self_thinking.py（思维链剥离）
│   ├── card_guard.py       ← memory/card_guard.py（字段级泄漏检测）
│   └── card_text.py        ← memory/card_text.py（占位符/模板抄回检测）
├── prompts/
│   ├── __init__.py
│   └── buckets.py          ← memory/prompts_v1.py（桶指引与语言归一）
├── scoring/
│   ├── __init__.py
│   ├── relevance.py        ← context_memory_selection.py（相关性打分）
│   └── selector.py         ← memory_index_selector.py（选卡）
└── guards/
    ├── __init__.py
    └── dream_gates.py      ← memory/dream_gates.py（做梦出口硬闸）
```

搬迁后原路径全部改为 re-export，内容不变。

**依赖闭包已验证**（本计划撰写时逐个 grep 确认）：这 9 个模块只依赖标准库和彼此，
`core/self_thinking.py`(253)、`core/protocol_leak.py`(280)、`memory/prompts_v1.py`(146)
三个传递依赖均为零外部依赖。合计 2122 行。

---

### Task 1: 包骨架与守卫测试

**Files:**
- Create: `backend/memory_garden/__init__.py`
- Create: `tests/test_memory_garden_purity.py`

**Interfaces:**
- Produces: 包 `memory_garden` 可导入；`tests/test_memory_garden_purity.py::test_kernel_imports_no_io`
  作为后续所有批次的硬指标守卫。

- [ ] **Step 1: 写守卫测试（先失败）**

```python
"""内核纯度守卫：memory_garden 包不得 import 任何 io 模块。

这是《内核提取》验收标准第 ② 条的自动化。一旦包里出现 import db 或
import identity.user_naming，后面所有目标都塌了 —— 用测试钉死，不靠人盯。
"""
import ast
import pathlib

_FORBIDDEN_ROOTS = frozenset({
    "db", "identity", "accounts", "bootstrap", "enclave",
    "debug_trace", "hosted_runtime", "provider_client",
})
# core 下只允许这些被搬进包的纯模块（它们自己也不 import io）
_ALLOWED_CORE = frozenset({"protocol_leak", "self_thinking"})


def _kernel_files():
    root = pathlib.Path(__file__).resolve().parents[1] / "backend" / "memory_garden"
    return sorted(root.rglob("*.py"))


def _imported_roots(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 相对 import，包内引用，放行
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_kernel_imports_no_io():
    files = _kernel_files()
    assert files, "memory_garden 包为空——搬迁没做或路径不对"
    offenders = []
    for path in files:
        for root in _imported_roots(path):
            if root in _FORBIDDEN_ROOTS:
                offenders.append(f"{path.name}: import {root}")
            if root == "core":
                offenders.append(f"{path.name}: import core.* —— 应改为包内相对引用")
            if root == "memory":
                offenders.append(f"{path.name}: import memory.* —— 应改为包内相对引用")
    assert not offenders, "内核里出现了 io 依赖:\n" + "\n".join(offenders)


def test_kernel_has_no_side_effect_imports():
    """包内模块不得在 import 期做 I/O（读文件、连库、发请求）。

    逐个 import 一遍，任何异常都说明有副作用或漏依赖。
    """
    import importlib
    root = pathlib.Path(__file__).resolve().parents[1] / "backend" / "memory_garden"
    for path in _kernel_files():
        rel = path.relative_to(root.parent).with_suffix("")
        module_name = ".".join(rel.parts)
        importlib.import_module(module_name)
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_purity.py -v`
Expected: FAIL —— "memory_garden 包为空"

- [ ] **Step 3: 建包骨架**

```python
# backend/memory_garden/__init__.py
"""Memory Garden 内核 —— 记忆的判断力，与宿主环境无关。

这个包只做判断，不做执行:
  · 什么值得记（三个策略档位各一把尺子）
  · 怎么归桶起线索、怎么校验模型输出、怎么去重
  · 这轮该想起哪几张（打分排序）
  · 要不要整理了、整理时怎么合并消矛盾

不在这里的（由调用方提供）:
  加解密与 enclave · 身份装配 · 所有权校验 · gates · 审计 ·
  锁与事务 · 捞聊天记录 · 定时器 · 真正调模型

硬指标: 本包不 import 任何 io 模块。由 tests/test_memory_garden_purity.py 守卫。
"""
```

- [ ] **Step 4: 建子包目录与空 `__init__.py`**

```bash
cd backend/memory_garden
for d in text prompts scoring guards; do mkdir -p $d && touch $d/__init__.py; done
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_purity.py -v`
Expected: PASS（包非空、无违规 import）

- [ ] **Step 6: 提交**

```bash
git add backend/memory_garden tests/test_memory_garden_purity.py
git commit -m "feat(memory-garden): 包骨架 + 内核纯度守卫测试"
```

---

### Task 2: 搬零依赖模块（source_policy / dream_gates / prompts_v1 / relevance）

**Files:**
- Create: `backend/memory_garden/types.py`（内容来自 `backend/memory/source_policy.py`）
- Create: `backend/memory_garden/guards/dream_gates.py`（来自 `backend/memory/dream_gates.py`）
- Create: `backend/memory_garden/prompts/buckets.py`（来自 `backend/memory/prompts_v1.py`）
- Create: `backend/memory_garden/scoring/relevance.py`（来自 `backend/context_memory_selection.py`）
- Modify: 上述四个原文件 → re-export

**Interfaces:**
- Produces: `memory_garden.types` 的 `MEMORY_SOURCE_VALUES` / `MEMORY_CAPTURE_MODE_VALUES` /
  `RESIDENT_ABSORB_SOURCE` / `RESIDENT_PATCH_SOURCE`；
  `memory_garden.guards.dream_gates` 的 `known_id_in_text` / `result_id_leak` 等全部公开名；
  `memory_garden.prompts.buckets` 的 `COMMON_BUCKETS_GUIDANCE_V1` /
  `normalize_bucket_language` / `COMMON_BUCKETS_V1`，以及私有名
  `_text_is_chinese` / `_COMMON_BUCKETS_ZH` / `_COMMON_BUCKETS_EN`（有跨模块引用，必须显式 re-export）；
  `memory_garden.scoring.relevance` 的 `memory_relevance_details` 等。

- [ ] **Step 1: 逐个 `git mv` 搬文件**

```bash
cd backend
git mv memory/source_policy.py            memory_garden/types.py
git mv memory/dream_gates.py              memory_garden/guards/dream_gates.py
git mv memory/prompts_v1.py               memory_garden/prompts/buckets.py
git mv context_memory_selection.py        memory_garden/scoring/relevance.py
```

⚠️ 用 `git mv` 而不是复制删除，保住 blame 历史。
⚠️ 本项目踩过：`git mv` 之后再改内容必须重新 `git add`，否则 commit 进旧内容。

- [ ] **Step 2: 在原位置写 re-export**

```python
# backend/memory/source_policy.py
"""已搬至 memory_garden.types —— 此处保留 re-export 以兼容现有 import 路径。"""
from memory_garden.types import *  # noqa: F401,F403
from memory_garden.types import (  # noqa: F401
    MEMORY_SOURCE_VALUES,
    MEMORY_CAPTURE_MODE_VALUES,
    RESIDENT_ABSORB_SOURCE,
    RESIDENT_PATCH_SOURCE,
)
```

```python
# backend/memory/dream_gates.py
"""已搬至 memory_garden.guards.dream_gates —— 保留 re-export。"""
from memory_garden.guards.dream_gates import *  # noqa: F401,F403
```

```python
# backend/memory/prompts_v1.py
"""已搬至 memory_garden.prompts.buckets —— 保留 re-export。

私有名 _text_is_chinese / _COMMON_BUCKETS_ZH / _COMMON_BUCKETS_EN 有跨模块引用
（card_guard.py:27、tests/test_capture_prompt_v1.py:244,263），`import *` 不覆盖，
必须显式列出。
"""
from memory_garden.prompts.buckets import *  # noqa: F401,F403
from memory_garden.prompts.buckets import (  # noqa: F401
    _text_is_chinese,
    _COMMON_BUCKETS_ZH,
    _COMMON_BUCKETS_EN,
)
```

```python
# backend/context_memory_selection.py
"""已搬至 memory_garden.scoring.relevance —— 保留 re-export。"""
from memory_garden.scoring.relevance import *  # noqa: F401,F403
```

- [ ] **Step 3: 跑纯度守卫 + 相关既有测试**

Run:
```bash
cd backend && python -m pytest ../tests/test_memory_garden_purity.py \
  ../tests/test_capture_prompt_v1.py ../tests/test_context_memories.py -v
```
Expected: 全 PASS。若 `_COMMON_BUCKETS_ZH` 报 ImportError，说明 Step 2 的显式 re-export 漏了。

- [ ] **Step 4: 全量收集，确认没有 import 断裂**

Run: `cd backend && python -m pytest ../tests --collect-only -q 2>&1 | tail -5`
Expected: 收集数量与搬迁前一致，无 collection error。

⚠️ 本项目的 `tests/conftest.py` 在**连不上 Postgres 时**才启用 `_PURE_UNIT` 白名单，
本地有 docker 环境时应确保 Postgres 已起，否则收集数会偏少、"全绿"是假的。

- [ ] **Step 5: 提交**

```bash
git add -A backend/ tests/
git commit -m "refactor(memory-garden): 搬入四个零依赖纯模块，原位置 re-export"
```

---

### Task 3: 搬有内部依赖的模块（protocol_leak / self_thinking / card_guard / card_text / selector）

**Files:**
- Create: `backend/memory_garden/text/protocol_leak.py`（来自 `backend/core/protocol_leak.py`）
- Create: `backend/memory_garden/text/self_thinking.py`（来自 `backend/core/self_thinking.py`）
- Create: `backend/memory_garden/text/card_guard.py`（来自 `backend/memory/card_guard.py`）
- Create: `backend/memory_garden/text/card_text.py`（来自 `backend/memory/card_text.py`）
- Create: `backend/memory_garden/scoring/selector.py`（来自 `backend/memory_index_selector.py`）
- Modify: 五个原文件 → re-export

**Interfaces:**
- Consumes: Task 2 产出的 `memory_garden.prompts.buckets`
- Produces: `memory_garden.text.card_text` 的字段校验入口、
  `memory_garden.text.card_guard` 的泄漏检测入口、
  `memory_garden.scoring.selector` 的选卡入口

- [ ] **Step 1: 搬文件**

```bash
cd backend
git mv core/protocol_leak.py       memory_garden/text/protocol_leak.py
git mv core/self_thinking.py       memory_garden/text/self_thinking.py
git mv memory/card_guard.py        memory_garden/text/card_guard.py
git mv memory/card_text.py         memory_garden/text/card_text.py
git mv memory_index_selector.py    memory_garden/scoring/selector.py
```

- [ ] **Step 2: 把包内 import 改成相对引用**

`memory_garden/text/card_guard.py`：
```python
from . import protocol_leak
from ..prompts.buckets import _text_is_chinese
```
（原为 `from core import protocol_leak` / `from memory.prompts_v1 import _text_is_chinese`）

`memory_garden/text/card_text.py`：
```python
from . import card_guard
from . import self_thinking
from ..prompts.buckets import normalize_bucket_language
```
（原为 `from core import self_thinking` / `from memory import card_guard` /
`from memory.prompts_v1 import normalize_bucket_language`）

`memory_garden/scoring/selector.py`：
```python
from .relevance import memory_relevance_details
```
（原为 `from context_memory_selection import memory_relevance_details`）

⚠️ 改完必须重新 `git add` —— 本项目踩过 `git mv` 后改内容忘记 re-add，
commit 进旧内容并推上共享分支。

- [ ] **Step 3: 五个原位置写 re-export**

```python
# backend/core/protocol_leak.py
"""已搬至 memory_garden.text.protocol_leak —— 保留 re-export。"""
from memory_garden.text.protocol_leak import *  # noqa: F401,F403
```

同形写 `core/self_thinking.py`、`memory/card_guard.py`、`memory/card_text.py`、
`memory_index_selector.py` 四个。

- [ ] **Step 4: 跑守卫 + 相关测试**

Run:
```bash
cd backend && python -m pytest ../tests/test_memory_garden_purity.py \
  ../tests/test_memory_index_selector.py ../tests/test_context_memories.py -v
```
Expected: 全 PASS。守卫测试会检查包内没有 `import core.*` / `import memory.*` 残留。

- [ ] **Step 5: 全量收集 + 跑与这几个模块相关的全部测试**

Run:
```bash
cd backend && python -m pytest ../tests --collect-only -q 2>&1 | tail -3
cd backend && python -m pytest ../tests -k "card or leak or thinking or selector or context" -q
```

- [ ] **Step 6: 提交**

```bash
git add -A backend/ tests/
git commit -m "refactor(memory-garden): 搬入 text/scoring 模块，包内改相对引用"
```

---

### Task 4: prompt 三件套进包，identity 依赖改传参

**Files:**
- Create: `backend/memory_garden/prompts/capture.py`（来自 `backend/memory/capture_prompt_v1.py`）
- Create: `backend/memory_garden/prompts/dream.py`（来自 `backend/memory/dream_prompt_v1.py`）
- Create: `backend/memory_garden/prompts/migrate.py`（来自 `backend/memory/migrate_prompt_v1.py`）
- Modify: 三个原文件 → re-export
- Test: `tests/test_memory_garden_prompt_params.py`

**Interfaces:**
- Consumes: `memory_garden.prompts.buckets`
- Produces: `build_capture_prompt(..., naming_rule, user_name)` /
  `build_dream_prompt(..., naming_rule, user_name)` —— 称呼规则改为显式入参，
  不再由内核 `from identity.user_naming import ...`

**背景**：这三个模块当前 `from identity.user_naming import _naming_rule, sanitize_user_name`，
是内核纯度的最后一处硬伤。改法是把 naming rule 与 user name 作为参数传入，
调用方（io 侧）负责装配。

- [ ] **Step 1: 写参数化测试（先失败）**

```python
"""prompt 构建不得依赖 identity 模块——称呼规则由调用方传入。"""
import pytest


def test_capture_prompt_takes_naming_rule_as_param():
    from memory_garden.prompts.capture import build_capture_prompt
    text = build_capture_prompt(
        ai_name="io",
        user_name="老王",
        naming_rule="叫他老王。",
        cards="（无）",
        window_text="用户：今天开了一天会\n我：辛苦了",
    )
    assert "老王" in text
    assert "叫他老王。" in text


def test_dream_prompt_takes_naming_rule_as_param():
    from memory_garden.prompts.dream import build_dream_prompt
    text = build_dream_prompt(
        ai_name="io",
        user_name="老王",
        naming_rule="叫他老王。",
        cards="（无）",
        recent_conversations="（无）",
    )
    assert "叫他老王。" in text
```

- [ ] **Step 2: 跑，确认失败（模块不存在）**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_prompt_params.py -v`

- [ ] **Step 3: 搬文件并改签名**

```bash
cd backend
git mv memory/capture_prompt_v1.py   memory_garden/prompts/capture.py
git mv memory/dream_prompt_v1.py     memory_garden/prompts/dream.py
git mv memory/migrate_prompt_v1.py   memory_garden/prompts/migrate.py
```

三个文件里：删掉 `from identity.user_naming import _naming_rule, sanitize_user_name`，
把函数签名改成显式接收 `naming_rule: str` 与已 sanitize 过的 `user_name: str`。
包内 import 改相对：`from .buckets import COMMON_BUCKETS_GUIDANCE_V1`。

- [ ] **Step 4: 原位置 re-export，并在 re-export 层补回旧签名**

```python
# backend/memory/capture_prompt_v1.py
"""已搬至 memory_garden.prompts.capture —— 保留 re-export 与旧签名。

内核不再 import identity；此处这层薄壳负责装配称呼规则，
让现有调用方（V1 consumer、V2 worker、genesis）无需改动。
"""
from identity.user_naming import _naming_rule, sanitize_user_name  # noqa: F401
from memory_garden.prompts import capture as _kernel
from memory_garden.prompts.capture import parse_capture_cards  # noqa: F401


def build_capture_prompt(*, ai_name, user_name, cards, window_text, **kwargs):
    safe_name = sanitize_user_name(user_name)
    return _kernel.build_capture_prompt(
        ai_name=ai_name,
        user_name=safe_name,
        naming_rule=_naming_rule(safe_name),
        cards=cards,
        window_text=window_text,
        **kwargs,
    )
```

dream / migrate 同形。

⚠️ 实际参数名以搬迁时读到的源码为准，本步骤须先 `git show` 确认现有签名再落笔。

- [ ] **Step 5: 跑新测试 + 全部 prompt 相关既有测试**

Run:
```bash
cd backend && python -m pytest ../tests/test_memory_garden_prompt_params.py \
  ../tests/test_memory_garden_purity.py \
  ../tests/test_capture_prompt_v1.py -v
```
Expected: 全 PASS。纯度守卫此时应确认 `memory_garden` 内再无 `identity` 引用。

- [ ] **Step 6: 提交**

```bash
git add -A backend/ tests/
git commit -m "refactor(memory-garden): prompt 三件套进包，称呼规则改为显式入参"
```

---

### Task 5: 策略档位 —— 消除 genesis 与 capture 的半拟合

**Files:**
- Create: `backend/memory_garden/policies.py`
- Create: `tests/test_memory_garden_policies.py`
- Modify: `backend/memory_garden/prompts/capture.py`（接受 policy 参数）

**Interfaces:**
- Produces: `POLICIES` 字典与 `get_policy(name)`，档位名
  `conversation_capture` / `history_import` / `curated_archive`；
  每个档位提供 `selection_rubric`（那把尺子的文字）、`max_cards`、`prefer_merge`、
  `keep_dates`、`seed_threads_from_tags` 等字段。

**背景**：这是本计划的核心价值点。当前 `genesis/prompts.py`(364) 与
`memory/capture_prompt_v1.py`(290) 各写一套「什么值得记」，共用的只有桶指引和写入口。
本 Task 把三把尺子收进一处，用参数区分，**但不抹平差异**——
统一成任何一把都是事故（详见 DESIGN 第二节）。

- [ ] **Step 1: 写档位测试（先失败）**

```python
"""三个策略档位必须共用同一套结构，但尺子各不相同。"""
import pytest

from memory_garden.policies import get_policy, POLICIES


def test_three_policies_exist():
    assert set(POLICIES) == {"conversation_capture", "history_import", "curated_archive"}


def test_conversation_capture_is_few_and_thick():
    p = get_policy("conversation_capture")
    assert p.max_cards <= 2, "日常聊天是少而厚，不能放开张数"
    assert p.prefer_merge is True


def test_curated_archive_keeps_everything():
    p = get_policy("curated_archive")
    assert p.max_cards is None, "用户整理的档案宁多勿漏，不能有张数上限"
    assert p.keep_dates is True, "档案里的日期要原样保留"
    assert p.seed_threads_from_tags is True


def test_history_import_filters_one_off_events():
    p = get_policy("history_import")
    assert "一次性" in p.selection_rubric or "闲聊" in p.selection_rubric


def test_policies_do_not_share_the_same_rubric():
    """三把尺子的文字必须真的不同 —— 统一了就是本计划要防的那个事故。"""
    rubrics = {name: get_policy(name).selection_rubric for name in POLICIES}
    assert len(set(rubrics.values())) == 3, f"尺子被抹平了: {rubrics}"
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_policies.py -v`

- [ ] **Step 3: 实现 policies.py**

尺子文字**逐字取自现有实现**，不重写：
- `conversation_capture` ← `memory/capture_prompt_v1.py` 现有措辞
- `history_import` ← `genesis/prompts.py` 的 `FACT_MAP_PROMPT` 过滤段
- `curated_archive` ← `genesis/prompts.py` 的 `FACT_MAP_KEEP_ALL_SUFFIX` /
  `FACT_WRITE_KEEP_ALL_SUFFIX`

```python
"""三个策略档位 —— 共用一套结构，尺子各不相同。

★ 这三把尺子必须保持不同。统一成「少而厚」→ 用户手动整理的 100 条只落 2 张；
  统一成「宁多勿漏」→ 日常聊天每句废话都变成卡。两种都是事故。
  见 docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md 第二节。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapturePolicy:
    name: str
    selection_rubric: str      # 那把尺子的文字，直接进 prompt
    max_cards: int | None      # None = 不限张数
    prefer_merge: bool         # 并入优于新增
    keep_dates: bool           # 原样保留 occurred_at
    seed_threads_from_tags: bool
```

（尺子文字在实现时从上述来源逐字复制，此处不重复粘贴以免与源漂移。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_policies.py -v`

- [ ] **Step 5: 让 capture prompt 接受 policy 参数（默认保持现行为）**

`build_capture_prompt` 增加 `policy: CapturePolicy | None = None`，
为 `None` 时用 `conversation_capture` —— **默认值保证现有调用行为逐字节不变**。

- [ ] **Step 6: 跑全部 prompt 与 genesis 相关测试**

Run:
```bash
cd backend && python -m pytest ../tests -k "capture or genesis or prompt or polic" -q
```

- [ ] **Step 7: 提交**

```bash
git add -A backend/ tests/
git commit -m "feat(memory-garden): 三个策略档位收进一处，尺子保持不同"
```

---

### Task 6: 存储 port 与适配器能力声明

**Files:**
- Create: `backend/memory_garden/storage.py`
- Create: `tests/test_memory_garden_storage_port.py`

**Interfaces:**
- Produces: `StoragePort` 协议、`Capabilities` 数据类、`Degradation` 上报结构

**背景**：接口现在定，成本几乎为零；等适配器都写完再改要全部返工。
本 Task **只定义接口，不接任何真实存储**，io 侧切换留待批 5-8。

- [ ] **Step 1: 写接口测试（先失败）**

```python
"""存储 port：能力声明 + 显式降级。"""
from memory_garden.storage import Capabilities, Degradation, plan_degradations


def test_full_capability_adapter_has_no_degradation():
    caps = Capabilities(
        supports_supersede=True,
        supports_atomic_batch=True,
        supports_custom_fields=True,
        supports_metadata_sort=True,
    )
    assert plan_degradations(caps) == []


def test_missing_supersede_degrades_explicitly():
    caps = Capabilities(
        supports_supersede=False,
        supports_atomic_batch=True,
        supports_custom_fields=True,
        supports_metadata_sort=True,
    )
    degradations = plan_degradations(caps)
    assert len(degradations) == 1
    d = degradations[0]
    assert isinstance(d, Degradation)
    assert d.capability == "supports_supersede"
    assert d.fallback         # 必须写明降级成什么
    assert d.risk             # 必须写明后果 —— 不允许静默降级


def test_every_missing_capability_is_reported():
    caps = Capabilities(False, False, False, False)
    assert len(plan_degradations(caps)) == 4
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_storage_port.py -v`

- [ ] **Step 3: 实现 storage.py**

```python
"""存储 port —— 内核只对着这个接口说话。

后端不一定是数据库，也可能是另一个记忆系统（mem0 / engram / 用户自己的库）。
对方不一定支持我们的全部操作，所以适配器要声明能力，内核遇到不支持的就降级。

★ 降级必须显式上报，不能静默 —— 静默降级会让用户以为功能都在，
  实际记忆库在悄悄变乱。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    supports_supersede: bool
    supports_atomic_batch: bool
    supports_custom_fields: bool
    supports_metadata_sort: bool


@dataclass(frozen=True)
class Degradation:
    capability: str
    fallback: str
    risk: str
```

`plan_degradations(caps)` 对每个 False 的能力产出一条 `Degradation`，
文案写明降级成什么、后果是什么。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_memory_garden_storage_port.py -v`

- [ ] **Step 5: 提交**

```bash
git add -A backend/ tests/
git commit -m "feat(memory-garden): 存储 port 与适配器能力声明"
```

---

### Task 7: 全量回归与 docker e2e

**Files:** 无新增，只跑验证。

- [ ] **Step 1: 确认 Postgres 已起（否则测试收集会静默变少）**

Run: `docker ps | grep -i postgres`
若未起，用本地 compose 起来后再继续。

- [ ] **Step 2: 全量收集，与基线对照**

```bash
cd backend && python -m pytest ../tests --collect-only -q 2>&1 | tail -3
```
Expected: 收集数量 ≥ 改造前（本分支创建时先记录基线数）。

- [ ] **Step 3: 跑全量单测**

```bash
cd backend && python -m pytest ../tests -q 2>&1 | tail -20
```

- [ ] **Step 4: 记录结果，未通过项逐条归因**

区分「本次改动引入」与「基线本来就红」——后者不算本批回归，但要写进交付说明。

- [ ] **Step 5: 提交（若有修复）**

---

## 本轮不做（等 hx 拍板）

- 批 5-8：切 V2 / V1 / genesis 的真实调用路径、拆 dream_scheduler
- 批 9：CLI / MCP 壳
- 合并到 test：**本分支只推送，合并由 hx 决定**

## 留给 hx 拍的点（做成可切换，拍完可丝滑改）

1. **包的最终落点**：现在放 `backend/memory_garden/`（跟现有 import 风格一致、
   sys.path 已覆盖）。将来要独立发布就整目录搬出去 + 加 pyproject。
   如果希望现在就放仓库根，改动是一次 `git mv` + 一处 sys.path。
2. **策略档位的数量与命名**：现在三个（conversation_capture / history_import /
   curated_archive）。VPS resident 的记忆 recheck 是否算第四个档位，待定；
   `POLICIES` 是字典，加一个档位不影响任何既有调用。
3. **`ombre_brain_sync`**：已查明只存在于 OpenAPI 契约与 schema 测试，
   无任何写入点（`git grep` 全仓库确认）。当作历史枚举保留，不影响本批边界。
4. **降级上报送到哪**：现在 `plan_degradations` 只返回结构，不决定往哪报
   （日志 / 指标 / 用户可见）。接上时再定。
