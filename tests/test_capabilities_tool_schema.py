import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import tool_schema, registry
from provider_types import ToolSpec


def test_catalog_covers_capabilities_plus_reply_minus_chat_image():
    specs = tool_schema.build_tool_specs()
    names = {s.name for s in specs}
    assert "reply" in names
    assert "chat_image_read" not in names   # BUG-1 mitigation
    for cap in registry.CAPABILITIES:
        if cap == "chat_image_read":
            continue
        assert cap in names, f"missing tool: {cap}"
    assert all(isinstance(s, ToolSpec) for s in specs)


def test_reply_tool_schema_shape():
    reply = next(s for s in tool_schema.build_tool_specs() if s.name == "reply")
    assert reply.parameters["required"] == ["text"]
    assert reply.parameters["properties"]["text"]["type"] == "string"


def test_write_tools_have_object_params():
    specs = {s.name: s for s in tool_schema.build_tool_specs()}
    for w in ("memory_write", "identity_patch", "schedule_wake"):
        assert specs[w].parameters["type"] == "object"
