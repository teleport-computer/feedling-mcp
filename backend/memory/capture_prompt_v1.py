"""io 的日常落卡档位。

落卡的提示词、解析、内容闸与重问都在 Garden 组件里（``memory.garden_component``
→ ``GardenComponent.capture`` / ``capture_session``），io 只经公开入口传参：
档位走 ``CaptureRequest.policy``，io 的泄漏识别器走 ``build_garden(signals=...)``。

这里以前是 ``memgarden.prompts.capture`` 的 ``import *`` 兼容壳加一层称呼装配；
两条 runtime 都换成组件会话之后，运行时代码已经不再调用那些内部函数，
壳随之删除（2026-09-15），只留下 io 自己的档位定义。
"""
from dataclasses import replace

from memgarden.policies import CONVERSATION_CAPTURE


# 提示词继续要求「少而厚」；硬上限只防失控批次，不再把模型判断截在 2 张。
# 从钉版 policy replace，确保 rubric 与其余行为逐字段保持原样。
IO_CONVERSATION_CAPTURE_POLICY = replace(CONVERSATION_CAPTURE, max_cards=50)
