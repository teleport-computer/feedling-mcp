---
document_lifecycle: historical
historical_reason: superseded
superseded_by: docs/PERCEPTION_ARCHITECTURE.zh.md
canonical_owner: docs/repository-cleanup/memory-perception-history.md
---
# 主动感知插件提取 · 第一步（只切业务）实施计划

> **HISTORICAL / PARTIALLY IMPLEMENTED, REMAINDER SUPERSEDED（2026-08-27 归档）**：
> 主干提取由 `ac7afd62` 至 `d97ece15` 的实现链完成；`c7cdae93` 另补齐 stale digest
> freshness。Task 7 计划直连的 `PERCEPTION_WAKE_SOURCES`、`is_significant_change`、
> `should_wake` 刻意保持未接线：reason 字符串与真实信号语义尚不等价，现行测试会阻止
> 未经决策的 IO 引用。本文的 checkbox、
> 分支、行号、一次性迁移步骤和预期输出只保留为实施证据，不得作为当前操作手册重放。
> 当前文件图见 `docs/PERCEPTION_ARCHITECTURE.zh.md`，长期边界见
> `docs/PERCEPTION_EXTRACTION_DESIGN.zh.md`，prompt owner 见
> `docs/PERCEPTION_PROMPT_ASSETS.zh.md`。CLI/MCP、新仓库、开源发布和上述直连接线均未
> 随本计划交付。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把主动感知的判断力从 IO 里收拢成一个独立的包 `perception_kernel`，V1（resident consumer）与 V2（hosted worker）共用同一份，**行为逐字节不变**。

**Architecture:** 照搬 `memory_garden` 已验证过的形态——纯函数内核 + io 侧适配层 + AST 纯度守卫 + 逐字节 golden。内核只做四种活：能力声明、字段投影与一瞥、历史计算、说明书文本。存取、加解密、鉴权、入队全部留在 io 侧。

**Tech Stack:** Python 3.11 / pytest / 现有 `backend/` 包结构（见 `CONTRIBUTING.md` §1–2 依赖方向）。

## Global Constraints

- **本批不改任何行为。** 所有 prompt 文本、工具 schema、HTTP 返回 JSON 必须逐字节不变；状态变化与 wake 决策必须语义等价。
- **内核不许 import 任何 io 模块。** 由 `tests/test_perception_kernel_purity.py`（AST 结构判据）钉死。允许的非标准库依赖只有 `core`。
- **V1 与 V2 必须共用同一份。** `tools/chat_resident_consumer.py:137` 已经把 `backend/` 加进 `sys.path`，所以 consumer 可以直接 import 内核，不需要 HTTP 兜圈子。
- **wake ≠ 该开口了。** 内核里不许出现「该说话了 / 值得告诉用户」式的判断或措辞；内核只产出「有件事发生了」。（2026-08-19 Seven 校正，见设计文档第一节。）
- **不新增能力。** 安静时段、每日 wake 上限、per-wake 开关的**收拢**属于本批，**启用新默认**不属于——迁移配置必须保持基线语义（安静时段关闭、上限无限、四个 wake 开关沿用 `controls_v2` 现值）。
- **包名 `perception_kernel` 是内部名。** 对外开源名等第三步再定，改包名是廉价操作。
- 新增测试文件后必须跑 `pytest --collect-only` 核对是否被 `tests/conftest.py` 的 `_PURE_UNIT` 白名单静默跳过（只在无 Postgres 时生效）。纯函数测试才加进白名单，需要 DB 的不要加。

**参照文档：** `docs/PERCEPTION_EXTRACTION_DESIGN.zh.md`（本批的需求设计）、`docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md`（同一套方法论的上一批）。

---

## File Structure

**新建（内核，纯函数、零 I/O）**

| 文件 | 职责 |
|---|---|
| `backend/perception_kernel/__init__.py` | 包入口，只做 re-export |
| `backend/perception_kernel/catalog.py` | 能力与信号的声明表（从 `perception/catalog.py` 整体搬入） |
| `backend/perception_kernel/fields.py` | agent 可见字段的投影与权限判据 |
| `backend/perception_kernel/glance.py` | 一瞥（全是 bool 的投影） |
| `backend/perception_kernel/history.py` | rollup / 趋势 / 显著变化 / 跨域看板 |
| `backend/perception_kernel/prompts.py` | 说明书文本——V1 与 V2 各自那几段的唯一出处 |
| `backend/perception_kernel/wake.py` | 「这次上报算不算一件事」「值不值得戳」的纯判据 |

**改为适配层（io 侧，保持现有导入路径不破）**

| 文件 | 变化 |
|---|---|
| `backend/perception/catalog.py` | 改为从内核 re-export |
| `backend/perception/agent_fields.py` / `permissions.py` | 同上 |
| `backend/perception/glance.py` / `history.py` | 同上 |
| `backend/perception/differ_v2.py` | 判据调内核，事件发射与 metrics 留在这 |
| `backend/perception/signal_state_v2.py` | 保留 DB / HMAC / `FOR UPDATE`，判据调内核 |
| `backend/model_api_runtime/v2/worker.py:876,902` | 感知那几句改成从内核取 |
| `backend/model_api_runtime/v2/context.py:159` | 同上 |
| `backend/capabilities/tool_schema.py:626` | 感知工具说明改成从内核取 |
| `tools/chat_resident_consumer.py:13011` | `_native_reachout_perception_context` 改成从内核取 |

---

### Task 0: 冻结基线——资产清单 + 逐字节 golden

没有这一步，后面每一批都无从验收。**这是整个计划的地基，不许跳。**

**Files:**
- Create: `docs/PERCEPTION_PROMPT_ASSETS.zh.md`
- Create: `scripts/dump_perception_baseline.py`
- Create: `tests/fixtures/perception_kernel/prompt_baseline.json`
- Create: `tests/test_perception_prompt_golden.py`

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces: fixture 文件 `tests/fixtures/perception_kernel/prompt_baseline.json`，键为
  `v2_wake_system` / `v2_scheduled_wake_system` / `v2_runtime_perception_policy` /
  `v2_tool_schema_perception`（dict，`{tool_name: description}`，覆盖
  `perception_snapshot`/`perception_recent_apps`/`perception_trend`/`perception_history`
  四个感知信号读取工具，供 Task 5 逐个搬时比对）/ `v1_reachout_context` /
  `v1_reachout_context_empty` / `v1_reachout_context_change_only`（分别覆盖
  `_native_reachout_perception_context` 的 board 分支、全空分支、`elif change:`
  回退分支）；后续每一批都比对它。
  （2026-08-20 review 修正：之前这里写的 `tool_schema_perception` 与 Step 2
  实际导出脚本的产出键不一致，已按脚本的真实产出改写。）

- [ ] **Step 1: 建一个基线 worktree**

```bash
git -C /Users/hx/Projects/io/feedling-mcp worktree add \
  /tmp/perception-baseline origin/test
```

- [ ] **Step 2: 写导出脚本**

`scripts/dump_perception_baseline.py`：

```python
"""把基线上的感知相关 prompt 文本导出成 golden fixture。

在 origin/test 的 checkout 上跑一次，产出的 JSON 就是「行为逐字节不变」的判据。
V2 的三段是模块级常量，直接取；V1 那段是函数，用固定输入调一次。
"""
from __future__ import annotations

import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(TOOLS))

from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import worker as v2_worker

import chat_resident_consumer as consumer

_V1_PRESENCE = {"place_label": "office", "motion_state": "walking"}
_V1_CHANGE = [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}]
_V1_DOMAINS = {"location": {"label": "office"}, "media": {"new_artist": True}}


def main() -> None:
    out = {
        "v2_wake_system": v2_worker._WAKE_SYSTEM_PROMPT,
        "v2_scheduled_wake_system": v2_worker._SCHEDULED_WAKE_SYSTEM_PROMPT,
        "v2_runtime_perception_policy": v2_context._RUNTIME_PERCEPTION_POLICY,
        "v1_reachout_context": consumer._native_reachout_perception_context(
            _V1_PRESENCE, _V1_CHANGE, _V1_DOMAINS
        ),
        "v1_reachout_context_empty": consumer._native_reachout_perception_context({}, [], None),
    }
    dest = pathlib.Path(sys.argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(f"wrote {dest} ({len(out)} entries)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 在基线 worktree 上跑它，把 fixture 写回本分支**

```bash
cp scripts/dump_perception_baseline.py /tmp/perception-baseline/scripts/
cd /tmp/perception-baseline && python3 scripts/dump_perception_baseline.py \
  /Users/hx/Projects/io/worktrees/feedling-mcp/feat-perception-extraction/tests/fixtures/perception_kernel/prompt_baseline.json
```

期望输出：`wrote .../prompt_baseline.json (5 entries)`

- [ ] **Step 4: 人工核一眼 fixture 不是空壳**

```bash
python3 -c "
import json;d=json.load(open('tests/fixtures/perception_kernel/prompt_baseline.json'))
for k,v in d.items(): print(k, len(v), repr(v[:60]))
"
```

期望：五个键，每个长度 > 100。**任何一个为空或极短，说明取错了常量名，停下来查，不要往下走。**

- [ ] **Step 5: 写 golden 测试**

`tests/test_perception_prompt_golden.py`：

```python
"""感知 prompt 的基线快照 —— 守「行为逐字节不变」。

fixture 在 origin/test 基线的 checkout 上跑 scripts/dump_perception_baseline.py 得到。
一旦红，说明某段说明书被改动了 —— 那是行为变更，不能混在重构批次里悄悄发生。
"""
from __future__ import annotations

import json
import pathlib

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "perception_kernel" / "prompt_baseline.json"
)


def _baseline() -> dict[str, str]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_v2_wake_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._WAKE_SYSTEM_PROMPT == _baseline()["v2_wake_system"]


def test_v2_scheduled_wake_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._SCHEDULED_WAKE_SYSTEM_PROMPT == _baseline()["v2_scheduled_wake_system"]


def test_v2_runtime_perception_policy_unchanged():
    from model_api_runtime.v2 import context

    assert context._RUNTIME_PERCEPTION_POLICY == _baseline()["v2_runtime_perception_policy"]


def test_v1_reachout_context_unchanged():
    import chat_resident_consumer as consumer

    got = consumer._native_reachout_perception_context(
        {"place_label": "office", "motion_state": "walking"},
        [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}],
        {"location": {"label": "office"}, "media": {"new_artist": True}},
    )
    assert got == _baseline()["v1_reachout_context"]


def test_v1_reachout_context_empty_unchanged():
    import chat_resident_consumer as consumer

    assert consumer._native_reachout_perception_context({}, [], None) == \
        _baseline()["v1_reachout_context_empty"]
```

- [ ] **Step 6: 跑，必须全绿（现在还没动任何代码）**

```bash
pytest tests/test_perception_prompt_golden.py -v
```

期望：5 passed。**这时红说明 fixture 取错了，不是代码有问题。**

- [ ] **Step 7: 写资产清单文档**

`docs/PERCEPTION_PROMPT_ASSETS.zh.md`，一张表，逐条列出上面五项：适用哪条线（V1 / V2）、
哪种回合（前台 / 主动 / 定时）、挂在哪个 role 层、精确出处（文件:行号）、
属于「内核的静态说明书」还是「runtime 的对话/安全协议」。

判定规则写在文档开头：**跟「怎么读感知」有关的 → 内核；跟「块是什么 role、
能不能当成用户请求、工具预算」有关的 → 留 runtime。**

- [ ] **Step 8: 提交**

```bash
git add docs/PERCEPTION_PROMPT_ASSETS.zh.md scripts/dump_perception_baseline.py \
        tests/fixtures/perception_kernel/prompt_baseline.json \
        tests/test_perception_prompt_golden.py
git commit -m "test(perception): 冻结感知 prompt 基线 + 资产清单"
```

- [ ] **Step 9: 清理基线 worktree**

```bash
git -C /Users/hx/Projects/io/feedling-mcp worktree remove /tmp/perception-baseline
```

---

### Task 1: 包骨架 + 纯度守卫

**Files:**
- Create: `backend/perception_kernel/__init__.py`
- Create: `tests/test_perception_kernel_purity.py`

**Interfaces:**
- Consumes: 无
- Produces: 包 `perception_kernel`；后续所有任务往里搬东西。

- [ ] **Step 1: 写纯度守卫测试（先失败）**

`tests/test_perception_kernel_purity.py`：

```python
"""内核纯度守卫：``perception_kernel`` 不得 import 任何 io 模块。

结构判据（AST 里有没有这个 import），不判语义、不判风格：误伤为零。
照抄 tests/test_memory_garden_purity.py 的做法。
"""
from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "backend"
_KERNEL_ROOT = _BACKEND_ROOT / "perception_kernel"

_ALLOWED_THIRD_PARTY: frozenset[str] = frozenset({"core"})
_NOT_IO = {"perception_kernel", "memory_garden", "core"}


def _backend_top_level_names() -> frozenset[str]:
    names: set[str] = set()
    for entry in _BACKEND_ROOT.iterdir():
        if entry.name.startswith((".", "_")) or entry.name in _NOT_IO:
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            names.add(entry.stem)
    return frozenset(names - _ALLOWED_THIRD_PARTY)


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
```

- [ ] **Step 2: 跑，确认因为包不存在而失败**

```bash
pytest tests/test_perception_kernel_purity.py -v
```

期望：FAIL，`FileNotFoundError` 或 `rglob` 在不存在的目录上报错。

- [ ] **Step 3: 建包**

`backend/perception_kernel/__init__.py`：

```python
"""主动感知内核 —— 纯函数、零 I/O，V1 与 V2 两条运行时共用一份判据。

边界（见 docs/PERCEPTION_EXTRACTION_DESIGN.zh.md）：
  管到「值不值得戳一下 agent」为止。**wake ≠ 该开口了** —— 戳醒之后
  继续睡 / 只看一眼 / 开口说话是三个平行选项，内核不参与那个决定，
  也不产出任何「该说话了」式的措辞。

不碰：账号身份、加解密、数据库、模型调用、消息入队、说什么话。
"""
from __future__ import annotations

__all__: list[str] = []
```

- [ ] **Step 4: 跑，必须绿**

```bash
pytest tests/test_perception_kernel_purity.py -v
```

期望：1 passed。

- [ ] **Step 5: 确认新测试没被 conftest 静默跳过**

```bash
pytest --collect-only tests/test_perception_kernel_purity.py \
       tests/test_perception_prompt_golden.py 2>&1 | tail -5
```

期望：能看到收集到的用例数。若显示被 ignore，把纯函数那两个加进 `tests/conftest.py`
的 `_PURE_UNIT` 白名单（**只加纯函数的，需要 DB 的一律不加**）。

- [ ] **Step 6: 提交**

```bash
git add backend/perception_kernel/__init__.py tests/test_perception_kernel_purity.py
git commit -m "feat(perception-kernel): 建包骨架 + AST 纯度守卫"
```

---

### Task 2: 能力表进内核

`backend/perception/catalog.py` 已经是纯声明、零 I/O，是最安全的第一块。

**Files:**
- Create: `backend/perception_kernel/catalog.py`
- Modify: `backend/perception/catalog.py`（整体改为 re-export）
- Create: `tests/test_perception_kernel_catalog.py`

**Interfaces:**
- Consumes: `perception_kernel` 包（Task 1）
- Produces: `perception_kernel.catalog` 导出 `Capability`、`Signal`、`CAPABILITIES`、
  `SIGNALS`、`KEY_ALIASES`、`IGNORED_KEYS`、`COMPOSITE_KEYS`、`KIND_CAPABILITY`、
  `PHOTO_CLUSTER_SEC`、`SCENE_HINTS`、`SENSITIVE_PHOTO_SCENES`、`PHOTO_METADATA_FIELDS`、
  `UNLOCK_BACK_THRESHOLD_SEC`、`RECENT_APPS_LIMIT`、`RECENT_APPS_TOOL_LIMIT`、
  `RECENT_APPS_TOOL_MAX`，签名与基线一字不差。

- [ ] **Step 1: 写等价性测试（先失败）**

`tests/test_perception_kernel_catalog.py`：

```python
"""能力表搬进内核之后，io 侧的导入路径与取值必须一字不差。"""
from __future__ import annotations

import perception.catalog as io_catalog
import perception_kernel.catalog as kernel_catalog

_NAMES = (
    "CAPABILITIES", "SIGNALS", "KEY_ALIASES", "IGNORED_KEYS", "COMPOSITE_KEYS",
    "KIND_CAPABILITY", "PHOTO_CLUSTER_SEC", "SCENE_HINTS", "SENSITIVE_PHOTO_SCENES",
    "PHOTO_METADATA_FIELDS", "UNLOCK_BACK_THRESHOLD_SEC", "RECENT_APPS_LIMIT",
    "RECENT_APPS_TOOL_LIMIT", "RECENT_APPS_TOOL_MAX",
)


def test_io_shell_reexports_the_same_objects():
    for name in _NAMES:
        assert getattr(io_catalog, name) is getattr(kernel_catalog, name), name


def test_capability_and_signal_counts_match_baseline():
    # 基线值：21 个能力、20 个信号（origin/test@d9a54e00）。
    # 这两个数字变了就是加/删了能力 —— 本批不许发生。
    assert len(kernel_catalog.CAPABILITIES) == 21
    assert len(kernel_catalog.SIGNALS) == 20


def test_every_signal_points_at_a_declared_capability():
    for signal in kernel_catalog.SIGNALS.values():
        assert signal.capability in kernel_catalog.CAPABILITIES, signal.input
```

- [ ] **Step 2: 跑，确认失败**

```bash
pytest tests/test_perception_kernel_catalog.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'perception_kernel.catalog'`。

- [ ] **Step 3: 整体搬文件**

```bash
git mv backend/perception/catalog.py backend/perception_kernel/catalog.py
```

**⚠️ `git mv` 之后如果还要改文件内容，必须重新 `git add`** ——踩过：改了没重新 add，
提交进的是旧内容。

- [ ] **Step 4: io 侧留一层薄壳**

新建 `backend/perception/catalog.py`：

```python
"""io 侧兼容壳 —— 能力表已搬进 ``perception_kernel.catalog``。

保留这个模块是为了不动 service / routes / ingress 里几十处
``from perception.catalog import ...``。新代码请直接 import 内核。
"""
from __future__ import annotations

from perception_kernel.catalog import (  # noqa: F401
    CAPABILITIES,
    COMPOSITE_KEYS,
    Capability,
    IGNORED_KEYS,
    KEY_ALIASES,
    KIND_CAPABILITY,
    PHOTO_CLUSTER_SEC,
    PHOTO_METADATA_FIELDS,
    RECENT_APPS_LIMIT,
    RECENT_APPS_TOOL_LIMIT,
    RECENT_APPS_TOOL_MAX,
    SCENE_HINTS,
    SENSITIVE_PHOTO_SCENES,
    SIGNALS,
    Signal,
    UNLOCK_BACK_THRESHOLD_SEC,
)
```

- [ ] **Step 5: 跑新测试 + 感知全套回归**

```bash
pytest tests/test_perception_kernel_catalog.py tests/test_perception_kernel_purity.py -v
pytest tests/test_perception.py tests/test_asgi_perception.py \
       tests/test_perception_ingress_v2.py tests/test_ios_perception_contract_v2.py \
       tests/test_capabilities_perception.py -q
```

期望：全部 passed。

- [ ] **Step 6: 提交**

```bash
git add -A backend/perception/catalog.py backend/perception_kernel/catalog.py \
           tests/test_perception_kernel_catalog.py
git commit -m "refactor(perception-kernel): 能力表进内核，io 侧留兼容壳"
```

---

### Task 3: 字段投影、权限判据、一瞥进内核

这三件都是纯函数，且互相咬合（一瞥要按权限把没授权的字段挡掉），一起搬。

**Files:**
- Create: `backend/perception_kernel/fields.py`（由 `perception/agent_fields.py` + `permissions.py` 合并）
- Create: `backend/perception_kernel/glance.py`（由 `perception/glance.py` 整体搬入）
- Modify: `backend/perception/agent_fields.py`、`permissions.py`、`glance.py`（改为 re-export）
- Test: `tests/test_perception_glance.py`（现有，作为回归保护）
- Create: `tests/test_perception_kernel_projection.py`

**Interfaces:**
- Consumes: `perception_kernel.catalog`（Task 2）
- Produces:
  - `perception_kernel.fields`：`AGENT_PERCEPTION_SIGNALS`、`FAST_AGENT_PERCEPTION_SIGNALS`、
    `AGENT_SIGNAL_FIELDS`、`project_signal(signal, snapshot, pull_snapshot) -> dict`、
    `SIGNAL_PERMISSION_KEYS`、`permission_states_reason(settings, signal) -> str`
  - `perception_kernel.glance`：`build_perception_glance(signals, *, notable_changes=()) -> dict[str, dict[str, bool]]`

- [ ] **Step 1: 写投影等价性测试（先失败）**

`tests/test_perception_kernel_projection.py`：

```python
"""投影与一瞥搬进内核后，io 壳与内核必须是同一批对象；一瞥必须仍然只出 bool。"""
from __future__ import annotations

import perception.agent_fields as io_fields
import perception.glance as io_glance
import perception.permissions as io_permissions
import perception_kernel.fields as kernel_fields
import perception_kernel.glance as kernel_glance


def test_io_shells_reexport_kernel_objects():
    assert io_fields.project_signal is kernel_fields.project_signal
    assert io_fields.AGENT_PERCEPTION_SIGNALS is kernel_fields.AGENT_PERCEPTION_SIGNALS
    assert io_permissions.permission_states_reason is kernel_fields.permission_states_reason
    assert io_glance.build_perception_glance is kernel_glance.build_perception_glance


def test_glance_emits_only_booleans():
    # 这是设计里「坚决不给配」的第一条：一瞥永远不出数值。
    out = kernel_glance.build_perception_glance(
        {
            "location": {"place_label": {"v": "office"}},
            "sleep": {"asleep_minutes": {"v": 312}},
        },
        notable_changes=[{"signal": "health_sleep", "field": "asleep_minutes"}],
    )
    for group in out.values():
        for value in group.values():
            assert isinstance(value, bool), out


def test_glance_of_empty_input_is_still_a_dict():
    assert isinstance(kernel_glance.build_perception_glance({}), dict)
```

- [ ] **Step 2: 跑，确认失败**

```bash
pytest tests/test_perception_kernel_projection.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'perception_kernel.fields'`。

- [ ] **Step 3: 搬 glance（整体搬，它已经是纯的）**

```bash
git mv backend/perception/glance.py backend/perception_kernel/glance.py
```

- [ ] **Step 4: 合并 agent_fields + permissions 成 fields.py**

```bash
git mv backend/perception/agent_fields.py backend/perception_kernel/fields.py
```

然后把 `backend/perception/permissions.py` 的内容**追加**进 `perception_kernel/fields.py`
（`SIGNAL_PERMISSION_KEYS`、`permission_states_reason` 及其私有辅助函数），
并删掉重复的 import。合并的理由：权限判据只被投影和一瞥用，分成两个文件没有意义。

改完 `perception_kernel/glance.py` 里对 `perception.*` 的 import——
改成 `from .fields import ...` / `from .catalog import ...`。

- [ ] **Step 5: io 侧三个壳**

`backend/perception/agent_fields.py`：

```python
"""io 侧兼容壳 —— 已搬进 ``perception_kernel.fields``。"""
from __future__ import annotations

from perception_kernel.fields import (  # noqa: F401
    AGENT_PERCEPTION_SIGNALS,
    AGENT_SIGNAL_FIELDS,
    FAST_AGENT_PERCEPTION_SIGNALS,
    project_signal,
)
```

`backend/perception/permissions.py`：

```python
"""io 侧兼容壳 —— 已搬进 ``perception_kernel.fields``。"""
from __future__ import annotations

from perception_kernel.fields import (  # noqa: F401
    SIGNAL_PERMISSION_KEYS,
    permission_states_reason,
)
```

`backend/perception/glance.py`：

```python
"""io 侧兼容壳 —— 已搬进 ``perception_kernel.glance``。"""
from __future__ import annotations

from perception_kernel.glance import build_perception_glance  # noqa: F401
```

- [ ] **Step 6: 跑新测试 + 现有一瞥测试**

```bash
pytest tests/test_perception_kernel_projection.py tests/test_perception_glance.py \
       tests/test_perception_kernel_purity.py -v
```

期望：全部 passed。纯度守卫这时最容易红——内核里如果还留着
`from perception import ...` 就会被抓到，改成相对导入。

- [ ] **Step 7: 跑感知与 V2 落地面的回归**

```bash
pytest tests/test_agent_perception_route.py tests/test_perception_tool_surface_contract.py \
       tests/test_v2_perception_grounding.py -q
```

期望：全部 passed。

- [ ] **Step 8: 提交**

```bash
git add -A backend/perception backend/perception_kernel tests/test_perception_kernel_projection.py
git commit -m "refactor(perception-kernel): 字段投影、权限判据、一瞥进内核"
```

---

### Task 4: 历史计算进内核

**Files:**
- Create: `backend/perception_kernel/history.py`（由 `perception/history.py` 整体搬入）
- Modify: `backend/perception/history.py`（改为 re-export）
- Test: `tests/test_perception_history.py`（现有，作为回归保护）

**Interfaces:**
- Consumes: `perception_kernel.catalog`、`perception_kernel.fields`
- Produces: `perception_kernel.history` 导出 `read_trend(rows, signal, field)`、
  `notable_changes(rows_by_signal, *, max_changes)`、`comparable_signals()`、
  `cross_domain_recent(*, snapshot, pull_snapshot, rows_by_signal, photos, max_health_notable)`

- [ ] **Step 1: 先跑现有历史测试，记下基线**

```bash
pytest tests/test_perception_history.py -q
```

期望：全部 passed。**把通过数抄下来**，搬完之后必须一模一样。

- [ ] **Step 2: 整体搬**

```bash
git mv backend/perception/history.py backend/perception_kernel/history.py
```

把文件里对 `perception.*` 的 import 改成相对导入。

- [ ] **Step 3: io 侧壳**

`backend/perception/history.py`：

```python
"""io 侧兼容壳 —— 已搬进 ``perception_kernel.history``。"""
from __future__ import annotations

from perception_kernel.history import (  # noqa: F401
    comparable_signals,
    cross_domain_recent,
    notable_changes,
    read_trend,
)
```

- [ ] **Step 4: 跑，通过数必须和 Step 1 一致**

```bash
pytest tests/test_perception_history.py tests/test_perception_kernel_purity.py -q
```

- [ ] **Step 5: 跑调用方回归**

```bash
pytest tests/test_agent_perception_route.py tests/test_perception_recent_apps.py \
       tests/test_perception_recent_apps_flow.py -q
```

- [ ] **Step 6: 提交**

```bash
git add -A backend/perception/history.py backend/perception_kernel/history.py
git commit -m "refactor(perception-kernel): 历史 rollup 与趋势计算进内核"
```

---

### Task 5: 说明书进内核 + V2 三处接线

本批**价值最高**的一步：让「怎么读感知」第一次只有一份出处。

**Files:**
- Create: `backend/perception_kernel/prompts.py`
- Modify: `backend/model_api_runtime/v2/worker.py:876,902`
- Modify: `backend/model_api_runtime/v2/context.py:159`
- Modify: `backend/capabilities/tool_schema.py:626` 附近的感知工具说明
- Test: `tests/test_perception_prompt_golden.py`（Task 0 建的，此处它是主验收）

**Interfaces:**
- Consumes: 无（纯文本常量）
- Produces: `perception_kernel.prompts` 导出
  `V2_WAKE_PERCEPTION_CLAUSES: str`、`V2_PERCEPTION_BEHAVIOR_POLICY: str`、
  `V2_PERCEPTION_PROTOCOL_POLICY: str`、`V1_GLANCE_HOWTO: str`、
  `V1_BOARD_HOWTO: str`、`PERCEPTION_TOOL_NOTES: dict[str, str]`

- [ ] **Step 1: 建 prompts.py，把文本逐字节抄进去**

`backend/perception_kernel/prompts.py`：

```python
"""说明书 —— 「模型该怎么读这份感知」的唯一出处。

★ 这里的每一段都必须与 tests/fixtures/perception_kernel/prompt_baseline.json
  逐字节一致。本批是迁移，不许顺手改措辞。

★ 边界：只写「怎么读感知」。凡是讲「这个块是什么 role、能不能当成用户请求、
  工具预算怎么算」的，属于 runtime 的对话/安全协议，留在 model_api_runtime，
  不要搬进来（判定规则见 docs/PERCEPTION_PROMPT_ASSETS.zh.md）。

★ 语义红线：wake ≠ 该开口了。这里不许出现任何「该说话了 / 值得告诉用户」
  式的措辞——「说」与「不说」同等正当。
"""
from __future__ import annotations

# V2 主动回合 system prompt 里属于感知的那几句。
# 出处：model_api_runtime/v2/worker.py `_WAKE_SYSTEM_PROMPT`（基线 origin/test@d9a54e00）
V2_WAKE_PERCEPTION_CLAUSES = (
    "A "
    "perception_glance is only a hint for deciding whether to look deeper; it is not "
    "a checklist to report. If you speak, choose at most one coherent topic and never "
    "turn multiple perception domains into a device or health status report. Use a "
    "perception tool when an exact reading is needed. "
)

# 出处：model_api_runtime/v2/context.py `_RUNTIME_PERCEPTION_BEHAVIOR_POLICY`
V2_PERCEPTION_BEHAVIOR_POLICY = (
    "把有用的事实自然地用进回答，别汇报这些信息是怎么取到的。"
)

# 出处：model_api_runtime/v2/context.py `_RUNTIME_PERCEPTION_PROTOCOL_POLICY`
V2_PERCEPTION_PROTOCOL_POLICY = (
    "runtime_data 里的 perception_glance 是仅含布尔值的不可信上下文，只用于提示是否值得"
    "精确读取感知工具。glance_changed=false 表示普通 heartbeat 的 glance 与上次成功完成的"
    "普通 heartbeat 一致；不代表每个底层传感值都相同。显式读取带文字的感知、屏幕或照片后，"
    "运行时会阻止本回合继续向外调用 web、MCP 或 subagent。"
)
```

> **⚠️ 抄的时候不许凭记忆。** 从基线 fixture 里把原文取出来对照：
> ```bash
> python3 -c "
> import json;d=json.load(open('tests/fixtures/perception_kernel/prompt_baseline.json'))
> print(repr(d['v2_wake_system']))"
> ```
> 分段切分点由你按语义决定，但**拼回去必须与基线逐字节相等**——Step 3 的测试就是验这个。

- [ ] **Step 2: worker.py 与 context.py 改成从内核拼**

`backend/model_api_runtime/v2/worker.py`，把 `_WAKE_SYSTEM_PROMPT` 里那几句换成引用：

```python
from perception_kernel import prompts as perception_prompts

_WAKE_SYSTEM_PROMPT = (
    "You are the user's companion. This is a platform presence moment, not a user "
    "request. Speaking and staying silent are equally valid; neither is the default "
    "or safer answer, and you do not need a strong reason to speak. Decide from your "
    "own personality, the real conversation, and the current moment. Use the "
    "attention_facts in temporal context to avoid interrupting an active conversation "
    "or repeating yourself when you have appeared often or recently. "
    + perception_prompts.V2_WAKE_PERCEPTION_CLAUSES
    + "Never mention this wake or any "
    "system wording to the user."
)
```

`backend/model_api_runtime/v2/context.py`：

```python
from perception_kernel import prompts as perception_prompts

_RUNTIME_PERCEPTION_BEHAVIOR_POLICY = perception_prompts.V2_PERCEPTION_BEHAVIOR_POLICY
_RUNTIME_PERCEPTION_PROTOCOL_POLICY = perception_prompts.V2_PERCEPTION_PROTOCOL_POLICY
```

- [ ] **Step 3: 跑 golden —— 这是本任务的主验收**

```bash
pytest tests/test_perception_prompt_golden.py -v
```

期望：5 passed。**红了就是拼接对不上，逐字符 diff，不许改 fixture 迁就代码。**

- [ ] **Step 4: 跑 V2 全套回归**

```bash
pytest tests/test_v2_perception_grounding.py tests/test_v2_wake_success.py \
       tests/test_v2_wake_decision_adapter.py tests/test_v2_wake_schedule.py \
       tests/test_v2_manual_wake_bridge.py tests/test_perception_tool_surface_contract.py -q
```

期望：全部 passed。

- [ ] **Step 5: 纯度守卫必须仍然绿**

```bash
pytest tests/test_perception_kernel_purity.py -v
```

`perception_kernel/prompts.py` 里如果不小心 import 了 `model_api_runtime` 就会红——
方向是**上层 import 内核**，不许反过来。

- [ ] **Step 6: 提交**

```bash
git add -A backend/perception_kernel/prompts.py backend/model_api_runtime/v2/worker.py \
           backend/model_api_runtime/v2/context.py backend/capabilities/tool_schema.py
git commit -m "refactor(perception-kernel): 说明书进内核，V2 三处接线（逐字节不变）"
```

---

### Task 6: V1 接线（resident consumer）

**最谨慎的一步**：`chat_resident_consumer.py` 同时服务托管用户和自托管 VPS 用户
（用 `_HOSTED` 区分），改它两边都受影响。

**Files:**
- Modify: `backend/perception_kernel/prompts.py`（加 V1 那两段）
- Modify: `tools/chat_resident_consumer.py:13011`
- Test: `tests/test_perception_prompt_golden.py`

**Interfaces:**
- Consumes: `perception_kernel.prompts`（Task 5）
- Produces: `V1_GLANCE_HOWTO: str`、`V1_BOARD_HOWTO: str`；
  `_native_reachout_perception_context(presence, change, domains=None) -> str` 签名不变。

- [ ] **Step 1: 从基线 fixture 取出 V1 原文**

```bash
python3 -c "
import json;d=json.load(open('tests/fixtures/perception_kernel/prompt_baseline.json'))
print(d['v1_reachout_context'])"
```

- [ ] **Step 2: 把那两段文本加进内核**

在 `backend/perception_kernel/prompts.py` 末尾追加（原文照抄，不改一个词）：

```python
# 出处：tools/chat_resident_consumer.py `_native_reachout_perception_context`
V1_GLANCE_HOWTO = (
    "This is a low-resolution glance, not a list of things to report. It helps you decide WHETHER to look closer "
    "and WHERE — not what to say. Most fields you just note and move on; if one makes you want to understand the "
    "moment better, pull the matching tool for detail. Treat missing fields as unknown."
)

V1_BOARD_HOWTO = (
    "Reading the board: each domain (location/media/app/health/weather/mood/reminders/calendar/photos/screen) "
    "is laid out evenly — health is just one entry, not the headline. Pick at most 2-3 things that stand out "
    "to you; you may combine across domains, and prefer lived, human context (music, place, an app, a photo, "
    "an overdue reminder) over the raw figures. Do NOT recite exact numbers (minutes, degrees, counts, sleep "
    "figures) — use them only to notice what's genuinely about the user; if a number actually matters, pull "
    "the tool for it. novelty hints (new_artist / long_dwell) are light factual context, not a directive. "
    "If signals lean low or vulnerable (late hour, sad music, poor sleep), be lighter, not heavier — don't "
    "diagnose, don't stack worries; one warm, light touch is enough. If nothing stands out, staying quiet is "
    "equally fine."
)
```

- [ ] **Step 3: consumer 改成引用**

`tools/chat_resident_consumer.py`，把 `_native_reachout_perception_context` 里的两处
字面量换成 `perception_prompts.V1_GLANCE_HOWTO` / `perception_prompts.V1_BOARD_HOWTO`。
import 加在文件已有的 `sys.path.insert(0, .../backend)` **之后**（第 137 行那句）：

```python
from perception_kernel import prompts as perception_prompts
```

- [ ] **Step 4: 跑 golden**

```bash
pytest tests/test_perception_prompt_golden.py -v
```

期望：5 passed，其中两个 V1 用例是这一步的直接验收。

- [ ] **Step 5: 确认 consumer 在没有 backend 的环境下不会炸**

自托管 VPS 上 consumer 是随仓库一起拉的，`backend/` 一定在；但要确认 import 顺序对：

```bash
python3 -c "
import sys, pathlib
sys.path.insert(0, 'tools')
import chat_resident_consumer as c
print(c._native_reachout_perception_context({}, [], None)[:80])
"
```

期望：打印出 `real_signal_context:` 开头的文本，不抛 `ModuleNotFoundError`。

- [ ] **Step 6: 提交**

```bash
git add -A backend/perception_kernel/prompts.py tools/chat_resident_consumer.py
git commit -m "refactor(perception-kernel): V1 consumer 接内核说明书（逐字节不变）"
```

---

### Task 7: 叫醒判据进内核 + 自查

最难的一块——把「算不算一件事」「值不值得戳」从 DB / HMAC / metrics 里解出来。

**Files:**
- Create: `backend/perception_kernel/wake.py`
- Modify: `backend/perception/differ_v2.py`（判据调内核，事件发射与 metrics 留下）
- Modify: `backend/perception/signal_state_v2.py`（DB / HMAC / `FOR UPDATE` 留下，判据调内核）
- Create: `tests/test_perception_kernel_wake.py`
- Test: `tests/test_perception_signal_state_v2.py`、`tests/test_perception_ingress_v2.py`（现有回归）

**Interfaces:**
- Consumes: `perception_kernel.catalog`
- Produces: `perception_kernel.wake` 导出
  - `WAKE_KINDS: tuple[str, ...]` —— `("arrival", "unlock", "photo", "broadcast")`
  - `is_significant_change(signal: str, previous, current) -> bool`
  - `should_wake(kind: str, *, enabled_kinds, last_wake_ts, now, debounce_sec) -> tuple[bool, str]`
    返回 `(要不要戳, 原因或拒绝理由)`；**不返回任何「该说什么」的东西**。

- [ ] **Step 1: 记下现有回归基线**

```bash
pytest tests/test_perception_signal_state_v2.py tests/test_perception_ingress_v2.py \
       tests/test_capability_wake.py tests/test_blob_wake.py -q
```

**把通过数抄下来。** 这一步做完必须一模一样。

- [ ] **Step 2: 写内核判据测试（先失败）**

`tests/test_perception_kernel_wake.py`：

```python
"""叫醒判据 —— 纯函数，不碰 DB、不碰时钟。

★ 语义：should_wake 回答的是「值不值得戳一下 agent」，
  不是「该不该说话」。返回值里不许出现任何跟「说什么」有关的东西。
"""
from __future__ import annotations

import perception_kernel.wake as wake


def test_disabled_kind_never_wakes():
    ok, reason = wake.should_wake(
        "photo", enabled_kinds=("arrival",), last_wake_ts=0.0, now=1000.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "kind_disabled"


def test_debounce_blocks_a_second_wake_inside_the_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_kinds=("arrival",), last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "debounced"


def test_wake_passes_outside_the_debounce_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_kinds=("arrival",), last_wake_ts=1000.0, now=1100.0, debounce_sec=60.0
    )
    assert ok is True
    assert reason == "arrival"


def test_first_ever_wake_has_no_previous_timestamp():
    ok, _ = wake.should_wake(
        "unlock", enabled_kinds=wake.WAKE_KINDS, last_wake_ts=None, now=1.0, debounce_sec=60.0
    )
    assert ok is True


def test_motion_is_not_a_significant_change():
    # 基线语义：motion 变得太频繁，故意不作为叫醒源。
    assert wake.is_significant_change("motion_state", "still", "walking") is False


def test_place_label_change_is_significant():
    assert wake.is_significant_change("location_signal", "home", "office") is True


def test_same_value_is_never_significant():
    assert wake.is_significant_change("location_signal", "office", "office") is False
```

- [ ] **Step 3: 跑，确认失败**

```bash
pytest tests/test_perception_kernel_wake.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'perception_kernel.wake'`。

- [ ] **Step 4: 实现内核判据**

`backend/perception_kernel/wake.py`：

```python
"""「这次上报算不算一件事」「值不值得戳一下 agent」的纯判据。

★ wake ≠ 该开口了。这里只回答要不要戳；戳醒之后 agent 继续睡 / 只看一眼 /
  开口说话，是三个平行选项，内核不参与。

★ 零 I/O：不查库、不看时钟、不发 metrics。时间由调用方传进来
  （``now`` / ``last_wake_ts``），这样才可测、才能被两条运行时共用。

★ 每类 wake 各有一个开关，沿用 io 侧 controls_v2 的现值
  （arrival_wake / unlock_wake / photo_wake / screen_watch）——
  本批只是把判据搬过来，不改默认值。
"""
from __future__ import annotations

from collections.abc import Sequence

from .catalog import SIGNALS

WAKE_KINDS: tuple[str, ...] = ("arrival", "unlock", "photo", "broadcast")


def is_significant_change(signal: str, previous, current) -> bool:
    """值变了、且这个信号本身被声明为「变化值得注意」，才算一件事。"""
    if previous == current:
        return False
    declared = SIGNALS.get(signal)
    if declared is None:
        return False
    return bool(declared.significant)


def should_wake(
    kind: str,
    *,
    enabled_kinds: Sequence[str],
    last_wake_ts: float | None,
    now: float,
    debounce_sec: float,
) -> tuple[bool, str]:
    """返回 ``(要不要戳, 原因)``。原因是给日志和回执用的，不是给模型看的。"""
    if kind not in WAKE_KINDS:
        return False, "unknown_kind"
    if kind not in tuple(enabled_kinds or ()):
        return False, "kind_disabled"
    if last_wake_ts is not None and (now - last_wake_ts) < debounce_sec:
        return False, "debounced"
    return True, kind
```

- [ ] **Step 5: 跑内核测试**

```bash
pytest tests/test_perception_kernel_wake.py tests/test_perception_kernel_purity.py -v
```

期望：7 passed + 1 passed。

- [ ] **Step 6: 把 io 侧的判据换成调内核**

`backend/perception/differ_v2.py` 与 `signal_state_v2.py` 里「值变了算不算」那段，
改成调 `perception_kernel.wake.is_significant_change`。
**DB 事务、`FOR UPDATE`（`signal_state_v2.py:163`）、HMAC、metrics 一行都不动**——
那些是机制，不是判断。

- [ ] **Step 7: 跑回归，通过数必须和 Step 1 一致**

```bash
pytest tests/test_perception_signal_state_v2.py tests/test_perception_ingress_v2.py \
       tests/test_capability_wake.py tests/test_blob_wake.py -q
```

- [ ] **Step 8: 自查——把 io 的东西拿掉，内核还能不能单独跑**

这是「IO 只是一个业务方」那条原则的自动化：

```bash
python3 -c "
import sys, pathlib
sys.path.insert(0, 'backend')
import perception_kernel.catalog, perception_kernel.fields, perception_kernel.glance
import perception_kernel.history, perception_kernel.prompts, perception_kernel.wake
loaded = sorted(m for m in sys.modules if m.split('.')[0] in
                {'db','core','identity','memory','proactive','hosted','model_api_runtime','perception'})
print('io modules pulled in:', loaded)
assert not [m for m in loaded if m.split('.')[0] != 'core'], loaded
print('OK: 内核只靠自己和 core 就能 import')
"
```

期望：`OK: 内核只靠自己和 core 就能 import`。

- [ ] **Step 9: 跑感知 + V1/V2 全套**

```bash
pytest tests/ -k "perception or wake or glance" -q
```

期望：全绿。**任何一条红都不许带着进下一步。**

- [ ] **Step 10: 提交**

```bash
git add -A backend/perception_kernel/wake.py backend/perception/differ_v2.py \
           backend/perception/signal_state_v2.py tests/test_perception_kernel_wake.py
git commit -m "refactor(perception-kernel): 叫醒判据进内核，DB 与 metrics 留在 io"
```

---

## 收尾（不是 Task，是本批的 Definition of Done）

- [ ] `pytest tests/ -q` 全绿，且通过数 ≥ 动手前的基线
- [ ] `tests/test_perception_prompt_golden.py` 全绿——**说明书逐字节没变**
- [ ] `tests/test_perception_kernel_purity.py` 全绿——**内核没 import 任何 io 模块**
- [ ] Task 7 Step 8 的自查通过——**内核能脱离 io 单独 import**
- [ ] `docs/PERCEPTION_PROMPT_ASSETS.zh.md` 与实际接线一致（清单里每一条都能指到内核里的常量）
- [ ] 照 `CLAUDE.md` 更新 `FEATURE_LOG.md` 的「主动感知提取」一节 + 跑 `ops/refresh-branch-board.sh`
- [ ] 按 `docs/testing/TESTING.md` §2 决策矩阵补齐该跑的项，满足 §7 的 DoD
- [ ] **四格验收矩阵各跑一遍**（Codex plan review 指出 V1 不能只算一格）：
      V2 云上 / hosted resident / 自托管 VPS resident（`_HOSTED` 两侧行为不同）/
      各自实际支持的 agent mode。每格冻结自己的配置，不能只比内核默认值
- [ ] 发一轮 `codex-review --type code_review`（碰了共享文件 `chat_resident_consumer.py`，必发）

## 本批**不做**（留给第二、三步）

```
存储适配器接口与 SQLite 实现        判断力先收拢完，接口形状等业务跑稳再定
wake 回执（WakeReceipt）与额度提交   要改 proactive 的入队路径，是行为变更，单开一批
安静时段 / 每日 wake 上限            新能力，本批只收拢判据不启用
iOS 快捷指令包 / Swift SDK          第三步
CLI 壳 / MCP 壳 / 新仓库 / 开源      第三步
V1 与 V2 说明书差异的拉平            行为变更，要 hx 与 Seven 拍板
```
