"""Proactive/perception V2 tool catalog.

This is the runtime-facing catalog, not an HTTP router. The first migration
step is to make the new contract explicit: every turn sees the same tool names
and cost classes, even while the old hosted/resident paths still use adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

CostClass = str
FAST: CostClass = "fast"
SLOW: CostClass = "slow"


@dataclass(frozen=True)
class ToolSpecV2:
    name: str
    group: str
    cost_class: CostClass
    description: str = ""
    wake_source: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_context(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "cost_class": self.cost_class,
            "description": self.description,
            "wake_source": self.wake_source,
            "metadata": dict(self.metadata or {}),
        }


class ToolCatalogV2:
    def __init__(self, specs: list[ToolSpecV2] | tuple[ToolSpecV2, ...]):
        self._specs = tuple(specs)
        self._by_name = {spec.name: spec for spec in self._specs}

    def specs(self) -> tuple[ToolSpecV2, ...]:
        return self._specs

    def context_tools(self) -> list[dict[str, Any]]:
        return [spec.as_context() for spec in self._specs]

    def signature(self) -> tuple[tuple[str, str, CostClass], ...]:
        return tuple((spec.name, spec.group, spec.cost_class) for spec in self._specs)

    def get(self, name: str) -> ToolSpecV2:
        return self._by_name[name]

    def cost_class_for(self, name: str, args: Mapping[str, Any] | None = None) -> CostClass:
        """Return the effective cost class.

        Most tools are static. A few have spec-approved parameter thresholds;
        keep that logic here so agents do not infer pricing themselves.
        """
        args = args or {}
        if name == "perception.calendar":
            try:
                window_days = float(args.get("window_days", args.get("days", 1)))
            except (TypeError, ValueError):
                window_days = 1
            return FAST if window_days <= 7 else SLOW
        if name == "screen.read":
            return SLOW if str(args.get("mode") or "caption").lower() == "full" else FAST
        return self.get(name).cost_class


DEFAULT_TOOL_SPECS_V2: tuple[ToolSpecV2, ...] = (
    ToolSpecV2("perception.now", "perception", FAST, "Current cheap authorized signals."),
    ToolSpecV2("perception.location", "perception", FAST, "Connectivity-derived coarse presence label."),
    ToolSpecV2("perception.calendar", "perception", FAST, "Calendar window; >7 days is slow."),
    ToolSpecV2("perception.now_playing", "perception", FAST, "Current media playback."),
    ToolSpecV2("perception.motion", "perception", FAST, "Motion is pull-only; it is not a wake source."),
    ToolSpecV2("perception.audio_route", "perception", FAST, "Current audio output route (type/bluetooth/device name)."),
    ToolSpecV2("perception.recent_apps", "perception", FAST,
               "App open/close trajectory, newest first (app/category/ts/event/minutes_ago). "
               "Use this for 'what have I been doing/using' -- perception.now only carries the app "
               "event observed in the last 15 minutes. Args: limit, hours."),
    ToolSpecV2("perception.weather", "perception", FAST, "WeatherKit: condition/temp/apparent/humidity/precip/UV/daylight/alerts."),
    ToolSpecV2("perception.steps", "perception", SLOW, "Today's exact step count (HealthKit)."),
    ToolSpecV2("perception.sleep_last_night", "perception", SLOW, "Last night sleep: asleep + core/deep/rem minutes."),
    ToolSpecV2("perception.workout", "perception", SLOW, "Today's workout: type/duration/count."),
    ToolSpecV2("perception.vitals", "perception", SLOW, "Vitals: resting/current HR, HRV, respiratory, SpO2, VO2max."),
    ToolSpecV2("perception.activity", "perception", SLOW, "Activity totals: active energy/exercise/stand/mindful minutes."),
    ToolSpecV2("perception.body", "perception", SLOW, "Body measurements: weight/BMI/body fat/height."),
    ToolSpecV2("perception.metabolic", "perception", SLOW, "Metabolic point values: blood glucose, blood pressure."),
    ToolSpecV2("perception.cycle", "perception", SLOW, "Menstrual cycle: flow level + active-period flag."),
    ToolSpecV2("perception.mood", "perception", SLOW, "State of Mind: valence + classification + kind."),
    ToolSpecV2("perception.reminders", "perception", SLOW, "Reminders: next/overdue/due-today + list."),
    ToolSpecV2("perception.photo_recent", "perception", SLOW, "Recent photo metadata and pullable content."),
    ToolSpecV2("screen.read", "screen", FAST, "Caption by default; mode=full is slow."),
    ToolSpecV2("screen.recent", "screen", SLOW, "Recent screen frames."),
    ToolSpecV2("memory.index", "memory", FAST, "Compact memory index."),
    ToolSpecV2("memory.fetch", "memory", SLOW, "Verbatim memory fetch."),
    ToolSpecV2(
        "memory.add",
        "memory",
        SLOW,
        "Add one encrypted memory card. Requires a pre-built v1 envelope with type; insight needs >=1 anchor_memory_ids and reflection needs >=2 plus the reflection cadence cap.",
    ),
    ToolSpecV2(
        "memory.supersede",
        "memory",
        SLOW,
        "Replace an old memory by creating one encrypted successor envelope and soft-retiring the old card. Requires supersedes/target id; insight/reflection anchor floors still apply.",
    ),
    ToolSpecV2(
        "memory.delete",
        "memory",
        FAST,
        "Delete one memory by explicit id. Intended only for explicit user deletion or cleanup, not correction flows.",
    ),
    ToolSpecV2(
        "memory.retype",
        "memory",
        FAST,
        "Change one memory type. New type must be a supported memory type; insight needs >=1 anchor_memory_ids and reflection needs >=2.",
    ),
    ToolSpecV2("send_message", "action", FAST, "Write chat message through DeliveryGate."),
    ToolSpecV2("sleep", "action", FAST, "End turn without visible speech."),
    ToolSpecV2(
        "schedule_wake",
        "action",
        FAST,
        "Create a durable future wake, optionally repeated every 24 hours or 7 days.",
    ),
    ToolSpecV2("cancel_wake", "action", FAST, "Cancel a durable future wake."),
)

FOREGROUND_CHAT_TOOL_NAMES_V2 = frozenset({
    "perception.now",
    "perception.location",
    "perception.calendar",
    "perception.motion",
    "perception.weather",
    "perception.recent_apps",
})


def default_tool_catalog_v2() -> ToolCatalogV2:
    return ToolCatalogV2(DEFAULT_TOOL_SPECS_V2)


def foreground_chat_tool_catalog_v2() -> ToolCatalogV2:
    """Fast-only additive tools for foreground chat.

    This surface intentionally excludes memory and action tools. Memory recall
    stays on the existing chat path; foreground perception is a pure add-on.
    Screen tools are also held back for now because the same screen.read name
    can become a slow full-frame read.
    """
    return ToolCatalogV2(tuple(
        spec for spec in DEFAULT_TOOL_SPECS_V2
        if spec.name in FOREGROUND_CHAT_TOOL_NAMES_V2
    ))


def foreground_chat_tool_context_v2() -> list[dict[str, Any]]:
    return foreground_chat_tool_catalog_v2().context_tools()


def tool_catalog_v2_for_runtime(runtime: str) -> ToolCatalogV2:
    """Return the shared V2 catalog for hosted/resident runtime surfaces."""
    normalized = str(runtime or "").strip().lower()
    if normalized not in {"hosted", "resident"}:
        raise ValueError(f"unknown v2 runtime surface: {runtime}")
    return default_tool_catalog_v2()


def tool_context_v2_for_runtime(runtime: str) -> list[dict[str, Any]]:
    return tool_catalog_v2_for_runtime(runtime).context_tools()
