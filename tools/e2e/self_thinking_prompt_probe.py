#!/usr/bin/env python3
"""T403 self-thinking prompt A/B matrix.

The default operation is offline plan generation.  Provider execution requires
both an explicit ``--execute`` action and
``FEEDLING_T403_PROVIDER_RUN_ALLOWED=P0_COMPLETE``.  This prevents an innocent
import, test, ``--help``, or plan review from consuming provider quota.

Each JSONL result row carries both arms' resolved ``__file__`` path and the
language-specific instruction SHA-256.  The baseline is loaded from the
installed ``agent-protocol-core`` distribution; the candidate is loaded from
the exact memgarden source path supplied when the plan is built.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import random
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from agent_protocol_core import self_thinking as installed_self_thinking  # noqa: E402

from evals import language as language_eval  # noqa: E402
from tools.e2e.config import load_keys  # noqa: E402


CORE_DISTRIBUTION = "agent-protocol-core"
CORE_MODULE_RELATIVE = Path("agent_protocol_core/self_thinking.py")
CANDIDATE_MODULE_RELATIVE = Path(
    "packages/agent-protocol-core/src/agent_protocol_core/self_thinking.py"
)
PROVIDER_RUN_GATE = "FEEDLING_T403_PROVIDER_RUN_ALLOWED"
PROVIDER_RUN_GATE_VALUE = "P0_COMPLETE"

LANGUAGES = ("zh", "en")
ARMS = ("baseline", "candidate")

USER_PROMPTS = {
    "zh": (
        "我今天终于把书桌挪到了窗边，下午写东西时能看到树影。"
        "这件小事让我心情很好，你简单回应我一下。"
    ),
    "en": (
        "I finally moved my desk beside the window today, and now I can watch "
        "the shadows of the trees while I write. This small change made me "
        "genuinely happy. Please respond briefly."
    ),
}


class ProbeConfigurationError(ValueError):
    """The frozen plan or local inputs cannot support a valid measurement."""


class UnmeasuredInput(RuntimeError):
    """No model response was measured for this scheduled row."""


@dataclass(frozen=True)
class MatrixCell:
    id: str
    access_cell: str
    provider: str
    model: str
    tier: str
    key_env: str
    base_url_env: str = ""
    model_env: str = ""

    def resolve_model(self, pool: dict[str, str]) -> str:
        if self.model_env:
            return str(pool.get(self.model_env) or "").strip()
        return self.model

    def resolve_base_url(self, pool: dict[str, str]) -> str:
        if not self.base_url_env:
            return ""
        return str(pool.get(self.base_url_env) or "").strip()


# Official providers carry both a flagship and a small tier.  Relays are
# separate access cells because a relay outage is UNMEASURED for that route,
# not evidence about the upstream provider family.
MATRIX_CELLS = (
    MatrixCell(
        "anthropic-flagship", "anthropic-official", "anthropic",
        "claude-sonnet-4-6", "flagship", "E2E_KEY_ANTHROPIC",
    ),
    MatrixCell(
        "anthropic-small", "anthropic-official", "anthropic",
        "claude-haiku-4-5-20251001", "small", "E2E_KEY_ANTHROPIC",
    ),
    MatrixCell(
        "openai-flagship", "openai-official", "openai",
        "gpt-5.2", "flagship", "E2E_KEY_OPENAI",
    ),
    MatrixCell(
        "openai-small", "openai-official", "openai",
        "gpt-5.2-mini", "small", "E2E_KEY_OPENAI",
    ),
    MatrixCell(
        "gemini-flagship", "gemini-official", "gemini",
        "gemini-3.1-pro-preview", "flagship", "E2E_KEY_GEMINI",
    ),
    MatrixCell(
        "gemini-small", "gemini-official", "gemini",
        "gemini-3.6-flash", "small", "E2E_KEY_GEMINI",
    ),
    MatrixCell(
        "deepseek-flagship", "deepseek-official", "deepseek",
        "deepseek-v4-pro", "flagship", "E2E_KEY_DEEPSEEK",
    ),
    MatrixCell(
        "deepseek-small", "deepseek-official", "deepseek",
        "deepseek-v4-flash", "small", "E2E_KEY_DEEPSEEK",
    ),
    MatrixCell(
        "openrouter-flagship", "openrouter", "openrouter",
        "anthropic/claude-sonnet-4.6", "flagship", "E2E_KEY_OPENROUTER",
    ),
    MatrixCell(
        "openrouter-small", "openrouter", "openrouter",
        "openai/gpt-5.2-mini", "small", "E2E_KEY_OPENROUTER",
    ),
    MatrixCell(
        "relay-openai-compatible", "relay-openai-compatible",
        "openai_compatible", "", "relay", "E2E_KEY_RELAY",
        base_url_env="E2E_RELAY_BASE", model_env="E2E_RELAY_MODEL",
    ),
    MatrixCell(
        "hojimi-relay", "hojimi-relay", "openai_compatible",
        "claude-haiku-4-5-20251001", "relay", "E2E_KEY_HOJIMI",
        base_url_env="E2E_HOJIMI_BASE",
    ),
)

PROFILE_DEFAULT_ROUNDS = {
    "canary": 1,
    "probe": 2,
    "full": 5,
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_candidate_module(candidate_repo: Path):
    source_file = (candidate_repo / CANDIDATE_MODULE_RELATIVE).resolve()
    if not source_file.is_file():
        raise ProbeConfigurationError(
            f"candidate self-thinking source is missing: {source_file}"
        )
    spec = util.spec_from_file_location("t403_candidate_self_thinking", source_file)
    if spec is None or spec.loader is None:
        raise ProbeConfigurationError(f"cannot load candidate source: {source_file}")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolved_module_file = Path(str(module.__file__)).resolve()
    if resolved_module_file != source_file:
        raise ProbeConfigurationError(
            f"candidate import resolved to {resolved_module_file}, expected {source_file}"
        )
    for name in ("INSTRUCTION_ZH", "INSTRUCTION_EN", "instruction_for_language"):
        if not hasattr(module, name):
            raise ProbeConfigurationError(f"candidate source lacks {name}")
    return module, source_file


def load_arm_texts(candidate_repo: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Resolve and anchor the installed baseline and source candidate arms."""

    repo = Path(candidate_repo).resolve()
    distribution = metadata.distribution(CORE_DISTRIBUTION)
    expected_baseline = Path(
        distribution.locate_file(str(CORE_MODULE_RELATIVE))
    ).resolve()
    actual_baseline = Path(str(installed_self_thinking.__file__)).resolve()
    if actual_baseline != expected_baseline:
        raise ProbeConfigurationError(
            "baseline import did not resolve to the installed distribution: "
            f"actual={actual_baseline} expected={expected_baseline}"
        )
    candidate_module, candidate_file = _load_candidate_module(repo)

    baseline_module_sha = _sha256_file(actual_baseline)
    candidate_module_sha = _sha256_file(candidate_file)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for language in LANGUAGES:
        baseline_text = str(installed_self_thinking.INSTRUCTION)
        candidate_text = str(
            candidate_module.instruction_for_language(
                "en" if language == "en" else "zh-Hans"
            )
        )
        baseline_provenance = language_eval.ArmProvenance.from_text(
            module_file=actual_baseline,
            text=baseline_text,
            module_sha256=baseline_module_sha,
            distribution=str(distribution.metadata.get("Name") or CORE_DISTRIBUTION),
            version=str(distribution.version),
        )
        candidate_provenance = language_eval.ArmProvenance.from_text(
            module_file=candidate_file,
            text=candidate_text,
            module_sha256=candidate_module_sha,
        )
        language_eval.assert_distinct_arm_sources(
            baseline_provenance,
            candidate_provenance,
        )
        out[language] = {
            "baseline": {
                "text": baseline_text,
                "provenance": baseline_provenance,
            },
            "candidate": {
                "text": candidate_text,
                "provenance": candidate_provenance,
            },
        }
    return out


def _assert_non_vacuous_arms(
    arms: dict[str, dict[str, dict[str, Any]]],
) -> None:
    identical = {
        language: (
            arms[language]["baseline"]["provenance"].text_sha256
            == arms[language]["candidate"]["provenance"].text_sha256
        )
        for language in LANGUAGES
    }
    if all(identical.values()):
        raise ProbeConfigurationError(
            "VACUOUS: baseline and candidate text are identical in every language"
        )


def cells_for_profile(profile: str) -> list[MatrixCell]:
    if profile == "canary":
        return [cell for cell in MATRIX_CELLS if cell.id == "anthropic-small"]
    if profile == "probe":
        return [cell for cell in MATRIX_CELLS if cell.tier in {"small", "relay"}]
    if profile == "full":
        return list(MATRIX_CELLS)
    raise ProbeConfigurationError(f"unknown profile: {profile!r}")


def _selected_cells(profile: str, only: set[str]) -> list[MatrixCell]:
    available = {cell.id: cell for cell in MATRIX_CELLS}
    unknown = only - set(available)
    if unknown:
        raise ProbeConfigurationError(
            f"unknown matrix cell(s): {', '.join(sorted(unknown))}"
        )
    if only:
        cells = [cell for cell in MATRIX_CELLS if cell.id in only]
        if profile == "canary" and len(cells) != 1:
            raise ProbeConfigurationError("canary --only must select exactly one cell")
    else:
        cells = cells_for_profile(profile)
    if not cells:
        raise ProbeConfigurationError("selected profile contains no matrix cells")
    return cells


def build_plan(
    *,
    candidate_repo: str | Path,
    profile: str,
    rounds: int | None = None,
    only: set[str] | None = None,
    seed: int = 403,
    run_id: str = "",
) -> dict[str, Any]:
    """Build a frozen, fully provenanced schedule without provider calls."""

    arms = load_arm_texts(candidate_repo)
    _assert_non_vacuous_arms(arms)
    cells = _selected_cells(profile, only or set())
    selected_rounds = PROFILE_DEFAULT_ROUNDS[profile] if rounds is None else int(rounds)
    if selected_rounds < 1:
        raise ProbeConfigurationError("rounds must be positive")
    replicates = 1 if profile == "canary" else 2
    run_id = run_id or datetime.now(timezone.utc).strftime("t403-%Y%m%dT%H%M%SZ")

    schedule: list[dict[str, Any]] = []
    for cell in cells:
        for language in LANGUAGES:
            provenance = {
                arm: asdict(arms[language][arm]["provenance"])
                for arm in ARMS
            }
            for replicate in range(replicates):
                for trial in range(selected_rounds):
                    for arm in ARMS:
                        schedule.append(
                            {
                                "access_cell": cell.access_cell,
                                "cell_id": cell.id,
                                "provider": cell.provider,
                                "model": cell.model,
                                "model_env": cell.model_env,
                                "tier": cell.tier,
                                "key_env": cell.key_env,
                                "base_url_env": cell.base_url_env,
                                "language": language,
                                "arm": arm,
                                "replicate": replicate,
                                "trial": trial,
                                "arm_provenance": provenance,
                                "active_source_file": provenance[arm]["module_file"],
                                "active_text_sha256": provenance[arm]["text_sha256"],
                                "arms_text_identical": (
                                    provenance["baseline"]["text_sha256"]
                                    == provenance["candidate"]["text_sha256"]
                                ),
                                "instruction": arms[language][arm]["text"],
                            }
                        )
    # Interleave arms/cells deterministically so time drift does not line up with
    # one treatment arm.  The seed is stored in the plan for exact replay.
    random.Random(seed).shuffle(schedule)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "profile": profile,
        "rounds_per_replicate": selected_rounds,
        "replicates": replicates,
        "seed": seed,
        "candidate_repo": str(Path(candidate_repo).resolve()),
        "prefill": False,
        "schedule": schedule,
    }


def _plan_provenance(plan: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    by_language: dict[str, dict[str, dict[str, str]]] = {}
    for entry in list(plan.get("schedule") or []):
        language = str(entry.get("language") or "")
        provenance = entry.get("arm_provenance")
        if language not in LANGUAGES or not isinstance(provenance, dict):
            raise ProbeConfigurationError("plan contains malformed provenance")
        try:
            expected_identical = (
                provenance["baseline"]["text_sha256"]
                == provenance["candidate"]["text_sha256"]
            )
        except (KeyError, TypeError) as exc:
            raise ProbeConfigurationError("plan contains malformed provenance") from exc
        if entry.get("arms_text_identical") is not expected_identical:
            raise ProbeConfigurationError(
                "plan arms_text_identical does not match its text hashes"
            )
        existing = by_language.setdefault(language, provenance)
        if existing != provenance:
            raise ProbeConfigurationError(
                f"plan contains multiple prompt provenances for language {language}"
            )
    if set(by_language) != set(LANGUAGES):
        raise ProbeConfigurationError("plan does not contain both zh and en rows")
    return by_language


def validate_plan_sources(plan: dict[str, Any]) -> None:
    """Re-read both artifacts immediately before execution and reject drift."""

    current = load_arm_texts(str(plan.get("candidate_repo") or ""))
    _assert_non_vacuous_arms(current)
    planned = _plan_provenance(plan)
    for language in LANGUAGES:
        for arm in ARMS:
            expected = asdict(current[language][arm]["provenance"])
            if planned[language].get(arm) != expected:
                raise ProbeConfigurationError(
                    f"{language}/{arm} prompt source or hash drifted after plan creation"
                )


def build_messages(instruction: str, language: str) -> list[dict[str, str]]:
    """Build the fixed A/B prompt; no assistant prefill or tool catalog."""

    system = (
        str(instruction).strip()
        + "\n\n"
        + language_eval.self_thinking_reply_language_rule(language)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": USER_PROMPTS[language]},
    ]


def _require_provider_approval() -> None:
    if os.environ.get(PROVIDER_RUN_GATE) != PROVIDER_RUN_GATE_VALUE:
        raise ProbeConfigurationError(
            "provider execution is locked; wait for the P0-complete signal, then set "
            f"{PROVIDER_RUN_GATE}={PROVIDER_RUN_GATE_VALUE}"
        )


def _call_provider(entry: dict[str, Any], pool: dict[str, str]) -> str:
    """Make one real provider call.  Called only behind the execution gate."""

    import provider_client

    key_env = str(entry.get("key_env") or "")
    key = str(pool.get(key_env) or "").strip()
    if not key:
        raise UnmeasuredInput(f"missing provider credential {key_env}")
    model_env = str(entry.get("model_env") or "")
    model = (
        str(pool.get(model_env) or "").strip()
        if model_env
        else str(entry.get("model") or "").strip()
    )
    if not model:
        raise UnmeasuredInput(f"missing provider model {model_env or 'model'}")
    base_url_env = str(entry.get("base_url_env") or "")
    base_url = str(pool.get(base_url_env) or "").strip() if base_url_env else ""
    if base_url_env and not base_url:
        raise UnmeasuredInput(f"missing relay base URL {base_url_env}")
    result = provider_client.chat_completion(
        provider_client.ProviderConfig(
            provider=str(entry["provider"]),
            model=model,
            api_key=key,
            base_url=base_url,
        ),
        build_messages(str(entry["instruction"]), str(entry["language"])),
        max_tokens=700,
        temperature=0.7,
        timeout=120.0,
        require_reply=False,
        include_reasoning=False,
        tools=None,
    )
    return str(result.get("reply") or "")


def _arm_provenance(entry: dict[str, Any], arm: str) -> language_eval.ArmProvenance:
    try:
        return language_eval.ArmProvenance(**entry["arm_provenance"][arm])
    except (KeyError, TypeError) as exc:
        raise ProbeConfigurationError(
            f"scheduled row lacks {arm} provenance"
        ) from exc


def execute_plan(
    plan: dict[str, Any],
    *,
    pool: dict[str, str],
    call_provider: Callable[[dict[str, Any], dict[str, str]], str] = _call_provider,
) -> list[dict[str, Any]]:
    """Execute a frozen plan after gates and provenance revalidation."""

    _require_provider_approval()
    validate_plan_sources(plan)
    rows = []
    for entry in list(plan.get("schedule") or []):
        model_env = str(entry.get("model_env") or "")
        model = (
            str(pool.get(model_env) or "").strip()
            if model_env
            else str(entry.get("model") or "")
        )
        common = {
            "run_id": str(plan["run_id"]),
            "access_cell": str(entry["access_cell"]),
            "provider": str(entry["provider"]),
            "model": model,
            "tier": str(entry["tier"]),
            "language": str(entry["language"]),
            "arm": str(entry["arm"]),
            "replicate": int(entry["replicate"]),
            "trial": int(entry["trial"]),
            "baseline": _arm_provenance(entry, "baseline"),
            "candidate": _arm_provenance(entry, "candidate"),
        }
        try:
            response = call_provider(entry, pool)
        except Exception as exc:  # noqa: BLE001 - classify before recording
            import provider_client

            if not isinstance(exc, (UnmeasuredInput, provider_client.ProviderError)):
                # A broken scorer/fixture/callback is a harness failure, not a
                # missing provider observation.  Do not launder it into the
                # UNMEASURED bucket used for credentials and transport errors.
                raise
            rows.append(
                language_eval.build_self_thinking_trial(
                    **common,
                    unmeasured_reason=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
        else:
            rows.append(
                language_eval.build_self_thinking_trial(
                    **common,
                    response=response,
                )
            )
    return rows


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _canary_allows_matrix(path: str | Path, plan: dict[str, Any]) -> None:
    rows = _read_jsonl(path)
    if not language_eval.canary_has_product_success(rows):
        raise ProbeConfigurationError(
            "canary has no measured row where the system passed all three metrics"
        )
    planned = _plan_provenance(plan)
    canary: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        language = str(row.get("language") or "")
        provenance = row.get("arm_provenance")
        if language not in LANGUAGES or not isinstance(provenance, dict):
            raise ProbeConfigurationError("canary contains malformed provenance")
        existing = canary.setdefault(language, provenance)
        if existing != provenance:
            raise ProbeConfigurationError(
                f"canary contains multiple prompt provenances for language {language}"
            )
    if canary != planned:
        raise ProbeConfigurationError(
            "canary prompt provenance does not match the matrix plan"
        )


def _parse_only(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", metavar="PATH", help="write an offline frozen plan")
    action.add_argument("--execute", metavar="PLAN", help="execute a frozen plan")
    action.add_argument("--summarize", metavar="JSONL", help="summarize saved rows offline")
    parser.add_argument("--candidate-repo", default="")
    parser.add_argument("--profile", choices=tuple(PROFILE_DEFAULT_ROUNDS), default="probe")
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--only", default="", help="comma-separated matrix cell ids")
    parser.add_argument("--seed", type=int, default=403)
    parser.add_argument("--output", default="")
    parser.add_argument("--canary-results", default="")
    args = parser.parse_args(argv)

    try:
        if args.plan:
            if not args.candidate_repo:
                parser.error("--candidate-repo is required with --plan")
            plan = build_plan(
                candidate_repo=args.candidate_repo,
                profile=args.profile,
                rounds=args.rounds,
                only=_parse_only(args.only),
                seed=args.seed,
            )
            Path(args.plan).write_text(
                json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            print(
                f"offline plan: {args.plan} rows={len(plan['schedule'])} "
                f"profile={plan['profile']} provider_calls=0"
            )
            return 0

        if args.execute:
            if not args.output:
                parser.error("--output is required with --execute")
            plan = json.loads(Path(args.execute).read_text())
            if plan.get("profile") != "canary":
                if not args.canary_results:
                    parser.error("non-canary execution requires --canary-results")
                _canary_allows_matrix(args.canary_results, plan)
            rows = execute_plan(plan, pool=load_keys())
            _write_jsonl(args.output, rows)
            print(f"results: {args.output} rows={len(rows)}")
            return 0

        summary = language_eval.summarize_self_thinking_trials(
            _read_jsonl(args.summarize)
        )
        rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).write_text(rendered)
            print(f"summary: {args.output} cells={len(summary['cells'])}")
        else:
            print(rendered, end="")
        return 0
    except (ProbeConfigurationError, language_eval.SelfThinkingEvalError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
