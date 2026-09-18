---
document_lifecycle: current
canonical_owner: self
---
# T547 独立 TCP 连接观测

## 采样与读数

`.github/workflows/tcp-connect-monitor.yml` 在 GitHub `ubuntu-24.04` runner
每小时第 7、22、37、52 分钟采样（UTC）。PR 与 test/main push 仅当 workflow
或 helper 改动时跑 canary；另支持 `workflow_dispatch`。
`.github/workflows/tcp_connect_probe.py` 只依赖 Python 标准库及支持 `%{json}`
的 curl（≥7.70）。不使用 secrets，不读取响应正文，不改服务或用户数据。

每轮对 `api.feedling.app/healthz`、`test-api.feedling.app/healthz`、
`test-enclave.feedling.app/healthz` 及控制组 `cloudflare.com/` 各发 10 次 HTTPS GET。
四主机交错采样；每次启动独立 curl，禁代理、`.curlrc`、重试与重定向，使用
HTTP/1.1，保留系统证书校验。连接阶段超时 5 秒（curl 此参数也包括 DNS/TLS），
整体超时 8 秒，子进程保护超时 10 秒。每组之间间隔 1 秒。

**主指标 probe failure = `time_connect == 0 OR curl_exit != 0`**。
每主机 ≥2/10 在 summary 标 `🔴 RED`，脚本退出 1。
0/10 或 1/10 仅标低于阈值，不代表所有请求健康。
原始每次 `http_code`、`time_connect`、curl exit、stderr、UTC 起止、
DNS/TLS/总耗时、远端 IP/port 均留档。summary 分列：

- `tcp_not_established`：没有记录到 TCP 完成，**包括 DNS 失败**，不能一律称对端拒连。
- `connect_errors`：curl exit 7，包括拒连；不把该退出码独自等同 ECONNREFUSED，详见 stderr。
- `timeouts`：exit 28；结合 `time_connect` 和 `failure_stage` 分辨 TCP 前后。
- `post_tcp_errors`：已 TCP 完成后失败，可能是 TLS、响应超时或其他传输错误。
- HTTP code 计数：HTTP 403/503 证明收到 HTTP 应答；curl 不带 `--fail`，它们不增加本次连接失败分子。
- `unmeasured`：curl 缺失、子进程保护超时、指标损坏等不是成功；分母仅含有效测量。
  有此项或少于 10 条时不会标绿，脚本退出 2；已有 ≥2 个实测失败仍标红且同时显示未知数。

每次运行以 `tcp-connect-<run_id>-<run_attempt>` artifact 保留 30 天：
`samples.jsonl` 每条立即 flush；`metadata.json` 在采样前写出；完成后生成
`summary.json` 与 `summary.md` 并写入 job summary。采样失败仍 `always()` 上传，
无文件上传报错。runner 被强制终止时可能只有部分原始数据，必须记录缺口。

字段语义依据 [curl 官方手册](https://curl.se/docs/manpage.html)：
`time_connect` 包括从开始到 TCP 完成的耗时（含 DNS），不是纯 TCP RTT。
它证明到所解析地址的路径情况，不能证明 CVM、入口、运营商各自的责任。

## 启用、首周对照与交付

**合入 test 不会启动 schedule，也不能宣称开始收集首周数据。**
[GitHub 官方调度规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
规定 schedule 仅默认分支运行，忙时可能延迟或丢弃；所以用实际运行记录做分母。
第一轮审阅看本地真实 curl 的 HTTP 成功/拒连/连接超时三类已知答案测试。
交叉审签后才 commit/push，PR canary 的 run URL、summary 和 artifact 必须在
`needs_merge` 前补齐。新 workflow 不在默认分支时，不能先承诺
`gh workflow run --ref test` 可用；失败应原样留证，PR trigger 是验证通路。
test 合并由 Supervisor 执行；main 晋级/生产部署须 Seven 经归口授权。

启用记录应同时列 main 合入 SHA/时刻、部署时刻、第一条成功产生测量的
**schedule run 实际 UTC 起点 T0**，三者不混。正式首周窗口 `[T0, T0+7天)`，
不是提交后的第七天；T547 在完整周报完成前保持未收口。
PR/push/manual canary 与 schedule 数据分开，不能凑入周报。

满周回收步骤（claude3 归口，codex3 执行统计）：

1. 用 `gh api --paginate repos/teleport-computer/feedling-mcp/actions/workflows/tcp-connect-monitor.yml/runs?event=schedule&per_page=100`
   保存运行清单，按 UTC 窗口筛选；同时记录取消、失败、延迟和没有 artifact 的 run。
   不只下载成功 jobs，因为红样本会让 job 失败。
2. 对各 run 用 `gh run download <run_id> --name tcp-connect-<run_id>-<run_attempt> --dir <run_id>-<run_attempt>`
   下载。以 `(run_id, run_attempt, url, sample)` 去重；重跑单列，不把两个 attempt
   当两个定时槽。原始记录与 summary 对拍，每主机应有 10 条，缺失单独报告。
3. 按主机、UTC 时段、远端 IP 列实际轮数/样本数、失败数/有效分母、未测量数、
   exit 7/28、DNS/TLS/HTTP 分布、≥2/10 的轮数、四主机同期重叠。
   与理论 672 个计划槽的差额只表示覆盖缺口，绝不补零算成功。
4. 历史参照是台账 T547 记载的 2026-09-10 **本机无代理各 10 次**：
   api 1/10、test-api 2/10、test-enclave 1/10、Cloudflare 0/10。
   这是历史小样本，原始 exit/精确时间/Cloudflare 完整 URL 未附，不能当作同窗实测。
   满周时再从本机用同一 helper 无代理采样，记录版本与 UTC，与 runner 对齐比较。
5. 所有线上结论按 fleet 证据要求记录认证方式（本探针为匿名公开 GET，无 key）、
   去值命令与原始读数。结果可以是“证据不足”：runner 全绿只能说明此窗口未复现；
   两端都只在目标域异常支持共享路径/入口问题，不能直接锁定 CVM；四域同时异常
   支持观测点出口/共享网络故障，需要其他证据。首周数据尚不存在时不得预写归因结论。

验证：`tests/test_tcp_connect_monitor.py` 覆盖 0/1/2/10 阈值、DNS/TLS/HTTP
边界、未测量、artifact 原始值、workflow 触发/留存及真实 curl 本机对照；显式接入 CI。
本次只增加运维观测，不改变 public API、运行时/部署拓扑或信任边界，无需改 public OpenAPI。

## CVM 对端可达性：设计评估（未实现，待 Seven 判断）

**观测位置。** 倾向独立定时 tick，把带 UTC/有效期的结果保存在诊断缓存，
必要时另一个诊断接口只读缓存；不在 `/healthz` 请求期间发起同步互探。
约束锚为 `backend/enclave/routes/health.py:16` 的 `_health_body`，其中第 19–22 行
明确 healthz 不做 crypto/后端 round-trip，只读启动状态；
`backend/enclave/routes/decrypt_selfcheck.py::decrypt_selfcheck` 已有经 runtime token
认证的 enclave→backend `/healthz` 单向自检，任何 HTTP 应答都算可达。
若要补，应先核已有自检是否足够；需区分 CVM 内网服务地址与公网域名出口，
内网可达不能证明外部 runner 到 ingress 可达，不应重复把同一条路径当独立证据。

**副作用。** 同步 backend↔enclave 互探会把对端慢变成本机 healthz 慢，甚至形成
循环依赖、放大请求和错误重启信号。独立 tick 应限制频率、并发、超时，加入错峰，
仅探固定配置的公共健康端点，避免任意 URL/用户身份/响应正文；缓存显示
`ok/failed/unknown/stale` 和最后采样时间，缺样不能显示 healthy。
只做观测时不把 peer 状态加入现有 `ok/ready`，不驱动自动重启。实现它会修改
运行时/API 或 compose 约束，需要单独设计、对应 public docs/契约测试及部署审核。

**Seven 要拍什么。** 是否值得增加第三个观测点，以及选“复用已有自检并补可观测性”
还是“独立后台 tick”；还需明确探内网、公网还是分别标记两条路径，以及失败只展示
还是参与 readiness/告警。建议先看独立 runner 首周数据，再决定是否加 tick；
本次未改 healthz、自检、compose，也未把设计建议当已批准实现。
