# 测试怎么做 — 从这里开始

> 2026-08-14 重整。**这个目录以前有 6 份文档、没有主次,不知道该从哪进。**
> 现在只有一个入口:就是这份。按「你现在要干什么」往下找,照抄命令即可。
>
> **只剩两条路径需要测**(V1 托管已不再维护):
> 1. **Runtime V2** —— 托管用户,我们的 worker 池跑模型
> 2. **Resident / VPS** —— 用户自己服务器上跑 consumer
>
> **同一件事在这两条路径里往往叫不同名字、在不同文件。**
> 做跨运行时对照之前先查 **[`RUNTIME_MAP.md`](RUNTIME_MAP.md)**(概念 → 各运行时坐标),
> 不要直接 grep 符号名 —— 一条 grep 返回 0,先问「这个符号在那一侧叫这个名字吗」。

---

## 0. 一次性准备(只做一次)

```sh
# 本地 PostgreSQL 要活着
pg_isready -h 127.0.0.1 -p 5432

# provider key 池(跑真实 E2E 才需要)
ls ~/.feedling-e2e-keys.env
```

**⚠️ 跑 pytest 永远要带这三个环境变量**,否则大批假红:

```sh
export NO_PROXY='*' no_proxy='*'
export DATABASE_URL="postgresql://$(whoami)@127.0.0.1:5432/feedling_ci?sslmode=disable"
export FEEDLING_TEST_PG="postgresql://$(whoami)@127.0.0.1:5432/postgres?sslmode=disable"
```

实测:同一批测试**裸跑 43 失败 / 带环境 70 通过**。报错第一行若是
`DATABASE_URL is not set`,那是环境不是代码。

---

## 1. 我刚改完代码,怎么复验?(开发循环)

**别猜跑哪些,问工具:**

```sh
~/fleet/bus/which_tests.sh                    # 看你当前未提交的改动
~/fleet/bus/which_tests.sh --vs origin/test   # 看整个分支
```

它按**真实引用关系**反查该跑哪些测试、按相关度排序,并直接吐出一条环境变量
已带好的命令。典型规模:**5 个文件 / 259 个用例 / 2 秒**,而不是 651 个文件。

改动如果碰到下面这些,**光跑 pytest 不够**:

| 你改了 | 还必须做 |
|---|---|
| 加密 / 信封 / enclave | 跑一次加密链路 E2E,确认服务端永不见明文 |
| provider / driver | 按模型家族分层验(Anthropic/OpenAI/Gemini/中转 wire 各不同) |
| `tools/chat_resident_consumer.py` | 走 §3 的 VPS 冒烟——这条线最容易被漏 |
| 工具 schema / capabilities | V2 和 VPS 两侧都要看,别只改一边造成分叉 |

细则(26 类改动 × 各自必做)见 **`TESTING.md` §2 决策矩阵**。

---

## 2. Runtime V2 这条线怎么单独测?

```sh
# 快:V2 的守卫测试
python3 -m pytest -q tests/test_v2_*.py tests/test_proactive_*v2*.py

# 真:部署态探针(需要 key 池,打真实 provider)
python3 tools/e2e/p0.py --only anthropic-official,openai-official
```

**V2 专属探针**(`tools/e2e/` 下,按需单跑):

| 探针 | 管什么 |
|---|---|
| `continuity_probe.py` | 记忆连续性 |
| `memory_probe.py` | 记忆读写 |
| `perception_wake_probe.py` | 感知触发的主动唤醒 |
| `screen_watch_probe.py` | 屏幕共享监看 |
| `repeat_wake_probe.py` / `idempotency_probe.py` | 重复唤醒 / 幂等 |
| `wake_write_gate_probe.py` | 唤醒时的写入闸 |
| `experience_probe.py` / `deep.py` | 综合体感 |

---

## 3. Resident / VPS 这条线怎么测?(最容易被漏的一条)

这条线的特殊性:**代码跑在用户自己机器上**,后端只提供 `/v1/chat/poll` 和
`/v1/chat/response`。所以只测后端等于没测。

```sh
# 真实形态:起一个本地 consumer,走完整回合
python3 tools/e2e/p0.py --only vps-claude-code
#   还可以 --only vps-codex / vps-hermes(取决于本机装了哪个 CLI)

# 消费端的守卫测试
python3 -m pytest -q tests/test_chat_resident_consumer*.py tests/test_consumer_*.py
```

**VPS 专属探针**:

| 探针 | 管什么 |
|---|---|
| `vps.py` | 完整形态:开号 → 起 consumer → verify_loop → 聊天回合 → 拆 |
| `resident_maintenance_smoke.py` | 维护动作 |
| `user_mcp_handshake_probe.py` | MCP 握手(自部署用户接自己的工具) |
| `memory_thinking_leak_probe.py` | 思维链泄漏进记忆 |
| `worldbook_probe.py` | 世界书 |
| `proactive_probe.py` | 主动唤醒 |

---

## 4. 要推生产了,全量复验怎么跑?

**一条命令,所有格子,一张结果表:**

```sh
python3 tools/e2e/p0.py            # 7 个托管 provider + 3 种 VPS 形态
python3 tools/e2e/p0.py --list     # 先看有哪些格子、key 齐不齐
```

退出码 0 = 无硬失败;**任何 P0 FAIL 阻断 test→main**。
只在 test 环境跑,建的账号会在 teardown 里删掉。

发版的完整协议(分层框架、能力矩阵、双签清单)见
**`RELEASE_TESTING_PROTOCOL.md`**。

---

## 5. 想验「某条修复的测试是真守卫还是纸糊的」

```sh
~/fleet/bus/mutation_check.sh <fix-commit-sha>
```

撤掉那次 fix 的源码、保留测试,再跑:变红 = 真守卫;仍绿 = 该 bug 能原样复发。
2026-08-14 抽验 29 个近期 fix:**26 真守卫**,结论是兵器可信、问题在没人跑。

---

## 目录里其余文件是什么

| 文件 | 是什么 | 什么时候看 |
|---|---|---|
| `TESTING.md` | **规范**:26 类改动各自要做什么 + 约 80 条通用坑 | 改完代码不确定要测什么时 |
| `RELEASE_TESTING_PROTOCOL.md` | **规范**:发版分层框架 + 能力矩阵 | 要推生产时 |
| `CHAT_ACTIVITY_V2_MANUAL.md` | 手工用例:聊天活动轨迹 | 动了 turn-activity 时 |
| `archive/` | 历史报告与模板,**不是规范** | 想看某次是怎么查的 |

## 跑 E2E 之前:先确认你不会撞上部署重启

`feedling-ci` 会在每次合入后自动 pin 一次 test 部署,**每 pin 一次 = test-api 重启一次**。
2026-08-14 凌晨实测:**3 小时内部署了 12 次**,平均每 15 分钟一次。
而一次 VPS P0 要跑 83~165 秒,整套 P0 更久 —— **撞上重启是常态,不是偶发**。

撞上之后的症状**看起来像服务坏了**,实际只是你跑到一半它重启了:
```
httpx.ConnectError: EOF occurred in violation of protocol (_ssl.c:997)
curl: SSL_ERROR_SYSCALL / HTTP 000
consumer 退出:test-enclave TLS handshake timeout
```

**判据(可证伪,别靠感觉)**:

```sh
# 跑之前记一次
before=$(curl -s https://test-api.feedling.app/healthz | python3 -c "import json,sys;print(json.load(sys.stdin)['uptime_s'])")
# ... 跑你的 E2E ...
# 跑完再记一次
after=$(curl -s .../healthz | python3 -c "...")
```
- **`after` 比本次 run 的时长还小 ⇒ 中途重启过**,这次失败是环境的,**重跑**
- **同时比对 `release.git_commit`:before/after 必须是同一个 SHA**。
  只看 uptime 有个漏洞 —— 服务可能重启后又跑了很久,uptime 照样大;
  **SHA 不变才真正锁死「整轮跑的是同一个东西」**(codexcodex 2026-08-14 补的,比原判据严)
- `uptime_s` 很小(< 120s)就开跑 ⇒ 你正踩在重启窗口上,**等一会儿再开**

**另一条必须做的对照**:本机代理(MacPacket/Shadowrocket)也会间歇抽风,
症状同样是 `SSL_ERROR_SYSCALL`。所以判"服务挂了"之前**必须直连对照一次**:
```sh
curl -x http://127.0.0.1:1082 .../healthz    # 走代理
curl --noproxy '*'            .../healthz    # 直连
```
**只有两者都挂,才是服务的问题。**

⚠️ 反过来也要小心:**服务通 ≠ 你的修复在上面**。healthz 的 `release.git_commit`
才是线上真正跑的那个 commit,合入到部署之间有 14~45 分钟的窗口。见 TESTING.md 里
"E2E 绿 ≠ 修复生效"那条。

## 「我改好了」到「用户身上变了」中间有五道关,每道都会静默失败

2026-08-14 一晚上,**这条链的三个不同环节各翻车一次**,而且三次的表象都是
「我明明弄好了,怎么没生效」。把它写全,以后按环节查:

| # | 环节 | 会怎样静默失败 | **验它的唯一方法** |
|---|---|---|---|
| 1 | 代码改好 | — | 跑测试,而且**变异验证**(见 V2_FAILURE_PATTERNS 模式 14/15) |
| 2 | 合入本地分支 | 合了但**没推** | `git log origin/test..HEAD` 应为空 |
| 3 | **推上 origin** | push 被拒 / 推了别的 | `git show origin/<branch>:<文件> \| grep <行为代码>` |
| 4 | CI 绿 | 该文件**根本没被 CI 执行** | 查 `.github/workflows/ci.yml` 里有没有它;豁免名单 469 条,**CI 只跑 27%** |
| 5 | **部署生效** | 部署滞后 14~45 分钟 | `healthz` 的 `release.git_commit`,再 `git show <该sha>:<文件>` |

### 三次实际翻车

- **环节 5**(T033):我判定「修复没合入 test」,实际早合了,**线上没变是部署滞后**。
  我用的是 `git merge-base --is-ancestor` —— 它在 rebase/squash 后会说谎。
- **环节 3**(T051):QA 报「已合入,内容核验命中,22 passed」——
  它查的是**本地工作树**。`origin/test` 上那个符号命中 **0**。
  merge commit 只存在于共享主树的本地分支,从没推上去。
- **环节 4**(claude4):两条隐私守卫「测试存在、变异证明是真闸」,
  但它们在 CI 豁免名单里,**CI 从来不执行**。

### 一条通用判据

**每一环都要用「那一环自己的观察点」去验,不能用上一环的证据推断下一环。**

具体地:
- 查「合入了吗」**按内容查,不按 ancestry、不按 PR 状态字**
  (`--is-ancestor` 在 rebase/squash 后失效;PR 显示 MERGED 不代表你本地看到的那个 SHA 在)
- 查「是同一份内容吗」用 **stable patch-id** 比对,cherry-pick 后 SHA 变了但 patch-id 不变
  (codexcodex 2026-08-14 用这招确认 `f5e59cae` 与 `90c03764` 内容等价)
- 查「线上跑的是什么」**只信 healthz 的 `release.git_commit`**

### ⚠️ 推之前必做的夹带检查

本地落后于 origin 时,`git diff origin/x HEAD` 会把**别人的新增算成你的删除**。
2026-08-14 我据此差点推上去一个 **758 行的回滚**(含别人的测试文件和一个 deploy pin)。

```sh
git diff --name-only origin/test HEAD | grep -vE '^(你预期要改的路径)'
```
**有输出就停下。** 正确做法是从 `origin/test` 起一棵临时 worktree、
cherry-pick 你要的提交、再推 —— 改动面会缩回你真正改的那些。
