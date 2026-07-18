#!/usr/bin/env python3
"""Finalize an offline persona review without conflating FAIL with execution.

The qualification agent uses this wrapper only against its private review copy.
A schema-valid bounded persona verdict is a successfully completed review step
whether the verdict is positive or negative. Operational, input, or schema
errors remain nonzero. The trusted parent independently re-finalizes its own
immutable capture after the worker exits and is the only release authority.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import genesis_e2e  # noqa: E402
from qa.request_live_scenario_probe import (  # noqa: E402
    LiveProbeRequestError,
    acquire_sequence_phase_gate,
)


def _bounded_report(report: object) -> dict:
    if not isinstance(report, Mapping) or type(report.get("ok")) is not bool:
        raise genesis_e2e.ExistingSessionDistillError(
            "finalize", "persona_review_report_invalid"
        )
    required_mappings = ("checks", "transport", "privacy", "evidence")
    if any(not isinstance(report.get(field), Mapping) for field in required_mappings):
        raise genesis_e2e.ExistingSessionDistillError(
            "finalize", "persona_review_report_invalid"
        )
    try:
        # Round-trip with NaN disabled so only bounded JSON data reaches the
        # agent event stream. Raw imported plaintext is never returned here.
        rendered = json.dumps(
            dict(report),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload = json.loads(rendered)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise genesis_e2e.ExistingSessionDistillError(
            "finalize", "persona_review_report_invalid"
        ) from None
    if len(rendered.encode("utf-8")) > 256 * 1024:
        raise genesis_e2e.ExistingSessionDistillError(
            "finalize", "persona_review_report_too_large"
        )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--private-evidence", required=True)
    parser.add_argument("--semantic-judgment", required=True)
    parser.add_argument("--artifact-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    gate_descriptor: int | None = None
    try:
        sequence_identity = (
            str(os.environ.get("QA_RUN_ID") or ""),
            str(os.environ.get("QA_PROFILE_ID") or ""),
            str(os.environ.get("QA_WORK_ROOT") or ""),
        )
        if any(sequence_identity):
            if not all(sequence_identity):
                raise LiveProbeRequestError(
                    "persona finalizer sequence identity is incomplete"
                )
            work_root = Path(sequence_identity[2])
            if Path(args.private_evidence).parent != work_root:
                raise LiveProbeRequestError(
                    "persona finalizer work root is inconsistent"
                )
            gate_descriptor = acquire_sequence_phase_gate(
                work_root,
                run_id=sequence_identity[0],
                profile_id=sequence_identity[1],
                phase="P0-06-FINALIZE",
            )
        report = genesis_e2e.finalize_existing_session_distill_acceptance(
            private_evidence_path=args.private_evidence,
            semantic_judgment_path=args.semantic_judgment,
            fixture=genesis_e2e._load_fixture(args.fixture),
            artifact_dir=args.artifact_dir,
        )
        payload = _bounded_report(report)
    except LiveProbeRequestError:
        print(
            json.dumps(
                {
                    "code": "persona_review_sequence_invalid",
                    "ok": False,
                    "stage": "sequence",
                },
                sort_keys=True,
            )
        )
        return 1
    except genesis_e2e.ExistingSessionDistillError as exc:
        print(json.dumps(exc.as_result(), sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    # A legitimate negative semantic/security verdict is completed evidence,
    # not an invocation failure. The parent-owned finalizer decides the gate.
    # Do not close gate_descriptor: the OS releases this advisory lock only as
    # the exact FINALIZE process exits, before P0-07 may acquire it.
    _ = gate_descriptor
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
