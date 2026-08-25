"""主动感知内核 —— 纯函数、零 I/O，V1 与 V2 两条运行时共用一份判据。

边界（见 docs/PERCEPTION_EXTRACTION_DESIGN.zh.md）：
  管到「值不值得戳一下 agent」为止。**wake ≠ 该开口了** —— 戳醒之后
  继续睡 / 只看一眼 / 开口说话是三个平行选项，内核不参与那个决定，
  也不产出任何「该说话了」式的措辞。

不碰：账号身份、加解密、数据库、模型调用、消息入队、说什么话。
"""
from __future__ import annotations

__all__: list[str] = []
