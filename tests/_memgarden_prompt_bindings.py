"""测试专用：内核提示词/解析函数绑上 io 的识别器与档位。

以前这些名字来自 ``backend/memory/{capture,dream}_prompt_v1.py`` 的
``import *`` 兼容壳。两条 runtime 换成 Garden 组件会话之后，**生产代码不再调用
它们**：组件在内部调同一批内核函数，io 经公开入口给参数 ——
``build_garden(signals=IO_LEAK_SIGNALS)`` 与 ``CaptureRequest.policy``。
壳已删除（2026-09-15），生产代码只 import memgarden 的公开 API。

仍直接测内核文本闸 / 解析行为的用例从这里取函数，绑定与组件一致：

- 解析器默认带 ``IO_LEAK_SIGNALS``（capture 另带 ``IO_CONVERSATION_CAPTURE_POLICY``）
- 提示词构造沿用旧壳的称呼装配（``sanitize_user_name`` + io 的 ``_naming_rule``）。
  生产落卡的称呼装配与此一致（``memory.garden_component.capture_request``），但还带
  组件挑的现有卡索引。断言「运行时真实提示词」的用例应走组件（见
  test_capture_request_index_and_naming.py / test_garden_dream_session.py /
  test_memgarden_dream_migrate_golden.py），不要用这里的构造函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from identity.user_naming import _naming_rule, sanitize_user_name  # noqa: E402,F401
from memgarden.prompts import capture as kernel_capture  # noqa: E402
from memgarden.prompts import dream as kernel_dream  # noqa: E402
from memory.garden_component import IO_CONVERSATION_CAPTURE_POLICY  # noqa: E402,F401
from memory.card_leak_signals import IO_LEAK_SIGNALS  # noqa: E402

CAPTURE_TYPES = kernel_capture.CAPTURE_TYPES
DREAM_OPS = kernel_dream.DREAM_OPS
build_capture_retry_prompt = kernel_capture.build_capture_retry_prompt
build_capture_semantic_retry_prompt = kernel_capture.build_capture_semantic_retry_prompt
capture_semantic_retry_reasons = kernel_capture.capture_semantic_retry_reasons
build_dream_retry_prompt = kernel_dream.build_dream_retry_prompt


def build_capture_prompt(
    *,
    ai_name: str,
    user_name: str,
    buckets: str,
    threads: str,
    identity: str,
    window: str,
    cards: str = "",
    locale: str,
) -> str:
    return kernel_capture.build_capture_prompt(
        ai_name=ai_name,
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name, locale=locale),
        locale=locale,
        buckets=buckets,
        threads=threads,
        identity=identity,
        window=window,
        cards=cards,
    )


def build_dream_prompt(
    *,
    ai_name: str,
    user_name: str,
    cards: str,
    recent_conversations: str,
    locale: str,
) -> str:
    return kernel_dream.build_dream_prompt(
        ai_name=ai_name,
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name, locale=locale),
        cards=cards,
        recent_conversations=recent_conversations,
        locale=locale,
    )


def parse_capture_cards(*args, **kwargs):
    kwargs.setdefault("signals", IO_LEAK_SIGNALS)
    kwargs.setdefault("policy", IO_CONVERSATION_CAPTURE_POLICY)
    return kernel_capture.parse_capture_cards(*args, **kwargs)


def parse_dream_consolidations(*args, **kwargs):
    kwargs.setdefault("signals", IO_LEAK_SIGNALS)
    return kernel_dream.parse_dream_consolidations(*args, **kwargs)
