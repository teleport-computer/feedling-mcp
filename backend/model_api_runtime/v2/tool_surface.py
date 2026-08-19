"""Pure Runtime V2 tool-surface degradation helpers.

The provider-visible directory is invariant under schema-folding policy: every
tool name remains present. Only non-resident argument manuals may be folded to
a bounded discovery description plus an empty schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from provider_types import ToolSpec


MAX_COLLAPSED_DESCRIPTION_CHARS = 120
COLLAPSE_POLICY_ALWAYS = "always"
COLLAPSE_POLICY_UNDER_PRESSURE = "under_pressure"
DEFAULT_COLLAPSE_POLICY = COLLAPSE_POLICY_UNDER_PRESSURE
COLLAPSE_POLICIES = frozenset(
    {COLLAPSE_POLICY_ALWAYS, COLLAPSE_POLICY_UNDER_PRESSURE}
)

# The smallest complete Runtime V2 action loop.  These schemas are required by
# the prompt frontier whenever they are present in the lane's actual catalog.
RESIDENT_TOOL_NAMES = frozenset(
    {
        "reply",
        "mcp_tool_search",
        "memory_search",
        "memory_write",
        "perception_snapshot",
    }
)


def normalize_collapse_policy(value: str | None) -> str:
    policy = str(value or DEFAULT_COLLAPSE_POLICY).strip().lower()
    if policy not in COLLAPSE_POLICIES:
        raise ValueError(
            "tool schema collapse policy must be always or under_pressure"
        )
    return policy


def collapsed_tool_spec(spec: ToolSpec) -> ToolSpec:
    """Return the deterministic discovery-only form of one tool schema."""
    description = " ".join(str(spec.description or "").split())
    if len(description) > MAX_COLLAPSED_DESCRIPTION_CHARS:
        description = (
            description[: MAX_COLLAPSED_DESCRIPTION_CHARS - 3].rstrip()
            + "..."
        )
    return ToolSpec(
        name=spec.name,
        description=description,
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


@dataclass
class SchemaRecoveryState:
    """Per-turn recovery state for folded Runtime V2 tools."""

    full_specs: Mapping[str, ToolSpec]
    pressure_collapsed_names: set[str] = field(default_factory=set)
    resolved_names: set[str] = field(default_factory=set)

    def set_pressure_collapsed(self, names) -> None:
        known = set(self.full_specs)
        self.pressure_collapsed_names = {
            str(name)
            for name in (names or ())
            if str(name) in known and str(name) not in self.resolved_names
        }

    def discoverable_specs(self) -> dict[str, ToolSpec]:
        return {
            name: self.full_specs[name]
            for name in self.pressure_collapsed_names
            if name not in self.resolved_names
        }

    def protected_names(self) -> set[str]:
        return set(self.resolved_names)

    def recovery_needed(self) -> bool:
        return bool(self.pressure_collapsed_names - self.resolved_names)

    def resolve(self, names) -> list[str]:
        resolved = [
            str(name)
            for name in (names or ())
            if str(name) in self.discoverable_specs()
        ]
        self.resolved_names.update(resolved)
        self.pressure_collapsed_names.difference_update(resolved)
        return resolved


def select_schema_names(
    args: dict | None,
    specs: Mapping[str, ToolSpec],
    *,
    max_results: int,
) -> dict:
    """Select deterministic exact-name or query matches from folded specs."""
    args = args if isinstance(args, dict) else {}
    raw_names = args.get("names")
    query = str(args.get("query") or "").strip()
    has_names = isinstance(raw_names, list) and bool(raw_names)
    if has_names == bool(query):
        return {
            "error": "provide exactly one of query or names",
            "selected": [],
            "not_found": [],
        }

    if has_names:
        selected: list[str] = []
        not_found: list[str] = []
        seen: set[str] = set()
        for value in raw_names:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name in specs:
                selected.append(name)
            else:
                not_found.append(name)
            if len(selected) >= max_results:
                break
        return {"selected": selected, "not_found": not_found}

    needle = query.casefold()
    terms = needle.split()
    ranked: list[tuple[int, str]] = []
    for name, spec in specs.items():
        folded_name = name.casefold()
        description = str(spec.description or "").casefold()
        haystack = f"{folded_name} {description}"
        if not all(term in haystack for term in terms):
            continue
        if folded_name == needle:
            rank = 0
        elif folded_name.startswith(needle):
            rank = 1
        elif needle in folded_name:
            rank = 2
        else:
            rank = 3
        ranked.append((rank, name))
    return {
        "selected": [
            name for _rank, name in sorted(ranked)
        ][:max_results],
        "not_found": [],
    }
