import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from capabilities.types import CapabilityResult, ok, err  # noqa: E402
from capabilities import errors  # noqa: E402


def test_ok_to_dict():
    r = ok({"n": 1})
    assert r.ok is True
    assert r.to_dict() == {"ok": True, "data": {"n": 1}, "trace": {}, "warnings": []}


def test_err_to_dict():
    r = err("capability_invalid_input", "bad", retryable=False)
    assert r.ok is False
    assert r.to_dict() == {"ok": False, "error": {"code": "capability_invalid_input", "message": "bad", "retryable": False}}


def test_code_and_retryable_for_status():
    assert errors.code_for_status(400) == "capability_invalid_input"
    assert errors.code_for_status(404) == "capability_not_found"
    assert errors.code_for_status(503) == "capability_upstream_error"
    assert errors.retryable_for_status(503) is True
    assert errors.retryable_for_status(400) is False


def test_message_for_body_extracts_and_caps():
    assert errors.message_for_body({"error": "boom"}, "d") == "boom"
    assert errors.message_for_body({"nope": 1}, "default") == "default"
    long = "x" * 5000
    assert errors.message_for_body({"message": long}, "d").endswith("…(capped)")


def test_cap_list_truncates():
    assert errors.cap_list(list(range(100)), limit=10) == list(range(10))
    assert errors.cap_list("not a list") == []


def test_cap_data_caps_lists_and_strings():
    out = errors.cap_data({"items": list(range(100)), "note": "x" * 5000, "n": 7})
    assert len(out["items"]) == 50
    assert out["note"].endswith("…(capped)")
    assert out["n"] == 7
