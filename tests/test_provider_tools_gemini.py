import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from capabilities.tool_schema import build_tool_specs
from provider_types import ToolSpec, ToolResult

TOOLS = [ToolSpec("web_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
         ToolSpec("get_time", "time", {"type": "object", "properties": {}})]


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _rejected_schema_keywords(schema):
    """Collect rejected schema keywords without mistaking property names for them."""
    found = set()
    if not isinstance(schema, dict):
        return found
    for key, value in schema.items():
        if key in pc._GEMINI_REJECTED_SCHEMA_KEYS:
            found.add(key)
        if key in pc._OPAQUE_SCHEMA_VALUE_KEYS:
            continue
        if key in pc._SCHEMA_MAP_KEYS and isinstance(value, dict):
            for child_schema in value.values():
                found.update(_rejected_schema_keywords(child_schema))
            continue
        if isinstance(value, dict):
            found.update(_rejected_schema_keywords(value))
        elif isinstance(value, list):
            for item in value:
                found.update(_rejected_schema_keywords(item))
    return found


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
        "$defs": {"unused": {"type": "string"}},
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
            "object_value": {
                "type": "object",
                "enum": [{"$ref": "https://example.invalid/value.json"}],
            },
        },
    }

    encoded_schema = pc._encode_tools_gemini(
        [ToolSpec("photo_read", "read", parameters)]
    )[0]["functionDeclarations"][0]["parameters"]
    encoded = encoded_schema["properties"]

    assert "$defs" not in encoded_schema
    assert encoded["include_image"] == {"type": "boolean", "default": True}
    assert encoded["limit"] == {"type": "integer"}
    assert encoded["mode"]["enum"] == ["fast", "full"]
    assert encoded["mixed"] == {"type": "string"}
    assert encoded["object_value"] == {"type": "object"}


def test_encode_tools_gemini_inlines_local_definition_refs_without_rejected_keys():
    parameters = {
        "$defs": {
            "coordinate": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "place/name": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "coordinate": {"$ref": "#/$defs/coordinate"},
                },
                "required": ["name", "coordinate"],
                "additionalProperties": False,
            },
        },
        "definitions": {
            "label": {"type": "string", "minLength": 1},
        },
        "type": "object",
        "properties": {
            "destination": {
                "$ref": "#/$defs/place~1name",
                "description": "Where to go",
            },
            "origin": {"$ref": "#/definitions/label"},
            # These are property names, not schema keywords, and must survive.
            "$ref": {"type": "string"},
            "$defs": {"type": "string"},
            "definitions": {"type": "string"},
            "patternProperties": {"type": "string"},
            "dependentSchemas": {"type": "string"},
        },
        "patternProperties": {"^x-": {"type": "string"}},
        "dependentSchemas": {"origin": {"required": ["destination"]}},
        "additionalProperties": False,
    }
    original = deepcopy(parameters)

    encoded = pc._encode_tools_gemini(
        [ToolSpec("route", "route", parameters)]
    )[0]["functionDeclarations"][0]["parameters"]

    assert parameters == original
    assert _rejected_schema_keywords(encoded) == set()
    assert set(encoded["properties"]) == {
        "destination",
        "origin",
        "$ref",
        "$defs",
        "definitions",
        "patternProperties",
        "dependentSchemas",
    }
    destination = encoded["properties"]["destination"]
    assert destination["allOf"][1] == {"description": "Where to go"}
    place = destination["allOf"][0]
    assert place["properties"]["coordinate"] == {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
    }
    assert "additionalProperties" not in place
    assert encoded["properties"]["origin"] == {
        "type": "string",
        "minLength": 1,
    }


def test_encode_tools_gemini_drops_dynamic_object_constraints_conservatively():
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            # Literal property names must remain even when schema keywords with
            # the same spelling are conservatively dropped.
            "patternProperties": {"type": "string"},
            "dependentSchemas": {"type": "object"},
        },
        "required": ["kind"],
        "patternProperties": {"^x-": {"type": "integer", "minimum": 0}},
        "dependentSchemas": {"kind": {"required": ["detail"]}},
    }

    encoded = pc._adapt_tool_schema_gemini(parameters)

    assert encoded == {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "patternProperties": {"type": "string"},
            "dependentSchemas": {"type": "object"},
        },
        "required": ["kind"],
    }


def test_encode_tools_gemini_cycle_falls_back_to_previous_adapter_behavior(
    monkeypatch,
):
    parameters = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/node"}},
                "additionalProperties": False,
            },
        },
        "$ref": "#/$defs/node",
        "additionalProperties": False,
    }
    resolve = pc._resolve_local_schema_ref_gemini
    resolved_refs = []

    def counting_resolve(root, ref):
        resolved_refs.append(ref)
        return resolve(root, ref)

    monkeypatch.setattr(pc, "_resolve_local_schema_ref_gemini", counting_resolve)

    encoded = pc._adapt_tool_schema_gemini(parameters)

    assert resolved_refs == ["#/$defs/node"]
    assert encoded == {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/node"}},
            },
        },
        "$ref": "#/$defs/node",
    }


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.invalid/schema.json#/$defs/item",
        "#",
        "#/$defs",
        "#/$defs/missing",
        "#/properties/item",
    ],
)
def test_encode_tools_gemini_unsupported_refs_fall_back_to_previous_behavior(ref):
    parameters = {"$ref": ref, "additionalProperties": False}

    assert pc._adapt_tool_schema_gemini(parameters) == {"$ref": ref}


def test_encode_tools_gemini_overdeep_ref_chain_falls_back_without_recursing():
    definitions = {
        f"level_{index}": {"$ref": f"#/$defs/level_{index + 1}"}
        for index in range(pc._GEMINI_MAX_REF_DEPTH + 1)
    }
    definitions[f"level_{pc._GEMINI_MAX_REF_DEPTH + 1}"] = {"type": "string"}
    parameters = {
        "$defs": definitions,
        "$ref": "#/$defs/level_0",
        "additionalProperties": False,
    }

    encoded = pc._adapt_tool_schema_gemini(parameters)

    assert encoded["$ref"] == "#/$defs/level_0"
    assert encoded["$defs"]["level_0"] == {"$ref": "#/$defs/level_1"}
    assert "additionalProperties" not in encoded


def test_encode_tools_gemini_overwide_ref_graph_falls_back_before_expansion():
    parameters = {
        "$defs": {"value": {"type": "string"}},
        "type": "object",
        "properties": {
            f"value_{index}": {"$ref": "#/$defs/value"}
            for index in range(pc._GEMINI_MAX_REF_EXPANSIONS + 1)
        },
        "additionalProperties": False,
    }

    encoded = pc._adapt_tool_schema_gemini(parameters)

    assert encoded["properties"]["value_0"] == {"$ref": "#/$defs/value"}
    assert "$defs" in encoded
    assert "additionalProperties" not in encoded


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


def test_encode_tools_gemini_builtin_schema_bytes_match_golden():
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "gemini_builtin_schema_digests.json"
    )
    expected = json.loads(fixture_path.read_text())
    specs = build_tool_specs()
    wire = pc._encode_tools_gemini(specs)
    declarations = wire[0]["functionDeclarations"]
    schema_digests = {
        declaration["name"]: hashlib.sha256(
            _canonical_json_bytes(declaration["parameters"])
        ).hexdigest()
        for declaration in declarations
    }

    assert len(specs) == len(expected["schema_sha256"])
    assert {spec.name for spec in specs} == set(expected["schema_sha256"])
    assert schema_digests == expected["schema_sha256"]
    web_fetch_schema = next(
        declaration["parameters"]
        for declaration in declarations
        if declaration["name"] == "web_fetch"
    )
    assert _rejected_schema_keywords(web_fetch_schema) == set()
    assert web_fetch_schema["properties"]["offset"] == {"type": "integer"}
    # photo_read's source schema no longer carries the Gemini-invalid boolean
    # enum. Its already-sanitized native Gemini wire must remain byte-stable.
    assert schema_digests["photo_read"] == expected["schema_sha256"]["photo_read"]
    wire_bytes = _canonical_json_bytes(wire)
    assert len(wire_bytes) == expected["wire_bytes"]
    assert hashlib.sha256(wire_bytes).hexdigest() == expected["wire_sha256"]


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
