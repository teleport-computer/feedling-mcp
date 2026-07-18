import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import tool_schema, registry
from provider_types import ToolSpec


def test_catalog_covers_capabilities_plus_synthetic_tools_minus_internal_reads():
    specs = tool_schema.build_tool_specs()
    names = {s.name for s in specs}
    assert "reply" in names
    assert "task" in names
    assert "chat_image_read" not in names   # BUG-1 mitigation
    assert "chat_file_read" not in names    # internal-only, never offered to the model
    for cap in registry.CAPABILITIES:
        if cap in ("chat_image_read", "chat_file_read"):
            continue
        assert cap in names, f"missing tool: {cap}"
    assert all(isinstance(s, ToolSpec) for s in specs)


def test_reply_tool_schema_shape():
    reply = next(s for s in tool_schema.build_tool_specs() if s.name == "reply")
    assert reply.parameters["required"] == ["text"]
    assert reply.parameters["properties"]["text"]["type"] == "string"


def test_task_tool_is_read_only_and_requires_a_nonempty_prompt():
    task = next(s for s in tool_schema.build_tool_specs() if s.name == "task")
    assert task.parameters["required"] == ["prompt"]
    assert task.parameters["properties"]["workspace_mode"]["enum"] == [
        "read_only"
    ]
    assert tool_schema.validate_tool_args(
        "task", {"prompt": "inspect the artifact"}
    ) is None
    assert tool_schema.validate_tool_args(
        "task", {"prompt": "   "}
    ) == "task requires a non-empty prompt"
    assert "unsupported value" in tool_schema.validate_tool_args(
        "task",
        {"prompt": "edit it", "workspace_mode": "overlay"},
    )


def test_write_tools_have_object_params():
    specs = {s.name: s for s in tool_schema.build_tool_specs()}
    for w in ("memory_write", "identity_patch", "schedule_wake", "workspace_write"):
        assert specs[w].parameters["type"] == "object"


def test_all_model_facing_tools_reject_unknown_top_level_fields():
    for spec in tool_schema.build_tool_specs():
        assert spec.parameters["additionalProperties"] is False, spec.name


def test_server_validation_covers_required_types_unknowns_and_array_items():
    assert tool_schema.validate_tool_args("web_search", {}) == "missing required field: query"
    assert tool_schema.validate_tool_args("web_search", {"query": 42}) == "args.query must be string"
    assert tool_schema.validate_tool_args("memory_index", {"limit": True}) == "args.limit must be integer"
    assert tool_schema.validate_tool_args("identity_get", {"unused": "x"}) == "unknown field: unused"
    assert tool_schema.validate_tool_args("memory_fetch", {"ids": ["ok", 2]}) == "args.ids[1] must be string"
    assert tool_schema.validate_tool_args("schedule_wake", {"at": "tomorrow"}) is None
