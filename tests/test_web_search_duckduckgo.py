"""DuckDuckGo HTML parser contract; all HTTP is replaced with fixtures."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime import tools  # noqa: E402


_REAL_RESULT_EXCERPT = (
    Path(__file__).parent / "fixtures" / "duckduckgo_html_result_excerpt.html"
).read_text()
_REAL_CHALLENGE_EXCERPT = (
    Path(__file__).parent / "fixtures" / "duckduckgo_html_challenge_excerpt.html"
).read_text()


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _serve(monkeypatch, body: str) -> None:
    monkeypatch.setattr(tools.httpx, "get", lambda *args, **kwargs: _Response(body))


def test_real_response_excerpt_pins_container_independently_from_item_anchor():
    """Guard-for-guard: the fixture is a raw live-response excerpt captured
    2026-08-27, and neither assertion is constructed from production constants.
    """
    assert re.search(
        r'<div\s+id="links"\s+class="results">', _REAL_RESULT_EXCERPT
    )
    assert re.search(
        r'<a[^>]+class="[^"]*\bresult__a\b[^"]*"', _REAL_RESULT_EXCERPT
    )


def test_missing_result_container_is_retryable_upstream_shape(monkeypatch):
    # The captured challenge contains several ordinary divs. A guard widened
    # from the real results container to generic markup must still reject it.
    assert "<div" in _REAL_CHALLENGE_EXCERPT
    assert 'id="links"' not in _REAL_CHALLENGE_EXCERPT
    _serve(monkeypatch, _REAL_CHALLENGE_EXCERPT)
    with pytest.raises(RuntimeError, match="not an HTML results page"):
        tools.web_search_duckduckgo("feedling", limit=5, timeout_sec=1)


def test_present_result_container_with_no_items_is_honest_empty(monkeypatch):
    _serve(monkeypatch, '<html><div id="links" class="results"></div></html>')
    assert tools.web_search_duckduckgo(
        "no matching pages", limit=5, timeout_sec=1
    ) == []


def test_real_result_excerpt_still_parses_an_item(monkeypatch):
    _serve(monkeypatch, _REAL_RESULT_EXCERPT)
    (result,) = tools.web_search_duckduckgo("feedling", limit=1, timeout_sec=1)
    assert result["title"] == "GitHub - teleport-computer/feedling-mcp"
    assert result["url"] == "https://github.com/teleport-computer/feedling-mcp"
    assert "Feedling gives your Personal Agent a body" in result["snippet"]
