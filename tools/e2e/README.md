# tools/e2e — 发版 P0 冒烟（一键）

`docs/testing/RELEASE_TESTING_PROTOCOL.md` §3 的可执行实现。**只打 test 环境**
（client 硬拒 prod host）；每个测试账号用完即删（test-account-hygiene）。

## 用法

```bash
python3 tools/e2e/p0.py --list                 # 看格子和 key 就位情况
python3 tools/e2e/p0.py                        # 全量（无 key 的格子自动 SKIP）
python3 tools/e2e/p0.py --only vps-claude-code # 只跑指定格子
```

退出码：0 = 无硬失败（skip/warn 允许），1 = 有 FAIL（发版阻断）。

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

**中转站要测两家**:不同中转站 `/models` 的目录格式差异很大(带日期后缀 /
带方括号标签 / 裸名),推荐链路只测一家不够 —— `relay-openai-compatible`
与 `hojimi-relay` 两个格子就是为此并存。
