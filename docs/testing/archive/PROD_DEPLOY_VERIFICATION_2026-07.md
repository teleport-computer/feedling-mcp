# Prod 部署验证清单 — 2026-07 test→main 批次

> 本文是 2026-07 某一批 test→main 部署的实例快照（一次性产物），保留是因为
> `RELEASE_TESTING_PROTOCOL.md` §6 把它当结构模板引用。别按文中具体
> commit/用户号执行。

Status: PREPARED (部署前备好)。执行时机:zhihao 完成 prod 部署后,claude 逐项跑。
本批 test→main 合并携带 13 个实质 commit(另有更早已合并未部署的 fix ①)。

执行约定:admin API 走 `https://api.feedling.app`,X-Admin-Token 见会话内记录;
网页看板 `https://api.feedling.app/admin/data-track`(密码通道配置文档已因
2026-07-14 密码泄漏事故删除并 rotate,不再引用)。
所有时间以看板显示的北京时间为准。

## 0. 部署本身

- [ ] **记录发版时间线**：main CVM deploy 起止 UTC ___～___；runner CVM 起止
      ___～___；当时 runner 拓扑（单点/双机）___。事后"连不上"投诉先对此表。

- [ ] runner CVM image bump commit 出现在 main,prod 后端 `/v1/...` 正常响应。
- [ ] **ec55ae18 singleton deploy guard**:部署流程未被门禁误拦(zhihao 执行时反馈即可);
      若被拦,说明拓扑判定误报,找我或 codex2。

## 1. resident 会话回归批(usr_c190"来了"循环)

- [ ] **consumer 自更新**:`/v1/admin/data-track/users/usr_c190c10ecc8bb83f`,
      等她的 consumer 下一次 poll 后,consumer header 的 commit 应更新为部署 commit
      (自更新跟随 chat-poll 响应的 `expected_consumer_commit`)。
      注意:她的 consumer 可能处于停跑状态(7-16 13:27 后无回复)——若无更新,
      先确认她机器是否开着,别误判成自更新失败。
- [ ] **行为验证**(需用户侧配合或观察聊天 metadata):她再发消息后,agent 回复
      不再是"来了/在了"式报到;回复延迟从 1-3 分钟降到正常;`role=system`
      错误气泡不再增长(部署前 12 条)。
- [ ] **20 个 pending proactive job**:她 consumer 恢复后观察是否被领走
      (`proactive.jobs_by_status.pending` 下降)。注意衍生风险:堆积 job 会补跑
      (无 max-age)——若出现给用户连发过时主动消息,截图记录,作为 job max-age
      议题(挂 Seven 的 ② 频控评审)的实证。
- [ ] **stale sid 自愈**(被动项,无法主动触发):后续排查任何 resident 用户时,
      日志/trace 里的 `agent.session.stale_resume_retry` 事件即证明路径工作。

## 2. resident 假掉线修复(38657a72)

- [ ] 看板用户列表:活跃 resident 用户(参考 usr_6d8c6387242778cb、
      usr_254797947b3bddd6 等高活用户)connection 显示 **在线**,不再全员"掉线"。
- [ ] 60s 节流生效的间接验证:看板反复刷新无异常延迟(锁内单行 persist 不拖 poll)。
- [ ] 语义确认:真停跑的 consumer(如 usr_c190 若机器关着)仍显示掉线——修复
      不是把"掉线"改成永远在线。

## 3. debug ring buffer 防刷爆(939f79ce)

- [ ] `GET /v1/admin/data-track/debug?user_id=usr_6d8c6387242778cb&limit=300`:
      事件不再 100% 是 `enclave.call.start/done` + `tee_replicate:*`;能看到
      聊天轮事件(agent.model.call.* 等)。
- [ ] 顺手完成搁置项:**usr_6d8c 的 159 个错误气泡**类别分布现在可查——看
      `agent.model.call.error` 的 error_detail 聚类,产出一段结论(什么错误、
      user_provider 还是 system、要不要专项)。
- [ ] tee_replicate 的 error/timeout 事件仍会出现(若复制失败)——不出现不算失败项。

## 4. fix ① 用户轮优先(0949c514,更早合并、随本次部署生效)

- [ ] **usr_a7b0aba726648d40**(DeepSeek 用户"十几分钟才回"):部署后观察
      `last_user_at` → `last_agent_at` 间隔;用户消息在 proactive 批次进行中
      到达时应最多等一个模型轮。
- [ ] 日志侧(若可见):`deferring remaining background job(s): user message pending`
      出现即路径生效。
- [ ] proactive 未被饿死:看板 proactive 日发送量与部署前同量级(fail-open 生效)。

## 5. proactive self-wake floor / loop guard(c4d7139d/6db5aba5/f25df528,另一条线)

- [ ] usr_f13f922a9ab518ba:proactive 消息频率恢复正常,无自激励唤醒风暴
      (部署前 37 条里 20 条 proactive)。
- [ ] 普通用户 scheduled wake 仍按时触发(floor 只 clamp self_wake=true)。

## 6. admin 密码(若 zhihao 本次一并配)

- [ ] `POST /admin/login` 错密码 401、对密码 303;cookie 7 天;API key 通道仍通。
- [ ] 未配则跳过,不算失败项。

## 7. 后台任务节流三件套(a905db14,test E2E 已过,生产观察项)

- [ ] **wake 15min 过期(A)**:部署后看堆积用户(usr_5ad737403b69e749 191 个 /
      usr_fcbd9e7ab873371d 145 / usr_cec2150e50cbee15 134 / usr_c190 20)——
      他们的 consumer 一旦回来 poll,陈年 wake job 应批量转 `expired/
      stale_wake_expired` 而不是补跑;admin user detail 的 jobs_by_status
      出现 expired 计数,且不计入失败。consumer 不回来则 job 留 pending,
      不算失败项(过期发生在出票口)。
- [ ] **dream/migrate 合并(B-server)**:堆积用户回归后同类维护 job 只跑最新,
      旧的 `skipped/superseded_by_newer` 可在 admin 看到。
- [ ] **软空档(B-consumer)+ 蒸馏抢占(C)**:consumer 侧行为,test E2E 已真实
      验证(蒸馏中插聊天 → 聊天先回 → 续蒸不重跑,5 卡零重复);生产上靠
      resident 用户 consumer 自更新后生效,无需专项验证,出问题看日志
      "deferring maintenance job" / "resident distill" 行。

## 完成后

- 逐项结果表回报 Seven(✅/❌/跳过+原因)。
- 与 cohort 扫描报告(2026-07-16:275 零聊天卡 identity、24 人 job 堆积、
  16 人 dead-loop)合并成一条 Router 阶段汇总。
- usr_c190 若行为恢复,微信侧告知她结果;她 CLAUDE.md 兜底句(troubleshooting
  #13 第 2 条)仍建议保留。
