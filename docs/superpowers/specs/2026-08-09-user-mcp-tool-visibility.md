# 用户 MCP 工具到不了模型 — 两个真 bug，一个撤回

分支 `fix/user-mcp-tool-visibility`，基线 `origin/test` @ d7bf85da。

用户报的是"AI 说没有权限 / 搜不到 / 有时候调不出来"。**"权限"是模型编的说法**——
真实情况是工具压根没进它的工具表，它只是给这个缺口找了个词。

## 已修（本分支）

### 1. Hosted V2：按服务器名字母序截断，整台饿死

`hosted/mcp_tools.py` 把所有候选排成一列（键是服务器名）后填到 64 上限为止。
用真实用户配置（`usr_1baf`，6 台 / 107 个工具）跑出来：

    旧： game 8/8  gaodemap 12/12  gardenforum 25/25  luckin 19/30
         mcdonalds 0/28  ❌   tavily 0/4  ❌
    新： game 8/8  gaodemap 12/12  gardenforum 14/25  luckin 13/30
         mcdonalds 13/28      tavily 4/4  ✅

改成轮转分配（每台先各拿一个），和 pi 桥 2026-08-09 的修法一致——同一个 bug、
同一个用户、同一个 tavily，当时只修了 pi 那条路。

两个上限在同一趟里一起管：**数量上限直接停止分配；字符上限只跳过溢出的那一个候选**
（一个超大 schema 不该替所有人结束这一轮）。游标在字符检查之前推进，所以反复被跳过的
候选不会把循环转死。

截断不再无声：超限时按服务器打一行 `kept/offered`。**必须报分配后的数字**——
报发现数会把一台工具全丢光的服务器写成正常的（pi 那边 Codex 审出过这个错）。

### 2. 自托管 claude：MCP 从来没接上

`tools/README.md` 给的示例是 `claude --print --output-format json "{message}"`，
**没有 `{mcp}`**。没有占位符 → `_user_mcp_cli_value` 返回空 → `--mcp-config` 不下发
→ app 里配的 server 一台都到不了 agent。app 显示"已连接、发现 N 个工具"是控制面探针
直连服务器测的，和 agent 走的是两条路，所以绿灯和"看不到"能同时成立。

chat 轮现在一定带上两个参数：模板有 `{mcp}` 的由占位符展开，没有的由 consumer 注入。
文档的示例也补上了占位符。

> **Codex code review 抓到的 Critical（已修）**：初版只让「没有 `{mcp}`」的命令补授权，
> 而我同时把文档示例改成了推荐 `{mcp}`——`{mcp}` 那条分支只展开 `--mcp-config`、不带授权。
> 等于旧文档的命令修好了，**照新文档配的反而仍然只接线不授权**。现在两条分支统一。

**为什么是两个参数**（实测，claude-code 2.1.217 + 真实 MCP server，以磁盘落文件为准）：
只给 `--mcp-config`，调用进 `permission_denials`，模型回"这个工具需要授权"——
和用户原话一致。托管用户不中招是因为授权规则在我们生成的 `settings.json` 里，
自托管没有那个文件。

**两个都用 `=` 绑定**：两个 flag 都是变参，裸 `--mcp-config <path>` 会把后面的
positional prompt 吞掉——手敲那条文档命令实测 claude 拿提示词当配置文件路径去打开，
exit 1 "Invalid MCP configuration"。

⚠️ **准确说**：consumer 自己对 claude/codex/pi 是把提示词走 **stdin** 的
（`_driver_reads_stdin`），所以这个吞参数在产品路径上走不到——真正的 Critical 是
**缺授权**。`=` 绑定是让任何模板形状都不踩，不是在修一个正在发生的故障。
（初稿把它写成了产品路径故障，过头了，这里更正。）

旁路性：非 chat 轮 / 非 claude / 没有启用的 server / 配置文件不存在 /
operator 自己写了 `--mcp-config` —— 任一条成立，argv 逐字节不变。
operator 自己写了 `--allowed-tools` 时只补 `--mcp-config` 并打 warning 告诉他加哪几条，
不去猜多个同名 flag 的合并语义，也不覆盖他可能依赖的 allowlist。

## 撤回：V1 claude 的"授权被拒"不是真的

初版 spec 断言 `claude -p` 下命令行 `--allowed-tools` **覆盖** settings.json，
因此托管 V1 用户的 MCP 100% 被拒。**实测推翻**（2.1.217，真实 MCP server，
判定看磁盘是否真落文件，不听模型自报）：

| settings.json | `--allowed-tools` | 结果 |
|---|---|---|
| 无 | 无 | ❌ 被拒 |
| **有** | 无 | ✅ **通过** |
| 无 | 有 | ✅ 通过 |
| 有 | 有 | ✅ 通过 |

settings.json 单独就足够，两者是并集不是覆盖。托管 V1 那条路本来就是好的，
**这次一行没动**。

依据的那条 CHANGELOG 记录是 2026-07 图片 `Read` 权限的实测，不能外推到
`mcp__*` 通配规则。教训：拿历史记录当当前行为的证据之前，先复现。

> 版本差：本地实测 2.1.217，runner 镜像烤的是 2.1.195。结论依赖的是
> "settings.json 单独生效"这个方向，不依赖两者的优先级，风险低但不为零。

## 没做，留给下一批

- **失败原因出圈**。工具消失至少有八条路径（服务器掉线、config 解密失败、schema
  超限、数量/字符上限、mutation recovery 拦截、读了外部/私密内容后的 fence、
  prompt 预算不足省掉整个工具表、provider 拒 schema），现在全部只写日志。
  模型不知道自己丢了工具，只能编理由。需要一份贯穿整轮的结构化 omission ledger，
  只放平台生成的服务器名 + 枚举码，不带远端异常文本。
- **连接测试口径**。probe 只能说"连上了、静态检查通过 N 个"，"这一轮实际可用 N 个"
  必须来自真实 turn snapshot。
- **远端描述被全剥**。工具描述、参数说明、示例、枚举值全部丢弃（防注入），
  模型只剩工具名可猜——Ombre Brain 的 `breath/hold/grow/trace/pulse` 就是活例子。
  而 pi 桥是**原样透传**的。同一个产品两条路安全姿态相反，需要产品 + 安全一起定，
  不该由这一批单方面选边。
