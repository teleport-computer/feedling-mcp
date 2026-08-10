import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import tool_schema, registry
from provider_types import ToolSpec


def test_catalog_covers_capabilities_plus_synthetic_tools_minus_internal_actions():
    specs = tool_schema.build_tool_specs()
    names = {s.name for s in specs}
    assert "reply" in names
    assert "task" in names
    assert "chat_image_read" not in names   # BUG-1 mitigation
    assert "chat_file_read" not in names    # internal-only, never offered to the model
    assert "perception_glance" not in names  # proactive-runtime only
    for cap in registry.CAPABILITIES:
        if cap in ("chat_image_read", "chat_file_read", "perception_glance"):
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


def test_memory_index_exposes_partition_and_sensitivity_filters_without_offset():
    spec = next(
        item for item in tool_schema.build_tool_specs() if item.name == "memory_index"
    )

    assert set(spec.parameters["properties"]) == {
        "limit", "bucket", "thread", "ambient", "include_sensitive"
    }
    assert "offset" not in spec.parameters["properties"]
    assert tool_schema.validate_tool_args(
        "memory_index", {"bucket": "旅行", "thread": "京都"}
    ) is None
    assert "memory_search" in spec.description
    assert "memory_organize" in spec.description


def test_recent_apps_and_memory_fetch_parity_parameters_are_model_facing():
    specs = {item.name: item for item in tool_schema.build_tool_specs()}
    assert set(specs["perception_recent_apps"].parameters["properties"]) == {
        "limit", "hours"
    }
    assert set(specs["memory_fetch"].parameters["properties"]) == {
        "ids", "limit", "include_archived", "include_superseded"
    }
    assert tool_schema.validate_tool_args(
        "memory_fetch",
        {
            "ids": ["m1"],
            "limit": 20,
            "include_archived": True,
            "include_superseded": False,
        },
    ) is None


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
    # Replay validation remains backward-compatible for an already-enqueued old call.
    assert tool_schema.validate_tool_args("identity_patch", {"agent_name": "老6"}) is None
    # Live model validation applies D4 before enqueue so the model can self-correct.
    assert "self_introduction" in tool_schema.validate_tool_args(
        "identity_patch", {"agent_name": "老6"}, live_model_call=True
    )
    assert tool_schema.validate_tool_args(
        "identity_patch",
        {"agent_name": "老6", "self_introduction": "我是老6"},
        live_model_call=True,
    ) is None


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


def test_identity_patch_advertises_and_accepts_relationship_days():
    # The model can only recalibrate the day count if the tool description says how.
    spec = next(s for s in tool_schema.build_tool_specs() if s.name == "identity_patch")
    assert "relationship_days" in spec.description
    # relationship_days inside the open `patch` object validates through the gate,
    # including the 0 edge ("we met today") which must NOT read as an empty patch.
    assert tool_schema.validate_tool_args(
        "identity_patch", {"patch": {"relationship_days": 300}}) is None
    assert tool_schema.validate_tool_args(
        "identity_patch", {"patch": {"relationship_days": 0}}) is None


def test_identity_patch_empty_gate_matches_baseline():
    # Round-4: the empty-patch gate is EXACTLY origin/test's semantics plus the
    # relationship_days presence rule — nothing else. A bare `[]`/`null` is falsy,
    # so it reads as empty regardless of field (round-4 reverts the round-2 "any
    # list is content" widening, the Important-3 hole where signature:null passed
    # this gate and then died at the sink as a fake success).
    V = tool_schema.validate_tool_args
    # unknown key with [] -> empty
    assert V("identity_patch", {"patch": {"unknown": []}}) is not None
    # string field with [] -> empty
    assert V("identity_patch", {"patch": {"category": []}}) is not None
    # a real list field with [] -> ALSO empty now (baseline behavior, bool([]) False)
    assert V("identity_patch", {"patch": {"signature": []}}) is not None
    # a real list field with null -> empty (the Important-3 signature:null case)
    assert V("identity_patch", {"patch": {"signature": None}}) is not None
    # a real list field WITH content -> passes (normal non-empty patch untouched)
    assert V("identity_patch", {"patch": {"signature": ["sig"]}}) is None


def test_identity_patch_relationship_days_null_reaches_live_gate():
    # Round-3 fix: relationship_days keys off PRESENCE, not `value is not None`,
    # so null/False are NOT swallowed as an empty patch — they pass the empty
    # gate and hit the live pre-enqueue gate, which returns a stable error the
    # model can self-correct from (never a silent enqueue).
    from capabilities import identity as cap_identity
    V = tool_schema.validate_tool_args
    # not treated as empty (empty gate returns None -> would proceed to live gate)
    assert V("identity_patch", {"patch": {"relationship_days": None}}) is None
    assert V("identity_patch", {"patch": {"relationship_days": False}}) is None
    # 0 = "we met today" is a valid, non-empty patch
    assert V("identity_patch", {"patch": {"relationship_days": 0}}) is None
    # the live gate gives null/False a STABLE error (so they never enqueue)
    assert cap_identity.relationship_days_error(
        {"patch": {"relationship_days": None}}) == "relationship_days_must_be_non_negative_int"
    assert cap_identity.relationship_days_error(
        {"patch": {"relationship_days": False}}) == "relationship_days_must_be_non_negative_int"


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
    assert "only one method" in d
    assert "D4" in d


def test_memory_write_reason_and_relative_wake_are_described_and_valid():
    specs = {item.name: item for item in tool_schema.build_tool_specs()}
    assert "reason" in specs["memory_write"].parameters["properties"]["actions"]["items"]["properties"]
    assert tool_schema.validate_tool_args(
        "memory_write",
        {"actions": [{
            "op": "delete", "target_id": "m1", "reason": "user corrected this"
        }]},
    ) is None
    assert "in 2 hours" in specs["schedule_wake"].description


def test_memory_search_shares_the_index_filters_it_already_consumes():
    """memory_search 和 memory_index 共用 memory_index_core,那个 core 一直在消费
    bucket / thread / include_sensitive —— index 的 schema 开了,search 漏了。

    后果:搜索不能限定在某个桶或某条线索里,只能拿回宽泛结果自己挑。
    V1 的 `memory-index --query` 本来可以组合这些条件,V2 拆成 search 之后丢了。

    ambient 刻意不开:search 强制带 query,走 exact-query 分支,ambient_top_n
    不参与候选限制(Codex 2026-08-10 核实),开了也是个哑参数。
    """
    spec = next(
        item for item in tool_schema.build_tool_specs() if item.name == "memory_search"
    )

    assert set(spec.parameters["properties"]) == {
        "query", "limit", "bucket", "thread", "include_sensitive"
    }
    assert tool_schema.validate_tool_args(
        "memory_search",
        {"query": "骑行", "bucket": "爱好", "thread": "自行车", "include_sensitive": True},
    ) is None


def test_identity_patch_description_names_the_irregular_replace_key():
    """描述教模型 `replace_<field>`,list 字段列的是 "signature"(单数),
    模型自然推出 `replace_signature` —— 而实现唯一认的是 `replace_signatures`(复数)。

    未知 key 不会报错,会走到 identity/actions.py 的 `changed` 为空分支,
    返回 200 + noop:true。于是模型以为改成功了,回头告诉用户「签名改好了」,
    实际什么都没动(Codex 2026-08-10 发现,我读代码确认)。

    这里断言的是描述里**逐字**出现真实 key,而不是去扫描自然语言猜字段名 ——
    后者会把示例和普通名词误当字段(Codex 明确反对做通用扫描)。
    """
    from identity.actions import _LIST_OP_FIELDS

    desc = tool_schema.DESCRIPTIONS["identity_patch"]
    real_keys = {keys[2] for keys in _LIST_OP_FIELDS.values()}
    irregular = {k for k in real_keys if not k.startswith("replace_") or k[len("replace_"):] not in _LIST_OP_FIELDS}

    assert "replace_signatures" in irregular, "前提变了:signature 不再是不规则例外"
    for key in irregular:
        assert key in desc, f"描述必须逐字点名不规则的 {key},否则模型会按规则推错"
