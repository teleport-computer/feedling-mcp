# Memory Garden 内核提取 · 技术方案(初版)

> 配套《Memory Garden 内核提取 · 方向》。那份讲为什么和边界,本文讲**具体怎么做**:
> 现状盘点、内核暴露什么、现有代码搬到哪、分几批、怎么验收。
>
> 代码基线 `origin/test@2c4d5377`。初版,待评审。

---

## 一、现状盘点:不是从零开始

**origin/test 上已经有 627 行是内核形态的模块**,团队已经在这条路上走过一段:

| 文件 | 行数 | 是什么 | 状态 |
|---|---|---|---|
| `memory/card_text.py` | 368 | 卡片字段内容校验(占位符/模板抄回检测) | ✅ 纯函数零 IO,V1/V2 已共用 |
| `memory/card_guard.py` | 129 | 模型原始输出泄漏检测 | ✅ 纯函数零 IO |
| `memory/dream_gates.py` | 97 | 做梦出口硬闸(卡 id 泄漏、爆炸半径) | ✅ 纯函数零 IO,V1/V2 已共用 |
| `memory/source_policy.py` | 33 | 来源与 capture mode 枚举 | ✅ 纯常量 |

这四个模块的注释里反复出现同一句话——「**纯函数、零 I/O,以便 resident 与 V2 两条
运行时共用一份判据**」。**本批要做的就是把这个已经被验证的做法推广到剩下的部分。**

而且这些模块都是事故驱动产生的(占位符卡、协议泄漏卡、墓碑卡),说明这条路不是设计洁癖,
是踩出来的。

### 没共用的那部分,代价在持续付

最近一例(2026-08-10):

> `fix(memory): give V2's hand-written card path the naming and bucket rules`
> —— 命名和归桶规则没共用,V2 那条路径漏了,事后单独补

同期还有 `feat(voice): 通话全文归档 + 记忆走主动记忆(删摘要与平行管线)`(08-07),
说明消除平行管线这件事团队已经在做。

### ⚠️ 排期:这块每天在动

近 7 天 `backend/memory/` 有 8 次提交,最近一次 08-11。**大重构会持续跟在途改动撞车**,
批次怎么切、什么时候切,得跟手上正在改这块的人一起定,不能闷头开工。

### 还没纯化的部分

| 文件 | 行数 | 问题 |
|---|---|---|
| `memory/actions.py` | 1167 | 动作执行 + 信封封装 + 身份联动 + gates + 锁 + 审计,全交织 |
| `memory/memory_core.py` | 545 | 路由体逻辑,import db / accounts / gates / identity |
| `memory/service.py` | 327 | 存储与锁(`mutation_lock` 跨进程 fence + 全量替换) |
| `memory/capture_prompt_v1.py` | 290 | prompt 逻辑纯,但 import `identity.user_naming` |
| `memory/dream_prompt_v1.py` | 244 | 同上 |
| `context_memory_selection.py` | 525 | ✅ 已是零依赖纯打分,可直接搬 |
| `memory_index_selector.py` | 291 | ✅ 只依赖上一个,可直接搬 |
| `memory_readside_core.py` | 381 | 走 httpx 调 enclave,属于适配器侧 |
| `genesis/prompts.py` | 364 | 含落卡 prompt + voice/persona/identity,要拆 |
| `proactive/dream_scheduler.py` | 397 | 时机判据(IO)与必要性判据(Garden)混在一起 |

---

## 二、内核暴露什么

四个能力,全是纯函数——进什么出什么,不查库、不调模型、不碰网络。

```
① needs_dream(快照, 上次的账本) → (要 / 不要, 理由)
     快照:可用卡数、种子卡数、签名
     账本:上次的种子卡数、上次签名、上次完成时间
     ★ 只用明文元数据,不解密

② select(候选卡的明文元数据, 查询?) → 该选哪几张
     打分:重要度 × 时间衰减 × 最近被想起
     ★ 带查询词的内容匹配不在这里,那要解密,属于适配器侧

③ build_prompt(对话, 现有卡, 身份参数, 策略档位) → 一段 prompt
     策略档位决定用哪把尺子(见第五节)
     ★ 身份参数由调用方传入,内核不 import identity

④ parse_and_plan(模型输出的 JSON, 现有卡) → 一批该执行的 mutation
     解析 → 字段校验(card_text/card_guard) → 去重 → 算出 add/merge/supersede
     → 出口硬闸(dream_gates)
     ★ 校验和硬闸这两步已经有现成实现,直接并入
```

**内核不提供的**:加解密、身份装配、所有权校验、gates、审计、锁、事务、
捞聊天记录、定时器。

---

## 三、存储接口(port)

内核只对着一个接口说话,实现由调用方注入。

### 读侧

```
load(tenant, 过滤条件) → 一批卡的信封
   ★ 只返回明文元数据 + 密文原样,不解密
   ★ 解密是适配器在内核挑完候选之后另做的一步
```

### 写侧:必须是原子单位

```
apply(一批 mutation, tenant, 幂等键, 期望版本) → 每个动作的结果
```

**不能拆成 save / update / mark 三个独立调用。** 现有代码用跨进程 advisory fence
包住整个 load→mutate→save(`memory/service.py` 的 `mutation_lock`),底层是全量替换。
拆开会导致:

    · 两张 active 卡同时存在(合并写了新卡,旧卡没标记成功)
    · 记忆凭空消失(标记了旧卡,新卡没写成)
    · 丢掉别人刚写的卡(全量替换基于过期快照)

并发是真实的:V1 consumer、V2 worker、genesis、做梦可能同时写同一个用户。

### 适配器要声明自己支持什么

存储后端不一定是数据库,也可能是另一个记忆系统(mem0 / engram / 用户自己的库)。
对方不一定支持我们的全部操作,所以接口留一个能力声明:

```
supports_supersede        能不能「标记为被取代」而不是删掉
supports_atomic_batch     能不能把一批 mutation 作为一个原子单位
supports_custom_fields    能不能原样保留桶 / 线索这些自定义字段
supports_metadata_sort    能不能按元数据排序(否则要全量拉回来本地排)
```

内核遇到不支持的能力就降级:

```
不支持 supersede       → 退化成直接覆盖内容
不支持原子批量          → 退化成逐条写
不支持自定义字段        → 桶/线索塞进 metadata 或正文
```

⚠️ **降级必须显式上报到调用方,不能静默**。静默降级会让用户以为功能都在,
实际记忆库在悄悄变乱。

IO 的 Postgres 适配器声明支持全部能力,**所以 IO 侧行为不受这个机制影响**;
降级只发生在外部适配器上。

**为什么现在就要留**:接口一旦写成「所有适配器必须支持 supersede」,
外部记忆库就永远接不上,将来要把所有适配器返工。现在多一层声明,成本几乎为零。

### 加解密:适配器内部的事,但不是透明变换

```
AAD 绑定 owner_user_id | v | item_id  —— 改一个字 enclave 就拒绝解
解密要 POST 到 enclave,且必须带 api_key 或 scoped runtime token
带查询词的搜索:条件在密文正文里,需要分页解密候选
```

所以适配器接口要能表达:调用凭据、pre-sealed 与明文两种写入形态、
无法解密 / 部分批次失败、批量分页与超时、key rotation。

**内核不知道这些存在**,它只见明文。

---

## 四、搬迁映射

```
已经就位,直接并入内核
   memory/card_text.py       → core/validation
   memory/card_guard.py      → core/validation
   memory/dream_gates.py     → core/guards
   memory/source_policy.py   → core/types

零依赖,直接搬
   context_memory_selection.py  → core/scoring
   memory_index_selector.py     → core/scoring

拆一刀:prompt 逻辑进内核,身份依赖改成传参
   memory/capture_prompt_v1.py  → core/prompts + policies
   memory/dream_prompt_v1.py    → core/prompts
   memory/migrate_prompt_v1.py  → core/prompts
   memory/prompts_v1.py         → core/prompts(桶指引)
   ⚠️ 三份都 import identity.user_naming,必须改成由调用方传 naming rule

拆两半:必要性判据进内核,时机判据留 IO
   proactive/dream_scheduler.py
      → core:  只数种子卡这条规则、攒够多少算够、签名怎么算
      → 留 IO: 夜间窗口、上次跑完没、失败退避、用户开关、入队

大手术:domain 判断与 IO 应用分离
   memory/actions.py (1167)
      → core:  类型/anchor 规则、去重判断、动作计划
      → 适配器: 信封构建、锁、写入
      → IO 应用层: 身份关系参数装配、ownership、gates、审计、effects

留在 IO,不进包
   memory/service.py        → 适配器(锁与全量替换的实现)
   memory_readside_core.py  → 适配器(enclave 调用)
   memory/memory_core.py    → IO 应用层(路由体)
   memory/routes_asgi.py    → IO(HTTP 层)

genesis 只交出落卡那部分
   genesis/prompts.py → 落卡 prompt 片段进内核;
                        voice / persona / identity 留在 genesis
   genesis/dedup.py   → 去重原语进内核,分窗/checkpoint 逻辑留 genesis
```

---

## 五、策略档位:待确认的一处

方向文档假设有三个档位(日常聊天 / 历史导入 / 用户整理的档案)。
但 `source_policy.py` 里实际列了 **16 种 source**:

```
bootstrap · chat · genesis_import · genesis_resident_distill · history_import ·
hosted_runtime_state · live_conversation · memory_capture · memory_dream ·
memory_migrate · model_api_capture · model_api_correction · model_api_repair ·
ombre_brain_sync · resident_absorb · resident_patch
```

**source(这张卡从哪来)和 policy(用哪把尺子判断值不值得记)不是一回事**,
多个 source 可能共用一个档位。但「到底该有几个档位、怎么映射」必须先盘清楚,
否则内核的策略参数会漏掉真实来源。

> ⚠️ `ombre_brain_sync` 这个值来历不明。它只出现在一个 commit 里
> (`fix(memory): preserve production source provenance`,2026-07-30),
> 涉及的全是枚举、OpenAPI 和契约测试,**代码里没有任何同步逻辑**。
> 更可能是「生产库里已存在这个来源的卡,补进白名单免得校验失败」,
> 而不是正在进行的对接。但来历要问清楚——如果真有对接,本批边界会扩大。

**这是开工前必须回答的第一个问题。**

---

## 六、分批

每批独立验收、可回滚。

```
批 0  盘清来源与档位映射(第五节)+ 在本地环境造 golden fixtures
      落卡、做梦、历史导入、用户档案、迁移、pre-sealed envelope 各一组
      ★ 用本地调试环境(devtools/local-console)起服务生成,不从生产捞
        —— 省掉解密和脱敏,成本比原估的低一个量级
      ★ 验收要的是「同样输入,新旧代码输出一致」,样本不需要来自生产
      ★ 后续每批的验收都靠它,没有它无从验起

批 1  把已经纯化的四个模块 + 两个零依赖打分模块收进包
      ★ 调用路径不变,行为应逐字节不变。这批风险最低,用来跑通打包与注入机制

批 2  prompt 三件套进包,identity 依赖改成传参
      ★ 验收:三条线产出的卡与 fixtures 逐条对照一致

批 3  存储 port + Postgres 适配器(包住现有锁与信封逻辑)
      ★ 同批做一个内存适配器,验证 port 抽象是真的通
      ★ SQLite 不进主路

批 4  actions.py 拆分(domain / 适配器 / IO 应用层)
      ★ 本批最重,也是唯一动写入路径的一批

批 5  切 V2:read/tool → capture → dream,分三次
批 6  切 V1 resident
      ★ chat_resident_consumer.py 同时服务 hosted 和自托管,不能只靠单测宣布等价
批 7  切 genesis:onboarding / add_memory / keep_all / resident recheck 分别切
批 8  dream_scheduler 拆两半
批 9  CLI 与 MCP 两层壳(为开源准备,不以开源为本批验收目标)
```

---

## 七、验收

```
① 卡结构、归桶命名、解析校验、去重原语、写入语义的实现只有一份
   —— 但各策略档位有各自的 profile 与契约测试,不要求产出一致

② 整个发布包不 import 任何 IO 模块:
   db / identity / accounts / bootstrap / enclave / core.store

③ 所有 mutation 走同一套原子契约。并发测试不出现:
   两张 active 卡、记忆丢失、基于过期快照覆盖

④ 换存储实现(Postgres → 内存)不改内核一行

⑤ golden fixtures 在每批前后逐条对照通过

⑥ 真实模型 E2E:prompt 行为类 bug 单测抓不到,必须在目标 runtime 上真跑
```

第 ② 条最容易被破坏——一旦包里出现一个 `import identity.user_naming`,后面全塌。

---

## 八、风险

```
🔴 actions.py 拆分(批 4)
   1167 行里 domain 与 IO 交织,是全案唯一动写入路径的改动。
   若拆不干净,内核会被迫留一个「知道信封长什么样」的口子。

🔴 V1 / hosted 共用文件(批 6)
   chat_resident_consumer.py 同时服务 hosted 和 VPS 自托管。
   历史上有过「改共享文件拖垮云聊天」的事故,本批必须双端都验。

🔴 与在途改动撞车(全程)
   近 7 天 backend/memory/ 有 8 次提交,最近一次 08-11。
   大重构会持续跟这些改动冲突,批次切法要跟在途工作一起排。

🟠 ombre_brain_sync 来历不明(第五节)
   枚举里有这个来源但没有对应代码,需确认是历史遗留还是别处的对接。
```

---

## 九、开工前要答的三个问题

1. **策略档位怎么映射 16 种 source。**(第五节)
   —— 不答清楚,内核的策略参数就是猜的,会漏掉真实来源。

2. **`ombre_brain_sync` 是历史遗留还是真有对接、归谁。**
   —— 如果真有,本批边界要扩大。

3. **在途改动的冲突面有多大。**
   —— 这块每天在动,手上正在改的东西和本批的重叠范围,决定批次怎么切、何时切。

> golden fixtures 原本列为第四个问题(从生产捞、含加密数据、成本未估),
> **现已降级**:本地调试环境可以起服务直接生成,省掉解密与脱敏。
> 验收要的是「同样输入新旧输出一致」,样本不必来自生产。
