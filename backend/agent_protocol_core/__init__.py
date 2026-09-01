"""模型协议残留处理 + 自我思考（``<think>``）—— **io 自己的东西**。

## 为什么它在 io 里，而不在 memgarden 那边

2026-09-02 从 memgarden 仓库搬回来的。搬之前它作为 `agent-protocol-core`
和 memgarden 一起公开发布，那是**放错了地方**：

    Garden 的职责是「记忆的判断力」—— 什么值得记、怎么归桶、该想起哪几张、
    要不要整理。这里装的是 io 的思维链产品实现：人格文案（「你平时跟他说话
    的那个口气」）、屏幕监看的说辞、两个 FEEDLING_ 开头的开关。

    结果是别人 `pip install memgarden` 会连带装上一个包，里面写着 io 的产品
    设定，而 Garden 内核一次都用不到它们（实测内核只用了 3 个符号）。

当初合并的理由是「聊天和记忆都要处理 ``<think>``」。但两边解决的**不是同一
个问题**，只是长得像：

    Garden 要的  别让推理文字干扰我的 JSON 解析      → 解析健壮性
    io 要的      把推理抽出来给用户看、判断写没写完  → 产品功能

所以现在各管各的：Garden 自带一个 60 行的剥离工具
（`memgarden/text/reasoning.py`），io 保留这一整套。两边不会「漂」——
它们本来就在回答不同的问题。

## 为什么放在 backend/ 下、包名不变

搬家不改行为。放这里 `from agent_protocol_core import self_thinking` 照常
工作，io 的 25 个引用点一处都不用动，diff 里只有「文件换了位置」这一件事。
"""
from . import protocol_leak, self_thinking

__all__ = ["protocol_leak", "self_thinking"]
