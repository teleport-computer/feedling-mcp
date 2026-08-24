# 仓库清理审计

这里保存仓库清理的可复现证据。目标是减少会误导工程师和 agent 的过期检索面，而不是追求删除文件数或代码行数。

## 权威产物

- [`baseline.md`](baseline.md)：清理开始时的 tracked 文件和已知矛盾快照。
- [`agent-diagnostic-benchmark.md`](agent-diagnostic-benchmark.md)：固定排查题、判分规则和前后对比协议。
- `candidates/`：候选项的生产消费者、兼容义务、验证证据和结论；只在出现强候选后创建记录。
- [`tools/repository_inventory.py`](../../tools/repository_inventory.py)：确定性的 tracked 文件分类器。

运行基线分类：

```bash
python3 tools/repository_inventory.py --format markdown
```

工具只读取 `git ls-files`。因此 `.worktrees/`、虚拟环境、构建输出、未 tracked 的本地文件和秘密文件不会进入报告。分类描述的是仓库表面，不代表文件是否仍在使用；特别是 `historical-review` 只表示“需要做生命周期评审”，不能据此自动归档或删除。

## 证据规则

1. live test/pre/prod 证据和实际部署 commit 优先于仓库内的描述性文档。
2. 部署配置、运行时代码、持久化/wire 契约和测试共同决定一个候选是否可删。
3. 静态零引用只能产生候选，不能证明没有运维、配置、远程调用或兼容消费者。
4. Alembic 历史、generated/vendor 内容和安全/隔离边界默认受保护。
5. 文档归档与运行时行为修改分开评审。

## VPS resident consumer 保护边界

`tools/chat_resident_consumer.py` 保持单文件分发，不拆成新的 Python 模块。它直接运行在用户 VPS 上，并参与 systemd 启动、自更新 checkout/re-exec、测试 import seam 和 hosted agent-runner 镜像。文件长度本身不是重构依据。

允许在完整调用、配置、wire、持久化和真实运行证据下删除内部废弃逻辑，但不得顺带改变固定脚本路径、进程模型、自更新相关性、checkpoint 或 session 格式。
