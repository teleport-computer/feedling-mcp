"""api_error(): 统一错误信封的中央 helper（spec Phase A / A1）。

Run:  python -m pytest tests/test_api_error_envelope.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import responses  # noqa: E402


def _body(resp):
    return json.loads(resp.body)


def test_minimal_envelope_only_error_field():
    resp = responses.api_error(404, "not_found")
    assert resp.status_code == 404
    assert _body(resp) == {"error": "not_found"}   # 可选字段缺省不出现


def test_full_envelope():
    resp = responses.api_error(
        500, "internal_error",
        blame="system", detail={"hint": "x"}, request_id="req_a1b2c3d4")
    assert _body(resp) == {
        "error": "internal_error",
        "blame": "system",
        "detail": {"hint": "x"},
        "request_id": "req_a1b2c3d4",
    }


def test_invalid_blame_rejected():
    # blame 是固定枚举（FRONTEND_ERROR_CONTRACT.md §二），传错是编程错误——立刻炸
    import pytest
    with pytest.raises(ValueError):
        responses.api_error(400, "x", blame="somebody_else")
