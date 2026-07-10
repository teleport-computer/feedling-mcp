# Hosted Runtime V2 — 多模态（图片）设计

> 承接 `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` §A「chat image」行 + §E BUG-1，以及
> `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-agent-loop-design.md` §12 决策 2
> （「先止血、多模态另立一轮」）。本文是那一轮。

**Goal:** 让 V2 的模型真正看见用户发的图片和随图的文字。

**Core claim:** 图片必须**在对话里（in-band）**进入模型，而不是作为某个 capability 的返回值。

---

## 1. 核实过的现状（不是推断）

| 事实 | 位置 |
|---|---|
| `provider_client` **已经支持**多模态 wire，且三条路径各有测试 | `_content_to_anthropic:337`、`_content_to_gemini_parts:357`、`_image_parts:316`；`tests/test_provider_client.py` 三个 `*_image_parts` 测试 |
| openai / openai_compatible / deepseek / openrouter 走 `_build_openai_compat_payload`，`messages` **原样透传** | `provider_client.py:773-790` |
| 内部规范形状 = OpenAI 风格 content block 列表 | `[{"type":"text","text":...},{"type":"image_url","image_url":{"url":"data:<mime>;base64,<b64>"}}]` |
| 已有调用方在构造它 | `hosted/turn.py:1784`、`enclave/routes/frames.py:226` |
| **V2 的 `_read_tail` 把图片消息直接写成字面量 `"[image]"`，从不解密** | `serve_worker.py:186` |
| **V2 的 `_read_messages` 同样写 `"[image]"`** | `serve_worker.py` |
| **随图的 caption 被完整丢弃** | `chat_send_core` 把它单独加密进 `extra.caption_*`（`chat/service.py:115`）；V2 两个读侧一个都不读 |

**所以缺口在数据入口，不在 wire。** 今天用户发一张图配一句「这个报告哪里有问题」，模型看到的是 `[image]` 五个字符，那句话人间蒸发。这不是 BUG-1（BUG-1 是 planner 选了 `chat_image_read` 之后毒化 grounding context），是更早、更基础的一个洞。

## 2. 为什么图片走 tail，不走 capability

resident 那边 codex 能读图，是因为图片在**对话**里（`chat_resident_consumer.py:2768` 原生 attach）。

而 capability 的返回值最终会经 `responder._fold_action_results` 折进**文本** grounding context —— 那正是 BUG-1 的成因。把图片塞回工具通道，等于把刚堵上的洞重新挖开。

**结论：`chat_image_read` 永久留在 planner 词表外**（不是暂时止血）。图片经 tail 到达 responder。词表里少一个 action，模型多一份真实视觉输入。

## 3. 致命约束：compaction 和 responder 共用同一个 `read_tail`

`worker._run_compaction` 调 `deps.read_tail(user_id, watermark, 10_000)`，而
`compaction._render_old_messages` 做 `f"{role}: {content}"`。

于是**天真的做法（让 `read_tail` 直接返回 image block 列表）会同时造成两个灾难**：

1. 把 `data:image/jpeg;base64,...` 整段塞进**摘要器**的 prompt —— BUG-1 在 maintenance lane 原地复活。
2. `limit=10_000` 意味着对该用户**历史上每一张图**都发起一次 enclave 解密往返 —— enclave 是单线程瓶颈，这是整个子项目存在的理由。

**所以 `read_tail` 的返回形状必须保持纯文本、保持不变。**

## 4. 架构：图片走一条独立的、有上限的注入通道

```
_read_tail          → 纯文本（不变）。图片行 content = caption 或 "[image]"，
                      附非敏感标记 has_image / image_mime。compaction 原样受益（终于看得见 caption）。
deps.read_images    → 新注入依赖。只对**指定的 message_id** 做 enclave 解密，返回 b64。
worker              → 读完 tail 后，挑最近 ≤ _TAIL_IMAGE_LIMIT 个 has_image 行，
                      调 read_images，把那几行的 content 就地换成 content block 列表。
context.build_turn_messages → 放行列表型 content。
responder / provider_client → 一行不改。
```

`compaction` 走的还是原来的 `read_tail`，拿到的还是纯文本，**零 enclave 图片解密、零 b64 污染**。

### 4.0 `read_images` 不写新的解密代码

`capabilities/chat.image_read` 已经在做这件事（打 enclave `GET /v1/chat/history`，按 id 取
`image_mime` + `image_b64`），而且它**仍然注册在 registry 里、只是 planner 词表够不到**。
`serve_worker._read_images` 直接经 `cap_registry.run_capability("chat_image_read", ...)` 复用它。

这正是「把 `chat_image_read` 移出词表」和「删掉这个 capability」的区别：能力还在，只是**不再由模型
选择、不再流经文本 grounding context**，改由 worker 在确定的位置、带确定的上限调用。

caption 则由 `_read_tail` 自己解（重建 `caption_*` 前缀的信封，镜像
`enclave/routes/chat.py:79-92` 的重建方式；AEAD AAD 是 `owner_user_id||v||id`，所以必须用
`caption_id` 而不是消息自己的 `id`）。这样 compaction 和 planner 也能看到 caption。

### 4.1 上限（不是可选项）

| 常量 | 默认 | 理由 |
|---|---|---|
| `_TAIL_IMAGE_LIMIT` | 2 | 每回合最多注入最近 2 张图。enclave 单线程，且图片每回合重发会让 token 爆。 |
| `_CAPTION_DECRYPT_LIMIT` | 8 | `_read_tail` 只为最近 8 个图片行解 caption；更早的退化成 `"[image]"`。挡住 compaction 的 `limit=10_000` 把 caption 解密放大成历史全量。 |
| `_IMAGE_MAX_B64_CHARS` | 2_000_000 | 单张图 b64 超限则跳过注入、退化成文本标记。不引入图像缩放依赖（无 Pillow）。 |

超限一律**静默降级为文本标记**，绝不失败整个回合——用户宁可拿到一条看不见图的回复，也不要拿到 error chip。

### 4.2 planner 看什么

planner 是 **JSON 文本通道**，绝不喂 b64。`_read_messages` 给它 `caption or "[image]"`。planner 知道「有张图 + 用户说了什么」，足够决定要不要查记忆/感知。

## 5. 不变量（不得放松）

- **BYOK-only**：不变。图片不引入任何新的 LLM 调用。
- **单次解密**：`provider_config` 仍然一回合解一次。
- **ENCLAVE_SEMAPHORE**：`read_images` 的调用必须在 worker 现有的 `async with enclave_sem` 块内，与 `read_summary`/`read_tail` 同一把闸。**每回合新增的 enclave 往返 ≤ `_TAIL_IMAGE_LIMIT`。**
- **no-filler**：图片解密失败不写气泡、不弹 error，降级成文本继续作答。
- **依赖方向**：`context.py` 保持纯（stdlib）。`worker.py` 不 import `hosted`。`read_images` 经 `TurnDeps` 注入，生产实现在 `serve_worker`（装配层）。
- **compaction 的 tail 永远是纯文本。**

## 6. 诚实的损失 / 已知边界

1. **图片只在 tail 窗口内可见。** 滚出窗口后模型再也看不到它，摘要里只剩 caption 文本。这与 resident 一致（CLI 也只看当前对话）。
2. **弱/中转 provider 可能不支持图片块。** openai_compatible 原样透传给中转站；不支持的会报错。`reliable_chat_completion_async` 已有重试/分类，失败会走 `ResponderError` → chat lane 弹 error chip。**这是本轮唯一的用户可见退化风险**，需要在 rollout 时盯 `provider_config` 类错误率。
3. **每回合重发图片。** 无 prompt caching。这是 `_TAIL_IMAGE_LIMIT=2` 存在的直接原因。

## 7. 落地文件

- `backend/model_api_runtime/v2/context.py`：`build_turn_messages` 放行列表 content；新增纯函数 `text_of(content) -> str`
- `backend/model_api_runtime/v2/worker.py`：`TurnDeps.read_images`；`_inject_tail_images(tail, deps, user_id)`；chat + wake 两条 responder 路径接线；三个上限常量
- `backend/model_api_runtime/v2/serve_worker.py`：`_read_tail`/`_read_messages` 解 caption + 打 `has_image`/`image_mime` 标记；新 `_read_images`
- **不改**：`responder.py`、`provider_client.py`、`executor.py`、`compaction.py`、`capabilities/*`、planner 词表（`chat_image_read` 保持移除）

## 8. 明确不在本轮范围

- prompt caching（图片重发的真正解药，属 `native_tools` 那一轮）
- 图像缩放 / 重编码（会引入 Pillow 依赖）
- 把 `chat_image_read` 加回 planner 词表 —— **本轮明确否决，见 §2**
- BUG-2 / BUG-3、`schedule_wake`、dream / screen_watch lane
