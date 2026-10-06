"""Test-only helpers for presence wakes' look-first round (T723).

Heartbeat and manual_wake withhold reply/stay_silent from their first provider
call. Tests that script the semantic outcome answer that call with "looked,
nothing needed" without consuming a scripted response; look-first behaviour
itself has dedicated tests that script the look round explicitly.
"""
from capabilities import tool_schema as cap_tool_schema
from model_api_runtime.v2 import worker


class ScriptedCalls(list):
    """Provider calls a test scripted; ``look_rounds`` holds look-first calls."""

    def __init__(self):
        super().__init__()
        self.look_rounds = []


def is_look_first_round(tools, messages=None, tool_choice=None):
    """Presence-wake look-first call: the wake prompt is present and tools are
    offered without a tool_choice, but reply/stay_silent are not. Terminal and
    forced rounds (tool_choice none/required) are never look rounds."""
    if tool_choice in {"none", "required"}:
        return False
    names = {getattr(spec, "name", spec) for spec in (tools or [])}
    if not names or names & {"reply", cap_tool_schema.STAY_SILENT_TOOL}:
        return False
    return messages is None or worker._WAKE_SYSTEM_PROMPT[:40] in str(messages)


def looked_nothing_needed():
    return {"reply": "", "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
