# 发版测试方案 · 补充二（幂等/竞态/注入污染/观测自身/故障注入）

> 2026-07-18 · claude3 补。**不替代**主文档三件套（`RELEASE_TESTING_PROTOCOL.md` 框架 /
> `RELEASE_TESTING_SUPPLEMENT.md` 体感篇 / `TESTING.md` 开发循环），针对其中已点名的
> 事故族展开**可执行细则**(体感篇 S1"消息重复"、S4 部署态、S6 追问在此细化落地)。
> 来源是另一批真实事故:**重发双份**(usr_9f5d)、**一条消息两条相似回复**
> (usr_a0b7)、**记忆卡叫用户"用户"**(usr_fee1)、**中转站流断吐"."**(usr_6f5a)、
> **debug trace 写后读旧数据**(flaky test 报的警)。它们的共同点:**happy path 全绿,
> 坏在"第二次/同时/丢了响应/喂错词"上**。
>
> 每节末尾标 **【并入点】**。

---

## T1. 幂等与重试路径——"重试按钮"是一等测试对象

三个事实,全部来自 usr_9f5d 事故:

1. **"先落库、后响应"的端点,响应丢了就是重复**。`/v1/model_api/chat/send` 存完消息
   等 8s 才回 202——移动网抖一下,服务端认为成功、客户端认为失败。
2. **重试路径和首发路径是两套代码**。iOS 重发按钮走 `retryMessage`,和 `sendMessage`
   不同源——事故正是重发走错了端点(model_api 文本重发打到 resident 端点)。
3. **客户端去重防线依赖字段一致性**,一个 source 字段不匹配就整条失效。

**测试规则**:

| 规则 | 怎么测 |
|---|---|
| 每个用户可见的"重试/再试一次"按钮 = 独立 P1 用例 | 造失败(断网/停 key)→ 点重试 → 断言走的端点、payload、最终**恰好一份**数据 |
| 幂等键回归(backend 仓 cbecec05 + ios 仓 49afa7a) | 同 UUID 双 POST → 1 user row、1 个 turn;**跨端点**(hosted↔resident)同 UUID 也收敛;>600s 窗口后同 UUID = 新消息;无 UUID 老客户端行为不变 |
| "响应丢失"模拟 | E2E 发消息后**不读响应直接重发**(带同 UUID)→ 服务端 1 行;这模拟的就是 lost-202 |
| 同一用户动作的全部代码路径清单化 | 首发文字/重发文字/图片/图片重发/文件……逐条列出走哪个端点;**新路径加入时表必更**(和能力矩阵同性质的门禁) |

**当前覆盖边界(known gap,别当已有能力)**:iOS 侧幂等键目前只接了**文字首发 +
文字重试**(ios 仓 49afa7a);model_api **图片重试**会重新 sendImage、**文件**重新
sendFile——它们是新逻辑发送,lost-response 后尚不能承诺"最终恰好一份"。图片/文件带
幂等键列为 P1 目标。

**【并入点】** → 主文档 P1 增"重试路径专项"一行;§7 事故库加 usr_9f5d 行(已修:
backend 仓 cbecec05 幂等窗 + ios 仓 9f33d65 重试路由 + ios 仓 49afa7a 幂等键)。

---

## T2. 并发与竞态——"两个同时到会怎样"

三起事故同一个形状:**check-then-act 没有原子性**。

| 案例 | 竞态 | 后果 |
|---|---|---|
| usr_a0b7 双回复 | 回复排他守卫:读父消息 reply_status → append → 标 replied,两条回复同秒都过检查 | 一条消息两条相似回复 |
| proactive 撞车 | realize 前 peek"有没有用户消息在等",peek 之后消息才到 | 唤醒与聊天回合同分钟双发 |
| debug_trace 写后读 | flush worker 手里的 in-flight batch 队列里看不见 | 读到旧数据、flaky test 假红 |

**测试规则**:

1. **写路径自查话术**:凡是"同一用户的两个执行体可能同时写同一资源"(chat turn ×
   proactive × 多 worker × 多 consumer),改动前问:"两个同时到会怎样?"答不出就补
   确定性并发测试。
2. **确定性并发测试模式**(不许拿 sleep 碰运气):monkeypatch 慢路径挂 `threading.Event`
   gate → 第一个执行体阻塞在临界区 → 第二个执行体进入 → 断言它**等待/被拒**而不是
   早退/双写 → 放行 gate → 断言最终恰好一份。样板:`tests/test_debug_trace.py::
   test_flush_pending_waits_for_worker_in_flight_batch`;codex3 在批⑥用同模式做过
   确定性**复现**(先证明 bug 存在,再证明修复)——复现测试和回归测试同等重要。
3. **同分钟双事件回归**(0c5daa6b,**best-effort 缓解,非原子仲裁**):非定时、非
   introduction 的 proactive wake,与用户消息/聊天回复同窗(90s)到达 → wake 转
   `skipped/chat_collision`(admin `job_failed_reasons` 可见拦截量)。它仍是 consumer
   侧 post 前 check→post,不能笼统断言"同分钟永远只一条";introduction/
   scheduled_wake/scheduled_transparency 明确豁免。
4. **reply 侧原子 CAS 仍未完成**(截至本文):response 的 check→append→mark-replied
   竞态(usr_a0b7 案的最后一道底)是进行中的独立批次。CAS 落地前,runtime 仅有
   poll claim CAS(只保证**领取**排他)与 post 端 already_answered best-effort
   guard(非原子);本节 1/2 是工程与测试纪律,3 只缓解 proactive 撞车——**没有
   任何一道能证明 response post 原子排他**,文档读者别把"缓解"当"已修"。
5. **部署重叠窗**:runner CVM 原地更新时新老 consumer 短暂共存——凡 at-least-once
   投递,消费端会话级去重(`_mark_seen`)跨进程无效,防线必须在**服务端持久层**
   (claim CAS / reply 排他 / 幂等键),测试也要打在服务端层。

**【并入点】** → 主文档 §7 加 usr_a0b7 行;`TESTING.md` §2 矩阵 F 行"额外必做"加
"并发写自查 + 确定性并发测试"。

---

## T3. Flaky test 处理规范——先当真 bug,禁止静默重试

批⑥的教训值得成文:`test_memory_capture_trace` 被当"顺序依赖 fixture 问题"挂了几天,
实际是 **0a7fb3d2**(2026-07-05 trace 写异步化)引入的真实读写竞态——后续全量 suite
在 939f79ce 附近把这个既有竞态**暴露**了出来。测试在忠实报警:生产 admin 读同样会
拿到旧数据(修复 e4b38e39)。

**规则**:
1. flaky 出现 → **先按真 bug 立案排查**,拿到"确属测试自身问题"的证据(如:干净 HEAD
   复现路径分析)才允许改测试;
2. **禁止** `@pytest.mark.flaky` / retry / skip 静默掩盖——要么修产品竞态,要么修
   fixture 并写明根因;
3. 排查起手式:单跑 vs 全量跑差异、干净 HEAD 树(`git archive` 导出跑,别 stash 共享区)、
   进程内全局状态(线程/缓存/单例)清单。

**【并入点】** → `TESTING.md` §6 通用坑加此条;主文档 §7 事故库加 debug_trace 行
(e4b38e39)。

---

## T4. 注入文本污染——喂给模型的每个词都会出现在用户屏幕上

usr_fee1 事故的一般化:模型是复读机,**转写标签、prompt 术语、硬编码兜底文案都会被
照搬进用户可见产物**。"记忆卡叫她'用户'"的根因不是模型不听话,是转写行首写死了
`user:`。同族还有:占位"名字"(用户/user/TA)存进身份卡后反向污染、产品术语双语义
(产品面 TA=AI,prompt 内部 TA=用户本人)。

**测试规则**:

| 规则 | 怎么测 |
|---|---|
| 注入文本审计 | 新增/修改任何喂给模型的注入段(转写、identity 上下文、记忆、OCR),grep 一遍**禁词表**:`user:` `agent:` `用户` 裸 `TA`(指人)——出现即需论证 |
| 产物断言 | 记忆卡/主动消息等**用户可见的模型产物**,E2E 断言不含系统称谓(落卡 E2E:卡正文无"用户") |
| 占位值防呆(写读双端) | "名字"类字段:写入端拒绝占位词(card_policy 守卫,name-writeback 项),读取端 sanitize(`sanitize_user_name`,单一保留词事实源);**逐候选字段** sanitize,占位名不得遮蔽后备真名 |
| 术语双语义清单 | 用户可见文案里的保留词(TA=AI)维护一张小表;prompt 内部借用同词必须显式声明"仅指令内标记"并禁止进产物(8c7d818b 的写法) |
| 硬编码兜底文案全量 grep | 改称谓/术语时,`grep -rn` 全仓(backend/enclave/consumer/iOS xcstrings)——usr_fee1 案第一轮漏了 enclave readside 的 legacy 路径,codex3 实调才抓到 |

**【并入点】** → 主文档新增一行事故库(usr_fee1)。backend 仓 SHA 是**净效果组合**:
8c7d818b(称呼修复本体)曾误夹带并行 LTM hunk,dc9caf0a 精确撤回——cherry-pick
场景两者必须成对移动,只拿前者会把未审 hunk 重新带上。
`TESTING.md` §2 矩阵加一类"**N. Prompt/注入文本**":L1 必做 prompt 断言测试 +
禁词 grep,碰产物的必做落卡/dream E2E 产物断言。

---

## T5. 观测系统自身也是产品——排障工具坏了 = 盲飞

两起实证:①prod admin debug 视图旧实现**全用户扫描 2N 串行 SQL**慢到不可用;事故
当天单用户过滤查询实测也 >400s 超时(叠加 worker 拥塞),修复(get_blobs_for_users
批量)后实测 2.5s——排障时工具不可用;②trace ring 200 事件被 enclave 噪声稀释,
活跃用户只能回看 ~1 小时,usr_a0b7 的事故窗口直接滚没了,上游扳机至今没实锤。

**测试/预算规则**:

1. **admin 关键读性能预算**:单用户 detail < 10s、单用户 debug trace < 5s(实测
   2.5s)、users 列表 < 30s——L3 固定项各打一次表并记录数值,**劣化趋势也要报**,
   不只 pass/fail;
2. **写后读一致性(bounded best-effort,e4b38e39 的真实契约)**:read 前的 flush
   等待保证 worker 手中的 in-flight batch 不会被误判为"队列空"而读旧——在
   0.5s deadline 内、DB 正常时写后可见;DB 卡过 deadline 仍可能读旧,这是设计内
   的 best-effort,别写成无条件强一致断言;
3. **事故窗口保留量**:ring 噪声比(enclave 事件占比)纳入观察——若 >90% 说明信噪比
   退化,该调 verbose 分级或扩 ring;
4. **排障 SOP 一条**:用户投诉要"趁热"拉 trace(活跃用户 ~1h 窗);超窗的教训写进
   `incident-diagnosis-lessons`(消息重复两族分诊法已在)。

**【并入点】** → 主文档 §6(L3)固定项加"admin 三读性能打表";§7 加 debug 慢查询行
(已修:get_blobs_for_users)。

---

## T6. 发版窗口自身是事故源——部署时间线进发版记录

usr_ed21"总是连不上"= 单点 CVM 原地部署窗;usr_a0b7 排查时也第一时间对了当天 deploy
时间线(排除后才转向撞车假说)。**"先查当天有没有 deploy"已经是分诊第一步**,那发版侧
就要把时间线留好:

1. **发版记录必含**:deploy 起止 UTC 时间(main CVM / runner CVM 分开记)、当时 runner
   拓扑(单点 or 双机)——事后任何"连不上"投诉先对这张表。±分钟级吻合 = 标记
   **deploy-window candidate**,仍需核对 health/恢复时间线后才能归因收案并保留记录;
   **不许拿时间相关性自动驳回**——真回归可能恰好撞上部署窗;
2. **发版前拓扑检查**:runner 单点时(现状,`PROD_RUNNER_TOPOLOGY_ENFORCE` 未武装),
   发版=主动制造 503 窗——选低峰时段,且发版通告里预留"几分钟内连不上属预期";
3. **部署重叠行为**(接 T2.5):runner 原地更新的新老共存窗,是双 turn 一类竞态的
   高发期——L3 验证抓部署后 30 分钟的两个信号:`chat_collision`(admin proactive
   `job_failed_reasons` 有现成聚合)与 `already_answered` 409(目前只在 per-user
   trace ring 的 `chat.response.gated` 事件里,**无稳定聚合指标,手工抽 trace,
   仪表待补**);异常升高是部署重叠竞态的**强候选信号**,需结合部署时间线、流量
   基线与具体 trace 才能归因(与本节第 1 点同一原则)。

**【并入点】** → 主文档 §6 加"发版时间线记录"固定项;`PROD_DEPLOY_VERIFICATION`
模板头部加 deploy 起止时间栏。

---

## T7. 故障注入——中转站的坏,要主动造出来测

key 池的"中转站代表"只能测到**当天恰好发生**的坏;usr_6f5a/usr_9f5d 展示的真实故障族
远比 happy path 丰富:SSE 流中途掐断(零 token/一 token 后断)、假模型名(挂"Claude"
实际转 Gemini,tool call 400 proto error)、慢首 token、间歇 5xx。

**建议(需 Seven 拍板投入)**:`tools/e2e/` 加一个 **mock relay**(几十行 openai_compatible
代理,按环境变量注入故障模式),P1 加一节"故障注入四连":

| 注入 | 断言(用户视角) |
|---|---|
| 流断在首 token 前 | 先**原地单次 retry**(385f636c):retry 成功 → 无错误气泡,用户无感;retry 再失败 → 气泡归因 `upstream_unavailable`(怪 relay 不怪我们) |
| 流断在首 token 后 | **不出现"."之类退化气泡**(degenerate guard);proactive 轮按失败记账不发 |
| tool call 400(Gemini proto 族) | 降级不带工具重试或明确报错,不无限循环 |
| 慢(>60s 首 token) | 打字指示不消失(5min 窗),不误标发送失败 |

归因正确性单独强调:**错误气泡的"怪谁"必须对**——上游的错说"你的模型服务暂时不可用",
我们的错才说"连接模型服务时出了问题"。归因错 = 用户来找我们修 relay 的问题(usr_6f5a
就是被修复前误归因的"连接模型服务时出了问题"引导来的)。

**【并入点】** → 主文档 §1.2 key 池表后加 mock relay 一行;P1 加"故障注入四连"
(待 mock relay 落地,前置项列入 §9 落地顺序)。

---

## T8. 双签作为测试手段——数据与最小动作清单

2026-07-17 一天六批的实证:独立 gatekeep 抓出 **10+ 个测试没抓到的真缺陷**(idle-streak
记账反转、保留词遮蔽真名、identity 文本泄漏、TA 双语义误伤旧卡、readside 漏网兜底、
空窗口回归、ack-on-pop 早退、reader/worker RMW 覆盖、UUID handoff 被洗、误夹带 hunk)。
**审查是拦截率最高的一道测试**,值得规范化:

**什么必须双签**(现行实践成文):用户可见行为变更、共享接缝(补充篇 S3 表)、并发/
存储原语、加密/账号链路、prompt 注入文本。单签豁免仅限**不改变契约/流程/运维门禁的
说明性 docs 与注释**——改变发版规范、测试义务、API 契约的文档(包括本文件)照样双签。

**gatekeep 最小动作清单**(审者):
```
[ ] 读完整 diff(不是 summary);对照声明的设计逐 hunk 问"为什么"
[ ] 独立跑测试(别信提交者的数字);有 PG 依赖的用 real PG
[ ] 边界三问:空值/保留值?两个同时到?失败路径 fail-open 还是 fail-closed,和承诺一致吗?
[ ] 用户可见文字:术语/归因/双语义对吗?
[ ] 远端复验:push 后 fetch 对 SHA、文件集、净 diff(cherry-pick 场景防夹带/防丢对)
```

**提交纪律**(提交者,共享工作区血泪):commit 前 `git diff --cached` 与**审定 diff
逐 hunk 比对**——文件名集合一致不够(批④误夹带 LTM hunk 就是只对了文件名);进行中
工作放专属 worktree;工具脚本按用途选目录:`scripts/agent-mailbox/*` 从 canonical
主仓根跑(worktree 里跑会投错信箱),**pytest/build/lint 必须在被审的 worktree/commit
里跑**(在主仓跑等于审了别的树)。

**【并入点】** → 主文档新增"§2.5 双签范围与 gatekeep 清单";`TESTING.md` §7 DoD 加
"双签范围内的改动:有独立 gatekeep 记录"。

---

## T9. 客户端状态机——iOS 没有单测,就把走查清单当测试

本周三个 iOS bug(重发走错端点、去重被 source 击穿、UUID 被 handoff 洗掉)全部落在
**消息发送状态机**上,而 iOS 无单测 target(codex3 已确认),纯靠 review + build。
在有单测 target 之前,把状态机走查清单固化为 L1 义务:

**发送状态机走查表**(动了 ChatViewModel 发送/重试/合并逻辑必查):

| 转换 | 查什么 |
|---|---|
| sending → sent(响应正常) | 乐观气泡与服务端副本合并,本地字段(quotedMemory/imageData/clientMsgID)不丢 |
| sending → failed(超时/断网) | 气泡可重试;**服务端可能已收到**——**text**:clientMsgID 必须还在(重试幂等);**image/file**:当前只要求本地 payload 可重试,UUID 幂等是 P1 目标(T1 known gap),未完成前不得声称 lost-response 恰好一份 |
| failed → 重试 → sent | 走对端点(route-aware);**text** 复用同 UUID;不产生第二个气泡 |
| sync/poll 合并 | 服务端副本与本地失败副本按内容+source 等价对收敛;不同 source 的历史对({model_api,chat})也收敛 |
| 相邻双消息 | 用户真心连发两条相同文本(>15s 或不同内容)**不被误合并** |

加一条结构性建议(排期由 Seven 定):**iOS 建单测 target**。路线:先提取/开放两个
合并 helper(choosePreferredCopy / isLikelyOptimisticDuplicate,纯函数但当前
private),再把 retryMessage 的**路由决策**抽成可注入的纯 helper 单测(retryMessage
本体会改状态、发网络请求,不是纯函数);网络状态机后续用 fake API 另测。这三处
恰好是三个 bug 的宿主。

**【并入点】** → `TESTING.md` §2 矩阵 J 行(iOS)加"发送状态机走查表";主文档 §9
落地顺序加"iOS 单测 target(最小核:消息状态机)"。

---

## T10. 一页纸:我发版前会追加问的 5 个问题(接补充篇 S6)

7. **第二次**:每条数据写入,重发/重试一遍还是一份吗?(T1)
8. **同时**:两个执行体同时到,防线在服务端持久层吗?(T2)
9. **喂词**:这次改动往模型嘴里塞了什么新词?会被照搬到用户屏幕上吗?(T4)
10. **工具**:如果现在出事故,admin 读得动、trace 还在窗口内吗?(T5)
11. **归因**:新增的错误路径,气泡怪对人了吗?(T7)
