"""docs/API_ERRORS.md 与代码不脱钩的守卫（spec Phase A / A3）。

不追求全量反向核对（部分 slug 是动态拼接），只锁两个方向：
1. 本计划引入/触碰的关键 slug 必须在文档里；
2. 文档里的每个 slug 行格式合法（可被 iOS 侧脚本解析成本地化表）。
Run:  python -m pytest tests/test_api_errors_doc.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).parent.parent / "docs" / "API_ERRORS.md"

MUST_HAVE = {
    "internal_error", "invalid_payload", "envelope_missing_fields",
    "thinking_envelope_missing_fields", "anchor_required",
    "unauthorized", "forbidden", "service_busy", "not_found",
    "model_api_key_decrypt_failed", "already_answered",
    "unsupported_file_type", "invalid_file",
    # BYOK model catalog (POST /v1/model_api/models) — the client-facing slugs
    # must stay documented; a markdown row alone is not enough, this set guards
    # them. `model_catalog_unsupported` is intentionally absent: it is a 200
    # catalog_supported:false success, not an error slug.
    "model_catalog_auth_failed", "model_catalog_access_denied",
    "model_catalog_rate_limited", "model_catalog_temporarily_unavailable",
    "model_catalog_invalid_response",
}


def _doc_slugs():
    text = DOC.read_text(encoding="utf-8")
    # 契约表行格式：| `slug` | 状态码 | ... |
    return set(re.findall(r"^\| `([a-z][a-z0-9_]+)` \|", text, re.M))


def test_doc_exists_and_has_required_slugs():
    slugs = _doc_slugs()
    missing = MUST_HAVE - slugs
    assert not missing, f"API_ERRORS.md 缺 slug: {sorted(missing)}"


def test_doc_rows_have_status_code_column():
    text = DOC.read_text(encoding="utf-8")
    rows = [l for l in text.splitlines() if re.match(r"^\| `[a-z]", l)]
    assert rows, "契约表为空"
    for l in rows:
        cols = [c.strip() for c in l.split("|")]
        # | `slug` | <code> | <blame> | <说明> | → split 后至少 6 段
        assert len(cols) >= 6, f"行格式不对: {l}"
        assert re.match(r"^\d{3}(/\d{3})*$|^—$", cols[2]), f"状态码列不合法: {l}"
