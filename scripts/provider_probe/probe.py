"""Hosted Runtime V2 PR B / Task 10 — MANUAL live 4-provider tool-call probe.

This script is MANUAL and NOT run in CI. It makes real network calls against
real provider APIs using real BYOK keys supplied via environment variables
(``PROBE_OPENAI_CHAT_KEY``, ``PROBE_OPENAI_RESPONSES_KEY``,
``PROBE_ANTHROPIC_KEY``, ``PROBE_GEMINI_KEY`` — see ``WIRES`` below for the
matching ``*_MODEL`` override vars and defaults). A wire whose key env var is
unset is SKIPPED, not failed — this lets a human run the probe against
whichever subset of providers they have live keys for. The CI-safe proof
that all four wires correctly encode/decode/round-trip two tool_calls lives
in ``tests/test_provider_tools_acceptance.py``, which exercises the same
codec functions this script calls but against canned bodies instead of a
live network response — this script exists to catch drift between our canned
fixtures and what a real provider actually sends over the wire, which a pure
codec test cannot catch.

For each configured wire this script:
  1. Sends one chat_completion() call with 2 ToolSpecs (build_probe_tools())
     and a prompt that asks for both tools in the same turn.
  2. Reports whether the provider returned exactly 2 tool_calls with distinct
     ids.
  3. Locally round-trips 2 ToolResults (one per returned call id) through the
     wire's _encode_tool_results_<wire> codec and reports whether both
     results are present in the encoded wire shape, keyed by the right id
     (or, for Gemini, the right function name via the id_to_name map built
     from the decoded calls).

Usage (manual, local, needs real provider API keys)::

    PROBE_ANTHROPIC_KEY=sk-ant-... \\
    PROBE_OPENAI_CHAT_KEY=sk-... \\
    PROBE_GEMINI_KEY=... \\
        python -m scripts.provider_probe.probe
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import provider_client as pc  # noqa: E402
from provider_types import ToolResult, ToolSpec  # noqa: E402

PROBE_PROMPT = (
    "You must call BOTH tools in this same turn: call web_search with "
    "q='current weather in Tokyo', and call get_time with no arguments. "
    "Do not reply with text only — issue both tool calls now."
)


def build_probe_tools() -> "list[ToolSpec]":
    """2 ToolSpecs used to force a real 2-tool-call turn. No network — pure
    data, safe to import and call from a CI smoke test."""
    return [
        ToolSpec(
            "web_search", "Search the web for a query.",
            {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        ),
        ToolSpec(
            "get_time", "Get the current time.",
            {"type": "object", "properties": {}},
        ),
    ]


class WireSpec:
    def __init__(
        self,
        *,
        key_env: str,
        model_env: str,
        default_model: str,
        provider: str,
        encode_results: Callable[[list[ToolResult], dict[str, str]], list[dict[str, Any]]],
    ) -> None:
        self.key_env = key_env
        self.model_env = model_env
        self.default_model = default_model
        self.provider = provider
        self.encode_results = encode_results


WIRES: dict[str, WireSpec] = {
    "openai_chat": WireSpec(
        key_env="PROBE_OPENAI_CHAT_KEY",
        model_env="PROBE_OPENAI_CHAT_MODEL",
        default_model="gpt-4o-mini",
        provider="openai",
        encode_results=lambda results, _id_to_name: pc._encode_tool_results_openai_chat(results),
    ),
    "openai_responses": WireSpec(
        key_env="PROBE_OPENAI_RESPONSES_KEY",
        model_env="PROBE_OPENAI_RESPONSES_MODEL",
        default_model="gpt-5-mini",
        provider="openai",
        encode_results=lambda results, _id_to_name: pc._encode_tool_results_openai_responses(results),
    ),
    "anthropic": WireSpec(
        key_env="PROBE_ANTHROPIC_KEY",
        model_env="PROBE_ANTHROPIC_MODEL",
        default_model="claude-3-5-haiku-20241022",
        provider="anthropic",
        encode_results=lambda results, _id_to_name: pc._encode_tool_results_anthropic(results),
    ),
    "gemini": WireSpec(
        key_env="PROBE_GEMINI_KEY",
        model_env="PROBE_GEMINI_MODEL",
        default_model="gemini-2.0-flash",
        provider="gemini",
        encode_results=lambda results, id_to_name: pc._encode_tool_results_gemini(results, id_to_name),
    ),
}


def run_probe(wire: str) -> dict[str, Any]:
    """Run the live probe for one wire. Returns a report dict; never raises —
    network/provider errors are captured in report['error'] so one wire's
    failure doesn't abort the others."""
    spec = WIRES[wire]
    key = os.environ.get(spec.key_env, "").strip()
    if not key:
        return {"wire": wire, "status": "skipped", "reason": f"{spec.key_env} not set"}

    model = os.environ.get(spec.model_env, spec.default_model)
    config = pc.ProviderConfig(provider=spec.provider, model=model, api_key=key)
    tools = build_probe_tools()

    try:
        result = pc.chat_completion(
            config,
            [
                {"role": "system", "content": "You are a tool-calling test harness."},
                {"role": "user", "content": PROBE_PROMPT},
            ],
            max_tokens=512,
            temperature=0.0,
            timeout=60.0,
            require_reply=False,
            tools=tools,
        )
    except Exception as e:  # noqa: BLE001 - report, don't crash the other wires
        return {"wire": wire, "status": "error", "error": f"{type(e).__name__}: {e}"}

    tool_calls = result.get("tool_calls") or []
    ids = [c.get("id") for c in tool_calls]
    got_two_distinct = len(tool_calls) == 2 and len(set(ids)) == 2

    round_trip_ok = False
    encoded_results = None
    if got_two_distinct:
        id_to_name = {c["id"]: c["name"] for c in tool_calls}
        results = [ToolResult(c["id"], f"probe-result-for-{c['name']}") for c in tool_calls]
        encoded_results = spec.encode_results(results, id_to_name)
        # A minimal presence check: every result call_id (or, for Gemini, the
        # name it maps to) shows up somewhere in the encoded wire payload.
        encoded_str = str(encoded_results)
        round_trip_ok = all(
            (id_to_name[i] if wire == "gemini" else i) in encoded_str for i in ids
        )

    return {
        "wire": wire,
        "status": "ok",
        "model": model,
        "tool_calls": tool_calls,
        "got_two_distinct_tool_calls": got_two_distinct,
        "round_trip_ok": round_trip_ok,
        "encoded_results": encoded_results,
    }


def main() -> int:
    reports = [run_probe(wire) for wire in WIRES]
    any_ran = False
    any_failed = False
    for report in reports:
        wire = report["wire"]
        status = report["status"]
        if status == "skipped":
            print(f"[{wire}] SKIPPED ({report['reason']})")
            continue
        any_ran = True
        if status == "error":
            any_failed = True
            print(f"[{wire}] ERROR: {report['error']}")
            continue
        two_ok = report["got_two_distinct_tool_calls"]
        rt_ok = report["round_trip_ok"]
        verdict = "PASS" if (two_ok and rt_ok) else "FAIL"
        if verdict == "FAIL":
            any_failed = True
        print(
            f"[{wire}] {verdict} model={report['model']} "
            f"two_tool_calls={two_ok} round_trip_ok={rt_ok} "
            f"tool_calls={[(c.get('id'), c.get('name')) for c in report['tool_calls']]}"
        )

    if not any_ran:
        print("\nNo provider keys set — nothing probed. Set at least one of: "
              + ", ".join(spec.key_env for spec in WIRES.values()))
        return 0
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
