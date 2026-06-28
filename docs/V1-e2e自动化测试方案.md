# V1 e2e 自动化测试方案(AI 自动 / hx 手动 两段)

> 目标:让 **AI 尽量完整地自动 e2e** 覆盖「后端正确性 + 事实不丢」;hx 只补 e2e 盖不了的(**UI + 主观质量**)。
> 0628 夜定:本文是 **CC 草案**(下列设计点是*建议*,不是定论)。Codex **先 review** —— 认同就开跑,觉得哪点不对就**改了并在报告里写明原因**(它跑过 migration e2e、更懂 harness)。不等 hx;hx 早上看结果 + 拍最终。

---

## 关键前提 / 已拍的决定

- hx 本机 resident:`/Users/hx/resident-runtime`,真 `test-api` + 真 enclave,driver=**claude**(env `AGENT_CLI_CMD`;⚠️ live 待确认,旧 log 有 codex-agent.sh)。
- **决定①(账号隔离)**:Codex 自动部分 **注册自己的 throwaway user**,用 Codex 自己的 consumer 跑 —— **完全不碰 hx 的账号/key/Garden,零污染**。(不依赖、不污染 hx resident。)
- **决定②(真实 loop vs 后端)**:确定性后端链路 = Codex 自动(Part A);真实 app 聊天体验 + UI + 主观质量 = hx 手动(Part B)。
- **断言原则**:只断言 **结构 + 事实存活 + 状态机**,不断言 LLM 措辞质量(质量 = eval,另算)。LLM 非确定性 → 用「包含 / ≥N / 状态」+ 可重试。
- **定位**:上线前 smoke,不进每-commit CI(每跑真打 LLM,花 token/慢)。

---

## Part A —— Codex 自动测(overnight,throwaway user,零污染)

逐条断言(只看结构+事实存活+状态):

> ⚠️ **硬约束(最高优先级):绝不影响正常业务代码。** 纯**新增**测试文件(harness/seed)+ 测试 env 旋钮;**backend/consumer 业务逻辑一行不动**。只在 **throwaway 账号 / 隔离环境**跑,不碰真实用户/数据/prod。**若某条断言要改业务码才测得到(如加埋点)→ 不改、降级或标 TODO**,别为测试动业务。

目标:把 **memory 场景尽量测全**(能测多少测多少)。每条只断 **结构 + 事实存活 + 状态**。

**写**
- **W1 显式稳定事实不静默丢(北极星)**:"我叫 Z""狗叫蛋子" → 有卡含该事实。
- **W2 dedup / resolve-before-create**:同一事实重复说 → 不重复建卡(桶不膨胀)。
- **W3 supersede / 纠正**:先"蛋子"后"改名球球" → 旧卡 superseded、新卡 active、内容更新。
- **W4 不该写不写**:寒暄 / 一次性闲聊 → 不落卡(W1 反例)。
- **W5 触发**:turn backstop / 安静窗口到 → capture 真 fire。
- **W6 语言**:中文对话 → 卡字段中文(无 "pets"/"travel"),专名保留。

**读**
- **R1 相关召回**:问存过的 → index 命中 → fetch → 回复含事实。
- **R2 没相关不编**:问没存过的 → 不 fetch / 说没找到。
- **R3 结构**:卡有 bucket/threads(非"未分类"占位)。
- **R4(claude 路)agent 真走 index→fetch**:flow-trace M1 埋点未做 → **本次降级为只断 index/fetch 端点 + readside 能读到**(不为此改 consumer)。

**生命周期 / 其它**
- **L1 genesis 蒸馏** → identity + memory(含已知事实、能解密、done)。
- **L2 migration** legacy→v1(`c9c5a5e`:id 稳 + 能解密 + CAS stale + done)回归。
- **L3 CAS 并发**不覆盖用户改动。
- **L4 可见性**:local_only / 敏感卡 agent 读不到。
- **L5 route** → `agent_runtime` + flow-trace 事件。

**= eval 不是 e2e(标注,不在本次)**:召回相关性质量、voice 像不像、重写顺不顺。

**API 路确认**:顺带确认 test 上 IO 托管 agent_runtime 开没开 + API(model-key)路能否纳入本次(后端共用已覆盖,只差"托管 spawn"那段);开着加一条,没开标 blocked。

**产出**:① 一条命令的报告(逐条 pass/fail/blocked + **Codex 改了哪些设计点、为什么**);② harness 提到 **分支 `feat/v1-e2e-suite`**(不直接进 test,留 hx review 后合);③ 失败/blocked 项贴断言 + 现象。

---

## Part B —— hx 手动(明早,e2e 盖不了的)

1. **真机 Garden UI**:迁移 / capture 后卡片显示对、没乱、内容在(API e2e 看不到界面)。
2. **真实 chat loop**:发条消息,回复自然、不卡(主观体感)。
3. **主观质量**:voice/人设、召回相关性、重写读起来顺不顺(= eval 的人看部分,TODO C)。
4. **review Part A 结果** + 拍下面几个确认。

---

## hx 明早 review / 拍板清单

- [ ] Part A 报告:过没过、有没有真问题。
- [ ] 确认 resident **live driver 是不是 claude**(env 写 claude,旧 log 有 codex-agent.sh)。
- [ ] 覆盖项要不要加 / 砍。
- [ ] `feat/v1-e2e-suite` review 后合 test。
- [ ] eval(主观质量)排期(TODO C)。

---

## 分工

- **CC**:断言清单 / seed 规格 + 本方案文档(已出)。
- **Codex**:建 harness + 自动跑 Part A,产出报告 + 分支。
- **hx**:跑 Part B + 拍 review 清单。
