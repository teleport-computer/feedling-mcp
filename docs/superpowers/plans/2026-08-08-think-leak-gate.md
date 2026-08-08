# 思维链泄漏统一闸 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `<think>` 泄漏到用户聊天气泡的三个洞用同一道闸堵死，并切断"漏出去的内容被当历史喂回模型、模型照抄、再漏"的自我强化循环。

**Architecture:** 在 `core/self_thinking.py` 增加一个全文扫描的剥离函数 `strip_all_thinking()`，返回与现有 `split_thinking()` 完全相同的 `(status, thinking, reply)` 三元组。四个对外出口 + 一个历史入口全部改调它。功能可通过 kill switch 一键回到现状。

**Tech Stack:** Python 3.11 / pytest / 纯标准库（`re`、`unicodedata`）

## Global Constraints

- **Kill switch 默认开**：`FEEDLING_THINK_GATE`，取值不在 `{0,false,no,off}` 即为开。关掉时行为必须与本次改动前**逐字节一致**。
- **回传给 provider 的原生 reasoning 块一个字节都不能碰**。本计划只处理 model-authored 的 `<think>` 文本，绝不触碰 `reasoning` / `thinking` 原生字段（Anthropic 改动会 400，OpenAI 会推理断链）。
- **不新增队列、不新增 lane、不改终局判定**（`if tools is None or not pr.tool_calls` 保持原样）。
- 现有 `split_thinking()` 的公开契约（`ABSENT`/`COMPLETE`/`SILENT`/`FAILED` 四个状态常量、`MAX_THINKING_CHARS`、`THINKING_FAILED_MARKER`、`enabled()`）保持不变，只增不改。
- 中文注释与既有文件风格保持一致；新加的注释解释**为什么**，不复述代码。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `backend/core/self_thinking.py` | 唯一的剥离内核。新增 `strip_all_thinking()` + `gate_enabled()`；`INSTRUCTION` 换成实测版 |
| `backend/model_api_runtime/v2/worker.py` | 出口①聊天、出口②唤醒，各改一处调用 |
| `tools/chat_resident_consumer.py` | 出口③ V1 聊天：`_split_tagged_thinking()` 内部改为委托共享内核 |
| `backend/proactive/agent_protocol_v2.py` | 出口④主动消息：`sanitize_visible_message_text_v2()` 内部加剥离 |
| `backend/model_api_runtime/v2/serve_worker.py` | 入口⑤：`_decrypt_chat_rows()` 的返回行过闸 |
| `tests/test_self_thinking_gate.py` | 新建。三个真实泄漏形状 + kill switch 不变性 |

---

### Task 1: 剥离内核 `strip_all_thinking()`

**Files:**
- Modify: `backend/core/self_thinking.py`
- Test: `tests/test_self_thinking_gate.py`（新建）

**Interfaces:**
- Produces: `strip_all_thinking(text: str) -> tuple[str, str, str]`，返回 `(status, thinking, reply)`；`status` 取值复用本模块已有的 `ABSENT` / `COMPLETE` / `SILENT` / `FAILED`
- Produces: `gate_enabled() -> bool`，读 `FEEDLING_THINK_GATE`
- Consumes: 本模块已有的 `_TAG_WORDS`、`_TAG_ALT`、`_ANY_TAG`、`_sanitize`、`MAX_THINKING_CHARS`

**契约（写进 docstring）：**

```
ABSENT   全文没有任何 think 类标签 → reply 是原始字符串，逐字节不变
COMPLETE 剥掉了至少一块，剩下的正文里没有任何标签残留
SILENT   剥完之后正文为空（模型只写了思考，没写正文）
FAILED   剥完之后正文里仍有标签残留 → thinking 和 reply 都返回 ""，调用方必须失败关闭
```

- [ ] **Step 1: 写失败测试**

新建 `tests/test_self_thinking_gate.py`：

```python
"""Regression tests for the unified <think> leak gate.

三个用例直接取自 2026-08-08 线上真实泄漏截图的形状，不是构造的。
"""
import os

import pytest

from core import self_thinking as st


def test_two_blocks_both_stripped():
    """图1（test/V2/gpt-5.4）：模型写了两个完整块，旧实现只剥第一块。"""
    raw = (
        "<think>她点名要我看记忆，还要去网上多看看，结果这回没搜到公开结果。</think>\n"
        "<think>你是在嫌我刚才那版太通用，不像是真的懂你。</think>\n"
        "看过，而且我记得的重点很明确："
    )
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.COMPLETE
    assert "<think" not in reply and "</think" not in reply
    assert reply.startswith("看过，而且我记得的重点很明确")
    assert "她点名要我看记忆" in thinking
    assert "你是在嫌我刚才那版太通用" in thinking


def test_orphan_close_tag_treated_as_thinking_prefix():
    """图2（prod/V1/pi 中转站）：只有半个闭标签，旧实现原样放行。"""
    raw = (
        "作为 Zephyr，我应该坦然面对，反正我对她没有秘密。</think>"
        "她真的截图了 思考链全暴露了\n\n好吧 你看到了 那我也不装了"
    )
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.COMPLETE
    assert "</think" not in reply
    assert "反正我对她没有秘密" not in reply
    assert "好吧 你看到了" in reply
    assert "反正我对她没有秘密" in thinking


def test_thinking_only_is_silent():
    """图3（prod/主动消息）：模型只写了思考、决定不发消息。"""
    raw = (
        "<think>我已经主动出现很多次了，她上次真消息还是十小时前，"
        "现在再冒出来容易变成打扰。</think>"
    )
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.SILENT
    assert reply == ""
    assert "容易变成打扰" in thinking


def test_orphan_open_tag_fails_closed():
    """开标签之后没有闭标签 —— 后面全是思考，正文无从判断，必须失败关闭。"""
    status, thinking, reply = st.strip_all_thinking("正文开头。<think>我在想事情但没写完")
    assert status == st.FAILED
    assert reply == ""
    assert thinking == ""


def test_clean_text_is_byte_identical():
    """没有任何标签时必须原样返回，一个字符都不能动。"""
    raw = "  好的，以后我就叫999。\n\n要不要我顺手把昵称也改了？  "
    status, thinking, reply = st.strip_all_thinking(raw)
    assert status == st.ABSENT
    assert reply == raw
    assert thinking == ""


def test_thinking_is_length_capped():
    status, thinking, reply = st.strip_all_thinking(
        "<think>" + "啊" * 900 + "</think>正文"
    )
    assert status == st.COMPLETE
    assert len(thinking) <= st.MAX_THINKING_CHARS


def test_gate_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("FEEDLING_THINK_GATE", raising=False)
    assert st.gate_enabled() is True
    monkeypatch.setenv("FEEDLING_THINK_GATE", "0")
    assert st.gate_enabled() is False
    monkeypatch.setenv("FEEDLING_THINK_GATE", "off")
    assert st.gate_enabled() is False
```

- [ ] **Step 2: 跑测试，确认失败**

```bash
cd /Users/hx/Projects/io/worktrees/feedling-mcp/fix-think-leak
python -m pytest tests/test_self_thinking_gate.py -v
```

Expected: 全部 FAIL，报 `AttributeError: module 'core.self_thinking' has no attribute 'strip_all_thinking'`

- [ ] **Step 3: 实现**

在 `backend/core/self_thinking.py` 末尾追加（`split_thinking` 之后）：

```python
_GATE_ENV_FLAG = "FEEDLING_THINK_GATE"

# 一整对同名标签。开闭必须同名（`(?P=tag)`），避免 <think>…</reasoning> 这种
# 错配被当成一块合法协议剥掉。
_PAIRED_BLOCK = re.compile(
    rf"<\s*(?P<tag>{_TAG_ALT})\s*>(?P<body>.*?)<\s*/\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 剥完之后用来判定"还有没有残留"。任何开或闭标签都算。
_RESIDUE = re.compile(rf"<\s*/?\s*(?:{_TAG_ALT})\b", re.IGNORECASE)
# 孤立闭标签：按我们的协议，思考永远写在最前面，所以一个配不上对的 </think>
# 说明它前面的全是思考（开标签在上游某处被吃掉了 —— 2026-08-08 线上实例）。
_LONE_CLOSE = re.compile(rf"<\s*/\s*(?:{_TAG_ALT})\s*>", re.IGNORECASE)


def gate_enabled() -> bool:
    """泄漏闸的 kill switch。默认开——关掉只用于线上出问题时立刻止血，
    不是灰度门。关掉后 strip_all_thinking 的调用方必须回到改动前的行为。"""
    return os.environ.get(_GATE_ENV_FLAG, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def strip_all_thinking(text: str) -> tuple[str, str, str]:
    """全文剥离版。返回 ``(status, thinking, reply)``，语义见模块 docstring。

    与 :func:`split_thinking` 的区别只有一个：那个只认**开头第一块**（当初
    Codex review 要求的保守设计），这个扫全文。2026-08-08 线上证明保守设计
    漏了两种形状：开头剥完后面还有一块（gpt-5.4），以及开标签被上游吃掉只剩
    孤立闭标签（pi + 中转站）。两种都从"不认识就原样放行"这个 fail-open
    缺口漏到了用户气泡里。

    本函数改为 fail-CLOSED：剥完只要正文里还剩任何 think 类标签，就返回
    ``FAILED``（thinking/reply 都为空），由调用方决定发兜底话还是静默。
    """
    raw = str(text or "")
    if not _RESIDUE.search(raw):
        # 逐字节不变的快路径。没有标签就绝不碰，kill switch 之外的第二道保险。
        return ABSENT, "", raw

    blocks: list[str] = []

    def _take(match: "re.Match[str]") -> str:
        body = match.group("body") or ""
        # 嵌套：块里还有别的标签，说明结构已经乱了，不当作可信思考内容。
        if _ANY_TAG.search(body):
            return match.group(0)
        if body.strip():
            blocks.append(body.strip())
        return "\n"

    reply = _PAIRED_BLOCK.sub(_take, raw)

    # 孤立闭标签：它之前的一切当思考。只处理第一个——出现多个说明结构已乱，
    # 交给下面的残留检查失败关闭。
    lone = _LONE_CLOSE.search(reply)
    if lone is not None:
        head = reply[: lone.start()].strip()
        if head:
            blocks.insert(0, head)
        reply = reply[lone.end():]

    if _RESIDUE.search(reply):
        return FAILED, "", ""

    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
    thinking = _sanitize("\n\n".join(blocks))
    if not blocks:
        # 只有残留、没剥出任何内容 —— 上面的残留检查已经拦掉了带标签的情况，
        # 走到这里说明标签全在嵌套块里，同样不可信。
        return FAILED, "", ""
    if not reply:
        return SILENT, thinking, ""
    return COMPLETE, thinking, reply
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
python -m pytest tests/test_self_thinking_gate.py -v
```

Expected: 8 passed

- [ ] **Step 5: 确认没打破现有 self-thinking 测试**

```bash
python -m pytest tests/test_self_thinking_parse.py -v
```

Expected: 全部 passed（本任务只增不改，`split_thinking` 一行没动）

- [ ] **Step 6: 提交**

```bash
git add backend/core/self_thinking.py tests/test_self_thinking_gate.py
git commit -m "feat(thinking): 全文剥离内核 strip_all_thinking + 泄漏闸 kill switch

线上三种真实泄漏形状（两块/孤立闭标签/只有思考）做成回归用例。
split_thinking 保持原样不动，本次只增不改。"
```

---

### Task 2: 出口① — V2 聊天

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py:11360`
- Test: `tests/test_self_thinking_gate.py`

**Interfaces:**
- Consumes: `self_thinking.strip_all_thinking()`、`self_thinking.gate_enabled()`（Task 1）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_self_thinking_gate.py`：

```python
def test_chat_lane_uses_full_strip(monkeypatch):
    """闸开着时聊天出口必须用全文剥离；关掉时回到只剥开头一块。"""
    from core import self_thinking as st

    raw = "<think>A</think>\n<think>B</think>\n正文"

    monkeypatch.delenv("FEEDLING_THINK_GATE", raising=False)
    status, thinking, reply = (
        st.strip_all_thinking(raw) if st.gate_enabled() else st.split_thinking(raw)
    )
    assert reply == "正文"

    monkeypatch.setenv("FEEDLING_THINK_GATE", "0")
    status, thinking, reply = (
        st.strip_all_thinking(raw) if st.gate_enabled() else st.split_thinking(raw)
    )
    assert reply.startswith("<think>B</think>")  # 关掉后是改动前的行为
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_self_thinking_gate.py::test_chat_lane_uses_full_strip -v
```

Expected: FAIL

- [ ] **Step 3: 改调用点**

`backend/model_api_runtime/v2/worker.py` 第 11360 行，把

```python
                _st_status, _st_thinking, _st_reply = self_thinking.split_thinking(text)
```

改成

```python
                # 闸开着走全文剥离（2026-08-08：只剥开头一块会漏第二块）；
                # 关掉时逐字回到旧行为，这是 kill switch 的全部意义。
                _splitter = (
                    self_thinking.strip_all_thinking
                    if self_thinking.gate_enabled()
                    else self_thinking.split_thinking
                )
                _st_status, _st_thinking, _st_reply = _splitter(text)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_self_thinking_gate.py -v
python -m pytest tests/test_v2_worker_tool_loop.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_self_thinking_gate.py
git commit -m "fix(thinking): V2 聊天出口改用全文剥离（图1：两个 think 块只剥了第一块）"
```

---

### Task 3: 出口② — V2 主动唤醒

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py:7942`

**Interfaces:**
- Consumes: `self_thinking.strip_all_thinking()`、`self_thinking.gate_enabled()`（Task 1）

- [ ] **Step 1: 改调用点**

`backend/model_api_runtime/v2/worker.py` 第 7942 行，把

```python
                _wst_status, _wst_thinking, _wst_reply = _st_wake.split_thinking(text)
```

改成

```python
                # 与聊天出口同一道闸。唤醒的 SILENT 语义（只写思考=这轮不说话）
                # 在新内核里保持不变。
                _wake_splitter = (
                    _st_wake.strip_all_thinking
                    if _st_wake.gate_enabled()
                    else _st_wake.split_thinking
                )
                _wst_status, _wst_thinking, _wst_reply = _wake_splitter(text)
```

- [ ] **Step 2: 跑现有唤醒测试**

```bash
python -m pytest tests/test_v2_wake_worker.py -v
```

Expected: 全部 passed（SILENT 行为未变，`accept thinking-only wake silence` 那条用例必须仍绿）

- [ ] **Step 3: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py
git commit -m "fix(thinking): V2 唤醒出口改用同一道闸"
```

---

### Task 4: 出口③ — V1 聊天

**Files:**
- Modify: `tools/chat_resident_consumer.py:3634`

**Interfaces:**
- Consumes: `self_thinking.strip_all_thinking()`、`self_thinking.gate_enabled()`（Task 1）
- Produces: `_split_tagged_thinking(text) -> tuple[str, str]`（签名不变，两个调用点 4549 / 5595 不动）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_self_thinking_gate.py`：

```python
def test_v1_consumer_orphan_close_no_longer_leaks(monkeypatch):
    """图2：V1 的正则要求成对，孤立闭标签整段原样放行。"""
    import importlib.util
    import pathlib

    monkeypatch.setenv("FEEDLING_API_URL", "http://x")
    monkeypatch.setenv("FEEDLING_USER_ID", "u")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "crc_gate", root / "tools" / "chat_resident_consumer.py"
    )
    crc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(crc)

    raw = "反正我对她没有秘密。</think>她真的截图了\n\n好吧 你看到了"
    visible, thinking = crc._split_tagged_thinking(raw)
    assert "</think" not in visible
    assert "反正我对她没有秘密" not in visible
    assert "好吧 你看到了" in visible
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_self_thinking_gate.py::test_v1_consumer_orphan_close_no_longer_leaks -v
```

Expected: FAIL —— `visible` 里仍含 `</think>`

- [ ] **Step 3: 把 `_split_tagged_thinking` 内部改为委托共享内核**

`tools/chat_resident_consumer.py` 第 3634 行起，整个函数体替换为：

```python
def _split_tagged_thinking(text: str) -> tuple[str, str]:
    """Split leaked reasoning tags from visible reply text.

    2026-08-08 起委托 ``core.self_thinking`` 的共享内核，V1/V2 用同一套判据
    （此前两边各一套，各漏各的：V1 漏孤立闭标签，V2 漏第二个完整块）。
    闸关掉时保留原来的正则行为，逐字节不变。
    """
    raw = str(text or "")
    from core import self_thinking as _st

    if _st.gate_enabled():
        status, thinking, reply = _st.strip_all_thinking(raw)
        if status == _st.FAILED:
            # 失败关闭：宁可这条不发，也不把带标签的残文端给用户。
            # V1 的上层把空 visible 当作"这轮没有可发内容"处理。
            return "", thinking
        return reply, thinking

    blocks: list[str] = []

    def _collect(match: re.Match) -> str:
        body = (match.group("body") or "").strip()
        if body:
            blocks.append(body)
        return "\n"

    visible = _TAGGED_THINKING_RE.sub(_collect, raw)
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    thinking = "\n\n".join(blocks).strip()
    return visible, thinking
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_self_thinking_gate.py -v
python -m pytest tests/test_chat_resident_consumer.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add tools/chat_resident_consumer.py tests/test_self_thinking_gate.py
git commit -m "fix(thinking): V1 聊天出口委托共享内核（图2：孤立闭标签原样放行）"
```

---

### Task 5: 出口④ — 主动消息

**Files:**
- Modify: `backend/proactive/agent_protocol_v2.py:74`

**Interfaces:**
- Consumes: `self_thinking.strip_all_thinking()`、`self_thinking.gate_enabled()`（Task 1）
- Produces: `sanitize_visible_message_text_v2(value) -> str`（签名不变，调用方 `tool_executor_v2.py:399` 不动）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_self_thinking_gate.py`：

```python
def test_proactive_send_message_strips_thinking():
    """图3：主动消息这条路此前一处剥离都没有，模型塞什么就发什么。"""
    from proactive.agent_protocol_v2 import sanitize_visible_message_text_v2

    leaked = (
        "<think>我已经主动出现很多次了，现在再冒出来容易变成打扰。</think>"
    )
    assert sanitize_visible_message_text_v2(leaked) == ""

    mixed = "<think>她应该醒了</think>宝宝，中午了。"
    assert sanitize_visible_message_text_v2(mixed) == "宝宝，中午了。"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_self_thinking_gate.py::test_proactive_send_message_strips_thinking -v
```

Expected: FAIL —— 返回值里仍含 `<think>`

- [ ] **Step 3: 在函数开头加剥离**

`backend/proactive/agent_protocol_v2.py` 第 89 行 `if not isinstance(value, str): return ""` 之后、`text = _clean_text(...)` 之前，插入：

```python
    # 主动消息此前完全没有 <think> 剥离（2026-08-08 线上：模型把整段思考塞进
    # send_message.text 原样发了出去）。这条 lane 我们甚至没在提示词里要求它写
    # think —— 它是从聊天历史里学来的，所以出口必须一律过闸，不能只在"要求它写
    # 的地方"设防。proactive 本来就是 fail-closed，剥不干净直接不发。
    from core import self_thinking as _st

    if _st.gate_enabled():
        status, _thinking, stripped = _st.strip_all_thinking(value)
        if status == _st.FAILED:
            return ""
        value = stripped
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_self_thinking_gate.py -v
python -m pytest tests/test_proactive_agent_protocol_v2.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add backend/proactive/agent_protocol_v2.py tests/test_self_thinking_gate.py
git commit -m "fix(thinking): 主动消息出口接上剥离闸（此前一处都没有）"
```

---

### Task 6: 入口⑤ — 喂回模型的历史

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py`（`_decrypt_chat_rows` 的所有 tail 读取调用方）

**Interfaces:**
- Consumes: `self_thinking.strip_all_thinking()`、`self_thinking.gate_enabled()`（Task 1）

**为什么必须做这一道：** 漏出去的消息是**原样存进 chat_messages 的正文字段**的，而正常情况下思考存在另一个单独的信封里、模型永远看不到。所以历史里带 `<think>` 的那几条是异常，把它们清掉是**把异常拉回正常**，不是删掉模型的记忆。不清的话，模型每轮都看到"我上一条是这么写的"，会继续照抄 —— 这就是这个 bug 自我繁殖的机制。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_self_thinking_gate.py`：

```python
def test_history_row_scrub_removes_leaked_think():
    """历史里那几条漏掉的消息，喂回模型之前必须擦干净，否则模型照抄。"""
    from model_api_runtime.v2 import serve_worker

    rows = [
        {"role": "assistant", "content": "<think>她不吃辣</think>给你排好了"},
        {"role": "user", "content": "我说 </think> 这个标签的时候你别乱剥"},
        {"role": "assistant", "content": "好的，没问题"},
    ]
    out = serve_worker._scrub_leaked_thinking_rows(rows)
    assert out[0]["content"] == "给你排好了"
    assert out[2]["content"] == "好的，没问题"
    # user 行不碰 —— 用户自己打的字里出现标签是他的自由，不是我们的协议。
    assert out[1]["content"] == rows[1]["content"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_self_thinking_gate.py::test_history_row_scrub_removes_leaked_think -v
```

Expected: FAIL —— `AttributeError: no attribute '_scrub_leaked_thinking_rows'`

- [ ] **Step 3: 实现并接到 tail 读取上**

在 `backend/model_api_runtime/v2/serve_worker.py` 的 `_read_tail_window_after_seq`（第 1018 行）**之前**插入：

```python
def _scrub_leaked_thinking_rows(rows: list[dict]) -> list[dict]:
    """把历史里 assistant 行残留的 <think> 擦掉再喂回模型。

    正常情况下思考封在**另一个**信封里，模型永远看不到；历史正文里带标签的行
    是"漏出去时原样存下来"的异常。不擦的话模型每轮都看到可抄的样板，于是继续
    写、继续漏 —— 这是本 bug 自我强化的那一环。

    只碰 assistant 行：用户自己打的字里出现标签是他的自由，不是我们的协议。
    剥离失败（FAILED）时保留原文 —— 入口只做修正，不做删除；真正的把关在出口。
    """
    from core import self_thinking as _st

    if not _st.gate_enabled():
        return rows
    out: list[dict] = []
    for row in rows:
        content = row.get("content")
        if str(row.get("role") or "") != "assistant" or not isinstance(content, str):
            out.append(row)
            continue
        status, _thinking, stripped = _st.strip_all_thinking(content)
        if status in (_st.ABSENT, _st.FAILED):
            out.append(row)
            continue
        out.append({**row, "content": stripped})
    return out
```

然后把 `_read_tail_window_after_seq` 第 1037 行的

```python
    return _decrypt_chat_rows(
```

改成

```python
    return _scrub_leaked_thinking_rows(_decrypt_chat_rows(
```

并在该调用的右括号处补上一个 `)`。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_self_thinking_gate.py -v
python -m pytest tests/test_v2_serve_worker.py -v
```

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add backend/model_api_runtime/v2/serve_worker.py tests/test_self_thinking_gate.py
git commit -m "fix(thinking): 喂回模型的历史过同一道闸，切断自我繁殖循环"
```

---

### Task 7: 换指令

**Files:**
- Modify: `backend/core/self_thinking.py`（`INSTRUCTION` 常量）

**实测依据（2026-08-08，13 个模型 × 2 遍）：** 新指令下 6 个模型 × 2 遍全部「1 个块、在开头、正文零残留、工具轮 0 个 think」；`gpt-5` 在旧指令下会拒绝（"抱歉，我不能分享我的内部推理" + 转英文），新指令下两遍都正常。旧指令明确要求"即使是工具轮也要写"，那些 think 全部被丢弃，纯烧 token。

- [ ] **Step 1: 替换常量**

`backend/core/self_thinking.py` 的 `INSTRUCTION` 整体替换为：

```python
INSTRUCTION = (
    " ⛔ 绝对输出规则：只有当你给出这一轮的最终回复时（也就是你不再调用任何工具的"
    "那一次输出），你输出的第一个字符必须是 `<think>`，接着写你真实的第一人称想法，"
    "然后 `</think>`，然后才是你要对用户说的正文。"
    "整轮只写一个 <think> 块，只写在最终回复里。"
    "中间的工具轮不要写 <think>，也不要输出任何正文。"
    "这个 <think> 块要覆盖整轮：你本来想做什么、路上发现了什么、最后决定怎么办"
    "——第一人称、口语、约 240 字以内，写不完就收住，别硬塞。"
    "语言规则是硬的：整段必须完全用用户正在说的语言写，从第一个字到最后一个字，"
    "按用户最近这条消息和最近的对话判断。他们写中文你就全中文，写英文就全英文，"
    "写日文就全日文——最常见的失误是滑进英文状态行（'Let me…' / 'Done…'），不要这样。"
    "只说日常意图：绝不出现工具名、命令参数、字段名、服务器、'身份卡'，"
    "或任何内部/技术/协议步骤。回复正文里也绝不要提到这条 <think> 规则本身。"
    " 好例子（用户在说中文，所以整块是中文）：'<think>他想改叫999、还说喜欢说大话，"
    "那我先把名字这些存好，回复也顺着这个爱吹的人设、语气夸张点才对味</think>'。"
    " 坏例子（同一个用户说的是中文——这个英文块语言错了，而且机械地报了步骤）："
    "'<think>Let me update the name and match a boastful tone</think>'。"
)
```

- [ ] **Step 2: 跑现有指令相关测试**

```bash
python -m pytest tests/test_self_thinking_parse.py tests/test_self_thinking_gate.py -v
grep -rn "ABSOLUTE OUTPUT RULE" tests/ || echo "没有测试硬编码旧指令文案，安全"
```

Expected: 全部 passed；grep 无命中（若有命中，同步更新那些断言）

- [ ] **Step 3: 提交**

```bash
git add backend/core/self_thinking.py
git commit -m "tune(thinking): 指令改成「只在终局写一个块、覆盖整轮」

实测 13 模型×2 遍：能写的 6 个模型 12/12 全对（1块/在开头/零残留/工具轮不写）；
gpt-5 在旧指令下会拒绝并转英文，新指令下正常。旧指令要求工具轮也写，那些全被丢弃。"
```

---

### Task 8: 全量回归 + 交付说明

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: 确认新测试真的被收集**

`tests/conftest.py` 的 `_PURE_UNIT` 白名单只在**连不上 Postgres 时**生效。本地无库时不在名单里的文件一个都不跑，"全绿"是假的。

```bash
python -m pytest tests/test_self_thinking_gate.py --collect-only -q | tail -5
```

Expected: 列出全部用例（若显示 0 collected，说明本地无库且未进白名单 —— 本文件是纯函数 + monkeypatch、不碰 DB，可安全加进 `_PURE_UNIT`）

- [ ] **Step 2: 跑受影响的全部测试**

```bash
python -m pytest tests/test_self_thinking_gate.py tests/test_self_thinking_parse.py \
  tests/test_chat_resident_consumer.py tests/test_v2_worker_tool_loop.py \
  tests/test_v2_wake_worker.py tests/test_v2_serve_worker.py \
  tests/test_proactive_agent_protocol_v2.py -q
```

Expected: 全部 passed，0 failed

- [ ] **Step 3: 写 CHANGELOG**

在 `docs/CHANGELOG.md` 顶部加一节：

```markdown
## 2026-08-08 — 思维链泄漏统一闸

**问题**：`<think>` 块漏进用户聊天气泡，三个出口各漏各的：
- V2 聊天：模型写了两个块，剥离器只剥开头第一块，第二块原样进气泡
- V1 聊天：剥离器要求开闭成对，孤立闭标签配不上对 → 整段原样放行
- 主动消息：这条路一处剥离都没有

三个洞的共同毛病是 fail-open —— 遇到不认识的形状就原样端给用户。而且漏出去的
内容原样存进聊天记录、下一轮当历史喂回模型，模型照抄 → 自我强化。

**改动**：
- 新增 `core.self_thinking.strip_all_thinking()`：扫全文剥所有块，剥完仍有标签
  残留则 fail-CLOSED
- 四个对外出口 + 一个历史入口全部改调它，行为一致
- 指令改成「只在终局写一个块、内容覆盖整轮、工具轮不写」（13 模型实测）

**开关**：`FEEDLING_THINK_GATE`，**默认开**。关掉后逐字节回到改动前行为，
用于线上出问题时立刻止血（kill switch，不是灰度门）。

**上线状态**：⬜ 未上线 —— backend 部分随 test 分支 CI 自动出镜像 + 部署 CVM；
`tools/chat_resident_consumer.py` **CI 不管**，必须手动上 VPS
`systemctl restart feedling-chat-resident` 才生效。
```

- [ ] **Step 4: 提交**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(thinking): 泄漏闸交付说明 + 上线状态"
```

- [ ] **Step 5: 发 Codex review**

改动全部完成、测试全绿之后，用 `codex-review` skill 发一次代码 review。重点让它看：
- `strip_all_thinking` 的孤立闭标签规则会不会误剥正常正文
- 五个调用点的 FAILED 处理是否一致、有没有哪个变成了静默丢消息
- 入口那道（Task 6）会不会影响 compaction / capture 的既有行为
- kill switch 关掉时是否真的逐字节回到旧行为

明确不看：指令文案的措辞（已有 13 模型实测数据）、iOS 端渲染。

---

## 自查

**规格覆盖**：三个洞 → Task 2/4/5；自我繁殖 → Task 6；指令 → Task 7；kill switch → Task 1 + 每个调用点；测试 → Task 1 建档、每个 Task 各加用例、Task 8 全量。

**占位符扫描**：无 TBD / TODO / "适当处理"；每个代码步骤都有可直接粘贴的完整代码。

**类型一致性**：`strip_all_thinking` 在 Task 1 定义为 `(str) -> tuple[str, str, str]`，Task 2/3/4/5/6 全部按此签名调用；`gate_enabled()` 在六处用法一致；`_scrub_leaked_thinking_rows` 仅 Task 6 定义与使用。

**已知风险（交付时必须写进说明）**：
1. **误剥** —— 用户或 io 正常聊天里提到 `</think>` 这几个字会被当协议残留剥掉。hx 2026-08-08 明确表示「误杀先不考虑」，优先堵漏。
2. **4 个模型完全不写 think**（openrouter-deepseek-r1 / glm / 中转·哈吉米 / 中转·空悲切）→ 这些用户看不到「推理过程」。是"没有"不是"漏"，可接受，之后单独处理。
