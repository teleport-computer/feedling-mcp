"""把基线上的感知相关 prompt 文本导出成 golden fixture。

在 origin/test 的 checkout 上跑一次，产出的 JSON 就是「行为逐字节不变」的判据。
V2 的三段是模块级常量，直接取；V1 的三段是函数，用固定输入调一次；
`v2_tool_schema_perception` 是 `capabilities.tool_schema.DESCRIPTIONS` 里
感知信号读取工具（perception_snapshot/perception_recent_apps/perception_trend/
perception_history）的 {tool_name: description} 子集快照——同样直接从这份
origin/test checkout 里 import，与其余条目走同一条 provenance，不依赖
"当前分支这个文件是否被改过"这类会漂移的前提。

（2026-08-20 复核记录：曾有过"当前分支 tool_schema.py 未改动，working tree
内容等于 origin/test 基线"的假设——实测不成立：当时分支落后 origin/test，
其间有提交改过 tool_schema.py 的其它工具描述（identity_dimensions_set/
history_search/MEMORY_ORGANIZE_TOOL/MCP_TOOL_SEARCH_TOOL），只是恰好都不在
感知工具那四条里。为避免这条 provenance 假设以后再失效，改为始终从
origin/test 的 checkout 直接 import，不依赖当前分支的工作区文件——这份
`prompt_baseline.json` 已按此方式核对过，与基线文本逐字节一致。）

（2026-08-20 补充：四格验收矩阵。新增条目走**真实入口**而不是裸常量——
`v2_wake_prompt_*` 五个经 `worker._wake_system_prompt_for_lane`（V2 wake 系统
prompt 按 lane 的真实选择器），`v2_build_turn_messages_system` 经
`context.build_turn_messages`（V2 runtime context 的真实拼装函数）。V1
hosted/self-hosted/agent-mode 三格不需要新增基线条目——
`_native_reachout_perception_context` 全文不读 `_HOSTED`/`AGENT_MODE`，
在 origin/test 与本分支都成立（git diff 核对过），所以那三格直接对比
已有的 `v1_reachout_context*` 三个基线值，由测试端用 subprocess 分别以
`_HOSTED=True/False`×`AGENT_MODE=cli/http` 四种真实模块导入态调用同一
入口来证明"确实不受影响"，而不是静态断言。）
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(TOOLS))

from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import worker as v2_worker
from capabilities import tool_schema as v2_tool_schema

import chat_resident_consumer as consumer

# Force the self-thinking suffix ON for the wake-prompt matrix capture so the
# fixture doesn't depend on the ambient env of whichever machine runs this
# script. core/self_thinking.py reads this env var at call time (not import
# time), and it is untouched by the perception-kernel refactor.
os.environ["FEEDLING_V2_SELF_THINKING"] = "1"

_V1_PRESENCE = {"place_label": "office", "motion_state": "walking"}
_V1_CHANGE = [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}]
_V1_DOMAINS = {"location": {"label": "office"}, "media": {"new_artist": True}}

# The perception signal-reading cluster in capabilities.tool_schema.DESCRIPTIONS
# (Task 5 will move these). Snapshotted as a {name: description} dict rather
# than one concatenated blob so a later reader can tell which tool's text
# changed, and so Task 5 can move them one at a time against this same fixture.
_PERCEPTION_TOOL_NAMES = (
    "perception_snapshot",
    "perception_recent_apps",
    "perception_trend",
    "perception_history",
)


def main() -> None:
    out = {
        "v2_wake_system": v2_worker._WAKE_SYSTEM_PROMPT,
        "v2_scheduled_wake_system": v2_worker._SCHEDULED_WAKE_SYSTEM_PROMPT,
        "v2_runtime_perception_policy": v2_context._RUNTIME_PERCEPTION_POLICY,
        "v2_tool_schema_perception": {
            name: v2_tool_schema.DESCRIPTIONS[name] for name in _PERCEPTION_TOOL_NAMES
        },
        "v1_reachout_context": consumer._native_reachout_perception_context(
            _V1_PRESENCE, _V1_CHANGE, _V1_DOMAINS
        ),
        "v1_reachout_context_empty": consumer._native_reachout_perception_context({}, [], None),
        # Exercises the `elif change:` back-compat branch (domains falsy,
        # change non-empty) — the only branch the first two inputs never hit.
        "v1_reachout_context_change_only": consumer._native_reachout_perception_context(
            _V1_PRESENCE, _V1_CHANGE, None
        ),
        # --- Task-matrix cell 1 (V2 hosted worker): wake system prompt AS
        # ACTUALLY SELECTED per lane, via the real selector
        # `worker._wake_system_prompt_for_lane`. `_wake_builder` (the real
        # call site inside `_run_wake`) picks the base prompt per lane BEFORE
        # calling the selector:
        #   screen_watch          -> _SCREEN_WATCH_SYSTEM_PROMPT
        #   scheduled + due notes -> _SCHEDULED_WAKE_SYSTEM_PROMPT
        #   everything else       -> _WAKE_SYSTEM_PROMPT
        # so this captures both that base-prompt selection AND the selector's
        # own lane-conditional suffix assembly (self-thinking instruction +
        # optional screen_watch/non-scheduled suffixes).
        "v2_screen_watch_system": v2_worker._SCREEN_WATCH_SYSTEM_PROMPT,
        "v2_wake_prompt_heartbeat": v2_worker._wake_system_prompt_for_lane(
            "heartbeat", v2_worker._WAKE_SYSTEM_PROMPT
        ),
        "v2_wake_prompt_manual_wake": v2_worker._wake_system_prompt_for_lane(
            "manual_wake", v2_worker._WAKE_SYSTEM_PROMPT
        ),
        "v2_wake_prompt_screen_watch": v2_worker._wake_system_prompt_for_lane(
            "screen_watch", v2_worker._SCREEN_WATCH_SYSTEM_PROMPT
        ),
        # scheduled lane with no due reminder notes: `_run_wake` never swaps in
        # `_SCHEDULED_WAKE_SYSTEM_PROMPT`, so the base stays `_WAKE_SYSTEM_PROMPT`.
        "v2_wake_prompt_scheduled_no_notes": v2_worker._wake_system_prompt_for_lane(
            "scheduled", v2_worker._WAKE_SYSTEM_PROMPT
        ),
        # scheduled lane WITH due reminder notes (the replacement variant).
        "v2_wake_prompt_scheduled_with_notes": v2_worker._wake_system_prompt_for_lane(
            "scheduled", v2_worker._SCHEDULED_WAKE_SYSTEM_PROMPT
        ),
        # --- Task-matrix cell 1 (V2 runtime context policy assembly): the
        # REAL assembly entry point `context.build_turn_messages`, not the
        # bare `_RUNTIME_PERCEPTION_POLICY` constant. With every optional arg
        # left at its default, messages[0] (system role) is exactly
        # `system_prompt.strip() + "\n\n" + _RUNTIME_CONTEXT_POLICY` — the
        # composed block that actually ships the perception policy text.
        "v2_build_turn_messages_system": v2_context.build_turn_messages(
            system_prompt="SENTINEL_SYSTEM_PROMPT", summary="", tail=[]
        )[0]["content"],
    }
    dest = pathlib.Path(sys.argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    print(f"wrote {dest} ({len(out)} entries)")


if __name__ == "__main__":
    main()
