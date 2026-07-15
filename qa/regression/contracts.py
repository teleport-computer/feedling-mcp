"""Versioned, dependency-free contracts for persona and memory regression.

The checked-in JSON documents are intentionally small and boring: exact
top-level keys, explicit schema versions, and canonical SHA-256 fingerprints.
Semantic payloads such as rubrics and memory facts remain JSON objects so the
evaluation logic can evolve without pulling a framework into the harness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_FAILURE_RE = re.compile(r"^(?:NONE|[A-Z][A-Z0-9_]{0,63})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ROLES = frozenset({"user", "assistant", "system", "tool"})
_BOUNDARIES = frozenset({"none", "clear_history", "rotate_runtime_session"})
_TRANSITIONS = frozenset(
    {"always", "contains", "not_contains", "equals", "regex", "contains_any", "contains_all"}
)
_TRAJECTORY_STATUSES = frozenset(
    {"COMPLETED", "INFRA_ERROR", "BLOCKED_EVIDENCE"}
)
_METRIC_STATUSES = frozenset({"PASS", "FAIL", "INFRA_ERROR", "BLOCKED_EVIDENCE", "SKIP"})
_RESULT_STATUSES = frozenset({"PASS", "FAIL", "INFRA_ERROR", "BLOCKED_EVIDENCE"})
_EVALUATOR_TYPES = frozenset({"DETERMINISTIC", "LLM_JUDGE", "HUMAN"})
_TARGET_LABELS = frozenset({"baseline", "candidate"})


class ContractError(ValueError):
    """A contract is malformed, ambiguous, or not canonical JSON."""


def _fail(path: str, message: str) -> None:
    raise ContractError(f"{path}: {message}")


def _schema(value: Any) -> None:
    if value != SCHEMA_VERSION or isinstance(value, bool):
        _fail("$.schema_version", f"must equal {SCHEMA_VERSION}")


def _json_copy(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(path, "number must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, "object keys must be strings")
            result[key] = _json_copy(item, f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_copy(item, f"{path}[{index}]") for index, item in enumerate(value)]
    _fail(path, "value is not JSON-compatible")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical encoding used by all fixture fingerprints."""

    copied = _json_copy(value)
    try:
        return json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ContractError("$: value cannot be canonically encoded") from None


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: Any, path: str, *, nonempty: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "must be an object")
    result = _json_copy(value, path)
    if nonempty and not result:
        _fail(path, "must not be empty")
    return result


def _strings(value: Any, path: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail(path, "must be an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _fail(f"{path}[{index}]", "must be a non-empty string")
        result.append(item)
    if nonempty and not result:
        _fail(path, "must not be empty")
    if len(set(result)) != len(result):
        _fail(path, "must not contain duplicates")
    return tuple(result)


def _id(value: Any, path: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail(path, "must be a safe non-empty identifier")
    return value


def _text(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(path, "must be a non-empty string")
    return value


def _optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _text(value, path)


def _sha(value: Any, path: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(path, "must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, path: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        _fail(path, "must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(path, "must be an RFC3339 timestamp")
    if parsed.tzinfo is None:
        _fail(path, "must include a timezone")
    return value


def _strict(data: Any, kind: str, required: set[str], optional: set[str] = set()) -> dict[str, Any]:
    row = _mapping(data, "$")
    if row.get("schema_version") != SCHEMA_VERSION or isinstance(row.get("schema_version"), bool):
        _fail("$.schema_version", f"must equal {SCHEMA_VERSION}")
    if "kind" in row and row["kind"] != kind:
        _fail("$.kind", f"must equal {kind!r}")
    allowed = required | optional | {"schema_version", "kind"}
    unknown = sorted(set(row) - allowed)
    missing = sorted(required - set(row))
    if unknown:
        _fail("$", f"unknown keys: {', '.join(unknown)}")
    if missing:
        _fail("$", f"missing keys: {', '.join(missing)}")
    return row


class _Contract:
    KIND: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def fingerprint_sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())


@dataclass(frozen=True, slots=True, kw_only=True)
class Transition(_Contract):
    target_turn_id: str | None
    condition: str = "always"
    value: Any = ""
    case_sensitive: bool = False
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "transition"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        if self.target_turn_id is not None:
            _id(self.target_turn_id, "$.target_turn_id")
        if self.condition not in _TRANSITIONS:
            _fail("$.condition", "is unsupported")
        if type(self.case_sensitive) is not bool:
            _fail("$.case_sensitive", "must be a boolean")
        copied = _json_copy(self.value, "$.value")
        if self.condition == "always" and copied not in ("", None):
            _fail("$.value", "must be empty for an always transition")
        if self.condition in {"contains_any", "contains_all"}:
            copied = list(_strings(copied, "$.value", nonempty=True))
        elif self.condition != "always" and (not isinstance(copied, str) or not copied):
            _fail("$.value", "must be a non-empty string")
        if self.condition == "regex":
            try:
                re.compile(copied)
            except re.error:
                _fail("$.value", "must be a valid regular expression")
        object.__setattr__(self, "value", copied)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.KIND,
            "target_turn_id": self.target_turn_id,
            "condition": self.condition,
            "value": _json_copy(self.value),
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Transition":
        row = _strict(data, cls.KIND, {"target_turn_id", "condition", "value", "case_sensitive"})
        return cls(**{key: row[key] for key in ("target_turn_id", "condition", "value", "case_sensitive")})


@dataclass(frozen=True, slots=True, kw_only=True)
class Turn(_Contract):
    turn_id: str
    content: str
    role: str = "user"
    session_key: str = "default"
    boundary_before: str = "none"
    requirements: tuple[str, ...] = ()
    transitions: tuple[Transition, ...] = ()
    next_turn_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "turn"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _id(self.turn_id, "$.turn_id")
        _text(self.content, "$.content")
        if self.role not in _ROLES:
            _fail("$.role", "is unsupported")
        _id(self.session_key, "$.session_key")
        if self.boundary_before not in _BOUNDARIES:
            _fail("$.boundary_before", "is unsupported")
        object.__setattr__(self, "requirements", _strings(self.requirements, "$.requirements"))
        converted_rows: list[Transition] = []
        for item in self.transitions:
            if isinstance(item, Transition):
                converted_rows.append(item)
                continue
            nested = dict(item)
            nested.setdefault("schema_version", SCHEMA_VERSION)
            nested.setdefault("kind", Transition.KIND)
            nested.setdefault("condition", "always")
            nested.setdefault("value", "")
            nested.setdefault("case_sensitive", False)
            converted_rows.append(Transition.from_dict(nested))
        converted = tuple(converted_rows)
        if sum(item.condition == "always" for item in converted) > 1:
            _fail("$.transitions", "may contain at most one always transition")
        object.__setattr__(self, "transitions", converted)
        if self.next_turn_id is not None:
            _id(self.next_turn_id, "$.next_turn_id")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.KIND,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "session_key": self.session_key,
            "boundary_before": self.boundary_before,
            "requirements": list(self.requirements),
            "transitions": [item.to_dict() for item in self.transitions],
            "next_turn_id": self.next_turn_id,
            "metadata": _json_copy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Turn":
        required = {"turn_id", "role", "content", "session_key", "boundary_before", "requirements", "metadata"}
        optional = {"transitions", "next_turn_id"}
        row = _strict(data, cls.KIND, required, optional)
        return cls(
            turn_id=row["turn_id"], role=row["role"], content=row["content"],
            session_key=row["session_key"], boundary_before=row["boundary_before"],
            requirements=tuple(row["requirements"]), transitions=tuple(row.get("transitions", ())),
            next_turn_id=row.get("next_turn_id"), metadata=row["metadata"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenPersona(_Contract):
    persona_id: str
    persona_version: str
    display_name: str
    role: str
    traits: tuple[str, ...]
    tone_style: tuple[str, ...]
    behavioral_invariants: tuple[str, ...]
    do_not_say: tuple[str, ...]
    boundaries: tuple[str, ...]
    signature_phrases: tuple[str, ...]
    relationship: Mapping[str, Any]
    memory_facts: tuple[Mapping[str, Any], ...]
    rubric: Mapping[str, Any]
    source_fixture_id: str
    source_fixture_sha256: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "golden_persona"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _id(self.persona_id, "$.persona_id")
        _id(self.persona_version, "$.persona_version")
        _text(self.display_name, "$.display_name")
        _text(self.role, "$.role")
        for name in ("traits", "tone_style", "behavioral_invariants", "do_not_say", "boundaries", "signature_phrases"):
            object.__setattr__(self, name, _strings(getattr(self, name), f"$.{name}"))
        object.__setattr__(self, "relationship", _mapping(self.relationship, "$.relationship"))
        facts = self.memory_facts
        if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes, bytearray)):
            _fail("$.memory_facts", "must be an array of objects")
        object.__setattr__(self, "memory_facts", tuple(_mapping(item, f"$.memory_facts[{index}]", nonempty=True) for index, item in enumerate(facts)))
        object.__setattr__(self, "rubric", _mapping(self.rubric, "$.rubric", nonempty=True))
        _id(self.source_fixture_id, "$.source_fixture_id")
        _sha(self.source_fixture_sha256, "$.source_fixture_sha256")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    @property
    def agent_name(self) -> str:
        return self.display_name

    @property
    def invariants(self) -> tuple[str, ...]:
        return self.behavioral_invariants

    @property
    def signature(self) -> tuple[str, ...]:
        return self.signature_phrases

    @property
    def hard_constraints(self) -> Mapping[str, Any]:
        value = self.rubric.get("hard_constraints", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def fixture_sha256(self) -> str:
        row = self.to_dict()
        row.pop("rubric")
        return canonical_json_sha256(row)

    @property
    def rubric_sha256(self) -> str:
        return canonical_json_sha256(self.rubric)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "kind": self.KIND,
            "persona_id": self.persona_id, "persona_version": self.persona_version,
            "display_name": self.display_name, "role": self.role,
            "traits": list(self.traits), "tone_style": list(self.tone_style),
            "behavioral_invariants": list(self.behavioral_invariants),
            "do_not_say": list(self.do_not_say), "boundaries": list(self.boundaries),
            "signature_phrases": list(self.signature_phrases), "relationship": _json_copy(self.relationship),
            "memory_facts": _json_copy(self.memory_facts), "rubric": _json_copy(self.rubric),
            "source_fixture_id": self.source_fixture_id,
            "source_fixture_sha256": self.source_fixture_sha256,
            "metadata": _json_copy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GoldenPersona":
        required = {"persona_id", "persona_version", "display_name", "role", "traits", "tone_style", "behavioral_invariants", "do_not_say", "boundaries", "signature_phrases", "relationship", "memory_facts", "rubric", "source_fixture_id", "source_fixture_sha256"}
        optional = {"metadata"}
        row = _strict(data, cls.KIND, required, optional)
        return cls(**{key: row[key] for key in required}, metadata=row.get("metadata", {}))


@dataclass(frozen=True, slots=True, kw_only=True)
class Scenario(_Contract):
    scenario_id: str
    scenario_version: str
    name: str
    description: str
    persona_id: str
    persona_version: str
    category: str
    turns: tuple[Turn, ...]
    metric_ids: tuple[str, ...]
    rubric: Mapping[str, Any]
    tags: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    entry_turn_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "scenario"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("scenario_id", "scenario_version", "persona_id", "persona_version", "category"):
            _id(getattr(self, name), f"$.{name}")
        _text(self.name, "$.name")
        _text(self.description, "$.description")
        converted_rows: list[Turn] = []
        for item in self.turns:
            if isinstance(item, Turn):
                converted_rows.append(item)
                continue
            nested = dict(item)
            nested.setdefault("schema_version", SCHEMA_VERSION)
            nested.setdefault("kind", Turn.KIND)
            converted_rows.append(Turn.from_dict(nested))
        converted = tuple(converted_rows)
        if not converted:
            _fail("$.turns", "must not be empty")
        ids = [item.turn_id for item in converted]
        if len(set(ids)) != len(ids):
            _fail("$.turns", "turn ids must be unique")
        entry = self.entry_turn_id or ids[0]
        _id(entry, "$.entry_turn_id")
        if entry not in ids:
            _fail("$.entry_turn_id", "must reference a scenario turn")
        for turn in converted:
            targets = [item.target_turn_id for item in turn.transitions] + [turn.next_turn_id]
            if any(target is not None and target not in ids for target in targets):
                _fail(f"$.turns.{turn.turn_id}", "transition references an unknown turn")
        object.__setattr__(self, "turns", converted)
        object.__setattr__(self, "entry_turn_id", entry)
        object.__setattr__(self, "metric_ids", _strings(self.metric_ids, "$.metric_ids", nonempty=True))
        object.__setattr__(self, "tags", _strings(self.tags, "$.tags"))
        object.__setattr__(self, "requirements", _strings(self.requirements, "$.requirements"))
        object.__setattr__(self, "rubric", _mapping(self.rubric, "$.rubric", nonempty=True))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    @property
    def version(self) -> str:
        return self.scenario_version

    @property
    def fixture_sha256(self) -> str:
        row = self.to_dict()
        row.pop("rubric")
        return canonical_json_sha256(row)

    @property
    def rubric_sha256(self) -> str:
        return canonical_json_sha256(self.rubric)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "kind": self.KIND,
            "scenario_id": self.scenario_id, "scenario_version": self.scenario_version,
            "name": self.name, "description": self.description,
            "persona_id": self.persona_id, "persona_version": self.persona_version,
            "category": self.category, "tags": list(self.tags),
            "requirements": list(self.requirements), "entry_turn_id": self.entry_turn_id,
            "turns": [item.to_dict() for item in self.turns], "metric_ids": list(self.metric_ids),
            "rubric": _json_copy(self.rubric), "metadata": _json_copy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scenario":
        required = {"scenario_id", "scenario_version", "name", "description", "persona_id", "persona_version", "category", "tags", "requirements", "turns", "metric_ids", "rubric", "metadata"}
        optional = {"entry_turn_id"}
        row = _strict(data, cls.KIND, required, optional)
        return cls(**{key: row[key] for key in required}, entry_turn_id=row.get("entry_turn_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnEvidence(_Contract):
    turn_id: str
    turn_index: int
    role: str
    prompt: str
    response: str
    session_key: str
    session_id: str
    session_generation: int
    boundary_before: str
    request_id: str
    response_id: str
    trace_id: str
    latency_ms: float | None
    next_turn_id: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "turn_evidence"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _id(self.turn_id, "$.turn_id")
        if type(self.turn_index) is not int or self.turn_index < 1:
            _fail("$.turn_index", "must be a positive integer")
        if self.role not in _ROLES:
            _fail("$.role", "is unsupported")
        _text(self.prompt, "$.prompt")
        _text(self.response, "$.response", allow_empty=True)
        for name in ("session_key", "session_id", "request_id", "response_id"):
            _id(getattr(self, name), f"$.{name}")
        if not isinstance(self.trace_id, str):
            _fail("$.trace_id", "must be a string")
        if self.trace_id:
            _id(self.trace_id, "$.trace_id")
        if type(self.session_generation) is not int or self.session_generation < 0:
            _fail("$.session_generation", "must be a non-negative integer")
        if self.boundary_before not in _BOUNDARIES:
            _fail("$.boundary_before", "is unsupported")
        if self.latency_ms is not None and (isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float)) or not math.isfinite(self.latency_ms) or self.latency_ms < 0):
            _fail("$.latency_ms", "must be a finite non-negative number or null")
        if self.next_turn_id is not None:
            _id(self.next_turn_id, "$.next_turn_id")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "kind": self.KIND, **{name: _json_copy(getattr(self, name)) for name in ("turn_id", "turn_index", "role", "prompt", "response", "session_key", "session_id", "session_generation", "boundary_before", "request_id", "response_id", "trace_id", "latency_ms", "next_turn_id", "metadata")}}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TurnEvidence":
        keys = {"turn_id", "turn_index", "role", "prompt", "response", "session_key", "session_id", "session_generation", "boundary_before", "request_id", "response_id", "trace_id", "latency_ms", "next_turn_id", "metadata"}
        row = _strict(data, cls.KIND, keys)
        return cls(**{key: row[key] for key in keys})


@dataclass(frozen=True, slots=True, kw_only=True)
class Trajectory(_Contract):
    trajectory_id: str
    experiment_id: str
    target_id: str
    scenario_id: str
    scenario_version: str
    scenario_sha256: str
    repeat_index: int
    status: str
    failure_code: str
    started_at: str
    finished_at: str
    turns: tuple[TurnEvidence, ...]
    boundary_evidence: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "trajectory"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("trajectory_id", "experiment_id", "target_id", "scenario_id", "scenario_version"):
            _id(getattr(self, name), f"$.{name}")
        _sha(self.scenario_sha256, "$.scenario_sha256")
        if type(self.repeat_index) is not int or self.repeat_index < 0:
            _fail("$.repeat_index", "must be a non-negative integer")
        if self.status not in _TRAJECTORY_STATUSES:
            _fail("$.status", "is unsupported")
        if not isinstance(self.failure_code, str) or _FAILURE_RE.fullmatch(self.failure_code) is None:
            _fail("$.failure_code", "must be NONE or an uppercase failure code")
        if (self.status == "COMPLETED") != (self.failure_code == "NONE"):
            _fail("$.failure_code", "must be NONE exactly when status is COMPLETED")
        _timestamp(self.started_at, "$.started_at")
        _timestamp(self.finished_at, "$.finished_at")
        if datetime.fromisoformat(self.finished_at.replace("Z", "+00:00")) < datetime.fromisoformat(self.started_at.replace("Z", "+00:00")):
            _fail("$.finished_at", "must not precede started_at")
        converted_turns: list[TurnEvidence] = []
        for item in self.turns:
            if isinstance(item, TurnEvidence):
                converted_turns.append(item)
                continue
            nested = dict(item)
            # Runner-owned private evidence is embedded inside a versioned
            # trajectory, so it need not redundantly carry its own envelope.
            nested.setdefault("schema_version", SCHEMA_VERSION)
            nested.setdefault("kind", TurnEvidence.KIND)
            converted_turns.append(TurnEvidence.from_dict(nested))
        object.__setattr__(self, "turns", tuple(converted_turns))
        if not isinstance(self.boundary_evidence, Sequence) or isinstance(self.boundary_evidence, (str, bytes, bytearray)):
            _fail("$.boundary_evidence", "must be an array")
        object.__setattr__(self, "boundary_evidence", tuple(_mapping(item, f"$.boundary_evidence[{index}]") for index, item in enumerate(self.boundary_evidence)))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    @property
    def repetition(self) -> int:
        return self.repeat_index

    @property
    def turn_evidence(self) -> tuple[TurnEvidence, ...]:
        return self.turns

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "kind": self.KIND, "trajectory_id": self.trajectory_id, "experiment_id": self.experiment_id, "target_id": self.target_id, "scenario_id": self.scenario_id, "scenario_version": self.scenario_version, "scenario_sha256": self.scenario_sha256, "repeat_index": self.repeat_index, "status": self.status, "failure_code": self.failure_code, "started_at": self.started_at, "finished_at": self.finished_at, "turns": [item.to_dict() for item in self.turns], "boundary_evidence": _json_copy(self.boundary_evidence), "metadata": _json_copy(self.metadata)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Trajectory":
        keys = {"trajectory_id", "experiment_id", "target_id", "scenario_id", "scenario_version", "scenario_sha256", "repeat_index", "status", "failure_code", "started_at", "finished_at", "turns", "boundary_evidence", "metadata"}
        row = _strict(data, cls.KIND, keys)
        return cls(**{key: row[key] for key in keys})


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricResult(_Contract):
    metric_id: str
    metric_version: str
    experiment_id: str
    target_id: str
    trajectory_id: str
    scenario_id: str
    evaluator_type: str
    status: str
    passed: bool | None
    score: float | None
    threshold: float
    hard_gate: bool
    failure_codes: tuple[str, ...]
    evidence: tuple[Mapping[str, Any], ...]
    summary: str
    rubric_sha256: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "metric_result"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        for name in ("metric_id", "metric_version", "experiment_id", "target_id", "trajectory_id", "scenario_id"):
            _id(getattr(self, name), f"$.{name}")
        if self.evaluator_type not in _EVALUATOR_TYPES or self.status not in _METRIC_STATUSES:
            _fail("$.status", "evaluator type or status is unsupported")
        if self.passed is not None and type(self.passed) is not bool:
            _fail("$.passed", "must be boolean or null")
        expected = True if self.status == "PASS" else False if self.status == "FAIL" else None
        if self.passed is not expected:
            _fail("$.passed", "must agree with status")
        for name in ("score", "threshold"):
            value = getattr(self, name)
            if value is None and name == "score":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                _fail(f"$.{name}", "must be between zero and one")
        if type(self.hard_gate) is not bool:
            _fail("$.hard_gate", "must be a boolean")
        codes = _strings(self.failure_codes, "$.failure_codes")
        if any(_FAILURE_RE.fullmatch(code) is None or code == "NONE" for code in codes):
            _fail("$.failure_codes", "contains an invalid failure code")
        object.__setattr__(self, "failure_codes", codes)
        object.__setattr__(self, "evidence", tuple(_mapping(item, f"$.evidence[{index}]") for index, item in enumerate(self.evidence)))
        _text(self.summary, "$.summary")
        _sha(self.rubric_sha256, "$.rubric_sha256", optional=True)
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "kind": self.KIND, **{name: _json_copy(getattr(self, name)) for name in ("metric_id", "metric_version", "experiment_id", "target_id", "trajectory_id", "scenario_id", "evaluator_type", "status", "passed", "score", "threshold", "hard_gate", "failure_codes", "evidence", "summary", "rubric_sha256", "metadata")}}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetricResult":
        keys = {"metric_id", "metric_version", "experiment_id", "target_id", "trajectory_id", "scenario_id", "evaluator_type", "status", "passed", "score", "threshold", "hard_gate", "failure_codes", "evidence", "summary", "rubric_sha256", "metadata"}
        row = _strict(data, cls.KIND, keys)
        return cls(**{key: row[key] for key in keys})


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentTarget(_Contract):
    target_id: str
    label: str
    base_url: str
    build_sha: str
    runtime_mode: str
    provider: str
    model: str
    configuration: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "experiment_target"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _id(self.target_id, "$.target_id")
        if self.label not in _TARGET_LABELS:
            _fail("$.label", "must be baseline or candidate")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            _fail("$.base_url", "must be an HTTP(S) URL without credentials")
        if not isinstance(self.build_sha, str) or _BUILD_SHA_RE.fullmatch(self.build_sha) is None:
            _fail("$.build_sha", "must be a full lowercase 40- or 64-character build SHA")
        for name in ("runtime_mode", "provider", "model"):
            _text(getattr(self, name), f"$.{name}")
        object.__setattr__(self, "configuration", _mapping(self.configuration, "$.configuration"))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "kind": self.KIND, "target_id": self.target_id, "label": self.label, "base_url": self.base_url, "build_sha": self.build_sha, "runtime_mode": self.runtime_mode, "provider": self.provider, "model": self.model, "configuration": _json_copy(self.configuration)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentTarget":
        keys = {"target_id", "label", "base_url", "build_sha", "runtime_mode", "provider", "model", "configuration"}
        row = _strict(data, cls.KIND, keys)
        return cls(**{key: row[key] for key in keys})


@dataclass(frozen=True, slots=True, kw_only=True)
class Experiment(_Contract):
    experiment_id: str
    persona_id: str
    persona_version: str
    repetitions: int
    concurrency: int
    scenario_ids: tuple[str, ...]
    targets: tuple[ExperimentTarget, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "experiment"

    def __post_init__(self) -> None:
        for name in ("experiment_id", "persona_id", "persona_version"):
            _id(getattr(self, name), f"$.{name}")
        for name in ("repetitions", "concurrency"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 100:
                _fail(f"$.{name}", "must be an integer between one and 100")
        object.__setattr__(
            self,
            "scenario_ids",
            _strings(self.scenario_ids, "$.scenario_ids", nonempty=True),
        )
        targets = tuple(
            item
            if isinstance(item, ExperimentTarget)
            else ExperimentTarget.from_dict(item)
            for item in self.targets
        )
        if not targets or len({item.target_id for item in targets}) != len(targets):
            _fail("$.targets", "must contain unique targets")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "metadata", _mapping(self.metadata, "$.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.KIND,
            "experiment_id": self.experiment_id,
            "persona_id": self.persona_id,
            "persona_version": self.persona_version,
            "repetitions": self.repetitions,
            "concurrency": self.concurrency,
            "scenario_ids": list(self.scenario_ids),
            "targets": [item.to_dict() for item in self.targets],
            "metadata": _json_copy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Experiment":
        keys = {
            "experiment_id",
            "persona_id",
            "persona_version",
            "repetitions",
            "concurrency",
            "scenario_ids",
            "targets",
            "metadata",
        }
        row = _strict(data, cls.KIND, keys)
        return cls(**{key: row[key] for key in keys})


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentResult(_Contract):
    experiment_id: str
    status: str
    started_at: str
    finished_at: str
    persona_fixture_sha256: str
    rubric_sha256: str
    scenario_fingerprints: Mapping[str, Any]
    targets: tuple[ExperimentTarget, ...]
    trajectories: tuple[Trajectory, ...]
    metric_results: tuple[MetricResult, ...]
    summary: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    KIND: ClassVar[str] = "experiment_result"

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _id(self.experiment_id, "$.experiment_id")
        if self.status not in _RESULT_STATUSES:
            _fail("$.status", "is unsupported")
        _timestamp(self.started_at, "$.started_at")
        _timestamp(self.finished_at, "$.finished_at")
        if datetime.fromisoformat(self.finished_at.replace("Z", "+00:00")) < datetime.fromisoformat(self.started_at.replace("Z", "+00:00")):
            _fail("$.finished_at", "must not precede started_at")
        _sha(self.persona_fixture_sha256, "$.persona_fixture_sha256")
        _sha(self.rubric_sha256, "$.rubric_sha256")
        fingerprints = _mapping(self.scenario_fingerprints, "$.scenario_fingerprints", nonempty=True)
        for key, digest in fingerprints.items():
            _id(key, "$.scenario_fingerprints key")
            _sha(digest, f"$.scenario_fingerprints.{key}")
        object.__setattr__(self, "scenario_fingerprints", fingerprints)
        targets = tuple(item if isinstance(item, ExperimentTarget) else ExperimentTarget.from_dict(item) for item in self.targets)
        if (
            not targets
            or len({item.target_id for item in targets}) != len(targets)
            or len({item.label for item in targets}) != len(targets)
        ):
            _fail("$.targets", "must contain unique target ids and labels")
        target_ids = {item.target_id for item in targets}
        trajectories = tuple(item if isinstance(item, Trajectory) else Trajectory.from_dict(item) for item in self.trajectories)
        if not trajectories or len({item.trajectory_id for item in trajectories}) != len(trajectories):
            _fail("$.trajectories", "must contain unique trajectory ids")
        repeat_bindings: set[tuple[str, str, int]] = set()
        trajectory_by_id: dict[str, Trajectory] = {}
        for index, trajectory in enumerate(trajectories):
            if trajectory.experiment_id != self.experiment_id:
                _fail(f"$.trajectories[{index}].experiment_id", "does not match result")
            if trajectory.target_id not in target_ids:
                _fail(f"$.trajectories[{index}].target_id", "does not match a result target")
            if fingerprints.get(trajectory.scenario_id) != trajectory.scenario_sha256:
                _fail(f"$.trajectories[{index}].scenario_sha256", "does not match scenario fingerprints")
            repeat_binding = (
                trajectory.target_id,
                trajectory.scenario_id,
                trajectory.repeat_index,
            )
            if repeat_binding in repeat_bindings:
                _fail("$.trajectories", "contains a duplicate target/scenario repeat")
            repeat_bindings.add(repeat_binding)
            trajectory_by_id[trajectory.trajectory_id] = trajectory
        if set(fingerprints) != {item.scenario_id for item in trajectories}:
            _fail("$.scenario_fingerprints", "does not exactly match trajectory scenarios")
        if {item.target_id for item in trajectories} != target_ids:
            _fail("$.targets", "contains a target with no trajectory evidence")

        metric_results = tuple(item if isinstance(item, MetricResult) else MetricResult.from_dict(item) for item in self.metric_results)
        if not metric_results:
            _fail("$.metric_results", "must not be empty")
        metric_bindings: set[tuple[str, str]] = set()
        metric_trajectory_ids: set[str] = set()
        for index, metric in enumerate(metric_results):
            trajectory = trajectory_by_id.get(metric.trajectory_id)
            if trajectory is None:
                _fail(f"$.metric_results[{index}].trajectory_id", "does not match a trajectory")
            if (
                metric.experiment_id != self.experiment_id
                or metric.target_id != trajectory.target_id
                or metric.scenario_id != trajectory.scenario_id
            ):
                _fail(f"$.metric_results[{index}]", "does not match its trajectory binding")
            binding = (metric.trajectory_id, metric.metric_id)
            if binding in metric_bindings:
                _fail("$.metric_results", "contains duplicate metric evidence")
            if metric.rubric_sha256 is None:
                _fail(f"$.metric_results[{index}].rubric_sha256", "must be bound")
            metric_bindings.add(binding)
            metric_trajectory_ids.add(metric.trajectory_id)
        if metric_trajectory_ids != set(trajectory_by_id):
            _fail("$.metric_results", "must cover every trajectory")

        metadata = _mapping(self.metadata, "$.metadata")
        coverage = metadata.get("coverage_contract")
        if not isinstance(coverage, Mapping) or set(coverage) != {"repetitions", "scenarios"}:
            _fail("$.metadata.coverage_contract", "is missing or invalid")
        repetitions = coverage.get("repetitions")
        coverage_scenarios = coverage.get("scenarios")
        if (
            type(repetitions) is not int
            or not 1 <= repetitions <= 100
            or not isinstance(coverage_scenarios, Mapping)
            or set(coverage_scenarios) != set(fingerprints)
        ):
            _fail("$.metadata.coverage_contract", "does not match result scenarios")
        expected_metrics: dict[str, set[str]] = {}
        for scenario_id, raw in coverage_scenarios.items():
            if not isinstance(raw, Mapping) or set(raw) != {"fingerprint_sha256", "metric_ids"}:
                _fail(f"$.metadata.coverage_contract.scenarios.{scenario_id}", "is invalid")
            if raw.get("fingerprint_sha256") != fingerprints[scenario_id]:
                _fail(f"$.metadata.coverage_contract.scenarios.{scenario_id}", "fingerprint does not match")
            metric_ids = _strings(
                raw.get("metric_ids"),
                f"$.metadata.coverage_contract.scenarios.{scenario_id}.metric_ids",
                nonempty=True,
            )
            for metric_id in metric_ids:
                _id(metric_id, f"$.metadata.coverage_contract.scenarios.{scenario_id}.metric_ids")
            expected_metrics[scenario_id] = set(metric_ids)
        for target_id in target_ids:
            for scenario_id in fingerprints:
                observed_repeats = {
                    item.repeat_index
                    for item in trajectories
                    if item.target_id == target_id and item.scenario_id == scenario_id
                }
                if observed_repeats != set(range(repetitions)):
                    _fail("$.trajectories", "does not satisfy expected repetition coverage")
        metrics_by_trajectory: dict[str, set[str]] = {}
        for metric in metric_results:
            metrics_by_trajectory.setdefault(metric.trajectory_id, set()).add(metric.metric_id)
        for trajectory in trajectories:
            if metrics_by_trajectory.get(trajectory.trajectory_id) != expected_metrics[trajectory.scenario_id]:
                _fail("$.metric_results", "does not satisfy expected metric coverage")

        if any(item.status == "INFRA_ERROR" for item in trajectories + metric_results):
            derived_status = "INFRA_ERROR"
        elif any(
            item.status in {"BLOCKED_EVIDENCE", "SKIP"}
            for item in metric_results
        ) or any(item.status == "BLOCKED_EVIDENCE" for item in trajectories):
            derived_status = "BLOCKED_EVIDENCE"
        elif any(
            item.status == "FAIL" and item.hard_gate for item in metric_results
        ):
            derived_status = "FAIL"
        else:
            derived_status = "PASS"
        if self.status != derived_status:
            _fail("$.status", "does not agree with child evidence")

        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "trajectories", trajectories)
        object.__setattr__(self, "metric_results", metric_results)
        object.__setattr__(self, "summary", _mapping(self.summary, "$.summary"))
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "kind": self.KIND, "experiment_id": self.experiment_id, "status": self.status, "started_at": self.started_at, "finished_at": self.finished_at, "persona_fixture_sha256": self.persona_fixture_sha256, "rubric_sha256": self.rubric_sha256, "scenario_fingerprints": _json_copy(self.scenario_fingerprints), "targets": [item.to_dict() for item in self.targets], "trajectories": [item.to_dict() for item in self.trajectories], "metric_results": [item.to_dict() for item in self.metric_results], "summary": _json_copy(self.summary), "metadata": _json_copy(self.metadata)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentResult":
        keys = {"experiment_id", "status", "started_at", "finished_at", "persona_fixture_sha256", "rubric_sha256", "scenario_fingerprints", "targets", "trajectories", "metric_results", "summary", "metadata"}
        row = _strict(data, cls.KIND, keys)
        return cls(**{key: row[key] for key in keys})


__all__ = [
    "SCHEMA_VERSION", "ContractError", "canonical_json_bytes", "canonical_json_sha256",
    "Transition", "Turn", "GoldenPersona", "Scenario", "TurnEvidence", "Trajectory",
    "MetricResult", "ExperimentTarget", "Experiment", "ExperimentResult",
]
