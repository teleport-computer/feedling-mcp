"""把基线上的感知相关 prompt 文本导出成 golden fixture。

在 origin/test 的 checkout 上跑一次，产出的 JSON 就是「行为逐字节不变」的判据。
V2 的三段是模块级常量，直接取；V1 那段是函数，用固定输入调一次。
"""
from __future__ import annotations

import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(TOOLS))

from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import worker as v2_worker

import chat_resident_consumer as consumer

_V1_PRESENCE = {"place_label": "office", "motion_state": "walking"}
_V1_CHANGE = [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}]
_V1_DOMAINS = {"location": {"label": "office"}, "media": {"new_artist": True}}


def main() -> None:
    out = {
        "v2_wake_system": v2_worker._WAKE_SYSTEM_PROMPT,
        "v2_scheduled_wake_system": v2_worker._SCHEDULED_WAKE_SYSTEM_PROMPT,
        "v2_runtime_perception_policy": v2_context._RUNTIME_PERCEPTION_POLICY,
        "v1_reachout_context": consumer._native_reachout_perception_context(
            _V1_PRESENCE, _V1_CHANGE, _V1_DOMAINS
        ),
        "v1_reachout_context_empty": consumer._native_reachout_perception_context({}, [], None),
    }
    dest = pathlib.Path(sys.argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(f"wrote {dest} ({len(out)} entries)")


if __name__ == "__main__":
    main()
