# Liko 新功能评审 & 上线打磨计划

**作者**: claude4（配合 Seven）·**日期**: 2026-07-28
**范围**: 三条未合后端分支的独立功能审计 + 设计分歧 + 打磨计划
- `codex/vision-model-routing`（视觉模型路由，含 `integration-file-activity`）
- `codex/integration-file-activity`（V2 文件生成 + 活动时间线）
- `codex/ai-voice-call-v2-gateway`（ElevenLabs 语音走 IO）

**目的**: Liko 自述这几个是半成品；本文档是「在其基础上调优、以好状态上线」的依据。审计由三个独立 agent 分别跑，结论下附。

---

## 0. 一句话结论

| 功能 | 可用性 | 安全 | 结论 |
|------|--------|------|------|
| **Vision 视觉模型** | ❌ **有 2 个 P0** = 现在用户发不了图的根因 | ok | **不改不能上**；且设计哲学与我们「fail-open 优先」冲突 |
| **File-activity 活动时间线** | ✅ gate 真实、可用 | ok | 可合可调；唯一要盯：它顺手把**全量聊天回复排序**改了 |
| **Voice 语音网关** | ✅ 架构干净 | ✅ 无 SECURITY_FAIL | 3 个 P2 收尾即可 |

---

## 1. Vision 视觉模型

### 1.1 Bug（P0，就是当前发图被堵的根因）
- **P0-1 `backend/hosted/chat_send_core.py:155-167`**：V2 发图时无 dedicated vision route 就回退主路由并要求 `vision_test_status=="ok"`；但该值**只有专门的 dedicated-vision-test 流程**会置 ok，普通 `model_api_setup` 从不探测 → 迁移 0063 把**所有存量行默认 `'untested'`** → 主模型本来能看图的 V2 用户，下次发图必 `409 vision_model_required`。
- **P0-2 `backend/hosted/setup_core.py:443-446`**：`/v1/vision/config` 的 `effective_status` 对 model_api 主路由永远返 `'untested'`；iOS `imagePreflight()` 只认 `"ok"`，`'untested'` 落 `default` → fail-closed **堵死相册/相机**。
- **P2 不自洽**：`vision_routing.py:47-55` `dedicated_route_for_send` 无 route 时 `(None,None)` 放行（对），但 `chat_send_core.py` 内联那段反过来拦主路由。两条 send-time gate 语义相反。

### 1.2 设计上跟我们想法不同的地方
1. **fail-closed 到「判死自己没探测过的主模型」**——违反我们 usr_fee1dfed 之后定的原则（fail-closed 门必须有客户端配套 + 可观测 + 优雅降级）。这是最坏的一种：对能看图的主模型默认 untested → 直接堵。
2. **「手动加一个独立视觉模型 + 手动 test」的心智**——我们原本更接近「自动探测主模型能否看图，不能才提示」。Liko 做成需要用户多两步操作（加模型、点测试）。
3. 后端把「没测过」和「不支持」在客户端合并成同一个 fail-closed 结果，丢了「能看图但没标记」这一档。

### 1.3 需要跟 Seven double-check
- 发图失败该 **硬 409 拦** 还是 **降级发过去、让模型自己说看不了**？（体感差别大）
- 视觉模型是「全局一个」还是「按对话/按主模型」？
- resident（自部署）用户的视觉能力靠 modality 上报，离线/旧 consumer 怎么退化？
- 我们要「自动探测优先、手动兜底」还是「纯手动配置」？

### 1.4 Polish
- 后端：主模型原生支持 vision → 短路 `effective_status="ok"` + 跳过 send-time gate（镜像 `dedicated_route_for_send`）；`model_api_setup` 时顺带探测 vision 能力落库。
- iOS：`imagePreflight` 对 404 / 未探测 / 网络失败 **fail-open**（老后端/未知按老逻辑放行）。
- 统一两条 send-time gate；删 orphan slug `vision_runtime_v2_required`（iOS 有 case、后端无发射）。

---

## 2. File-activity 活动时间线

### 2.1 可用性 / bug — PASS
「confirmed/trusted」gate 是真的：`backend/core/chat_activity.py` 是**只投影固定元数据**的层（activity_id/tool_name/state/duration/result_code + 计数），**架构上无法泄漏模型散文/工具参数/CoT**；write 活动反映的是**durable 已应用**（`worker.py:8386` 未 durably applied 直接 raise），不是模型嘴上说的。V1/V2 双时间线都走同一投影器，非 stub。迁移干净可逆。

### 2.2 设计分歧 / double-check
- **⚠️ `ordered_chat_replies=True` 对所有生产聊天生效**（`serve_worker.build_production_deps`），不只文件轮：改了 prompt 组装、关掉 `fold_new_messages`、每轮钉最老未答消息、多发时派 `ordered_followup` job。**这是一个「文件功能」顺手改了全量回复排序** → 是我们要的全局行为吗？并发连发的排序/合并语义变了，必须 soak。
- 时间线默认**隐藏「零结果的记忆检索」行**（`_collapse_memory_discovery`）——有意降噪还是该显示？（truth-preserving，但是「不显示发生过的事」的产品决定）
- 每条最终回复多一次 `status_events_for_job`（LIMIT 500，已建索引）DB 读——热路径新增成本，bounded 非致命。

### 2.3 Polish
多发/并发 soak（`docs/testing/CHAT_ACTIVITY_V2_MANUAL.md`）；确认排序改动是有意的全局决定；把「隐藏零结果」设成可配或明示。

---

## 3. Voice 语音网关

### 3.1 可用性 / 安全 — PASS，无 SECURITY_FAIL
**威胁模型是反的**：不是 IO 持 ElevenLabs key，而是 **ElevenLabs 调 IO**（`POST /v1/voice/chat/completions`，OpenAI 兼容 Custom LLM）。IO 不存/不传/不记任何 EL 凭证（grep 零命中）。唯一新凭证是短时 HMAC 签名的 **voice session token**（`core/voice_token.py`，绑 `(user_id, call_id)`、`aud="io_voice_llm"`、600s TTL）。存储全 AES-GCM 密文、`user_id` 进 GCM AAD、SQL 全 `WHERE user_id`，多租户安全。enclave 只多拷两个 bounded 明文路由 ID，不碰 attestation/隔离不变量。

### 3.2 设计分歧 / double-check
- **⚠️ `backend/accounts/accounts_core.py:45` `access_modes_switch` 里塞了个副作用**（切换接入方式时同步 hosted runtime mode，失败 best-effort 回滚）——**跟 voice 无关，搭在 voice 提交里**。回滚语义不全（重存 previous_mode 但不回滚已改的 runtime control）。合前应拆清、单独功能审。
- **dormant `/v1/internal/voice/delta`** 只 `require_auth` 没 `require_scope`（sibling `/reply` 有）——目前无调用方（死代码），先 scope-gate 或删，别裸留。
- `voice_turn_streams` 缺 FK + `ON DELETE CASCADE`（账号删留孤儿到 900s TTL）——与 `voice_turn_results` 不对称。

### 3.3 Polish
收这 3 个 P2；确认那个 access-mode runtime-sync 副作用是有意为之还是误入。

---

## 4. 跨功能的设计分歧 & 重点提醒（最需要跟 Seven 对齐）

1. **fail-closed 哲学冲突（vision）**——最需要对齐。我们的原则是 fail-closed 必须带客户端配套 + 可观测 + 降级；vision 把「没探测过的能看图主模型」判死，正是反例。
2. **功能搭错车（voice 提交夹带 access-mode runtime sync）**——hygiene，合前拆清楚，别让不相干改动混进 feature。
3. **全局行为被局部功能改（file 顺手改全量回复排序 `ordered_chat_replies`）**——scope creep，确认是有意的。
4. **三个功能都只保证 V2**——V1/resident 的降级路径逐个确认（尤其 vision）。

---

## 5. 测试计划（合完怎么验「可用 + 无 bug」）

- **Vision**：①主模型能看图的账号发图 → **必须 NOT 被拦**（验 P0 修好）；②配 dedicated 视觉模型 → 图路由到它；③都不能看图 → 正确提示、不硬崩；④iOS 连老后端(404) → fail-open 放行。
- **File-activity**：多发/并发 soak，活动时间线只显真事；记忆→文件集成用例。
- **Voice**：token 流（EL→`/v1/voice/chat/completions`，call_id 绑定校验）、900s TTL 过期、账号删除清理。
- **全部**走 L1 全测 + claude4 gatekeep 迁移单 head + upgrade/downgrade 双向。

---

## 6. 上线节奏建议

1. **codex4 integration**：修 vision 2 个 P0 + re-parent 迁移成单 head → **claude4 gatekeep** → 合 test。
2. **test 功能验收**（§5 测试计划）+ **iOS fail-open 兜底**先行。
3. **polish 一轮**：收各 P2 + 对齐 §4 设计分歧。
4. **再上 prod**；**vision 尤其要等 iOS 兜底一起发**，否则又是前后端不同步（重蹈 fail-closed 覆辙）。

---

## 附：审计出处
- 三份独立 agent 审计（general-purpose），2026-07-28。P0/P2 均带 `file:line`，见各功能小节。
- 迁移 DAG 多 head 与 re-parent 方案：交 codex4 处理（`codex4/integrate-vision-voice-fence`），claude4 gatekeep。
