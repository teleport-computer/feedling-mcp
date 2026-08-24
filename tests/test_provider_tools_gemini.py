import sys
from copy import deepcopy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from capabilities.tool_schema import build_tool_specs
from provider_types import ToolSpec, ToolResult

TOOLS = [ToolSpec("web_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
         ToolSpec("get_time", "time", {"type": "object", "properties": {}})]


def test_encode_tools_gemini():
    enc = pc._encode_tools_gemini(TOOLS)
    assert enc == [{"functionDeclarations": [
        {"name": "web_search", "description": "search", "parameters": TOOLS[0].parameters},
        {"name": "get_time", "description": "time", "parameters": TOOLS[1].parameters}]}]


def test_encode_tools_gemini_removes_rejected_schema_keywords_recursively():
    parameters = {
        "type": "object",
        "properties": {
            # These are parameter names, not schema keywords, and must survive.
            "additionalProperties": {"type": "string"},
            "enum": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "threads": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 4,
                            "enforceItemBounds": True,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }
    original = deepcopy(parameters)

    encoded = pc._encode_tools_gemini(
        [ToolSpec("memory_write", "write", parameters)]
    )[0]["functionDeclarations"][0]["parameters"]

    assert parameters == original
    assert "additionalProperties" not in encoded
    assert set(encoded["properties"]) == {
        "additionalProperties",
        "enum",
        "actions",
    }
    item_schema = encoded["properties"]["actions"]["items"]
    assert "additionalProperties" not in item_schema
    threads = item_schema["properties"]["threads"]
    assert "enforceItemBounds" not in threads
    assert threads["minItems"] == 1
    assert threads["maxItems"] == 4


def test_encode_tools_gemini_drops_only_non_string_enums():
    parameters = {
        "type": "object",
        "properties": {
            "include_image": {
                "type": "boolean",
                "enum": [True],
                "default": True,
            },
            "limit": {"type": "integer", "enum": [1, 2]},
            "mode": {"type": "string", "enum": ["fast", "full"]},
            "mixed": {"type": "string", "enum": ["fast", 1]},
        },
    }

    encoded = pc._encode_tools_gemini(
        [ToolSpec("photo_read", "read", parameters)]
    )[0]["functionDeclarations"][0]["parameters"]["properties"]

    assert encoded["include_image"] == {"type": "boolean", "default": True}
    assert encoded["limit"] == {"type": "integer"}
    assert encoded["mode"]["enum"] == ["fast", "full"]
    assert encoded["mixed"] == {"type": "string"}


def test_encode_tools_gemini_preserves_accepted_constraints_and_opaque_defaults():
    default = {"additionalProperties": "application data", "enum": [True]}
    parameters = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8,
                "default": default,
            },
            "count": {"type": "number", "minimum": 0, "maximum": 10},
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            },
        },
    }

    encoded = pc._encode_tools_gemini(
        [ToolSpec("accepted", "accepted", parameters)]
    )[0]["functionDeclarations"][0]["parameters"]

    assert encoded == parameters
    assert encoded["properties"]["value"]["default"] is not default


def test_encode_tools_gemini_adapts_the_complete_builtin_catalog():
    specs = build_tool_specs()
    declarations = pc._encode_tools_gemini(specs)[0][
        "functionDeclarations"
    ]
    by_name = {
        declaration["name"]: declaration["parameters"]
        for declaration in declarations
    }

    assert len(declarations) == len(specs)
    assert set(by_name) == {spec.name for spec in specs}
    assert all(
        "additionalProperties" not in declaration["parameters"]
        for declaration in declarations
    )
    memory_action = by_name["memory_write"]["properties"]["actions"]["items"]
    threads = memory_action["properties"]["threads"]
    assert "enforceItemBounds" not in threads
    assert threads["minItems"] == 1
    assert threads["maxItems"] == 4
    assert memory_action["properties"]["op"]["enum"] == [
        "add",
        "update",
        "delete",
    ]
    assert by_name["photo_read"]["properties"]["include_image"] == {
        "type": "boolean",
        "default": True,
    }


def test_decode_two_function_calls_gemini_synthesizes_ids():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "x"}}},
        {"functionCall": {"name": "get_time", "args": {}}}]}}]}
    calls = pc._decode_tool_calls_gemini(body)
    assert [c["id"] for c in calls] == ["call_0_web_search", "call_1_get_time"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "x"}


def test_same_tool_twice_disambiguated_by_index():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "a"}}},
        {"functionCall": {"name": "web_search", "args": {"q": "b"}}}]}}]}
    calls = pc._decode_tool_calls_gemini(body)
    assert [c["id"] for c in calls] == ["call_0_web_search", "call_1_web_search"]


def test_encode_tool_results_gemini_by_name_from_map():
    id_to_name = {"call_0_web_search": "web_search", "call_1_get_time": "get_time"}
    enc = pc._encode_tool_results_gemini(
        [ToolResult("call_0_web_search", "sunny"), ToolResult("call_1_get_time", "12:00")], id_to_name)
    assert enc == [{"role": "user", "parts": [
        {"functionResponse": {"name": "web_search", "response": {"content": "sunny"}}},
        {"functionResponse": {"name": "get_time", "response": {"content": "12:00"}}}]}]
