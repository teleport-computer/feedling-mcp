# codex 自签名证书链兼容性检测 — 设计

**日期：** 2026-07-18
**状态：** 设计已与用户口头确认（露出深度=consumer 日志 + /test 用户可见警告），待 spec 审阅。

## 目标

一句话：当 codex 用户的自配 MCP server 使用**单张自签名证书**（rustls 会拒）时，把当前的**无声失败**变成**明确、可操作的信号**——文档预防、consumer 日志留痕、/test 用户可见警告纠错。

## 背景（已实测确立的事实）

2026-07-18 的真回合 E2E + Docker Linux 隔离测（node:22-bookworm-slim = runner 同 Debian，
codex 0.142.4）定论：

- **claude / pi（Node TLS 栈）**：单张自签名证书、CA+叶子链，**两种都收**。claude 已本机
  端到端 PASS（真 probe→MAGIC，NODE_EXTRA_CA_CERTS 追加语义）。
- **codex（Rust rmcp，`rustls_platform_verifier`）**：
  - **单张自签名证书**（`openssl req -x509` 默认 CA:TRUE，直接当服务器叶子）→ rustls 报
    `CaUsedAsEndEntity`，**连不上**。
  - **CA(锚,CA:TRUE) + 叶子(CA:FALSE,SAN,被 CA 签) 链** → **端到端 PASS**（Docker 实测：
    codex 发真 mcp_tool_call → 拿到本轮随机、只宿主 server 知道的 `PROBE-OK-MAGIC`）。
  - 结论：**codex 支持自签名，但要求 CA+叶子链**，不能是单张自签名证书。这是 rustls 对
    RFC 5280 的严格执行（拒绝 CA 证书用作 end-entity），不是缺陷、无法在 consumer 侧绕过。

参考记忆：`codex-rmcp-ignores-ssl-cert-file-macos`；ledger `.superpowers/sdd/progress.md`
2026-07-18 三条。

## 硬约束（决定方案边界）

1. **consumer 无法让 codex 接受单张自签名证书**——rustls 是结构性拒绝，server 必须自己呈上
   一张真正的叶子证书。所以本设计的天花板是**检测 + 露出信号**，不是"修好让它连上"。
2. **不改 `_anchor_works`**（`tools/user_mcp_ca_fetch.py`）。它做的是"锚在真 OpenSSL 握手下能不能
   验过"，对 claude/pi（能用单张证书）是**正确**的；改它会误伤那两个驱动。缺的是"有没有东西
   检查 rustls 兼容性"——本设计**新增**这个检查，不动既有的。
3. **不改下游物化管线**：`ca_bundle_pem` / 双 CA 文件 / `_atomic_write_text` / fail-open /
   `_enrich_with_fetched_ca` 的抓取与预算逻辑一律不动。本设计只**新增**一个只读检测 + 三处露出。

## 检测（纯函数，两个薄实现）

**判据（精确）：** rustls 的 `CaUsedAsEndEntity` 触发条件 = **服务器叶子证书 `chain[0]` 的
X.509 basicConstraints 扩展是 `CA:TRUE`**。用 `cryptography`（后端已有依赖）解析。

关键点：**光看"抓到的锚"区分不了好坏**——单张自签名证书的锚是它自己（CA:TRUE），CA+叶子链的锚
是 CA（也 CA:TRUE）。唯一区别是链上**有没有一张独立的 CA:FALSE 叶子**。所以判据必须落在
`chain[0]`（叶子），不是锚。

因 `tools/`（走 openssl 子进程）与 `backend/hosted/`（走 ssl socket）是不同层、后端跨层 import
`tools/` 更糟，接受两个薄实现（各约 5 行 basicConstraints 读取），不强行抽公共层：

- **`tools/user_mcp_ca_fetch.py`：`leaf_is_ca(chain_pems: list[str]) -> bool | None`**
  纯函数，输入已抓到的链，判 `chain[0]` 是否 CA:TRUE。`None` = 空链/解析失败（信息不足，不误报）。
  为避免第二次网络抓取，`fetch_trust_anchor` 内部已 `_fetch_chain` 拿到链——新增一个
  `fetch_anchor_and_leaf_ca(url) -> tuple[str | None, bool | None]`（anchor, leaf_is_ca），
  `fetch_trust_anchor` 收敛为取第一个返回值的薄包装（保持既有调用点签名不变）。
- **`backend/hosted/mcp_probe.py`：`leaf_is_ca(url) -> bool | None`**
  用一个 `ssl.CERT_NONE` 的 socket 连上、`getpeercert(binary_form=True)` 取叶子 DER、
  `cryptography` 解析 basicConstraints。**不信任、只读证书**（自签名服务器也能读到叶子）。
  网络失败/无证书 → `None`。SSRF 防护沿用既有 `blocked_url_kind` 前置闸。

## 三处露出

### ① consumer 日志（常驻，覆盖 hosted + VPS codex）

`tools/chat_resident_consumer.py`：`_enrich_with_fetched_ca` 用新的
`fetch_anchor_and_leaf_ca`。当**本 consumer 是 codex 驱动**（`_cli_template_is_codex()`）且某
enabled server 的 `leaf_is_ca is True` 时，打一条 WARNING，明确点名：server 名、该 server 用了
单张自签名证书、codex(rustls) 会拒、需改成 CA+叶子链。不额外发网络请求（复用抓取时已拿到的链）。

覆盖面：VPS 自托管 codex 用户后端不知其 driver，只能靠这条日志 + 文档；hosted codex 用户日志与
/test 警告二者都有。

### ② 后端 `/test` 用户可见警告（仅 hosted codex）

`backend/hosted/mcp_core.py: test_server(store, name, caller_api_key)`：
- 经 `hosted_config_store._load_runtime_provider_config(store, caller_api_key)` →
  `agent_runtime_cutover.driver_for_provider(config["provider"])` 取 driver（`chat_send_core.py`
  有先例）。
- **仅当 driver == "codex"** 且 `mcp_probe.leaf_is_ca(url) is True` 时，返回新 kind
  **`codex_cert_chain_required`**。
- 这一步顺手**纠正一处现有误导**：TOFU 用户不贴 ca_pem 时 /test 走验证失败回 `tls`，iOS 现文案是
  "已保存，你的 agent 会自己处理"——但对 **codex + 单张证书这句是错的**（agent 处理不了）。新 kind
  在此条件下**盖过** `tls`，给出精准指引。claude/pi 用户不受影响（不满足 driver==codex，仍回原
  `tls` 文案，且对他们那句"agent 会自己处理"是对的）。
- driver 未配置/非 codex → 无此警告（无法确证，不误报）。

### ③ iOS 文案（复用现成 warningText 琥珀通道）

`feedling-mcp-ios`（独立仓，未提交）：`SceneErrorCopy.mcpMessage` 映射新 kind
`codex_cert_chain_required` → 新本地化串 `scene.error.mcp.codex_cert_chain_required`（en+zh-Hans），
意为"这看起来是单张自签名证书，codex 用不了——请改成 CA + 服务器证书的证书链"。经现成
`warningText`（`exclamationmark.triangle` + `Color.cinWarning`）显示。文案守 IO 而非 Feedling、
无裸 hex/字号。

## Fix 1：文档

写明"codex 需 CA+叶子链、单张自签名只对 claude/pi 有效"，并给一段最小 `openssl` 生成 CA+叶子链的
配方：

- `docs-site/content/docs/workflows/mcp.mdx`（主落点，自签名章节）
- `docs-site/content/docs/changelog.mdx`（Unreleased）
- `docs-site/content/docs/self-hosting.mdx`（一句交叉引用，因 VPS codex 用户尤其相关）
- io-onboarding `skill-resident-agent.md`（**独立公开仓，需单独 push**，非本仓）

## 测试

- **纯检测器单测**（登记进 `tests/conftest.py` 的 `_PURE_UNIT`）：单张自签名(CA:TRUE)→True /
  CA+叶子(叶子 CA:FALSE)→False / 空链→None。两个实现各测。
- **consumer 单测**：codex 驱动 + 叶子 CA:TRUE → 警告日志；claude 驱动同证书 → 无警告；
  codex + CA+叶子 → 无警告。用注入的 fetch 桩，不真连网络。
- **`test_server` 单测**（需 PG）：codex + 单张证书 → `codex_cert_chain_required`；claude + 单张
  → 仍原 `tls`；codex + CA+叶子 → 无此 kind。
- **集成**：复用已跑通的 openssl+uvicorn 自签名靶子（scratchpad/mcp_e2e：单张 vs CA+叶子两套证书）。
- 标准命令：`python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
  + `python -m pyflakes` 改动包；起 PG（容器 feedling-test-pg, 127.0.0.1:55432）否则静默漏 DB 模块；
  判据零新增红（已知 pre-existing 8-9 条）。

## 非目标

- 不让 codex 接受单张自签名证书（rustls 结构性拒绝，做不到）。
- 不改 `_anchor_works` / 抓取预算 / 双 CA 文件 / 下游物化。
- 不为 claude/pi 用户加"单张证书"提示（对他们能用，加了是噪音）。
- 不做 VPS codex 用户的后端侧 /test 警告（后端不知其 driver；靠 consumer 日志 + 文档兜底）。

## 约束（沿用全局/仓规）

- 未经明确授权不 `git add`/`commit`——计划里 commit 步骤标「待授权」。
- 目标分支 test。iOS 改动在独立仓 `feedling-mcp-ios`，直接压 main（现状）。
- io-onboarding 是独立公开仓，需单独授权 push。
