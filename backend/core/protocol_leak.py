"""已搬至 ``memory_garden.text.protocol_leak`` —— 此处保留 re-export。

协议泄漏的证据原语是纯字段级判据（零依赖、零 I/O），属于内核。
现有调用方（``model_api_runtime/v2/worker.py`` 等）无需改动。
"""
from memory_garden.text.protocol_leak import *  # noqa: F401,F403
