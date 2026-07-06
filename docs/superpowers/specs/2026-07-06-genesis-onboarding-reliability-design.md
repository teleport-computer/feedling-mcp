# Genesis Onboarding 可靠性 — 记忆抽取健壮化 + 部分成功 + 可观测/可补 — 设计方案

状态:CC 写方案 + Codex review 已折入(顶回 1/2/3 + 错误分类补充 + 后端优先)→ **CC 实现(TDD),本次只后端**。
分支:后端 `feat/genesis-onboarding-reliability`(基于 origin/test);iOS 新分支(基于 origin/main,含三 material sheet)。
前置:Round1-3 已在 test/prod(mode 分派 / 身份吃满卡 / 改身份重建 persona / 空重试 / combined flag[仅 test 开] / 报错硬化)。

---

## 背景(真实失败数据)

PROD 最近 genesis trace 窗口:6 个蒸馏 turn,**2 成 4 败,失败全在 `genesis_v2_foreground` 阶段**,失败时 `memory_action_count=0`、identity 未写、无回复。真实样本:
- `usr_5239999` / `genesis_7b2c6c2749964fe9`:`history_count=28538`、27 chunk → `ProviderError:ReadTimeout` → 整单挂。
- `usr_81a0645d` / `genesis_1204e31bf6f745ae`:`history_count=0`(只有 support 材料)→ ReadTimeout → 整单挂。
- `usr_0d9f7003` × 2:`provider response had no usable reply text` → 整单挂。

CC 复现:openrouter/sonnet-4.5(prod 同款)+ 同 4 份文件,test 上 4 次 acceptance **3 次失败** → **provider 不稳是主因**;前台链路偏重(大导入 27 次串行 fact_map)放大了失败。

**根因定性**:失败 = provider 短时抖动(ReadTimeout / 空回复)+ 前台链路重,**不是写库问题、不是 enclave 直接问题**。

---

## 核心原则(本方案的地基)

> **onboarding 完成 ≠ 记忆/身份成功。onboarding 应几乎总能完成(空上传 = fresh start 也合法,用户之后自己命名 / 加记忆)。本次优化的对象只有「记忆抽取」——让它健壮、降级、可观测、可补,而绝不把整单 onboarding 拖挂。**

明确**不做**:
- 不开 combined(voice/persona 保持后台;开了前台更重、失败更多)。
- 不后台精修 identity(用户上传后可能手动改过身份,精修会覆盖用户编辑)。
- 不做 memory core/full 前后台拆分(收益小、添乱;身份保持吃全量)。
- 不用绝对记忆条数当门槛(小上传本就记忆少,不能因此判失败)。

---

## Part 1 · 错误分类(所有判定的基础)(收敛 Codex 补充)

**主判据 = 复用现有 `provider_client.classify_provider_error`**(已较全),**message 关键词只做兜底**(防未来别的层把异常包装掉 → 分错类)。**结构化分类是主,字符串匹配是兜底,不当主判据。**

| 类 | 结构化(主) | message 兜底词 | 处理 |
|---|---|---|---|
| **transient(重试)** | `TimeoutException`/`TransportError`、408/425/429/500/502/503/504、`ProviderError(status_code=None)` | `ReadTimeout`/`timeout`/`no usable reply`/`returned non-json`/`returned non-object` | **重试**(带退避) |
| **provider_config(硬,不重试)** | 400/401/402/403/404/422 | `402`/`quota`/`credit`/`insufficient`/`invalid api key`/`unauthorized`/`forbidden` | **整单 abort**,报"额度/密钥" |
| **infra(abort)** | enclave / 信封 / 存储失败 | — | **整单 abort**,报 infra |

> 现状 bug:Round3 `_complete_json_retry_empty` 只在"结果空"时重试;**ReadTimeout 是抛异常,直接冲出重试包 → mark_failed 整单死**。底层 `reliable_chat_completion` 虽重试 timeout 但**次数/时长不够**。本方案把 transient 覆盖全 + 加"跳过"兜底。

---

## Part 2 · 记忆抽取健壮化(核心)

### 2.1 逐块 fact_map(每个 chunk)
```
调 provider →
  成功 → 收候选
  临时错误 → 重试 N 次(建议 cap 2-3 + 退避)
      成功 → 收候选
      重试用尽仍挂 → 【跳过这一块】,记一次 failed window,继续下一块   ← 不拖挂整单
  硬错误 → 整单 abort(报额度/密钥)
  infra 错误 → 整单 abort(报 infra)
```

### 2.2 fact_write(汇总写记忆,1 次)
- 临时错误 → 重试;用尽 → 当作"这次没写成记忆",**继续**(不拖挂)。
- 硬/infra → abort。

### 2.3 identity(Codex 顶回 2:兜底 ≠ 无名 done)
- 先正常 provider derive。
- transient 用尽后 → **尝试非 LLM 轻量身份**:从 support 材料(人物卡/档案里显式的名字/角色)+ relationship anchor 凑一个 identity signal(名字或维度),**不再调 LLM**。
- **只有 `fresh_start`(真空)才允许无名 done**;有内容时若连轻量身份都无 signal → 走 §3 的"有内容全失败"failed。
- 硬错误 → abort。

> 为什么必须这样:现有 `onboarding_validation.py` 里,**done job 但 identity card 空,validate 仍卡 `identity_card` 步**(有测试语义)。若实现成"无名也 done",会出现 job=done 但 app 卡在身份卡 = 用户像 onboarding 没完成的灰区。所以有内容时必须凑出 identity signal,否则明确 failed。

### 2.3b greeting
- 临时 → 重试;用尽 → 模板兜底(已有,永不失败)。

### 2.4 大导入前台采样(减重,只砍聊天史桶)
- `_plaintext_source_groups` 已按来源分桶:ai_persona / user_profile / memory_summary 各是小桶(各 1 块),history 是大桶。
- **只对 history 桶采样**:chunk 数超上限(如 >8)时 `_select_evenly` 到 ~8 块跑前台;**人物卡/档案/长期记忆桶全读**(身份名字/核心在人物卡,不受采样影响)。
- 被采样掉的 history chunk 的完整抽取 → **后台补全**(`_run_plaintext_background_enrichment`)。
- **不后台精修 identity**(见原则)。

### 2.5 support-only / 空输入轻量路(Codex 顶回 1:两者不能混)
- **support-only(有内容,history_count==0 但传了人物卡/档案/长期记忆)**:走**轻量身份写入**(用 support 材料直接写身份卡,不跑重 fact_map)→ **必须至少写出 identity signal**(它是有内容输入,不许"无名空过");写不出 → failed 让重试。
- **fresh_start(真空,啥都没传)**:现有 fresh_start 路,**允许无名 done**。
- 两者关键区别:**有材料 = 必须出身份;真空 = 可无名过。**

---

## Part 3 · job 结果判定(什么成 / 什么败)

**✅ done(成功):**
- 全好 / 部分 history 块跳过(降级)—— 成。
- **有内容 → 必须有 identity signal**(support-only 也是,见 §2.5)才算成。
- **fresh_start(真空)→ 0 记忆 + 无名也算成**。

**❌ failed(失败)—— 只在:**
1. **硬错误**(402/欠费/key 废)。
2. **基础设施错误**(enclave/信封/存储)。
3. **"有真内容但没凑出身份"**:输入非空(history>0 或有 support)**且** 正常 derive + 轻量兜底后**仍无 identity signal**(常伴 successful history window=0)→ **failed + "服务临时不可用,请重试"**。理由:进一个空 companion 会误导;整份重试通常就好。

> 即:**有内容凑不出身份 = 失败让重试;有内容成了一部分 = 降级完成可补;真空 = 直接过。**(判据从"记忆条数"改成"有没有 identity signal",不用绝对记忆数)

---

## Part 4 · 可观测字段(Codex 顶回 3:只算 history)

### 4.1 后端记录(本方案范围)
job / validate 带上:`history_windows_total`、`history_windows_failed`、`support_inputs_present`(bool)、`memories_created`、`degraded`(bool)。

- **`history_windows_failed` 只统计聊天记录分窗**——support 材料(人物卡/档案)不算进"聊天段落没抽全",否则用户文案会很怪(为兼容可后端仍留一个 `windows_failed` 别名,但**语义限定为 history**)。
- **时机**:`history_windows_failed` 只统计**重试全部用尽后仍失败**的块;临时抖动重试即恢复的**不计入**。

### 4.2 iOS 提示 —— **本方案不做,下一步单独做**(Codex 分步建议)
后端先落地(§1-3 + §4.1 字段 + 测试),CC review 过 → **再**做 iOS 文案层。iOS 侧(留档,下一步):
1. 降级完成 **引导文案(不加按钮)**:`history_windows_failed>0` → "有 M 段记忆没抽全,你可以到记忆花园重新上传聊天记录来补充"(指向现有 GardenMaterialSheet / add_memory,不新建 UI)。
2. 硬错误友好文案:402→"额度不足"、infra→"服务暂时不可用"、有内容全失败→"没能处理你的材料,请重试"。
3. 本地化 key(zh+en)。

---

## 验收(后端;单测 + 真机 e2e 复用 tools/genesis_e2e.py)
1. **重试**:注入临时错误(ReadTimeout/空回复)→ 重试后成功;注入 402 → 立即 failed 报额度。
2. **部分成功(降级)**:让某几块 history 持续失败 → job=done、`history_windows_failed>0`、`degraded=true`、其余记忆正常写、identity 正常。
3. **大导入采样**:27 chunk history + 人物卡 → 前台只跑 ~8 块 history、人物卡全读、身份名字正确;后台补全其余;不后台改身份。
4. **support-only(有材料无聊天史)**:history_count=0 + 有人物卡 → 走轻量路、**写出 identity signal**、不空跑重 fact_map、不失败。
5. **fresh_start(真空)**:啥都没传 → done、可无名。
6. **有内容凑不出身份**:输入非空 + derive+轻量都无 signal → job=failed + "服务临时不可用请重试"(不是空成功)。
7. **不回归**:combined 仍关(prod);身份仍吃全量;不后台精修身份;不用绝对记忆数当门槛。

## 铁律
- 派生 prompt(fact_map/fact_write/identity/voice/persona)不动。
- add_memory/update_identity 不碰相处天数。
- 硬错误(402)不重试。动加密信封 → 真机 e2e。
- **onboarding 完成与记忆成功解耦**;但**有内容必须出 identity signal**(否则和 validate 的 identity_card 冲突,见 §2.3)。
- **本方案只做后端**;iOS 文案下一步单独做(§4.2)。

## 执行流程(本次 = CC 写码)
CC 写方案(本文件)→ **CC 用 writing-plans 出计划 + 实现(TDD)** → CC 自测 → 给 diff(可选 Codex/hx review)。先只后端;iOS 下一步。
