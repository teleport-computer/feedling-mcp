"""Measure the RESIDENT runtime's tokens/turn by pointing its agent CLI at MockProvider.

Why this exists
---------------
`deploy/HOSTED_RUNTIME_V2_ROLLOUT.md` Step 1.3 gates the rollout on
``compare_tokens.py --resident-baseline <N>``. Nobody ever measured N, so the gate
was un-runnable. This produces N honestly.

Why it must spawn the real CLI
------------------------------
The resident does NOT call the provider itself — it shells out to `codex` (or
`claude`), which injects its OWN system prompt and tool catalog into every request.
Measured on codex-cli 0.142.5 for a trivial "say ok" prompt: `instructions` alone is
~20,771 chars and `tools` carries 9 function definitions, for a ~38 KB request body.
That overhead is re-sent on every turn and dominates the resident's tokens/turn.
Estimating it from our own prompt text would understate it by more than 10x — a
fabricated baseline is worse than none, because it green-lights a real regression.

Wire note
---------
codex speaks the OpenAI **Responses** API (`POST /v1/responses`), not
`/chat/completions`. `MockProvider` serves both; token accounting for the Responses
route sums `instructions` + `input` + `tools`.

Gotcha
------
`codex exec` reads stdin when it is a pipe ("Reading additional input from stdin...").
Always pass ``stdin=DEVNULL`` or it hangs forever and you will see zero requests.

Usage
-----
    python scripts/loadtest/measure_resident.py            # 3 default fixtures
    python scripts/loadtest/measure_resident.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.loadtest.mock_provider import MockProvider

# The same user messages the V2 single-round baseline used, so the two numbers
# are comparable (see docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md).
DEFAULT_FIXTURES = [
    "今天过得怎么样",
    "我还是有点焦虑，面试没过",
    "帮我回忆一下上周说的那个计划",
]

_CONFIG_TOML = """\
model = "gpt-5"
model_provider = "loadtest"
[model_providers.loadtest]
name = "loadtest"
base_url = "{base_url}/v1"
env_key = "CODEX_API_KEY"
wire_api = "responses"
[features]
collab = false
"""


def run_codex_turn(prompt: str, *, base_url: str, codex_home: Path, workdir: Path,
                   timeout: float = 90.0) -> int:
    """Drive one codex turn against the mock. Returns the process return code."""
    (codex_home / "config.toml").write_text(_CONFIG_TOML.format(base_url=base_url))
    env = {
        **_os_environ(),
        "CODEX_HOME": str(codex_home),
        "CODEX_API_KEY": "loadtest-mock-key",
    }
    proc = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", prompt],
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,   # NOT optional — see module docstring
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return proc.returncode


def _os_environ() -> dict:
    import os
    return dict(os.environ)


def measure(fixtures: list[str]) -> dict:
    """Run each fixture as one resident turn; read the mock's server-side accumulators."""
    if not fixtures:
        raise ValueError("measure_resident needs at least one fixture")
    with MockProvider(reply="ok", estimate_tokens=True) as provider:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex_home"
            codex_home.mkdir()
            # An EMPTY workdir on purpose: codex folds any AGENTS.md / repo instructions
            # it finds into `instructions`. Running inside this repo would inflate the
            # baseline with OUR docs and make the number unreproducible elsewhere.
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            failures = 0
            for prompt in fixtures:
                rc = run_codex_turn(prompt, base_url=provider.base_url,
                                    codex_home=codex_home, workdir=workdir)
                if rc != 0:
                    failures += 1
        turns = float(len(fixtures))
        total = provider.total_prompt_tokens + provider.total_completion_tokens
        return {
            "tokens_per_turn": total / turns,
            "llm_calls_per_turn": provider.request_count / turns,
            "prompt_tokens_total": provider.total_prompt_tokens,
            "completion_tokens_total": provider.total_completion_tokens,
            "turns": int(turns),
            "codex_failures": failures,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = ap.parse_args(argv)

    report = measure(DEFAULT_FIXTURES)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(json.dumps(report, indent=2))
        print("\nFeed this into the D4 rollback gate:")
        print(f"  python scripts/loadtest/compare_tokens.py "
              f"--resident-baseline {report['tokens_per_turn']:.1f}")
    if report["codex_failures"]:
        print(f"WARNING: {report['codex_failures']} codex turn(s) exited non-zero; "
              f"tokens still counted, but investigate before trusting the number.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
