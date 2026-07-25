"""B2: identity.replace may overwrite the relationship anchor ONLY when the upload carries
an explicit, valid relationship time (real ISO date + evidence); otherwise it is preserved.

These lock the two pure decision helpers — the anchor overwrite must never fire for an
existing (empty relationship time) upload, so the change is additive to the normal flow.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import service as gs  # noqa: E402
from identity import actions  # noqa: E402

_EXISTING = {
    "relationship_started_at": "2026-06-01",
    "relationship_anchor_source": "history_import",
    "relationship_anchor_evidence": "old-evidence",
}


# --- service-layer decision: overwrite vs preserve ---

def test_preserve_when_output_has_no_anchor():
    out = gs._relationship_anchor_fields_for_replace(_EXISTING, {"identity": {}})
    assert out["relationship_started_at"] == "2026-06-01"
    assert out["relationship_anchor_evidence"] == "old-evidence"


def test_overwrite_when_explicit_valid_date_and_evidence():
    out = gs._relationship_anchor_fields_for_replace(_EXISTING, {
        "relationship_anchor": {"relationship_started_at": "2026-01-15",
                                "relationship_anchor_evidence": "user stated in the doc"},
    })
    assert out["relationship_started_at"] == "2026-01-15"
    assert out["relationship_anchor_evidence"] == "user stated in the doc"
    assert out["relationship_anchor_source"] == "upload"


def test_user_calibrated_anchor_never_overwritten_by_redistill():
    # Priority guard: a user_calibrated anchor (the top tier, set when the user
    # explicitly corrects the day count) must survive a redistill/upload that
    # restates a duration — a derived anchor must NOT clobber it.
    existing_calibrated = {
        "relationship_started_at": "2026-06-01",
        "relationship_anchor_source": "user_calibrated",
        "relationship_anchor_evidence": "user set 42 days by hand",
    }
    out = gs._relationship_anchor_fields_for_replace(existing_calibrated, {
        "relationship_anchor": {"relationship_started_at": "2026-01-15",
                                "relationship_anchor_evidence": "doc says two years"},
    })
    assert out == existing_calibrated


def test_preserve_when_date_is_not_a_real_date():
    # legality guard: a vague phrase must NOT reset the anchor
    out = gs._relationship_anchor_fields_for_replace(_EXISTING, {
        "relationship_anchor": {"relationship_started_at": "a while ago",
                                "relationship_anchor_evidence": "x"},
    })
    assert out["relationship_started_at"] == "2026-06-01"


def test_preserve_when_evidence_missing():
    out = gs._relationship_anchor_fields_for_replace(_EXISTING, {
        "relationship_anchor": {"relationship_started_at": "2026-01-15", "relationship_anchor_evidence": ""},
    })
    assert out["relationship_started_at"] == "2026-06-01"


def test_empty_upload_anchor_preserves():
    # the exact "normal flow" case today: 人设 entry uploads relationship_started_at=""
    out = gs._relationship_anchor_fields_for_replace(_EXISTING, {"relationship_anchor": {}})
    assert out == {
        "relationship_started_at": "2026-06-01",
        "relationship_anchor_source": "history_import",
        "relationship_anchor_evidence": "old-evidence",
    }


# --- resident action → anchor builder ---

def test_action_anchor_from_days_with_user():
    a = actions._replace_relationship_anchor({"days_with_user": 10, "relationship_anchor_evidence": "runtime history"})
    assert a["relationship_started_at"] == (date.today() - timedelta(days=10)).isoformat()
    assert a["relationship_anchor_evidence"] == "runtime history"
    assert a["relationship_anchor_source"] == "genesis_resident_distill"


def test_action_anchor_explicit_started_at_wins():
    a = actions._replace_relationship_anchor({"relationship_started_at": "2026-02-02", "relationship_anchor_evidence": "e"})
    assert a["relationship_started_at"] == "2026-02-02"


def test_action_anchor_empty_without_evidence():
    assert actions._replace_relationship_anchor({"days_with_user": 10}) == {}


def test_action_anchor_empty_without_any_time():
    assert actions._replace_relationship_anchor({"relationship_anchor_evidence": "x"}) == {}


def test_action_anchor_rejects_bool_days():
    # days_with_user=True must not be treated as int 1
    assert actions._replace_relationship_anchor({"days_with_user": True, "relationship_anchor_evidence": "e"}) == {}
