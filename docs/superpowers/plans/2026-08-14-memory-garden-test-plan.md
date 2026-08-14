# Memory Garden 内核提取 · 测试方案

> 配套分支 `feat/memory-garden-kernel`（15 个提交）。
> 本文回答：这次改动的风险面在哪、已经验了什么、**还缺什么**、上线前必须过哪些关。
> 初稿由 Claude 起草，待 Codex 评审补充。

---

## 一、改了什么 → 风险落在哪

| 改动 | 风险 | 最坏后果 |
|---|---|---|
| 9 个模块搬进 `memory_garden/`，10 个转发壳删除 | 漏改某处 import | 启动即崩（已被全量收集覆盖） |
| 39 个文件的调用方切到内核 | 拿到不同的函数版本 | 行为静默漂移 |
| prompt 三件套的 identity 依赖改为传参 | 称呼装配错位 | 卡里出现「用户」这类系统称谓 |
| 三个策略档位收拢 | 尺子被用错档 | 用户整理的 100 条只落 2 张卡 |
| **语言规则统一并接线** | prompt 文本变了 | 落卡语言分布变化 |
| dream 判据搬进内核 | 签名/幂等键算法漂移 | 部署当天全体用户误触发做梦 |
| 存储 port + 能力声明 | 只有接口未接实现 | 无（本批不切流） |

**风险最高的两处**：语言规则（真的改了 prompt）与 dream 签名（变了会波及全体用户）。

---

## 二、已经验过的（附证据）

```
① 全量单测        9 failed / 9456 passed
                  与 origin/test 基线失败集合**逐条相同**（e2b 模板 / PDF 提取 /
                  prod runner 拓扑 / 可下载文件，均与本改动无关）
                  基线是在临时 worktree 上跑同一命令得到的，不是估计值

② prompt 字节对比  5 组参数（典型/全空/中英混合/名字带空格/正文含花括号）
                  在基线与分支上产出逐字节相同
                  语言规则接线后改为「除语言段外逐字节相同」

③ Codex 独立验证   capture 2916 组 + dream 108 组参数矩阵，默认路径全部字节一致
                  12 个兼容壳无缺失公开导出；无 getattr/importlib 动态导入；依赖无环

④ dream 算法对拍   把改造前的签名与幂等键算法直译成参照实现，
                  对 6 组卡片集合与 4 组状态逐一比对，全等

⑤ genesis prompt   8 组参数（keep_all × 有无用户名 × map/write 两阶段）逐字节一致

⑥ 真模型 e2e      DeepSeek temperature=0，三场景（纯中文/纯英文/中英混合）
                  各跑改前与改后

⑦ 本地端到端      docker + local-console 起服务（独立库），验证：
                  空花园返回 no_memory_cards；插 8 张卡（6 seed + 2 dream）后
                  内核算出 seed_card_count=6；服务端 signature == 内核直接算的值

⑧ 静态检查        import asgi_app 通过（无成环）；pyflakes 干净；
                  AST 守卫确认内核不 import backend/ 下任何模块
```

---

## 三、⚠️ 还没验的（这是本文的重点）

### 缺口 1：V1 consumer（VPS 自托管）从未真跑

`tools/chat_resident_consumer.py` 改了 7 处 import，但**只有单测覆盖，没有真实
resident 跑过一轮**。这条线的特殊性：

- 它是 hosted 与 VPS 自托管**共用**的文件（历史上有过改共用文件拖垮云聊天的事故）
- 它 spawn CLI（claude/codex），行为与 V2 的结构化 tool call 不同
- 落卡走的是「agent 产出明文草稿 → consumer 封信封 → /v1/memory/actions」

**必须验**：起一个 resident，真跑一轮对话 → 触发落卡 → 确认卡入库且字段正常。

### 缺口 2：genesis 完整导入流程从未真跑

只验了 prompt 文本字节一致，**没跑过真实的 onboarding 导入**。而 genesis 有
分窗、断点续跑、幂等键、fact_map → fact_write 两阶段。

**必须验**：上传一份历史记录，跑完整导入，确认落卡数量与语言符合预期；
特别是 `keep_all`（用户整理的档案）那条路径。

### 缺口 3：加密路径没有真实验证

端到端验证时插的是 `body_ct="fake-ct"` 的假卡。本批**没有改动加密逻辑**，
但改动了读侧的调用链（`memory_readside_core` 仍在 io 侧，但 selector 搬进了内核）。

**必须验**：真实信封的写入→读取→解密全链路，确认 AAD 绑定未受影响。
仓库红线明确要求：动加密相关必须真实部署 e2e，本地 fake-decrypt 不算数。

### 缺口 4：并发写入未测

`storage.py` 定义了原子写入契约，但**现有实现未切换**（仍走
`memory/service.py` 的 `mutation_lock`）。所以本批不引入并发风险，
但契约本身没有被并发测试验证过 —— 等后续真接适配器时必须补。

### 缺口 5：语言规则的行为变化未定性

真模型 e2e 显示：中英混合场景下，改后保留了原文（`migration`）而改前会翻译
（`迁移`）。**这是行为变化，好坏需要产品判断**，目前没有判定标准，
也没有跑足够多的样本来看分布。

### 缺口 6：英文用户拿到中文卡（已存在的问题）

e2e 查出：纯英文对话时模型把卡正文写成中文，导致归一化保持中文桶。
改前改后一致，**不是本批引入**，但本批的语言规则接线**没有解决它**。
需要单独立项（让 prompt 主体随用户语言变）。

---

## 四、上线前必须过的关

按仓库 `docs/testing/TESTING.md` 的决策矩阵，本批动了
`backend/**` + `tools/chat_resident_consumer.py` + prompt 文本，所以：

### 关 1：双端等价（阻塞项）

```
□ hosted V2 worker 真跑一轮落卡 + 一轮做梦
□ VPS resident consumer 真跑一轮落卡 + 一轮做梦
□ 两端产出的卡在结构上一致（字段齐全、桶/线索格式相同）
```

理由：`chat_resident_consumer.py` 是共用文件，单测绿不代表两端等价。

### 关 2：genesis 导入（阻塞项）

```
□ 普通历史记录导入（history_import 档）：落卡数量合理、语言跟素材
□ 用户整理的档案（curated_archive 档，keep_all）：条目基本不丢
□ 断点续跑：中途中断后恢复，不重复落卡（幂等键生效）
```

理由：genesis 是本批唯一碰到 onboarding 流程的地方。

### 关 3：加密链路（阻塞项）

```
□ 真实信封写入 → 读取 → enclave 解密全链路通
□ AAD 绑定未受影响（owner_user_id|v|item_id）
□ 带查询词的精确搜索（要分页解密候选）仍正常
```

理由：读侧调用链变了（selector 搬进内核），必须确认没有破坏解密。

### 关 4：做梦触发（阻塞项）

```
□ 部署后首次 tick 不应误触发 —— 签名算法未变，已有对拍测试，
   但仍需在有真实存量卡的环境验证一次
□ 阈值判据在真实数据上生效（not_enough_new_cards / already_dreamed）
□ force 触发仍能绕过判据
```

理由：签名变了会让全体用户在部署当天误做梦，这是最高风险项。

### 关 5：语言规则的产品验收（需 hx 拍板）

```
□ 跑 20~30 组真实对话样本，统计落卡语言分布
□ 对比改前改后：中文场景是否仍全中文、混合场景保留原文是否可接受
□ 由 hx 判定「migration vs 迁移」哪个更符合产品预期
```

理由：这是本批唯一有意的行为变化，测试判不了好坏。

---

## 五、回归基线怎么建立

**关键纪律：不要用「感觉没变」当结论。** 本批用的方法可复用：

```
1. 起一个临时 worktree 指向 origin/test（--detach）
2. 在两边跑**同一条命令**（含相同的 -k 筛选与 --ignore）
3. 比对失败集合，而不只是比数字
4. 跑完删掉临时 worktree
```

并发下有几个测试是 flaky（`test_v2_optional_anchor` 与
`test_ops_dashboard_queries` 都出现过），判定方法：单独跑 + 在基线上跑同一组合。
**不能因为「看着无关」就跳过验证。**

---

## 六、Codex 评审后的修订（2026-08-14）

评审结论是「不建议合进 test」，并指出**方向对但顺序和判定强度要调整**。
已按其意见重排，并补上原稿漏掉的一整道关卡。

### 新增「关 0：同步与测试发现」—— 最先阻塞（已完成）

原稿把「回归基线」放在第五节当方法论，但漏了一个致命前提：
**分支落后当时的 origin/test 64 个提交，两边同改 7 个文件**。
所以原来那句「零新增失败」只对旧基点成立。

    ✅ 同步当前 origin/test（merge，无冲突）
    ✅ 六个新增测试进入 conftest._PURE_UNIT 与 ci.yml 显式清单
       —— 此前两处都没有，CI 的 discovery guard 会硬失败
    ✅ 全量收集数、失败集合、pyflakes、CI guard 本地模拟

**教训**：新增测试文件必须同时登记两处，否则「100 个测试通过」不代表它们
进入了长期门禁。这条在仓库里踩过不止一次。

### 关 1 加强：契约要用真实 builder/parser 证明，不能只断言字段值

原稿只验「三个对象里的数字和文案不同」。Codex 实测指出：
**传 3 张卡返回 3 张，max_cards 根本没生效** —— 策略当时只是描述数据。

    ✅ 已修：parse_capture_cards 现在消费 policy
    □ 仍需补：三条真实路径（V1 / V2 / genesis）各自证明用的是对应那把尺子
    □ dream 的离线对拍：拿一批**只读的存量元数据**，用旧实现与新实现各跑一遍，
      逐用户比较 signature / verdict / reason / idempotency key
      —— 比「跑一个真实用户的 tick」省力且覆盖面大得多

### 关 2 合并：双 lane 与真实信封一起验

原稿把「双端等价」和「加密链路」列成两道关。Codex 指出一条场景就能同时覆盖：

    hosted V2 与 VPS resident 各跑一次完整链路：
    对话 → capture → 真实加密写入 → index/fetch → enclave 解密 → dream

    判定必须看**真实副作用**，不能只看模型回复：
    □ 卡只写一次（无重复 mutation）
    □ 字段齐全
    □ owner / AAD 正确
    □ 没有额外 mutation

### 关 3 补硬标准：genesis

原稿只说「落卡数量合理」，太软。补：

    □ windows_done == windows_total
    □ 中断恢复后不重复卡
    □ foreground / background 不双写
    □ 普通历史确实过滤掉一次性事件
    □ curated 输入的条目没有无解释丢失
    □ 状态与材料计数单调、终态完整
    □ 镜像 test 部署的实际 feature flags（不能只用本地默认值）

### 关 4 加强：语言验收要分格，且断言四个字段

原稿说「跑 20~30 组样本」，Codex 指出不够硬：

    分格：capture / genesis × 中文 / 英文 / 混合 × 默认模型 / 较弱模型
    纯英文的硬断言是 **bucket / threads / summary / content 四个字段均为英文**
    （专名与直接引语例外）—— 只看 bucket 没用，因为
    normalize_bucket_language 修不了正文语言
    混合语种保留到什么程度，由 hx 拍板

### 仍然成立的三个阻塞缺口

V1 真跑、genesis 真跑、加密链路真验 —— 这三项一项都没做，
**所以现在不能合进 test**。
