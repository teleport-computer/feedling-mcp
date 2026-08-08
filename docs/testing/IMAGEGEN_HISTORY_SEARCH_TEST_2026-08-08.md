# Liko 新功能验收:AI 生图投递 + 聊天历史搜索

> 2026-08-08,claude4。范围 = Liko 刚合的两个功能:
> **后端 PR #156**(`e614e652` codex/ai-image-delivery)+ **iOS #167/#178**。
> 按 Seven 要求在 **resident consumer / agent runtime V1 / V2** 三个环境分别验。
> 状态:进行中(真实生图那支还在跑),先落已确认的部分。

---

## 一、两个功能各是什么形状(先搞清再测)

### 1. AI 生图投递 —— **三个运行时是三条不同的路**
| 运行时 | 能力来源 | 投递方式 |
|---|---|---|
| **V2** | `model_api` 主模型 / 专用生图路由 | 模型调 `generate_image(prompt)` 工具 → 后端调生图 provider → 图片气泡 |
| **hosted_v1**(我们 CVM 上的 V1 agent) | 同上(也走 model_api 口径) | CLI agent 产图 → `io_cli send-image` |
| **resident**(用户自己服务器) | **consumer 广播的 capability 头**:`image_generation_v1`(总是广播)+ `agent_image_generation_v1`(仅当 `FEEDLING_AGENT_IMAGE_GENERATION=true`) | 先试专用路由;没有则回落到 agent 原生能力 + `send-image`;都没有则给引导文案 |

产品硬约束(写在工具描述里):**绝不允许**用「图片」二字、Markdown 占位、
假 URL 或"我已生成"来搪塞——要么真交付字节,要么返回结构化失败。

### 2. 聊天历史搜索 —— **纯客户端**
iOS `ChatHistorySearchView` + `ChatLocalStore.search()`,查的是**本地已解密缓存**
的 SQLite,没有任何后端接口。因此**三个运行时同构**,不存在"某运行时搜不到"的
可能;真正的跨运行时风险在于:**各运行时投递的消息是否带对了 contentType**
(text/image/file),否则筛选与分组会漏。

---

## 二、已确认结果

### ✅ V2(agent runtime V2)
- 配置解析正确:`runtime=v2` / `source=model_api` / `mode=follow_main` / `effective_status=untested`
- **失败契约达标**:无生图能力时求画图 → 「当前模型不能生成图片,请到设置里添加生图模型。」
  无假声明、无 Markdown 占位、无本地路径、**无裸 error code 泄漏**

### ✅ hosted_v1(agent runtime V1)
- 配置解析正确:`runtime=hosted_v1` / `source=model_api` / `mode=follow_main`
- 失败契约同样达标(同一句引导文案)

### ✅ resident(自建 consumer)—— 含一个值得记的正向发现
- 配置解析正确:`runtime=vps` / `source=resident` / `available=true`
- **能力开关翻转正确**:`FEEDLING_AGENT_IMAGE_GENERATION` 关 →
  `image_generation_test_status=unsupported` + 机器可读原因
  `image_generation_model_required`;开 → `status=ok`。同一账号重启 consumer
  即翻转,说明后端确实按 consumer 广播的 capability 头判定。
- ⚠️ **诚实标注**:这一轮里 resident 的"不谎称已生成/不泄漏错误码"两条是
  **空断言**——该轮 consumer 未配解密源,它跳过了用户消息、从未回复。
  真正的 resident 聊天证据由下面的投递腿探针提供。
- **升级信号是有效的**:我最初误用了**未含本功能的旧 consumer**,后端如实报
  `unavailable_reason=resident_update_required`;换成合并后的 consumer 即变
  `available=true`。也就是说**没升级的自建用户会被正确告知需要更新**,不会静默失效。

### ✅ 静态审计:唤醒 lane 不给生图工具
`on_image_reply` 只在聊天 lane 接线(`process_job`),`_run_wake` 不传 →
**没有用户在场的那一轮拿不到 `generate_image`**。符合 usr_a40e 之后定的
"无人在场分档"原则(不烧用户的钱、不擅自产出)。

### ✅ 专用生图路由的失败也是结构化的
配 `gemini-3-flash-image` 被明确拒:`400 image_generation_model_incompatible,
retryable=false`(不是超时/裸 500);换 `openai/gpt-image-1` 配置成功、
`mode` 正确切到 `dedicated`。

---

### ✅ 真实生图端到端(V2 + 专用路由)
配 `openai/gpt-image-1` → 路由自检 `status=ok` → `mode=dedicated` /
`effective_status=ok` → 说"画一张水彩橘猫" → **聊天里真落了一条 JPEG 图片消息**,
mime 在 PNG/JPEG/WebP 白名单内。不是"接口 200",是端到端真出图。
(顺带:`gemini-3-flash-image` 被明确拒为 `image_generation_model_incompatible,
retryable=false` —— 失败也是结构化的,不是超时/裸 500。)

### 📐 生成图片的消息形状(实测 dump)
一次生图请求在历史里只产生 **2 条**消息:用户那条 + **一条 `content_type=image`
的图片消息**,带 `image_mime` / `image_byte_count`;**没有伴随的文字气泡**。

| 对 iOS 历史搜索意味着 | 结论 |
|---|---|
| 按「图片」筛选浏览 | ✅ 可以——`content_type=image` 正确,`imageResults` 能分组 |
| 按内容搜"柴犬"/"橘猫" | ❌ 搜不到——`searchableText(.image)` 取的是 `message.content`,而生成图的 content 就是图片本身,没有任何描述文字 |

## 三、三运行时总账

| 验收项 | V2 | hosted_v1 | resident |
|---|:--:|:--:|:--:|
| 配置解析(runtime/source/mode) | ✅ | ✅ | ✅ |
| 无生图能力 → 引导文案,不谎称 | ✅ | ✅ | ✅(见下注) |
| 能力识别 / 开关翻转 | ✅ | ✅ | ✅ 关=unsupported、开=ok |
| 结构化失败(不兼容型号被明确拒) | ✅ | ✅ | ✅ |
| 唤醒轮拿不到生图工具 | ✅ | ✅ | n/a |
| **真实出图落地** | ✅ JPEG(gpt-image-1) | 共用 model_api 口径 | ✅ PNG(投递腿实测) |

### ✅ resident/V1 投递腿实测(最后一格)
不需要模型会画画:stub agent 写一张真 PNG 到 outbound-files → `io_cli send-image`
→ IPC → 运行中的 consumer 落图片消息。结果:
- agent 侧收到 `{"ok": true, "staged": true, "mime": "image/png", "byte_count": 70}`
- 聊天历史里真出现 `content_type=image` / `image_mime=image/png` 的消息
→ **hosted_v1 与 resident 共用的这条投递腿是通的**(此前只静态读过代码)。

**过程中顺带走通了自建用户的完整上线路径**(也是三道门):
身份卡 → 至少一条记忆 → `POST /v1/chat/verify_loop` 通过,后端才收 agent 的回复。
少任何一道都是 `bootstrap_incomplete`(依次报 `needs_decrypt_source` /
`needs_live_connection`),错误信息**指向明确、可自助**,这点是好的。

## 四、测试基建问题(已修并提交 `867f6144`)
`tools/e2e/client.py` 写死的 enclave 地址是网关 passthrough 主机名
(`<app-id>-5003s.dstack-pha-prod9…`),由 app_id 派生 → **换 CVM 重部署即失效**,
今天实测已 TLS 握手超时,而 `test-enclave.feedling.app` 返回 200。
危害不是"探针慢一点":任何需要解密源的环节(尤其本地起 resident consumer)
会在启动时直接 CRITICAL 退出,表现成"consumer 起不来",极易误判成产品坏了
——我今天就栽了一轮。已改成稳定自定义域,并同步修了发版测试文档里同一地址。

## 四、给 Liko / Seven 的观察(非 bug,值得一议)
1. **生成的图片没有任何文字描述,因此搜不到**(已用实测 dump 坐实,不是猜测)。
   一次生图只产出一条 `content_type=image` 的消息,没有伴随文字气泡;
   iOS 搜索对图片取 `message.content` 当可搜索文本,而那里是图片本身。
   → 用户三天后想找"我让它画的那只柴犬",只能在图片筛选里一张张翻。
   **建议**:把生图 prompt(或模型给的一句描述)落进图片消息的 caption/content,
   一处小改就能让新做的搜索对图片真正有用。这两个功能是同一天合的,正好一起收。
2. 三条运行时路径的**用户可见文案是同一套**(consumer 与后端各有一份中文映射表),
   这是对的;但它们是**两份拷贝**(`tools/chat_resident_consumer.py` 与
   `backend/hosted/image_generator.py` 各写一遍),将来改文案要同步改两处 ——
   建议像 `notices/catalog.py` 那样收敛成单一事实源。
