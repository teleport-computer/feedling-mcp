"""D4 Task 4: tokens/turn vs resident-runtime comparison — the rollback gate.

The V1 (resident) runtime drives the LLM via subprocess CLIs (codex/claude),
which cannot be cleanly pointed at an HTTP mock provider, so a true live A/B
of V2 vs resident is a MANUAL near-cutover activity (see the D4 plan, Task 5
runbook). What THIS module provides instead:

  1. ``compare_tokens_per_turn`` — pure comparison math against a supplied
     resident baseline number (produced out-of-band, e.g. from a manual
     resident run's logs/telemetry, or from ``collect.tokens_per_turn_samples``
     read against resident-tagged rows). A regression here (V2 using more
     than ``threshold`` extra tokens/turn than resident) is the documented
     ROLLBACK condition for the V2 cutover (D4 plan, hard gate).
  2. ``measure_v2_tokens_per_turn`` — actually drives V2's real
     ``model_api_runtime.v2.responder.respond`` path (same code the V2 worker
     calls in production) against fixture conversations, through a
     ``MockProvider`` (deterministic token counts, no BYOK credit burn), and
     returns the mean tokens/turn V2 produced. This is the "V2 side" of the
     comparison; the resident side is supplied by the caller/operator.

Usage (manual)::

    python -m scripts.loadtest.compare_tokens --resident-baseline 118.4
"""
from __future__ import annotations

import argparse
import asyncio
import json
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
from model_api_runtime.v2 import planner as v2_planner  # noqa: E402
from model_api_runtime.v2 import responder as v2_responder  # noqa: E402

from scripts.loadtest.mock_provider import MockProvider  # noqa: E402

DEFAULT_THRESHOLD = 0.10

# A small built-in fixture set for the __main__ entrypoint — a couple of
# short, plausible conversation turns. Real gate runs should pass a richer,
# representative fixture set (this module's ``measure_v2_tokens_per_turn``
# accepts any ``fixtures`` list, so callers are free to swap these out).
_DEFAULT_FIXTURES: list[dict[str, Any]] = [
    {
        "summary": "",
        "tail": [{"role": "user", "content": "hey, how's it going?"}],
    },
    {
        "summary": "- user mentioned they're prepping for a trip",
        "tail": [
            {"role": "user", "content": "packed everything for the trip yet?"},
            {"role": "openclaw", "content": "almost, just the charger left"},
            {"role": "user", "content": "nice, don't forget it this time"},
        ],
    },
]


def compare_tokens_per_turn(
    v2_mean: float, resident_baseline: float, *, threshold: float = DEFAULT_THRESHOLD
) -> dict[str, Any]:
    """Compare V2's mean tokens/turn against a resident-runtime baseline.

    ``regression`` is True when V2 uses MORE than ``threshold`` (fractional,
    e.g. 0.10 == +10%) extra tokens/turn than resident — this is the D4
    ROLLBACK condition: if this comes back True near cutover, the rollout
    must not proceed / must be rolled back to resident.

    ``resident_baseline <= 0`` is guarded (a zero/negative baseline makes the
    ratio meaningless, e.g. a caller passing an unset/placeholder value) —
    returns ``regression=False`` (never claims a spurious pass OR fail) with
    ``delta_ratio=None`` and a ``"reason"`` key explaining why no comparison
    was made.
    """
    if resident_baseline <= 0:
        return {
            "v2_mean": v2_mean,
            "resident_baseline": resident_baseline,
            "delta_ratio": None,
            "threshold": threshold,
            "regression": False,
            "reason": "resident_baseline must be > 0 to compute a delta_ratio; comparison skipped",
        }
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
    # routes reliable_chat_completion_async straight to the async HTTP POST
    # against an arbitrary base_url without a special-cased provider branch
    # (anthropic/gemini bounce through anyio.to_thread + a real-provider
    # wire shape; openai can bounce through the Responses API for reasoning
    # models) — openai_compatible always POSTs {base_url}/chat/completions
    # with the plain OpenAI chat-completions request/response shape, which
    # is exactly what MockProvider speaks. This exercises the SAME code path
    # (responder.respond -> provider_client.reliable_chat_completion_async)
    # the real V2 worker uses in production, just pointed at the mock.
    provider_config = provider_client.ProviderConfig(
        provider="openai_compatible",
        model="loadtest-mock",
        api_key="mock-key",
        base_url=mock_base_url,
    )
    totals: list[float] = []
    for fixture in fixtures:
        usage_out: dict[str, Any] = {}
        await v2_responder.respond(
            provider_config=provider_config,
            summary=str(fixture.get("summary") or ""),
            tail=list(fixture.get("tail") or []),
            usage_out=usage_out,
        )
        prompt_tokens = usage_out.get("prompt_tokens") or 0
        completion_tokens = usage_out.get("completion_tokens") or 0
        totals.append(float(prompt_tokens) + float(completion_tokens))
    return totals


def measure_v2_tokens_per_turn(
    fixtures: list[dict[str, Any]], *, mock_base_url: str
) -> float:
    """Drive each fixture (``{"summary": str, "tail": list[dict]}``) through
    the real ``model_api_runtime.v2.responder.respond`` path against a mock
    provider listening at ``mock_base_url``, and return the mean tokens/turn
    (prompt_tokens + completion_tokens per turn, averaged across fixtures).

    Raises if ``fixtures`` is empty (nothing to measure -> mean is undefined;
    callers should always pass at least one fixture).
    """
    if not fixtures:
        raise ValueError("measure_v2_tokens_per_turn requires at least one fixture")
    totals = asyncio.run(
        _measure_v2_tokens_per_turn_async(fixtures, mock_base_url=mock_base_url)
    )
    return sum(totals) / len(totals)


async def _drive_turn_async(provider_config, fixture: dict[str, Any]) -> None:
    """跑一个**完整回合**的 LLM 调用序列：planner（可能多轮）+ responder。

    这是 token 门唯一正确的观测口径。老的 `measure_v2_tokens_per_turn` 只跑 responder，
    因此对"循环让 planner 多跑几轮"这件事完全失明——而那恰恰是本次改动的全部风险。
    token 计数不在这里做：由 MockProvider 的服务端累加器统计，它看得见每一次调用，
    无论调用方是谁、调了几次。
    """
    tail = list(fixture.get("tail") or [])
    coalesced = [m for m in tail if m.get("role") == "user"]
    await v2_planner.plan(
        None,
        provider_config=provider_config, is_official=True,
        coalesced_messages=coalesced,
        digest={"messages": [{"content": str(m.get("content") or "")[:400]} for m in coalesced[-6:]]},
        memory_index={}, perception_summary={}, runtime_state={},
        lane="chat", reason="loadtest",
    )
    await v2_responder.respond(
        provider_config=provider_config,
        summary=str(fixture.get("summary") or ""),
        tail=tail,
    )


def measure_turn_tokens(fixtures: list[dict[str, Any]], *, provider) -> dict[str, Any]:
    """把每个 fixture 当成一个完整回合跑过 planner+responder，返回每回合的 token 与
    LLM 调用次数均值。`provider` 是一个已启动的 `MockProvider(estimate_tokens=True)`。

    fixtures 为空 → 抛（均值无定义）。
    """
    if not fixtures:
        raise ValueError("measure_turn_tokens requires at least one fixture")
    provider_config = provider_client.ProviderConfig(
        provider="openai_compatible", model="loadtest-mock",
        api_key="mock-key", base_url=provider.base_url,
    )

    async def _run() -> None:
        for fixture in fixtures:
            await _drive_turn_async(provider_config, fixture)

    asyncio.run(_run())
    turns = float(len(fixtures))
    total = provider.total_prompt_tokens + provider.total_completion_tokens
    return {
        "tokens_per_turn": total / turns,
        "llm_calls_per_turn": provider.request_count / turns,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare V2's mean tokens/turn (measured against a mock "
        "provider) to a supplied resident-runtime baseline. Exits nonzero "
        "(the rollback signal) if V2 regresses beyond --threshold."
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
    with MockProvider() as provider:
        v2_mean = measure_v2_tokens_per_turn(
            _DEFAULT_FIXTURES, mock_base_url=provider.base_url
        )
    result = compare_tokens_per_turn(
        v2_mean, args.resident_baseline, threshold=args.threshold
    )
    print(json.dumps(result, indent=2))
    return 1 if result["regression"] else 0


if __name__ == "__main__":
    sys.exit(main())
