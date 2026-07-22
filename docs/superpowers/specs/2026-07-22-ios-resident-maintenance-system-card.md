# iOS:resident 维护通知渲染成系统卡片(交接给 liko)

**状态**:后端全部就绪,**纯 iOS 前端改动**,无需任何后端配合。
**优先级**:中低(后端整改后老用户几乎不会再收到此类消息,但历史消息与新用户 onboarding 期仍会出现)。

## 1. 背景(30 秒)

Feedling 后端会在自托管 resident 出问题时,往用户聊天里注入一条「维护通知」消息
(内容是给用户侧 agent 看的修复指引)。这条消息在协议上是 `role: "user"`
(必须如此——resident consumer 只认领 user 消息,agent 才能收到并自动修复),
但 iOS 目前按 role 渲染,把它画成了**用户自己发出的右侧气泡**——用户看到一大段
自己"说"的技术文字,体验错误(Seven 2026-07-22 实际截图反馈)。

目标:把这类消息渲染成**整宽的系统维护卡片**,不再是用户气泡。

## 2. 后端契约(已上线,不用改一行后端)

`GET /v1/chat/history` 的每条消息**已经透传 `source` 字段**。

**唯一判定条件**:

```
role == "user" && source == "resident_maintenance"
```

- 辅助特征(**不要**作为判定依据,仅调试参考):`client_msg_id` 以
  `resident_maintenance_` 开头。
- `content` 走现有明文/解密管线即可,无需特判(这类消息服务端同时落了明文
  content,history 直接带回;信封也是标准共享信封,客户端解密同样可用)。
- **注意**:同一个 `source: "resident_maintenance"` 也会出现在**助手侧回复**上
  (`sender == "assistant"`)——那是 AI 处理完维护通知后对用户的说明,
  **保持普通助手气泡,不要特殊渲染**。只特判 user 侧那条。
- 未知/其它 source 值:保持现行渲染。只加一个特判,不要做成白名单反转。
- 推送:服务端已保证这类消息不触发用户可见 push,客户端无需处理。

## 3. 渲染规格(遵循 DESIGN.md,tokens 见其底部)

**卡片形态**(替代左右气泡,整宽、居中流内):

- 背景 `Color.feedlingSurface`,1pt 描边或分隔 `Color.feedlingDivider`,
  圆角 `Radius.md`,内边距 `Spacing.md`,与相邻消息间距沿用现有列表间距。
- 不显示用户头像/发送者标识;时间戳沿用聊天现有规则。

**头部行**:

- SF Symbol `wrench.and.screwdriver` + 文案「系统维护通知」。
- 字体 `.footnote` semibold,颜色 `Color.feedlingInkMuted`。
- 图标必须给 `accessibilityLabel`("系统维护通知")——DESIGN.md VoiceOver 规则。
- **不用红色/错误色**:这是 warning 级运维信息,不引入新颜色;图标+文字已满足
  "no color-only signaling"。

**正文**:

- 默认**折叠**:显示前 3 行(`lineLimit(3)`),`.footnote`,
  `Color.feedlingInkMuted`。全文是给 agent 读的技术指引,人类默认不需要全文。
- 尾部「展开」按钮(chevron),点击展开全文,可再收起;触达区 ≥44pt。
- 长按菜单提供「复制全文」(用户可能需要把指引发给运维/agent)。

**通用**:深色模式走 token 的 light/dark pair,零额外工作;Dynamic Type 生效;
禁止 raw hex / raw point size / raw font string(DESIGN.md 硬规则)。

## 4. 边界与不做的事

- 历史里已存在的旧维护消息自动按新规则渲染(纯客户端逻辑,无迁移)。
- **只改渲染**:未读计数、滚动、本地缓存等一切逻辑不动。
- 助手侧 `source == "resident_maintenance"` 的回复:普通气泡(见 §2)。

## 5. 验收清单

- [ ] `role=user, source=resident_maintenance` 显示为系统卡片,不再是右侧用户气泡
- [ ] 折叠(3 行)/ 展开 / 收起 / 长按复制全文 可用
- [ ] 助手对维护通知的回复仍是普通助手气泡
- [ ] 深色模式、Dynamic Type、VoiceOver(图标 label)正常
- [ ] 普通消息渲染零回归(尤其其它 source 值:chat / verify_ping 不受影响)

## 6. 怎么在开发中看到这条消息

真实触发需要一台故障的 resident,不现实。两个办法:

1. **SwiftUI Preview / mock**(推荐):本地构造一条
   `role: "user", source: "resident_maintenance", content: <长文本>` 的消息即可。
   真实 content 样例(截取):

   ```
   【Feedling 维护通知】(来自 Feedling 服务端,非用户本人发送)

   这条消息由 Feedling 后端的维护通知系统写入聊天:服务端检测到你所连接的
   resident 运行环境可能有问题。相同告警会同时显示在用户 Feedling App 的横幅里…

   检测到的问题:
   你所在的 resident consumer 上报的 commit 与服务端期望不一致…

   建议检查步骤:
   1. 在部署 resident 的机器上找到当初 clone 的仓库目录…
   (共 20-30 行)

   诊断信息(排查时引用):
   - reason: consumer_commit_mismatch
   - consumer_id: ip-xxx:12345
   ```

2. **test 环境真实注入**:找 claude3(本仓协作 agent),可以在 test 环境
   给测试账号人工触发一条真实注入,用真机验收。

## 7. 有问题找谁

判定字段 / 后端行为 / test 环境造数据:claude3(或 Seven 转)。
设计裁量(卡片视觉细节):按 DESIGN.md 自行裁量即可,本 spec 只锁语义与 token 合规。
