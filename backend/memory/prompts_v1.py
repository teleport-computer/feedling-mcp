"""已搬至 ``memory_garden.prompts.buckets`` —— 此处保留 re-export。

桶指引与语言归一属于「怎么组织」，是内核的一部分。

⚠️ 这几个私有名有跨模块引用，``import *`` 覆盖不到，必须显式列出：
  - ``_text_is_chinese``     → ``memory/card_guard.py``
  - ``_COMMON_BUCKETS_ZH``   → ``tests/test_capture_prompt_v1.py``
  - ``_COMMON_BUCKETS_EN``   → ``tests/test_capture_prompt_v1.py``
"""
from memory_garden.prompts.buckets import *  # noqa: F401,F403
from memory_garden.prompts.buckets import (  # noqa: F401
    _COMMON_BUCKETS_EN,
    _COMMON_BUCKETS_ZH,
    _text_is_chinese,
)
