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


def test_identity_patch_exposes_agent_name_so_a_rename_is_discoverable():
    """The model can only rename the persona if the tool says it can.

    agent_name was reachable only by hand-building a `patch` object — nothing in
    the schema or the description mentioned it. Asked to rename itself the model
    did the discoverable thing instead (rewrote self_introduction), so the app
    kept showing the old name while the agent said it was done.
    """
    spec = next(s for s in tool_schema.build_tool_specs() if s.name == "identity_patch")
    assert spec.parameters["properties"]["agent_name"] == {"type": "string"}
    assert "agent_name" in spec.description
    # top-level agent_name alone is a complete, valid call
    assert tool_schema.validate_tool_args("identity_patch", {"agent_name": "老6"}) is None


def test_identity_patch_validator_accepts_every_shape_the_old_code_accepted():
    """validate_tool_args also gates REPLAY of already-persisted effects.

    serve_worker validates a decrypted effect through this same function, and the
    outbox only terminal-discards an EffectTerminalError — a plain RuntimeError
    (which is what a validation failure becomes there) is treated as retryable. So
    any shape a pre-upgrade worker could legally enqueue must still validate, or a
    rolling upgrade turns those queued effects into infinite retries.

    The pre-change code took `patch` whole and ignored top-level fields, so a
    payload carrying BOTH — including the same key twice — was legal and is
    reachable in an existing outbox row.
    """
    for shape in (
        {"agent_name": "老6"},
        {"agent_name": "老6", "patch": {"self_introduction": "我是老6"}},
        {"agent_name": "老6", "patch": {"agent_name": "老6"}},
        {"agent_name": "老6", "patch": {"agent_name": "老七"}},       # 旧语义：patch 胜出
        {"self_introduction": "top", "patch": {"self_introduction": "nested"}},
    ):
        assert tool_schema.validate_tool_args("identity_patch", shape) is None, shape


def test_identity_patch_validator_still_rejects_empty_calls():
    assert tool_schema.validate_tool_args("identity_patch", {}) is not None
    assert tool_schema.validate_tool_args("identity_patch", {"agent_name": "   "}) is not None


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


def test_identity_nudge_is_model_facing_and_requires_dimension_and_delta():
    spec = next(s for s in tool_schema.build_tool_specs() if s.name == "identity_nudge")
    assert set(spec.parameters["required"]) == {"dimension", "delta"}
    assert tool_schema.validate_tool_args("identity_nudge", {"dimension": "warmth", "delta": 2}) is None
    assert tool_schema.validate_tool_args("identity_nudge", {"dimension": "warmth"}) == "missing required field: delta"
    # bool is a JSON boolean, not an integer
    assert tool_schema.validate_tool_args("identity_nudge", {"dimension": "warmth", "delta": True}) is not None
    assert "unknown field" in tool_schema.validate_tool_args(
        "identity_nudge", {"dimension": "warmth", "delta": 1, "x": 1})


def test_identity_patch_description_advertises_list_fields_and_ops():
    spec = next(s for s in tool_schema.build_tool_specs() if s.name == "identity_patch")
    d = spec.description
    # the add_/remove_/replace_ list-op convention must be discoverable
    assert "add_" in d and "remove_" in d and "replace_" in d
    # at least one of the newly-reachable list fields is named
    assert "boundaries" in d
