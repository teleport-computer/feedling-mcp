"""Tool-schema catalog (Plan C, Task 3 / C1).

Derives one `ToolSpec` per model-facing capability in `capabilities.registry.CAPABILITIES`
(everything except the internal-only `chat_image_read`, `chat_file_read`, and
`perception_glance`, which have no model-facing schema) plus the runtime-native `task`, `reply`, and
`provider_usage` tools.  The unified tool loop handles these specially instead
of dispatching them through the capability executor. `provider_usage` is
chat-lane only — it is deliberately absent from `worker._SUBAGENT_ALLOWED_TOOLS`
(so subagents never see it) and is always withheld from the wake/screen_watch/
manual_wake lane (see `worker._run_wake`); see Task 5's brief for why it is not
in `provenance.EXTERNAL_READS` but is in `worker._PRIVATE_READ_TOOLS`.

Each entry in `PARAMS` mirrors exactly the `params` fields each capability module reads —
see the module docstring/params usage cited per tool below. Do not add fields the
capability code does not consume; the executor will simply ignore them, but claiming they
exist misleads the model into fabricating structured junk.
"""
from __future__ import annotations

from provider_types import ToolSpec
from capabilities import registry

REPLY_TOOL = "reply"
FILE_REPLY_TOOL = "send_file"
TASK_TOOL = "task"
PROVIDER_USAGE_TOOL = "provider_usage"

_EXCLUDED = frozenset({"chat_image_read", "chat_file_read", "perception_glance"})

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_NO_ARGS: dict = {"type": "object", "properties": {}}

_MEMORY_TOOL_ACTION = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["add", "update", "delete"]},
        "summary": _STR,
        "content": _STR,
        "bucket": _STR,
        "target_id": _STR,
    },
    "required": ["op"],
    "additionalProperties": False,
}

PARAMS: dict[str, dict] = {
    # -- identity.py --
    # identity.get(store, ...): ignores params entirely.
    "identity_get": _NO_ARGS,
    # identity.patch(store, ...): params.get("patch") (dict), else falls back to
    # top-level agent_name/self_introduction/signature strings.
    "identity_patch": {
        "type": "object",
        "properties": {
            "patch": {"type": "object"},
            "agent_name": _STR,
            "self_introduction": _STR,
            "signature": _STR,
            # relationship_days is a FIRST-CLASS top-level arg (like agent_name),
            # NOT only reachable inside the free-form `patch` object. It used to
            # live only in `patch`, but with additionalProperties=False a model
            # that naturally called identity_patch(relationship_days=N) — the same
            # shape it uses for agent_name — got the arg rejected/dropped and then
            # falsely reported success. merge_patch_fields folds this top-level
            # value into the patch (patch wins on conflict), so both shapes work.
            "relationship_days": _INT,
        },
        "required": [],
    },
    # identity.nudge(store, ...): params.get("dimension") (str) + delta (int),
    # optional reason. Adjusts ONE existing dimension's score.
    "identity_nudge": {
        "type": "object",
        "properties": {
            "dimension": _STR,
            "delta": _INT,
            "reason": _STR,
        },
        "required": ["dimension", "delta"],
    },

    # -- memory.py (backed by memory_core.index/fetch/actions) --
    # memory.index(store, ...): payload passed through to memory_index_core; only
    # "limit" is inspected directly (memory_core.index reads payload.get("limit")).
    "memory_index": {
        "type": "object",
        "properties": {"limit": _INT},
        "required": [],
    },
    # memory.search(store, ...): params.get("query") (required, non-empty) + optional
    # limit (passed through like index).
    "memory_search": {
        "type": "object",
        "properties": {"query": _STR, "limit": _INT},
        "required": ["query"],
    },
    # memory.fetch(store, ...) -> memory_core.fetch: payload.get("ids") must be a
    # list of non-empty strings.
    "memory_fetch": {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": _STR}},
        "required": ["ids"],
    },
    # memory.write: the model emits PLAINTEXT cards; the worker's
    # `_memory_tool_actions` translates each into the server's memory-action shape
    # ({"type":"memory.add","memory":{summary,content,bucket,threads}}) and the
    # server builds the E2E envelope (it never sees a model-built envelope — see
    # worker._memory_tool_actions / _write_tool_effect_payload). The item schema is
    # explicit so the model supplies summary+content (required for add) rather than
    # guessing a shape the write path rejects with title_required/400.
    "memory_write": {
        "type": "object",
        "properties": {"actions": {"type": "array", "items": _MEMORY_TOOL_ACTION}},
        "required": ["actions"],
    },

    # -- perception.py (backed by agent/perception_core.py) --
    # perception.snapshot: params.get("signals") (list or csv string).
    "perception_snapshot": {
        "type": "object",
        "properties": {"signals": {"type": "array", "items": _STR}},
        "required": [],
    },
    # perception.trend: params.get("signal"), params.get("field"), params.get("days").
    "perception_trend": {
        "type": "object",
        "properties": {"signal": _STR, "field": _STR, "days": _INT},
        "required": [],
    },
    # perception.history: params.get("signal"), params.get("days").
    "perception_history": {
        "type": "object",
        "properties": {"signal": _STR, "days": _INT},
        "required": [],
    },

    # -- screen.py (backed by screen/screen_read_core.py) --
    # screen.recent: params.get("limit").
    "screen_recent": {
        "type": "object",
        "properties": {"limit": _INT},
        "required": [],
    },
    # screen.read: params.get("frame_id") (defaults to latest frame if absent),
    # params.get("include_image") (bool).
    "screen_read": {
        "type": "object",
        "properties": {"frame_id": _STR, "include_image": _BOOL},
        "required": [],
    },

    # -- photo.py (backed by perception/perception_read_core.py) --
    # photo.recent: params.get("limit").
    "photo_recent": {
        "type": "object",
        "properties": {"limit": _INT},
        "required": [],
    },
    # photo.read: params.get("photo_id") or params.get("id") (required),
    # params.get("include_image") (bool).
    "photo_read": {
        "type": "object",
        "properties": {"photo_id": _STR, "include_image": _BOOL},
        "required": ["photo_id"],
    },

    # -- web.py (keyless facade over model_api_runtime/tools.py) --
    # web.search: params: {"query": str, "limit": int?} — per module docstring.
    "web_search": {
        "type": "object",
        "properties": {"query": _STR, "limit": _INT},
        "required": ["query"],
    },
    # web.fetch: params: {"url": str} — per module docstring.
    "web_fetch": {
        "type": "object",
        "properties": {"url": _STR},
        "required": ["url"],
    },

    # -- wake.py (backed by proactive/scheduled_wake_v2.py) --
    # wake.schedule: params.get("at") (required, ISO-ish time string), optional
    # "tz" and "reason" strings. NOTE: the field is "at", not "when".
    "schedule_wake": {
        "type": "object",
        "properties": {"at": _STR, "tz": _STR, "reason": _STR},
        "required": ["at"],
    },
    # wake.cancel: params.get("wake_id") or params.get("id") (required), optional
    # "reason" string.
    "cancel_wake": {
        "type": "object",
        "properties": {"wake_id": _STR, "reason": _STR},
        "required": ["wake_id"],
    },

    # -- workspace.py (encrypted virtual workspace, never the host filesystem) --
    "workspace_list": {
        "type": "object",
        "properties": {"path": _STR, "recursive": _BOOL, "limit": _INT},
        "required": [],
    },
    "workspace_read": {
        "type": "object",
        "properties": {"path": _STR, "start_line": _INT, "line_count": _INT},
        "required": ["path"],
    },
    "workspace_write": {
        "type": "object",
        "properties": {
            "path": _STR,
            "content": _STR,
            "expected_revision": _INT,
        },
        "required": ["path", "content", "expected_revision"],
    },
    "workspace_delete": {
        "type": "object",
        "properties": {"path": _STR, "expected_revision": _INT},
        "required": ["path", "expected_revision"],
    },

    # -- synthetic bounded-subagent tool --
    # Overlay workspace merges are intentionally not advertised until the
    # worker has an isolated overlay + explicit CAS merge implementation.
    TASK_TOOL: {
        "type": "object",
        "properties": {
            "prompt": _STR,
            "label": _STR,
            "workspace_mode": {
                "type": "string",
                "enum": ["read_only"],
            },
        },
        "required": ["prompt"],
    },

    # -- synthetic reply tool --
    REPLY_TOOL: {
        "type": "object",
        "properties": {"text": _STR},
        "required": ["text"],
    },
    # Explicitly publish one file that already exists in the encrypted V2
    # workspace. The worker resolves the path for the current user; this never
    # accepts a host filesystem path.
    FILE_REPLY_TOOL: {
        "type": "object",
        "properties": {"path": _STR, "revision": _INT},
        "required": ["path", "revision"],
    },

    # -- runtime-native provider_usage tool (chat-lane only; see worker.py
    # _SUBAGENT_ALLOWED_TOOLS / _PRIVATE_READ_TOOLS / _run_wake disabled set) --
    PROVIDER_USAGE_TOOL: {"type": "object", "properties": {}, "additionalProperties": False},
}

# Tool arguments cross an untrusted model boundary.  Close every model-facing
# top-level object even when a provider ignores JSON Schema's
# ``additionalProperties`` keyword; ``validate_tool_args`` below enforces the
# same contract again before a capability is run or a write effect is queued.
# The identity ``patch`` object remains capability-owned/free-form. Memory
# actions are closed explicitly above so the provider sees the model-facing
# plaintext vocabulary instead of inventing shapes the sink will later reject.
for _parameters in PARAMS.values():
    if _parameters.get("type") == "object":
        _parameters["additionalProperties"] = False

DESCRIPTIONS: dict[str, str] = {
    "identity_get": "Read the persona's current identity/profile fields.",
    "identity_patch": ("Update the persona's own identity/profile. Use 'agent_name' "
                       "when the user renames you — that is the name shown to them, "
                       "and rewriting only 'self_introduction' does NOT rename you; "
                       "pass both when the new name should also appear in how you "
                       "introduce yourself. Put any of these inside a 'patch' object: "
                       "string fields agent_name, self_introduction, category, "
                       "user_preferred_name, agent_role, tone_style, "
                       "custom_persona_prompt, language_preference, relationship_anchor; "
                       "and list fields signature, boundaries, do_not_say, "
                       "stable_definitions. Edit a list field by whole-list replacement "
                       "or with op keys add_<field>/remove_<field>/replace_<field> "
                       "(e.g. add_signature, remove_boundaries). To recalibrate how "
                       "long you and the user have known each other, set the "
                       "'relationship_days' argument directly — e.g. identity_patch(relationship_days=300). "
                       "This is the day number AS THE USER SEES AND SAYS IT (the '第 N 天' shown "
                       "in the app: the day you met is day 1, not day 0). So if the user says "
                       "'make it day 45', pass 45 and the app will show 45. Whole number, "
                       "day 1 = the day you met. Only act on an explicit request."),
    "identity_nudge": ("Adjust ONE of the persona's relationship/personality dimension "
                       "scores by a signed integer 'delta' (|delta| ≤ 10). 'dimension' "
                       "must name a dimension that already exists — call identity_get "
                       "first to see the current dimensions and values. Optional "
                       "'reason'. To add or rename dimensions, use identity_patch."),
    "memory_index": ("List recent memory cards, optionally capped by limit. Use this "
                     "once for an open-ended overview such as all memories or an "
                     "overall relationship summary; do not pair it with or repeat "
                     "memory_search in the same turn."),
    "memory_search": ("Keyword-search memory cards by a required query string. Use "
                      "this only when the user asks about a specific remembered "
                      "subject, person, phrase, or event. Never use it for ordinary "
                      "conversation, model/runtime identity, or an all-memory "
                      "overview, and do not repeat it after one discovery result."),
    "memory_fetch": "Fetch specific memory cards by their ids.",
    "memory_write": ("Write, update, or delete memory cards. Each action needs an "
                     "'op': 'add' (supply a one-line 'summary' AND full 'content', "
                     "optional 'bucket'), 'update' (supply 'target_id' plus new "
                     "'summary'/'content'), or 'delete' (supply 'target_id'). Get "
                     "target_ids from memory_search/memory_index first."),
    "perception_snapshot": "Read the latest perception snapshot for the given signals.",
    "perception_trend": "Read a trend summary for a perception signal over recent days.",
    "perception_history": "Read raw historical values for a perception signal over recent days.",
    "screen_recent": "List recent screen-share frame metadata.",
    "screen_read": "Read (decrypt) a specific screen-share frame, or the latest one if no frame_id is given.",
    "photo_recent": "List recent photos, optionally capped by limit.",
    "photo_read": "Read a specific photo by id, optionally including its decrypted image.",
    "web_search": "Search the live public web for current information such as news, weather, prices, or recent events, or anything past your training data that you are not sure is current. Prefer this over guessing or telling the user you cannot access the internet.",
    "web_fetch": "Fetch a specific URL and return its main text content. Use when the user provides a link, or to read a page found through web_search.",
    "schedule_wake": "Schedule a future self-wake at a given time, with optional timezone and reason.",
    "cancel_wake": "Cancel a previously scheduled self-wake by its wake_id.",
    "workspace_list": ("List encrypted virtual workspace entries and revisions. "
                       "Namespaces are /artifacts (read-only), /skills (read-only), "
                       "/workspace (editable), and /memory/WORKING.md (editable)."),
    "workspace_read": ("Read a line range from a virtual text entry. This reads a "
                       "stored text view and does not materialize a physical artifact."),
    "workspace_write": ("Create or replace editable UTF-8 source using optimistic "
                        "revision control. For a downloadable .docx or .pdf target, "
                        "write clear Markdown-like source at that exact target path; "
                        "send_file renders the binary document. Use expected_revision=0 "
                        "to create. Generated files must use /workspace/<filename>; "
                        "never write them under /artifacts or /skills because those "
                        "namespaces are read-only."),
    "workspace_delete": ("Delete an editable virtual file at its exact revision. "
                         "Artifacts and skills cannot be deleted by the model."),
    TASK_TOOL: ("Run a bounded isolated subagent on one focused task. The child can "
                "read workspace/artifact, memory, and web data but cannot reply to "
                "the user, mutate state, call MCP, or spawn another task."),
    REPLY_TOOL: "Send an immediate reply bubble to the user with the given text.",
    FILE_REPLY_TOOL: (
        "Deliver an existing /workspace source as a downloadable attachment. "
        "Plain-text formats are sent directly; .docx and .pdf targets are rendered "
        "into real Word/PDF bytes. Preserve any format the user explicitly requested: "
        "Word means .docx and PDF means .pdf, and never replace either with Markdown. "
        "Choose this tool from the user's meaning, not from exact "
        "keywords, wording, language, or a named extension: use it whenever the "
        "requested result is meant to be a reusable standalone artifact they can "
        "save, open, download, share, or use outside the chat. If format or name "
        "is omitted, infer a useful text format and safe filename yourself; the "
        "user never needs to know /workspace or provide an internal path. Create "
        "or update the file with workspace_write first, wait for that tool result, "
        "then call send_file in a later round with the exact returned revision. "
        "Do not call this merely because a conversational answer contains a list "
        "or structured text. Host filesystem paths and /artifacts, /skills, or "
        "/memory entries are not accepted."
    ),
    PROVIDER_USAGE_TOOL: (
        "查询当前 AI 服务商账户的余额与用量（只读）。仅在用户明确询问余额、用量、"
        "还剩多少钱时调用；结果如实转述，查不到就说查不到。"
    ),
}


def _matches_json_type(value, expected: str) -> bool:
    """Small JSON-Schema type predicate for the schema subset used above."""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        # bool is an int subclass in Python, but a distinct JSON type.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _validate_value(value, schema: dict, *, path: str) -> str | None:
    """Validate the deliberately small JSON-Schema subset used by ``PARAMS``.

    Returning a stable error instead of raising lets the dispatcher feed a
    bounded tool error back to the model without exposing argument values.
    """
    expected = schema.get("type")
    if expected and not _matches_json_type(value, expected):
        return f"{path} must be {expected}"
    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        return f"{path} has unsupported value"

    if expected == "object":
        required = schema.get("required") or []
        for field in required:
            if field not in value:
                return f"missing required field: {field}"

        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(field) for field in value if field not in properties)
            if unknown:
                return f"unknown field: {unknown[0]}"

        for field, child_schema in properties.items():
            if field in value:
                error = _validate_value(value[field], child_schema, path=f"{path}.{field}")
                if error:
                    return error

    if expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            error = _validate_value(item, schema["items"], path=f"{path}[{index}]")
            if error:
                return error

    return None


def validate_tool_args(name: str, args) -> str | None:
    """Return ``None`` when args satisfy a model-facing tool's schema.

    This is intentionally dependency-free and covers the complete schema
    vocabulary currently emitted by this module: object/array/scalar types,
    required fields, array items, and closed top-level objects.
    """
    schema = PARAMS.get(name)
    if schema is None:
        return "tool is not model-facing"
    error = _validate_value(args, schema, path="args")
    if error:
        return error
    if name == "identity_patch":
        # Run the SAME merge the capability runs, so a call that validates here can't
        # quietly lose a field later. Import direction is safe: registry already pulls
        # capabilities.identity, and tool_schema imports registry.
        # Same normalization the capability applies, so the pre-enqueue emptiness
        # check and the actual payload can't disagree. It never rejects: this
        # function also gates replay of already-persisted effects, where a new
        # rejection rule would turn a legal-when-written payload into a retry loop
        # (see capabilities.identity.merge_patch_fields).
        from capabilities import identity as cap_identity
        merged = cap_identity.merge_patch_fields(args)
        # Emptiness gate == origin/test baseline, with EXACTLY ONE addition:
        # relationship_days counts by PRESENCE of the key (round-4). Rationale:
        #   * relationship_days -> present == content (0 = "we met today" is valid;
        #     null/False must still pass this gate and hit the live pre-enqueue gate
        #     (capabilities.identity.relationship_days_error) so the model gets a
        #     stable, self-correctable error instead of a silent empty-patch drop).
        #     Its VALIDITY (int/cap) is enforced there + at the server gate, never
        #     here — this function ALSO gates replay of already-persisted effects,
        #     where any NEW rejection rule would turn a legal-when-written effect
        #     into a retry loop.
        #   * every other field -> identical to origin/test: a str counts when
        #     non-blank after strip, everything else by bool(value). So bool([])
        #     is False — a bare `{"signature": []}` / `{"category": []}` /
        #     `{"unknown": []}` / `{"signature": null}` is empty, exactly as on
        #     baseline (round-4 reverts the round-2 "any list is content" widening,
        #     which was the Important-3 hole where signature:null passed this gate
        #     and then died at the sink as a fake success). RESULT: zero new
        #     rejections vs origin/test — the replay gate can never judge an old
        #     effect empty that baseline would have accepted.
        def _has_content(key: str, value) -> bool:
            if key == "relationship_days":
                return "relationship_days" in merged
            if isinstance(value, str):
                return bool(value.strip())
            return bool(value)
        if not any(_has_content(k, v) for k, v in merged.items()):
            return "identity_patch requires a non-empty patch or profile field"
    if name == "memory_write":
        actions = args.get("actions") or []
        if not actions:
            return "memory_write requires at least one action"
        for index, action in enumerate(actions):
            op = action["op"]
            summary = str(action.get("summary") or "").strip()
            content = str(action.get("content") or "").strip()
            target_id = str(action.get("target_id") or "").strip()
            if op == "add" and (not summary or not content):
                return f"args.actions[{index}] add requires summary and content"
            if op == "update" and (not target_id or not summary or not content):
                return f"args.actions[{index}] update requires target_id, summary, and content"
            if op == "delete" and not target_id:
                return f"args.actions[{index}] delete requires target_id"
    if name in {"workspace_write", "workspace_delete"}:
        revision = args.get("expected_revision")
        if type(revision) is not int or revision < 0:
            return "expected_revision must be a non-negative integer"
    if name == TASK_TOOL and not str(args.get("prompt") or "").strip():
        return "task requires a non-empty prompt"
    if name == FILE_REPLY_TOOL and not str(args.get("path") or "").strip():
        return "send_file requires a non-empty path"
    if name == FILE_REPLY_TOOL and (
        type(args.get("revision")) is not int or args["revision"] <= 0
    ):
        return "send_file revision must be a positive integer"
    return None


def build_tool_specs() -> list[ToolSpec]:
    specs = []
    for name in registry.CAPABILITIES:
        if name in _EXCLUDED:
            continue
        specs.append(ToolSpec(name=name, description=DESCRIPTIONS[name], parameters=PARAMS[name]))
    for name in (TASK_TOOL, REPLY_TOOL, FILE_REPLY_TOOL, PROVIDER_USAGE_TOOL):
        specs.append(ToolSpec(
            name=name,
            description=DESCRIPTIONS[name],
            parameters=PARAMS[name],
        ))
    return specs
