from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_persona_sop_documents_diagnostic_capture_only_exception():
    for relative_path in (
        "qa/SOP.md",
        "qa/scenarios/api-key-journey.md",
    ):
        contract = _normalized(ROOT / relative_path)

        assert "A parent-owned PASS capture" in contract
        assert "non-PASS parent capture with no private review evidence" in contract
        assert "MUST NOT run REVIEW or FINALIZE" in contract or (
            "MUST NOT invoke REVIEW or FINALIZE" in contract
        )
        assert "MUST continue with P0-07" in contract
        assert "Strict qualification rejects" in contract
