---
document_lifecycle: current
canonical_owner: self
---
# Resident consumer 源码地图

`tools/chat_resident_consumer.py` 是受保护的单文件分发边界，必须继续以如下方式执行：

```sh
python tools/chat_resident_consumer.py
```

不得把它拆分为额外的 Python 模块。同一个文件会被直接分发到用户 VPS、由固定
systemd 命令启动、通过测试 import seam 导入，并被打进 hosted agent-runner 镜像。
文件长度不是拆分依据。本地图用于导航和清理评审；它不改变可执行路径或运行时行为。

当前拓扑和运行时 owner 以
[`docs/CURRENT_STATE.md`](../CURRENT_STATE.md) 为准。运维配置和环境变量示例以
[`tools/README.md`](../../tools/README.md) 为准。

## 受保护的分发与进程契约

| 契约 | 当前证据 | 文档型清理 batch 中必须保持不变的内容 |
|---|---|---|
| 直接脚本 | [`tools/chat_resident_consumer.py`](../../tools/chat_resident_consumer.py) 在 `if __name__ == "__main__"` 下启动 `run()`。 | `python tools/chat_resident_consumer.py` 入口路径和单一可执行 consumer 文件。 |
| VPS 服务 | [`deploy/feedling-chat-resident.service`](../../deploy/feedling-chat-resident.service) 把 checkout 设为 `WorkingDirectory`、加载 `EnvironmentFile`，并使用 `ExecStart=/home/ubuntu/feedling-venv/bin/python tools/chat_resident_consumer.py`；恢复由 `Restart=always` 负责。 | 命令形状、单进程模型、环境文件交接和 systemd restart 语义。 |
| VPS P0 | [`tools/e2e/vps.py`](../../tools/e2e/vps.py) 的 `run_vps_cell()` 以子进程启动该精确脚本，提供 resident 环境，等待官方 poll heartbeat 和 `verify_loop`，再验证一个 chat round trip 与可解密回复。 | 直接子进程 seam、传入的 checkpoint/session 路径，以及端到端 consumer 生命周期。 |
| Hosted Resident | [`deploy/Dockerfile.agent-runner`](../../deploy/Dockerfile.agent-runner) 同时复制 `backend/` 和 `tools/`，然后启动 `backend/agent_runtime/supervisor.py`；supervisor 为每个用户托管一个 resident 进程。 [`tests/test_agent_runtime_resident_contract.py`](../../tests/test_agent_runtime_resident_contract.py) 固定 `spawners.consumer_env` → consumer import seam。 | hosted 镜像的 copy/import graph、supervisor → consumer 环境变量名称，以及逐用户进程隔离。 |
| 测试导入 | [`tests/test_chat_resident_self_update.py`](../../tests/test_chat_resident_self_update.py) 导入 `tools.chat_resident_consumer`；hosted contract test 通过 `importlib.util.spec_from_file_location` 加载脚本。 | 这些直接 import seam；将职责迁移到伴随模块会改变分发与 update-discovery 义务。 |

consumer 在每个 self-hosted 用户 VPS 安装中是一个前台进程。Hosted Resident 复用同一
文件，由 supervisor 为每个用户创建一个进程。任一形态都不授权把多模块打包或进程模型
变化作为清理的副作用。

## 配置、持久化与退出契约

| 范围 | 源码导航 | 契约 |
|---|---|---|
| import-time 配置 | `Config` section 以及模块级 `FEEDLING_API_URL`、`FEEDLING_API_KEY`、`AGENT_MODE`、`CHECKPOINT_FILE`、`AGENT_SESSION_FILE_TEMPLATE`。 | 这些环境配置参与 VPS env-file、E2E 与 hosted `consumer_env` seam，模块在 import 时读取它们，须保持该 import 行为。特别是环境变量 `AGENT_SESSION_FILE` 被读取并赋值给模块符号 `AGENT_SESSION_FILE_TEMPLATE`；VPS E2E 和 `consumer_env` 传入的是前者，而不是名为 `AGENT_SESSION_FILE_TEMPLATE` 的环境变量。 |
| 运行 release 与 hosted authentication | `Decrypt sources — at least one must be set for v1 encrypted backends` section：`RUNNING_COMMIT` 和 `FEEDLING_RUNTIME_TOKEN_FILE`。 | 保持 running-commit identity 以及 runtime-token/API-key authentication fallback。token file 还标记 hosted run，hosted run 不得 self-update。 |
| Checkpoint | `Checkpoint (persist last processed message timestamp)` section：`_load_checkpoint()`、`_save_checkpoint()`、`_load_proactive_checkpoint()`、`_save_proactive_checkpoint()`。 | 保持 checkpoint 文件位置行为与 JSON state shape，包括 `last_ts`、`last_job_ts`、`api_key_fingerprint` 和 scoped `user_id`。它是 restart/redelivery cursor，不是可随意删除的 cache。 |
| Native-agent session | `Agent backends` section：`_load_agent_session_id()`、`_save_agent_session_id()`、`_clear_agent_session_id()`、`_prepare_cli_command()`。 | 保持 session-file 放置、metadata 与 resume 语义。CLI session rotation 或 `--resume` 行为属于用户可感知的连续性契约。 |
| Poll、decrypt 与 reply wire | `Decrypt sources — plaintext content for v1 encrypted messages`、`Feedling API helpers`、`Main loop` sections，重点为 `poll_chat()`、`_process_messages()`、`run()`。 | 保持 poll/response endpoint 处理、encrypted-message 处理、checkpoint 推进规则和单一前台 loop。 |
| Exit 与 supervision | `Main loop` 的 `run()` 加上 `_apply_self_update()`。致命 authentication 情况会退出进程；re-exec 失败会干净退出，以便 supervisor/systemd restart。 | 不得改变 exit owner、signal/restart 预期，也不得把 daemon 变为 agent gateway 的 child task。 |

## Self-update 与 hosted-image 边界

`Self-update — keep a self-hosted resident on the commit the backend
deploys` section 负责 release convergence：

1. `_maybe_self_update()` 从 idle poll response 读取
   `client_release.expected_consumer_commit`。
2. `_run_self_update()` 将其与 `RUNNING_COMMIT` 比较，保护 dirty tree，并将
   unknown/diff-failure 状态视为“未证明兼容”。
3. `_runtime_repo_files()` 与 `_relevant_changed()` 判定一个 release 是否触及
   consumer 实际运行的 dependency set，包括 `tools/io_cli.py`、requirements files
   和 lazily imported backend paths。
4. `_apply_self_update()` checkout advertised commit，经 `_pip_install()` 安装
   changed requirements，然后对同一脚本执行 `os.execv`。

顺序是契约：checkout 必须先于 requirements installation，后者必须先于 re-exec。
未改变 relevant file 的 release 可以上报 compatibility commit，而不是强制 restart。
不得用以不同方式发现模块的通用 updater 替换它。

对于由 supervisor 管理、带 runtime-token-file 的运行，`_HOSTED` 为真。Hosted
consumer 不会 self-update：immutable image 由部署刷新。Dockerfile 有意将 consumer、
其 backend imports 和 agent CLIs 一并交付；拆分 consumer 将要求同时重新审查该镜像、
supervisor 与全部 direct-VPS update discovery。

## 职责索引

用下面的既有 section header 和 stable symbol 导航该大文件；不得仅为缩短本索引而
重排格式或拆分文件。

| 职责 | 既有 section header | 起始 stable symbol |
|---|---|---|
| 环境、路径、runtime mode 与进程级 policy | `Config` | `FEEDLING_API_URL`、`FEEDLING_API_KEY`、`AGENT_MODE`、`CHECKPOINT_FILE`、`AGENT_SESSION_FILE` → `AGENT_SESSION_FILE_TEMPLATE` |
| Running release identity 与 hosted token mode | `Decrypt sources — at least one must be set for v1 encrypted backends` | `RUNNING_COMMIT`、`FEEDLING_RUNTIME_TOKEN_FILE` |
| Backend commit convergence | `Self-update — keep a self-hosted resident on the commit the backend deploys` | `_runtime_repo_files()`、`_should_self_update()`、`_git_changed_files()`、`_apply_self_update()`、`_run_self_update()`、`_maybe_self_update()` |
| Chat/proactive cursor 与 replay protection | `Checkpoint (persist last processed message timestamp)` 和 `Message dedup` | `_load_checkpoint()`、`_save_checkpoint()`、`_load_proactive_checkpoint()`、`_save_proactive_checkpoint()`、`_msg_key()` |
| Decrypt source 与 health | `Decrypt sources — plaintext content for v1 encrypted messages` 和 `Resident decrypt-source health — reported to the backend on every poll` | `get_decrypted_history()`、`_poll_decrypt_since()`、`_apply_infra_health()` |
| Attachment 与 screen context | `Image message handling` 和 `Screen-sharing context` | `_hydrate_omitted_bodies()`、`_should_attach_screen_context()` |
| Agent invocation 与 session continuity | `Agent backends` | `_prepare_cli_command()`、`_load_agent_session_id()`、`_save_agent_session_id()`、`call_agent()` |
| Local capability handoff | `io_cli capability catalog injection — VPS/self-hosted CLI resident only` | `_IO_CLI_PATH`、`_agent_can_use_local_io_cli()`、`_prepend_io_cli_capability_catalog()` |
| Backend wire helper | `Feedling API helpers` | `_HEADERS`、`_load_whoami()`、`poll_chat()`、`post_reply()` |
| Foreground 与 maintenance work scheduling | `Main loop` 和 `Resident genesis-distill lane` | `_process_messages()`、`_process_resident_jobs()`、`_process_resident_distill_once()`、`run()` |

## 清理规则

只有当精确 call-site、configuration、wire、persistence、VPS/hosted-distribution
evidence 共同证明某职责已过时时，才允许删除内部实现。任何文档型清理 batch 都必须保持
固定 executable path、process model、import behavior、update-relevance logic、
checkpoint format 与 session format 不变。

在已接受的 internal deletion 后，运行：

```sh
python3 -m pytest -q tests/test_chat_resident_self_update.py \
  tests/test_agent_runtime_resident_contract.py \
  tests/test_chat_resident_consumer*.py
```

对于 behavior-affecting deletion，还必须在 `test` 运行适用的 VPS P0 path，并证明
checkout/re-exec、checkpoint preservation 以及下一次 chat turn。通常的 VPS 入口为：

```sh
python3 tools/e2e/p0.py --only vps-claude-code
```

若 Claude Code 不是受影响路径，选择已安装 CLI 对应的 cell。

## 未来 simplification artifact

本 baseline 没有 Task 9 Feedling simplification skill 或 standalone candidate template，
本任务也不会创建它们。任何未来 simplification skill 或 candidate template 都必须消费
这条保护规则：将 `tools/chat_resident_consumer.py` 分类为**排除拆分**，且在提出
internal deletion 前要求上述 evidence 和 verification gate。

Task 6 candidate record 位于 [`candidates/`](candidates/)，仍是独立 review artifact。
本源码地图既不接受也不删除任何 candidate。
