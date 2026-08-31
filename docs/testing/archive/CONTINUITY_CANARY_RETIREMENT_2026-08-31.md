---
document_lifecycle: historical
canonical_owner: docs/testing/TESTING.md
historical_reason: point-in-time
---
# continuity-canary 退役记录（2026-08-31）

## 决定

Seven 于 2026-08-31 决定退役 `continuity-canary`，Supervisor 同日通过
GitHub Actions 禁用了该 workflow。**结案依据类型是产品判断（PRODUCT
JUDGMENT），不是测量结论。** 现有证据没有证明这项覆盖不再需要，也没有查明
fixture 消失的原因。

仓库保留 `.github/workflows/continuity-canary.yml` 作为可执行历史；文件头标明
退役状态。除非完成下文的恢复条件，不应重新启用。

## 它原来验证什么

该 workflow 每日从生产数据库读取一个永久合成用户最老的共享
`K_enclave` v1 信封，再交给当前生产 enclave 解密。它验证的是：**跨越多日、
部署和密钥轮换后，生产环境仍能读取旧密文。**

这项证据不由以下现有探针替代：

- P0 在 test 环境创建临时账号和新信封，随后立即解密；
- deploy canary 验证新写入的即时 round-trip；
- 两者都不保留生产旧密文作为跨轮换的 aging anchor。

退役因此明确放弃了“生产旧密文跨轮换仍可读”这一项独特持续覆盖。

## 测得的时间线与边界

| 时间（UTC） | 测量结果 |
|---|---|
| 2026-07-04 | PR #45 引入 workflow；首次定时成功发生在 2026-07-05。 |
| 2026-07-17 08:29 | 最后一次真实定时成功：[run 29566598212](https://github.com/teleport-computer/feedling-mcp/actions/runs/29566598212)。最老 v1 信封在第一次尝试中解密成功。 |
| 2026-07-18 08:10 | 首次失败：[run 29637012001](https://github.com/teleport-computer/feedling-mcp/actions/runs/29637012001)。数据库查询已找不到该用户的共享 `K_enclave` chat 行，脚本尚未发起解密便以 exit 2 结束。 |
| 2026-07-18 至 2026-08-30 | 共 44 次定时 run 以相同的 exit 2 形状失败，无明确 owner 接手处置。此前“连红 6 天”只是 2026-08-25 起的观察窗口，不是实际故障时长。 |

最后一次成功和首次失败运行的是同一个 main SHA（`cafb2762326…`）、同一个
canary user 和同一个 enclave URL。workflow 在这段时间没有改动；首次失败之前
也没有新的 main/生产部署。仓库变量和 runtime token secret 在 7 月 17–18 日
没有变化；`DATABASE_URL` 到 8 月 22 日才更新，且更新前后失败形状相同。

因此能下的最窄结论是：永久 fixture 在 7 月 17–18 日之间被清除、重置，或移出
当前生产数据库查询面。**现有 Git 历史和 Actions 日志无法区分账号重置、chat 行
删除或仓库之外的数据库操作，也无法证明是谁、为何执行。** 是否属于有意操作需向
Zhihao 确认。

2026-08-10 的一次绿色 run 不是反证：该临时分支当时把同一路径替换成了 Runtime
V2 测试 job，并未执行 continuity canary。

## 为什么此时退役

原 fixture 已经丢失，原 aging anchor 不能原样恢复；44 天重复红灯也没有形成可执行
的责任闭环。在这一测量背景下，Seven 选择停止噪声并退役，而不是修复或替换。
这是对维护成本和产品优先级的判断，不应改写成“确认无需旧密文连续性监控”。

## 将来如何恢复

若要恢复这项覆盖，应作为新的、有人负责的监控重新上线：

1. 先向 Zhihao 确认旧 fixture 消失是否出于有意操作，并记录结论；
2. 通过正常生产路径创建受保护的永久合成用户和共享 `K_enclave` 信封，记录 owner、
   fixture 标识和创建时间，并排除在清理/重置流程之外；
3. 把“fixture 损坏/缺失”（exit 2）和“旧密文确实无法解密”（exit 1）分成不同告警，
   为两者指定可响应的 owner；
4. 通过 PR 移除 job 级退役门禁后手动验证 workflow，再让信封实际老化并跨至少
   一次部署或密钥轮换；
5. 通过 PR 恢复定时触发，显式重新启用 GitHub Actions workflow，并同步更新
   `docs/testing/TESTING.md`。

完整调查和原“repair”方案坐标：fleet task `T415`，mail
`20260830T184329Z_codexcodex_to_claudeclaude_80b6ba3d`。
