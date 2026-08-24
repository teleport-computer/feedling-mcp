---
document_lifecycle: current
canonical_owner: self
---
# 文档生命周期

本规则把当前事实、长期决策、历史证据和机器产物从同一个检索面中区分开。生命周期只说明文档应当怎样被使用，不评价内容质量，也不能代替生产和部署证据。

## 元数据

仓库自有 Markdown 在文件第一行使用 YAML front matter：

```yaml
---
document_lifecycle: current
canonical_owner: docs/CURRENT_STATE.md
---
```

`document_lifecycle` 只能是：

- `current`：随实现同步更新的当前操作、架构、测试或产品事实；
- `decision`：仍然约束当前或未来实现的已接受决策；
- `historical`：已实施、被取代、被否决或只代表一个时间点的证据；
- `generated`：可由确定性命令重建，生成器而非输出文件是权威源。

`canonical_owner` 必填。独立权威文档填 `self`；派生文档填仓库相对路径。current 文档只能指向 current/decision Markdown，不能把 archive、historical 或 generated 文档当唯一权威。

historical 文档还必须设置：

```yaml
historical_reason: implemented  # implemented | superseded | rejected | point-in-time
```

`superseded` 还要提供存在的 `superseded_by` 路径。generated 文档必须提供完整的 `generator` 命令，并把 `canonical_owner` 指向生成器。

## 转换规则

- current 内容被取代时，先把仍有效的契约、风险、替代方案和恢复条件转移到新的 current/decision owner，再标记 historical。
- decision 只有在新决策明确接管约束后才能标记 `superseded`；被否决的方案保留拒绝理由。
- implemented plan 保留为交付证据，但不能继续充当操作手册。
- point-in-time 报告保留 exact commit、环境和采集边界，不能外推为现状。
- generated 输出不手工修补；修改生成器后重新生成并一起评审。
- 移入 `docs/archive/` 只是降低默认检索权重，不能省略生命周期和 rationale transfer。

## 增量执行

当前 CI 只检查相对 PR 目标分支新增或修改的仓库自有 Markdown：

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
```

`contracts/lib/` 和 `vendor/` 属于第三方内容，不由本规则改写。全量迁移完成后才能把 CI 收紧为 `--all`；不得为了过 CI 把未评审历史批量标成 current。

查看已分类文档的确定性清单：

```bash
python3 tools/check_document_lifecycle.py --all --report
```
