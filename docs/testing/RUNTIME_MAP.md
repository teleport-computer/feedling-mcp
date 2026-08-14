# 概念 → 各运行时坐标对照表

> **做跨运行时对照之前先查这张表,不要直接 grep 符号名。**
> 一条 grep 返回 0,第一个问题是「这个符号在那一侧叫这个名字吗」,不是「那一侧没有这个功能」。

2026-08-14 建。快照提交 `6a7bf491`(origin/test)。

## 为什么有这份文档

同一件事在两条运行时里**叫不同名字、在不同文件、有时压根不是同一种机制**。
2026-08-14 一天之内因为按印象定位而下错结论三次:

- 有人用 `grep 'memory_search' tools/chat_resident_consumer.py → 0 命中` 论证
  「resident 侧没有这套逻辑」。**实测 `memory_search` 在 resident 侧出现 0 次,
  `memory-search` 在 `io_cli.py` 也是 0 次** —— 那条 grep 不可能命中,
  它证明不了任何事。resident 侧对应的是 `memory-index` / `memory-fetch`。
- 有人把 `occurred_at = now` 归因到 Runtime V2,实际先命中的是 resident consumer。
- 有人把 `chat_resident_consumer.py` 直接叫「V1」——
  `docs/testing/README.md` 明写现在只剩两条路径:**Runtime V2** 和 **Resident / VPS**,
  「V1 托管已不再维护」。

## 术语(先对齐,否则整张表都会读错)

| 叫法 | 指什么 | 主要坐标 |
|---|---|---|
| **Runtime V2** | 托管用户,我们的 worker 池跑模型 | `backend/model_api_runtime/v2/*` |
| **Resident consumer** | 一个 Python 进程,轮询 `/v1/chat/poll`、调用底层 CLI agent、把回复 POST 回来 | `tools/chat_resident_consumer.py` + `tools/io_cli.py` |
| **托管侧(agent-runner)** | **不是第三条运行时**。它在 CVM 里**托管同一个 resident consumer**,只多做多租户监管(lease/spawn/隔离),并负责生成 consumer 的家目录与系统提示文件 | `backend/agent_runtime/*` |

依据:`backend/agent_runtime/spawners.py` 模块 docstring —— *"The canonical consumer is the
existing VPS resident consumer (`tools/chat_resident_consumer.py`): the agent-runner hosts it
in the CVM."*

所以下表只有两列。凡是**只存在于托管侧、VPS 自跑用户没有**的,单独在备注里标出来。

## 怎么用这张表(以及为什么不写死行号)

**行号会烂。**本表初稿曾按行号写,几小时内就因为几个 PR 合入而全部偏移;
更早一次,同一批坐标在落后 71 个提交的工作树上读出来的行号与 `origin/test` 完全对不上。

所以每一格给的是 **文件 + 符号**,并附一条**可复跑的定位命令**。行号只作为快照参考。

```sh
# 定位任意一格(把 <file> / <symbol> 换掉)
git show origin/test:<file> | grep -n "<symbol>"
```

**证据等级约定**(每格都标注):

- ✅ **已实测命中** —— 我跑过上面那条命令并看到了它
- ⛔ **这一侧没有对应实现** —— 结论。后面写明我按哪些命名/路径找过才敢这么说
- ❓ **我没找到** —— 状态,不是结论。不要当成「不存在」

---

## 一、记忆读

| | Runtime V2 | Resident consumer |
|---|---|---|
| 索引/列表 | ✅ `capabilities/memory.py::index`(工具名 `memory_index`) | ✅ `io_cli.py::cmd_memory_index`(CLI 子命令 `memory-index`) |
| 取全文 | ✅ `capabilities/memory.py::fetch`(工具名 `memory_fetch`) | ✅ `io_cli.py::cmd_memory_fetch`(`memory-fetch`) |
| 搜索 | ✅ `capabilities/memory.py::search`(工具名 **`memory_search`**) | ⛔ **没有同名入口**。实测 `memory_search` 在 `chat_resident_consumer.py` 出现 0 次、`memory-search` 在 `io_cli.py` 出现 0 次;resident 侧要检索就是带 query 走 `memory-index` |

⚠️ **这一行就是 2026-08-14 那次错误归因的现场。**用 `memory_search` 去 grep resident 侧,
永远是 0,而那个 0 不代表 resident 没有检索能力。

## 二、记忆写

| | Runtime V2 | Resident consumer |
|---|---|---|
| 模型侧写入 | ✅ `capabilities/memory.py::write`(工具名 `memory_write`,`op=add/update/delete`) | ✅ `io_cli.py::cmd_memory_write` / `cmd_memory_patch` / `cmd_memory_delete`(三个独立 CLI 子命令) |
| 后台批量写 | ✅ `v2/serve_worker.py::_apply_memory_actions` | ✅ `chat_resident_consumer.py::execute_memory_actions` |

**命名差异要点**:V2 把增/改/删收敛进**一个工具 + op 参数**;resident 侧是**三个 CLI 子命令**。
所以「模型能不能删记忆」这个问题,两侧要查的东西完全不同。
⚠️ 两侧最终都打到同一套 HTTP 端点(`/v1/memory/index|fetch|actions`),差别在调用方式
(V2 进程内库调用,resident 是 HTTP 客户端)。**看到端点相同不等于行为相同。**

## 三、capture(对话中抓取记忆)

| | Runtime V2 | Resident consumer |
|---|---|---|
| 执行入口 | ✅ `v2/worker.py::_run_extraction`(`lane == "capture"`) | ✅ `chat_resident_consumer.py::_process_capture_jobs` |

## 四、dream(整理已有记忆)

| | Runtime V2 | Resident consumer |
|---|---|---|
| 执行入口 | ✅ `v2/worker.py::_run_extraction`(`lane == "dream"`,与 capture **同一个函数**,靠 lane 分流) | ✅ `chat_resident_consumer.py::_process_dream_jobs`(与 capture **是两个独立函数**) |

⚠️ **结构差异**:V2 的 capture 和 dream 是同一个函数的两个 lane,resident 侧是两个函数。
所以"改 dream 会不会影响 capture"在两侧答案不同。

### `occurred_at` 谁在写(这是被错误归因过的字段)

| | Runtime V2 | Resident consumer |
|---|---|---|
| capture | ✅ `v2/worker.py` `_run_extraction` 内:默认 `now`,再**倒序**扫 `prompt_tail` 取最后一条有 `ts` 的消息覆盖 | ✅ `chat_resident_consumer.py::_capture_occurred_at`:优先 `job.window.until_ts` → 否则最后一条消息 ts → 否则 `time.time()` |
| dream | 同上(走同一函数) | ✅ `_process_dream_jobs` 内直接 `time.time()`,**不看历史消息** |

⚠️ **两侧逻辑独立,不是一份代码。**排查 `occurred_at` 异常时先确定这个用户走哪条路径,
否则会像 2026-08-14 那次一样改错地方。

## 五、工具面构造(这一轮把哪些工具交给模型)

| | Runtime V2 | Resident consumer |
|---|---|---|
| 机制 | ✅ `v2/tool_loop.py::_turn_catalog` —— **作为结构化 tools 参数传给 provider**,每轮重算、按 `disabled_names` 过滤 | ✅ `tools/io_cli_catalog.py::build_catalog` + `chat_resident_consumer.py::_prepend_io_cli_capability_catalog` —— **没有 tools 参数,是把 `io_cli --help` 生成的目录文本前置进 prompt** |

⚠️ **这是全表差异最大的一格。**两侧不是"同一件事的不同实现",是**两种机制**:
V2 走 provider 原生 tool-calling;resident 侧靠"在提示词里告诉模型有哪些命令可用"。
所以「工具面被截断/丢失」这类问题在两侧的表现和排查手段完全不同
(V2 看 tools 数组,resident 看 prompt 里那段目录文本在不在)。

## 六、出口清洗(回复发给用户前的清理)

| 出口 | Runtime V2 | Resident consumer |
|---|---|---|
| 前台聊天 | ✅ `v2/worker.py` chat lane `_on_reply` 内调 `self_thinking.strip_all_thinking` | ✅ `chat_resident_consumer.py::_split_tagged_thinking` 内调 `_st.strip_all_thinking(raw, sanitize=False)` |
| 主动/唤醒 | ✅ `v2/worker.py` wake lane `_on_reply` 内调 `_st_wake.strip_all_thinking` | ❓ 未单独确认 resident 侧主动消息是否复用同一条剥离路径 |
| 喂回模型的历史 | ✅ `v2/serve_worker.py::_scrub_leaked_thinking_rows` | ❓ 未确认 |
| 协议残片抑制 | ✅ chat/wake 两个 lane 各有 `_torn_protocol_evidence` 判定 | ✅ `chat_resident_consumer.py::_suppress_torn_protocol_leaks` |

⚠️ **共享内核**:两侧的 `<think>` 剥离都调 `core/self_thinking.py` 的同一个
`strip_all_thinking`,**判据共享、调用点各自独立**。所以「闸有没有生效」要分两问:
① 内核逻辑对不对(共享,一处改两侧受益)② **这条出口有没有接上它**(各自独立,一处漏就漏)。

⚠️ **服务端所有剥离都在封装加密之前。**一旦封装落库就是密文,
服务端再补闸也擦不掉存量 —— 存量只能由客户端在渲染期处理,或重新封装。

## 七、身份卡 / persona(人格如何进入模型)

| | Runtime V2 | Resident consumer(由托管侧 agent-runner 生成家目录时) |
|---|---|---|
| 取出 | ✅ `v2/serve_worker.py::_load_genesis_persona`(逐回合 JIT 解密 `genesis_persona` blob) | ✅ `agent_runtime/spawners.py`:`_genesis_persona_content` 读同一个 blob,交给 `materialize_home(persona_content=...)` |
| **拼进最终提示的位置** | ⚠️ **人格在后**。`v2/context.py` 里 `trusted_parts = [system_prompt, _RUNTIME_CONTEXT_POLICY]`,**之后**才 extend 人格块 | ⚠️ **人格在前**。`spawners.py`:`system_append = f"{persona}\n\n---\n\n{system_append}"`,工具说明追加在后;identity block 再置于 persona 之前 |

**这一格是整张表最值得看的一格。**两侧都有 persona、都不截断、blob 也是同一个 ——
**所有"有没有"式的对照都会得出"parity 正常"**,而真实差异在**排第几**。

**验位置不要读代码,跑真装配打偏移量**:

```python
# 塞一个唯一哨兵进去,看它落在最终 system message 的第几个字符
msgs = context.build_turn_messages(
    system_prompt=context.chat_system_prompt(None),
    summary="", tail=[], trusted_system_blocks=("<<<PERSONA-SENTINEL>>>",))
s = msgs[0]["content"]; print(len(s), s.find("<<<PERSONA-SENTINEL>>>"))
```

快照 `6a7bf491` 实测:总长 **9387**,人格起点 **9365** —— 即人格落在最后 0.2%。

⚠️ 这个数**会漂**:同一段测量在两天前的树上是 9009 / 8987,几十个提交之后就变成
9387 / 9365(系统提示又长了 378 字符)。**所以看到本表的数字对不上,先重跑上面那段,
不要以为是坏了。**要看的是"人格在最后百分之几",不是那两个绝对值。

> ⚠️ 「V2 是否应改成人格在前」是**未决的产品判断**,不要按本表自行改。
> 本表只陈述现状。

## 八、唤醒(主动开口)

| | Runtime V2 | Resident consumer |
|---|---|---|
| 执行入口 | ✅ `v2/worker.py::_run_wake`(lane = heartbeat/scheduled/manual_wake/screen_watch) | ✅ `chat_resident_consumer.py::_process_proactive_jobs` |
| 拉取/触发 | 由 job 队列驱动 | ✅ `chat_resident_consumer.py` 轮询循环里按 `PROACTIVE_TICK_ENABLED` 打 tick,决策由后端下发 |

⚠️ **两侧都不是自己决定"要不要开口"** —— 决策在后端下发,consumer/worker 是执行者。
排查「不该说话时说了」不要从这两个入口找,要往上游的决策/闸走。

---

## 已知的坑(这些是我们真的踩过的)

1. **只 grep `backend/` 会得到假的「resident 侧没有」。**
   resident 侧的实现大量在 **`tools/`** 下(`chat_resident_consumer.py`、`io_cli.py`、
   `io_cli_catalog.py`)。2026-08-14 我据此错判「V1 整条没有剥离闸」,
   实际闸就在 `tools/chat_resident_consumer.py`。
   **做对照时 grep 范围必须含 `tools/`。**

2. **符号会改名。**本表初稿写的 `_extract_visible_thinking` 在快照提交上已不存在,
   现在叫 `_split_tagged_thinking`。所以每格给的是**定位命令**而不是承诺永远有效的坐标。

3. **两侧端点相同 ≠ 行为相同**(见第二节)。

4. **V1 是证据来源,不是设计权威。**两侧架构不同(进程内 CLI agent vs 池化 worker),
   "resident 这么做"不等于"V2 也该这么做"。

## 维护

发现某一格过期(符号改名/文件搬家/机制变了),**直接改这里**,并更新顶部的快照提交。
新增概念时照第 1–8 节的格式:**每格必须带证据等级标记,不许写「应该在 xxx」。**
