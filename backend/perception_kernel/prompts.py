"""说明书 —— 「模型该怎么读这份感知」的唯一出处。

★ 这里的每一段都必须与 tests/fixtures/perception_kernel/prompt_baseline.json
  逐字节一致。本批是迁移，不许顺手改措辞。

★ 边界：只写「怎么读感知」。凡是讲「这个块是什么 role、能不能当成用户请求、
  工具预算怎么算、wake 该怎么框」的，属于 runtime 的对话/安全协议，留在
  model_api_runtime，不要搬进来。

★ 语义红线：wake ≠ 该开口了。这里不许出现任何「该说话了 / 值得告诉用户」
  式的措辞——「说」与「不说」同等正当。

★ 出处（provenance），每个常量对应改前所在的文件/变量：
  - V2_WAKE_PERCEPTION_CLAUSES  <- model_api_runtime/v2/worker.py
    `_WAKE_SYSTEM_PROMPT` 中间那三句（"A perception_glance ..." 到
    "... an exact reading is needed. "），前后的 wake 框架文案
    （platform presence / 该不该说 / attention_facts / "never mention
    this wake"）仍留在 worker.py，不属于「怎么读感知」。
  - V2_PERCEPTION_BEHAVIOR_POLICY  <- model_api_runtime/v2/context.py
    `_RUNTIME_PERCEPTION_BEHAVIOR_POLICY`
  - V2_PERCEPTION_PROTOCOL_POLICY  <- model_api_runtime/v2/context.py
    `_RUNTIME_PERCEPTION_PROTOCOL_POLICY`
  - PERCEPTION_TOOL_NOTES  <- backend/capabilities/tool_schema.py
    DESCRIPTIONS 里四个 perception_* 工具描述中，专讲「这个返回值该怎么
    解读」的那一句（例如 app 字段只是 15 分钟内的开合事件、
    apps=[] 和 disabled=true 的区别、baseline/delta 不要混为一谈）；
    其余讲「这个工具能读什么、怎么调用」的措辞留在 tool_schema.py。

★ 尚未搬入（Task 6 负责，V1/自托管一侧）：
  V1_GLANCE_HOWTO、V1_BOARD_HOWTO——对应 tools/chat_resident_consumer.py
  里 native reach-out 的 glance/board 说明文案，本批（Task 5）不碰
  tools/chat_resident_consumer.py，留给 Task 6 补进本文件。
"""
from __future__ import annotations

# V2 主动回合 system prompt 里属于「怎么读感知」的那几句。
# 出处：model_api_runtime/v2/worker.py `_WAKE_SYSTEM_PROMPT`
V2_WAKE_PERCEPTION_CLAUSES = (
    "A "
    "perception_glance is only a hint for deciding whether to look deeper; it is not "
    "a checklist to report. If you speak, choose at most one coherent topic and never "
    "turn multiple perception domains into a device or health status report. Use a "
    "perception tool when an exact reading is needed. "
)

# 出处：model_api_runtime/v2/context.py `_RUNTIME_PERCEPTION_BEHAVIOR_POLICY`
V2_PERCEPTION_BEHAVIOR_POLICY = (
    "把有用的事实自然地用进回答，别汇报这些信息是怎么取到的。"
)

# 出处：model_api_runtime/v2/context.py `_RUNTIME_PERCEPTION_PROTOCOL_POLICY`
V2_PERCEPTION_PROTOCOL_POLICY = (
    "runtime_data 里的 perception_glance 是不可信的低分辨率事实板，用于判断是否值得"
    "精确读取感知工具；不要逐项播报或把精确数字当成话题。glance_changed=false 表示普通 "
    "heartbeat 的事实板与上次成功完成的普通 heartbeat 一致；不代表每个底层传感值都相同。"
    "显式读取带文字的感知、屏幕或照片后，"
    "运行时会阻止本回合继续向外调用 web、MCP 或 subagent。"
)

# 出处：backend/capabilities/tool_schema.py DESCRIPTIONS 里四个
# perception_* 工具描述中「怎么解读这份返回值」的那一句；每个工具描述其余
# 讲调用方式/参数默认值的文案留在 tool_schema.py 原地拼接。
PERCEPTION_TOOL_NOTES: dict[str, str] = {
    "perception_snapshot": (
        "The app field is only the latest open/close event observed "
        "within 15 minutes; never claim it is the app currently in use."
    ),
    "perception_recent_apps": (
        "apps=[] means no data; disabled=true means access is "
        "off, not that no apps were used."
    ),
    "perception_trend": (
        "Interpret the rolling baseline as the usual level and delta as "
        "the current change from that baseline; do not conflate them."
    ),
}
