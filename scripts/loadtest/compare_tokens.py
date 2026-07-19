"""D4 Task 4: tokens/turn vs the frozen resident-runtime efficiency baseline.

The V1 (resident) runtime drives the LLM via subprocess CLIs (codex/claude),
which cannot be cleanly pointed at an HTTP mock provider, so a true live A/B
of V2 vs resident is a MANUAL near-cutover activity (see the D4 plan, Task 5
runbook). What THIS module provides instead:

  1. ``compare_tokens_per_turn`` — pure comparison math against a supplied
     resident baseline number (produced out-of-band, e.g. from a manual
     resident run's logs/telemetry, or from ``collect.tokens_per_turn_samples``
     read against resident-tagged rows). A regression here (V2 using more
     than ``threshold`` extra tokens/turn than resident) is the documented
     regression condition for V2. The retired hosted resident is a historical
     measurement baseline, not a deployable rollback target.
  2. ``measure_v2_tokens_per_turn`` — drives V2's production unified native
     tool loop (``model_api_runtime.v2.tool_loop.run_tool_loop``) against
     fixture conversations through a ``MockProvider`` (deterministic token
     counts, no BYOK credit burn), and returns the mean tokens/turn V2
     produced. Tool fixtures exercise both a native tool-call round and the
     loop's reserved tools-disabled terminal reply. This is the "V2 side" of
     the comparison; the resident side is supplied by the caller/operator.

Usage (manual)::

    python -m scripts.loadtest.compare_tokens --resident-baseline 118.4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent.parent
_backend = str(_repo_root / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import provider_client  # noqa: E402
from provider_types import ToolExchange, ToolResult  # noqa: E402
from model_api_runtime.v2 import context as v2_context  # noqa: E402
from model_api_runtime.v2 import tool_loop as v2_tool_loop  # noqa: E402

from scripts.loadtest.mock_provider import MockProvider  # noqa: E402
from scripts.loadtest.fixtures import v2_turn_fixtures  # noqa: E402

DEFAULT_THRESHOLD = 0.10
# The custom mock endpoint has no provider-owned model metadata.  Runtime V2
# deliberately fails closed for such routes unless the context window is
# explicit, so the load-test harness must declare its synthetic model budget
# just like a real custom OpenAI-compatible deployment.
LOADTEST_CONTEXT_WINDOW_TOKENS = 128_000

# A small built-in fixture set for the __main__ entrypoint — a couple of
# short, plausible conversation turns. Real gate runs should pass a richer,
# representative fixture set (this module's ``measure_v2_tokens_per_turn``
# accepts any ``fixtures`` list, so callers are free to swap these out).
_DEFAULT_FIXTURES: list[dict[str, Any]] = v2_turn_fixtures()


def compare_tokens_per_turn(
    v2_mean: float, resident_baseline: float, *, threshold: float = DEFAULT_THRESHOLD
) -> dict[str, Any]:
    """Compare V2's mean tokens/turn against a resident-runtime baseline.

    ``regression`` is True when V2 uses MORE than ``threshold`` (fractional,
    e.g. 0.10 == +10%) extra tokens/turn than the frozen resident benchmark.
    This is a V2 optimization gate; the retired hosted runtime is not a
    rollback target.

    Invalid baselines/thresholds raise. A rollout gate must fail closed rather
    than convert a skipped comparison into exit code zero.
    """
    if not math.isfinite(resident_baseline) or resident_baseline <= 0:
        raise ValueError("resident_baseline must be > 0")
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be >= 0")
    if not math.isfinite(v2_mean) or v2_mean < 0:
        raise ValueError("v2_mean must be finite and >= 0")
    delta_ratio = (v2_mean - resident_baseline) / resident_baseline
    return {
        "v2_mean": v2_mean,
        "resident_baseline": resident_baseline,
        "delta_ratio": delta_ratio,
        "threshold": threshold,
        "regression": delta_ratio > threshold,
    }


async def _measure_v2_tokens_per_turn_async(
    fixtures: list[dict[str, Any]], *, mock_base_url: str
) -> list[float]:
    # provider="openai_compatible" is the only ProviderConfig shape that
    # routes chat_completion_async to an arbitrary OpenAI-compatible base URL;
    # all provider families now use native async HTTP, but the others require
    # their provider-specific wire shapes and endpoints. openai_compatible
    # always POSTs {base_url}/chat/completions
    # with the plain OpenAI chat-completions request/response shape, which
    # is exactly what MockProvider speaks. This exercises the SAME provider-native
    # loop and async transport the real V2 worker uses in production, just
    # pointed at the mock.
    provider_config = provider_client.ProviderConfig(
        provider="openai_compatible",
        model="loadtest-mock",
        api_key="mock-key",
        base_url=mock_base_url,
        context_window_tokens=LOADTEST_CONTEXT_WINDOW_TOKENS,
    )
    totals: list[float] = []
    for fixture in fixtures:
        result = await _drive_turn_async(provider_config, fixture)
        totals.append(result["tokens"])
    return totals


def measure_v2_tokens_per_turn(
    fixtures: list[dict[str, Any]], *, mock_base_url: str
) -> float:
    """Drive each fixture (``{"summary": str, "tail": list[dict]}``) through
    the real ``model_api_runtime.v2.tool_loop.run_tool_loop`` path against a
    mock provider listening at ``mock_base_url``, and return mean tokens/turn
    (prompt_tokens + completion_tokens across every call in each turn).

    Raises if ``fixtures`` is empty (nothing to measure -> mean is undefined;
    callers should always pass at least one fixture).
    """
    if not fixtures:
        raise ValueError("measure_v2_tokens_per_turn requires at least one fixture")
    totals = asyncio.run(
        _measure_v2_tokens_per_turn_async(fixtures, mock_base_url=mock_base_url)
    )
    return sum(totals) / len(totals)


def _build_messages_for_fixture(fixture: dict[str, Any]):
    """Mirror the production worker's base-context + native transcript shape."""
    tail = list(fixture.get("tail") or [])
    base = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        summary=str(fixture.get("summary") or ""),
        tail=tail,
    )

    def build_messages(transcript: list) -> list:
        rendered: list = []
        for item in transcript:
            if isinstance(item, ToolExchange):
                rendered.append(item)
                continue
            if isinstance(item, dict) and v2_context._has_payload(item.get("content")):
                rendered.append({"role": "user", "content": item["content"]})
        return list(base) + rendered

    return build_messages


async def _drive_turn_async(
    provider_config,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """Run one whole production V2 native-tool turn and collect all call usage.

    Capabilities are deterministic fakes because this is a token gate, not a
    capability integration test. The actual loop, tool catalog, OpenAI-compatible
    wire encoding, native ToolExchange replay, and tools-disabled final call are
    production code.
    """
    call_tokens: list[float] = []
    dispatched_batches: list[list] = []
    replies: list[tuple[str, bool]] = []

    async def _dispatch_tools(tool_calls) -> list[ToolResult]:
        calls = list(tool_calls)
        dispatched_batches.append(calls)
        return [
            ToolResult(
                call_id=call.id,
                content=json.dumps(
                    {"ok": True, "data": {"query": call.args.get("query"), "count": 1}},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            for call in calls
        ]

    async def _on_reply(text: str, *, final: bool) -> None:
        replies.append((str(text), final))

    async def _fold_new_messages() -> list[dict]:
        return []

    def _add_usage(usage) -> None:
        if not usage:
            call_tokens.append(0.0)
            return
        call_tokens.append(
            float(usage.get("prompt_tokens") or 0)
            + float(usage.get("completion_tokens") or 0)
        )

    outcome = await v2_tool_loop.run_tool_loop(
        provider_config=provider_config,
        build_messages=_build_messages_for_fixture(fixture),
        dispatch_tools=_dispatch_tools,
        on_reply=_on_reply,
        fold_new_messages=_fold_new_messages,
        add_usage=_add_usage,
        # One tools-enabled tool round plus the reserved tools-disabled final
        # reply is the representative multi-call fixture for this efficiency gate.
        max_calls=int(fixture.get("max_calls") or 2),
    )
    return {
        "tokens": sum(call_tokens),
        "llm_calls": outcome.rounds,
        "tool_batches": dispatched_batches,
        "replies": replies,
        "outcome": outcome,
    }


def measure_turn_tokens(fixtures: list[dict[str, Any]], *, provider) -> dict[str, Any]:
    """把每个 fixture 当成一个完整统一 tool-loop 回合，返回每回合的 token 与
    LLM 调用次数均值。`provider` 是一个已启动的 `MockProvider(estimate_tokens=True)`。

    fixtures 为空 → 抛（均值无定义）。
    """
    if not fixtures:
        raise ValueError("measure_turn_tokens requires at least one fixture")
    provider_config = provider_client.ProviderConfig(
        provider="openai_compatible", model="loadtest-mock",
        api_key="mock-key", base_url=provider.base_url,
        context_window_tokens=LOADTEST_CONTEXT_WINDOW_TOKENS,
    )
    before_prompt = provider.total_prompt_tokens
    before_completion = provider.total_completion_tokens
    before_calls = provider.request_count
    default_tool_call = provider.tool_call

    async def _run() -> None:
        try:
            for fixture in fixtures:
                # Fixture-local native tool behavior lets the shared workload mix
                # one-shot replies with tool-using turns deterministically.
                provider.tool_call = fixture.get("tool_call", default_tool_call)
                await _drive_turn_async(provider_config, fixture)
        finally:
            provider.tool_call = default_tool_call

    asyncio.run(_run())
    turns = float(len(fixtures))
    total = (
        provider.total_prompt_tokens - before_prompt
        + provider.total_completion_tokens - before_completion
    )
    return {
        "tokens_per_turn": total / turns,
        "llm_calls_per_turn": (provider.request_count - before_calls) / turns,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare V2's mean tokens/turn (measured against a mock "
        "provider) to a supplied resident-runtime baseline. Exits nonzero "
        "if V2 regresses beyond --threshold."
    )
    parser.add_argument(
        "--resident-baseline",
        type=float,
        required=True,
        help="resident runtime's mean tokens/turn on the same fixtures (supplied "
        "out-of-band from a manual resident run — see module docstring)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"fractional regression threshold (default: {DEFAULT_THRESHOLD})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if (
        not math.isfinite(args.resident_baseline)
        or args.resident_baseline <= 0
        or not math.isfinite(args.threshold)
        or args.threshold < 0
    ):
        print(json.dumps({
            "error": "invalid_gate_input",
            "resident_baseline": args.resident_baseline,
            "threshold": args.threshold,
        }, indent=2))
        return 2
    with MockProvider(reply="Measured final response.", estimate_tokens=True) as provider:
        report = measure_turn_tokens(_DEFAULT_FIXTURES, provider=provider)
    result = compare_tokens_per_turn(
        report["tokens_per_turn"], args.resident_baseline, threshold=args.threshold
    )
    result["llm_calls_per_turn"] = report["llm_calls_per_turn"]
    print(json.dumps(result, indent=2))
    return 1 if result["regression"] else 0


if __name__ == "__main__":
    sys.exit(main())
