"""T595: execution-only schema markers must not reach any provider wire."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from capabilities import tool_schema
from provider_types import ToolSpec


ENCODERS = [
    pc._encode_tools_openai_chat,
    pc._encode_tools_openai_responses,
    pc._encode_tools_anthropic,
    pc._encode_tools_bedrock,
]


@pytest.mark.parametrize("encode", ENCODERS + [pc._encode_tools_gemini],
                         ids=lambda fn: fn.__name__)
def test_builtin_wire_omits_local_markers_without_weakening_source(encode):
    specs = tool_schema.build_tool_specs()
    memory = next(spec for spec in specs if spec.name == "memory_write")
    before = deepcopy(memory.parameters)
    threads = memory.parameters["properties"]["actions"]["items"]["properties"]["threads"]
    assert threads["enforceItemBounds"] is True
    assert "enforceItemBounds" not in json.dumps(encode(specs))
    assert memory.parameters == before
    assert threads["enforceItemBounds"] is True
    for tags, expected in [([], "at least 1 items"),
                           (["a"] * 5, "at most 4 items"),
                           (["a"], None), (["a"] * 4, None)]:
        error = tool_schema.validate_tool_args(
            "memory_write", {"actions": [{"op": "add", "summary": "s", "content": "detail", "threads": tags}]})
        if expected is None:
            assert error is None
        else:
            assert expected in error


def _parameters(encode, wire):
    if encode is pc._encode_tools_openai_chat:
        return wire[0]["function"]["parameters"]
    if encode is pc._encode_tools_openai_responses:
        return wire[0]["parameters"]
    if encode is pc._encode_tools_anthropic:
        return wire[0]["input_schema"]
    return wire[0]["toolSpec"]["inputSchema"]["json"]


@pytest.mark.parametrize("encode", ENCODERS, ids=lambda fn: fn.__name__)
def test_wire_preserves_standard_constraints_names_and_literal_data(encode):
    bounded = {"type": "array", "items": {"type": "string"},
               "enforceItemBounds": True, "minItems": 1, "maxItems": 4}
    literal = {"enforceItemBounds": True}
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"enforceItemBounds": bounded},
        "$defs": {"enforceItemBounds": bounded},
        "dependencies": {"enforceItemBounds": bounded},
        "dependentRequired": {"enforceItemBounds": ["other"]},
        "allOf": [{"properties": {"tags": bounded}}],
        "default": literal, "const": literal, "enum": [literal],
        "examples": [literal], "required": ["enforceItemBounds"],
    }
    before = deepcopy(schema)
    wire = _parameters(encode, encode([ToolSpec("sample", "", schema)]))
    expected = deepcopy(before)
    for node in [expected["properties"]["enforceItemBounds"],
                 expected["$defs"]["enforceItemBounds"],
                 expected["dependencies"]["enforceItemBounds"],
                 expected["allOf"][0]["properties"]["tags"]]:
        node.pop("enforceItemBounds", None)
    assert wire == expected
    assert schema == before
    wire["default"]["enforceItemBounds"] = False
    assert schema == before
