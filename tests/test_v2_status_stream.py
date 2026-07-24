"""Public V2 status payloads expose only fixed labels and coarse counts."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import status_stream as ss  # noqa: E402


def test_redact_carries_only_label_and_coarse_count():
    event = ss.redact_status("writing_reply", count=3)
    assert event == {
        "kind": "writing_reply",
        "label": "正在回复（3）",
        "detail": {"count": 3},
    }


def test_redact_status_error_kind_has_neutral_label_not_fallback():
    event = ss.redact_status("error")
    assert event["kind"] == "error"
    assert event["label"] == "出问题了"
    assert event["detail"] == {}


def test_unknown_kind_uses_generic_processing_label_without_echoing_input():
    event = ss.redact_status("attacker-controlled-tool-name")
    assert event == {
        "kind": "attacker-controlled-tool-name",
        "label": "处理中",
        "detail": {},
    }
    assert "attacker" not in event["label"]
