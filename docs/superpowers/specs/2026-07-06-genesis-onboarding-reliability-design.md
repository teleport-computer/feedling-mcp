# Genesis Onboarding 可靠性 — 记忆抽取健壮化 + 部分成功 + 可观测/可补 — 设计方案

状态:CC 写方案 → 待 Codex review → 执行 → CC review。
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

## Part 1 · 错误分类(所有判定的基础)

| 类 | 例子 | 处理 |
|---|---|---|
| **临时错误** | ReadTimeout、`provider response had no usable reply text`(空回复)、429、5xx | **重试**(带退避) |
| **硬错误** | 402/欠费、key 无效、鉴权失败 | **不重试,整单 abort**(provider 全废,报"额度/密钥") |
| **基础设施错误** | enclave 拿不到 / 信封构建失败 / 存储失败 | **不重试,整单 abort**(报 infra) |

> 现状:Round3 `_complete_json_retry_empty` 只在"结果空"时重试;**ReadTimeout 是抛异常,直接冲出重试包 → mark_failed 整单死**。底层 `provider_client.reliable_chat_completion` 虽重试 timeout,但**次数/时长不够**(这批还是挂了)。本方案要把"临时错误"覆盖全、并加"跳过"兜底。

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

### 2.3 identity / greeting
- identity:临时错误 → 重试;用尽 → 兜底(可能无名 = fresh start);硬错误 → abort。**identity 无名不失败。**
- greeting:临时 → 重试;用尽 → 模板兜底(已有,永不失败)。

### 2.4 大导入前台采样(减重,只砍聊天史桶)
- `_plaintext_source_groups` 已按来源分桶:ai_persona / user_profile / memory_summary 各是小桶(各 1 块),history 是大桶。
- **只对 history 桶采样**:chunk 数超上限(如 >8)时 `_select_evenly` 到 ~8 块跑前台;**人物卡/档案/长期记忆桶全读**(身份名字/核心在人物卡,不受采样影响)。
- 被采样掉的 history chunk 的完整抽取 → **后台补全**(`_run_plaintext_background_enrichment`)。
- **不后台精修 identity**(见原则)。

### 2.5 support-only / 空输入轻量路
- 入口先判:`history_count==0`(没聊天史)→ **走轻量身份写入**(用现有 support 材料直接写身份卡,不跑分块 fact_map 那套重蒸馏)→ 秒成、不失败、不空跑 LLM。
- 完全空(fresh_start)→ 现有 fresh_start 路,直接完成。

---

## Part 3 · job 结果判定(什么成 / 什么败)

**✅ done(成功)—— 走到完成一律成:**
- 全好 / 部分块跳过(降级)/ 0 记忆 + 无名(空上传 / fresh start)—— **都算成**。

**❌ failed(失败)—— 只在:**
1. **硬错误**(402/欠费/key 废)。
2. **基础设施错误**(enclave/信封/存储)。
3. **"有真内容但成功块 = 0"**(输入非空、却因 provider 抖动全挂)→ **failed + 明确"服务临时不可用,请重试整个 onboarding"**。理由:进一个空 companion 会误导;整份重试通常就好。
   - 判据:**输入非空**(history_count>0 或有 support 材料)**且** 成功 window = 0 **且** 无 identity signal。

> 即:**有内容一条没成 = 失败让重试;有内容成了一部分 = 降级完成可补;本来没内容 = fresh start 直接过。**

---

## Part 4 · 可观测 + 可补(job + iOS)

### 4.1 后端记录
job / validate 带上:`windows_total`、`windows_failed`、`memories_created`、`degraded`(bool)。

### 4.2 iOS 提示(要改 iOS)
1. **降级完成态(面板)**:onboarding 完成页读 `windows_failed` / `memories_created` →
   - `已导入 26 条记忆（3 段未成功抽取，可重新上传补全）`
   - **「重新上传补全」按钮 → 打开现有「加记忆」(GardenMaterialSheet / add_memory)**,把没抽到的记忆补进花园。
2. **硬错误友好文案**(pollGenesisImport 失败分支,Round3 已显示 job.error,现在映射成人话):
   - 402/额度 → `AI 服务额度不足，请检查配置后重试`
   - enclave/infra → `服务暂时不可用，请稍后重试`
   - "有内容全失败" → `没能处理你上传的材料（服务临时不可用），请重试`
3. 对应本地化 key(zh + en)。

---

## 验收(真机 e2e,复用 tools/genesis_e2e.py)
1. **重试**:注入临时错误(ReadTimeout/空回复)→ 重试后成功;注入 402 → 立即 failed 报额度。
2. **部分成功**:让某几块持续失败 → job=done、`windows_failed>0`、其余记忆正常写、identity 正常、iOS 面板显示失败段数 + 可重传。
3. **大导入采样**:27 chunk history + 人物卡 → 前台只跑 ~8 块 history、人物卡全读、身份名字正确;后台补全其余;不后台改身份。
4. **support-only / 空**:history_count=0 → 走轻量路、秒成、无 LLM 空跑、不失败。
5. **有内容全失败**:输入非空 + 全部块失败 → job=failed + "服务临时不可用请重试"(不是空成功)。
6. **不回归**:combined 仍关(prod);身份仍吃全量;不后台精修身份。

## 铁律
- 派生 prompt(fact_map/fact_write/identity/voice/persona)不动。
- add_memory/update_identity 不碰相处天数。
- 硬错误(402)不重试。动加密信封 → 真机 e2e。
- **onboarding 完成与记忆/身份成功解耦**(空上传必过)。

## 执行流程
CC 写方案(本文件)→ Codex review(独立判断/顶回)→ 执行 → CC review。
