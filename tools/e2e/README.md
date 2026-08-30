---
document_lifecycle: current
canonical_owner: self
---

# tools/e2e — 发版 P0 冒烟（一键）

`docs/testing/RELEASE_TESTING_PROTOCOL.md` §3 的可执行实现。**只打 test 环境**
（client 硬拒 prod host）。成功格子的测试账号用完即删；失败格子保留七天供诊断，
避免 teardown 把同一轮 trace / trajectory / jobs 一并级联删除。

## 用法

```bash
python3 tools/e2e/p0.py --list                 # 看格子和 key 就位情况
python3 tools/e2e/p0.py                        # 全量（无 key 的格子自动 SKIP）
python3 tools/e2e/p0.py --only vps-claude-code # 只跑指定格子
python3 tools/e2e/p0.py --cleanup-expired-failures # P0 值班人清理满七天的失败现场
python3 tools/e2e/p0.py --cleanup-orphans      # 清理遗留账号（保留期内 FAIL 现场除外）
```

退出码：0 = 无硬失败（skip/warn 允许），1 = 有 FAIL（发版阻断）。

失败现场固定写入 `~/.feedling-e2e-failures/<user_id>/`（目录 0700、文件
0600），同时保留 `~/.feedling-e2e-orphans/<user_id>.json` 中的删号凭据。
输出会打印 `user_id`、现场路径和清理命令。bundle 保存用户 trace、admin
诊断快照及 job/trace 定位符；最终 provider 输入继续只存在服务器端加密
trajectory 中，不额外复制一份明文。七天清理由当班 P0 operator 执行；有本地
admin token 时，清理器必须从 admin 用户接口复核 404 后才删除本地现场。
`--cleanup-orphans` 也会在删号前检查该保留窗口：窗口内的 FAIL 账号与现场
一并保留；现场 manifest 损坏或无法读取时也拒绝删号。

## key 池

`~/.feedling-e2e-keys.env`（chmod 600，永不入 git）：

```
E2E_KEY_ANTHROPIC=sk-ant-…
E2E_KEY_OPENAI=sk-…
E2E_KEY_GEMINI=…
E2E_KEY_OPENROUTER=sk-or-…
E2E_KEY_RELAY=…            # 中转站代表
E2E_RELAY_BASE=https://…   # 中转站 base_url
E2E_RELAY_MODEL=…          # 中转站模型名（可带标签，测清洗）
E2E_KEY_DEEPSEEK=sk-…
```

## 结构

| 文件 | 职责 |
|---|---|
| `client.py` | 账号生命周期 + v1 信封封/解 + 聊天/记忆/蒸馏上传（走真实用户 wire，无后门） |
| `unlock.py` | 最小解锁配方（hosted 无解锁；resident 需 consumer 心跳 + verify_loop） |
| `config.py` | 六类 hosted key 格子 + 三类 VPS harness 格子（OpenClaw 暂免） |
| `hosted.py` | 托管格子流程：setup→聊天→连续性→记忆(warn)→零错误气泡→删号 |
| `vps.py` | VPS 格子流程：本地起真 consumer（子进程）→heartbeat→verify_loop→聊天→删号 |
| `p0.py` | 编排器 + 结果表（§8 报告格式） |

**专项探针**（不在 p0 编排里，触碰对应功能时手动跑）：

| 文件 | 验什么 | 何时跑 |
|---|---|---|
| `repeat_wake_probe.py` | 重复定时提醒：fire 后自动续排 +24h、用**已 fired 的旧 id** 能整串取消、同刻同 repeat 去重、一次性提醒不续排 | 动 `scheduled_wake_v2` / `schedule_wake` 工具面时 |
| `card_gate_probe.py` | 记忆卡内容闸不误杀真卡 | 动 capture/dream 判据时 |
| `temporal_probe.py` | 模型真的读到了注入的时间锚点 | 动 V2 上下文组装时 |
| `turn_failure_smoke.py` | 回合失败的字段/归责能下发到客户端 | 动错误分类或 consumer 兜底时 |
| `resident_maintenance_smoke.py` | resident 识别/poll/notice/genesis claim | 动 consumer 这几条时 |
| `provider_response_envelope_probe.py` | 上游完整响应包装器不会进入 V2 气泡，且只触发一次有界纠正 | 动 provider 回复解析、V2 tool loop 或最终回复闸时 |
| `wake_tool_markup_probe.py` | V2 manual wake 的工具标记在封装前剥离，用户私钥解密后只见正文 | 动 V2 wake 最终回复闸时 |
| `aup_gate_probe.py` | 陪伴提示词（`self_thinking.INSTRUCTION`）没有被上游 AUP 闸拦下，**且当轮证明过自己还有判别力** | bump `agent_protocol_core` / `memgarden` pin，或改 `self_thinking` 文案时 |

`aup_gate_probe` 的四个坑（写新探针时同样适用）：
- **必须喂生产同形提示词，不能喂裸 INSTRUCTION。** 该闸对文本**非单调**：同一段
  文案单独喂被拒、放进完整提示词里反而放行。
- **提示词的每一段都现场从生产件重建，一个快照都不存。** 初版把 io_cli 目录、
  MEMORY READ 段、FILE DELIVERY 段、回复语言规则存成 fixture；上真检查时
  **三段全和分支对不上**（目录少两个参数、memory/file 段长度不符、回复语言整段改过措辞）。
  ⇒ **快照必漂，而漂了不会有人发现**。现在这四段每轮由 `io_cli_catalog.build_catalog`、
  consumer 的两个 block 函数、`reply_language_system_line` 现生成。
  唯一手抄生产的是拼接胶水（`\n` 的个数），已在代码里点名。
- **对照组必须与被测对象不同。** 第一版草稿里 live 与 canary 是同一段文本，于是它
  **从未证明过自己能输出 PASS**——一个恒红的量具也能通过那种自测。现在 `control/distinct`
  这一格会当场拒跑。
- **灵敏度取决于跑在什么环境。** 裸 CI runner 上同一段文案可能根本不被拒。`canary/discriminating`
  就是环境自检：canary 没被拒 ⇒ **这个环境测不了这件事**，换环境重跑，
  别把它读成"我们没事"。退出码按 `deep.py` 的 qualification 口径：**默认任一非 PASS 都 rc=1**，
  `--diagnostic` 才容忍 BLOCKED_EVIDENCE。探针自身的回归在 `tests/test_aup_gate_probe.py`。

`repeat_wake_probe` 的两个坑（写新探针时同样适用）：
- `/v1/proactive/scheduled/fire` **只触发已到期的**（`due_at <= now`），
  所以要排在**过去**；排在未来那次 fire 是空转，看起来像"续排没发生"。
- 断言之间有依赖时要**显式声明前置**——本探针第一版里"取消后无残留 pending"
  是绿的，但那只是因为上一步压根没排出续排，**假通过**。

## 判定语义

- **fail**（阻断）：setup / 解锁 / 聊天回环或解密 / 连续性 / 错误气泡非零 / consumer 起不来
- **warn**（不阻断，报告可见）：记忆卡未在 5 分钟窗口出现（capture 异步天性）
- **skip**（不阻断）：key 缺失、本地无该 CLI binary

## 已知边界

- 首轮回复超时 300s（含 hosted runner 冷 spawn）；后续 180s。
- hermes 格子需要本地 `hermes` CLI + 其自身 profile 可用。
- P1 全功能清单（协议 §4）暂为半自动：复用本包 client 手工驱动，后续脚本化。

## 处理管线探针(processing_probe)

`docs/testing/TESTING.md` 的「入住/记忆处理」判据的可执行实现。跑真 provider
的 estimate → commit → status 全链,断言契约、防重、身份先行、分母诚实、
帧稳定性。

```bash
python3 -m tools.e2e.processing_probe --list                  # 看格子与 key
python3 -m tools.e2e.processing_probe                         # 全部已配 key 的 provider
python3 -m tools.e2e.processing_probe --only hojimi-relay     # 单格
python3 -m tools.e2e.processing_probe --large                 # 多窗大素材(慢,复刻大导入事故)
```

**为什么要真跑**:2026-08-03/04 这批上线前,本探针的前身在真跑里抓出四个
单测与契约测试全绿却真实存在的问题 —— 白名单缺失导致蒸馏 100% 挂、
combined_map 让 24 窗只蒸 8 窗、后端重启后用户被锁 30 分钟、status 帧
materials 抖动。共同点:只在「真 provider + 多帧观察」下暴露。

**合并前也能跑**:client 放行 `127.0.0.1`,配合本机 `serve_dev` + dev-seed
enclave 可以在分支上先真跑一遍(见 docs/testing/TESTING.md 的本地全栈配方),
再合 test。

完整 provider 响应包专项探针还需要同一数据库上的 V2 worker；三项服务启动后运行：

```bash
python3 tools/e2e/provider_response_envelope_probe.py
```

它在本机临时启动 OpenAI-compatible provider stub：setup 测活返回正常文本，
首个真实聊天回合返回现场截图的 Gemini relay 包，第二回合只返回恢复标记。真实
HTTP、队列、worker、信封加解密与账号清理均不替换。

V2 wake 工具标记专项探针使用同一套本地服务：

```bash
python3 tools/e2e/wake_tool_markup_probe.py
```

它用空历史强制触发 `manual_wake`（避免撞 active-conversation 延迟）；本地
provider stub 只在 wake 回复里混入现场同形的 `<parameter>` 标记，最终断言
钉在用户私钥解密后的正文。真实 HTTP、调度入队、worker、信封加解密与账号
清理均不替换。

**中转站要测两家**:不同中转站 `/models` 的目录格式差异很大(带日期后缀 /
带方括号标签 / 裸名),推荐链路只测一家不够 —— `relay-openai-compatible`
与 `hojimi-relay` 两个格子就是为此并存。
