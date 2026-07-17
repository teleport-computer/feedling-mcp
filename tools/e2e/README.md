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

## 判定语义

- **fail**（阻断）：setup / 解锁 / 聊天回环或解密 / 连续性 / 错误气泡非零 / consumer 起不来
- **warn**（不阻断，报告可见）：记忆卡未在 5 分钟窗口出现（capture 异步天性）
- **skip**（不阻断）：key 缺失、本地无该 CLI binary

## 已知边界

- 首轮回复超时 300s（含 hosted runner 冷 spawn）；后续 180s。
- hermes 格子需要本地 `hermes` CLI + 其自身 profile 可用。
- P1 全功能清单（协议 §4）暂为半自动：复用本包 client 手工驱动，后续脚本化。
