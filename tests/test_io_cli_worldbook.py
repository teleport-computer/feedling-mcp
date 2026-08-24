"""Pure tests for the model-invoked World Book read path."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


class _Emitted(Exception):
    def __init__(self, obj, code):
        self.obj = obj
        self.code = code


def _run(monkeypatch, query, response):
    monkeypatch.setattr(
        io_cli,
        "_require_backend",
        lambda: ("https://api.local", {"X-Feedling-Runtime-Token": "rt"}),
    )
    captured = {}

    def _fake_http(method, url, auth, *, payload=None, **kwargs):
        captured.update(
            method=method,
            url=url,
            auth=auth,
            payload=payload,
            kwargs=kwargs,
        )
        return response

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)
    monkeypatch.setattr(
        io_cli,
        "_emit",
        lambda obj, code=0: (_ for _ in ()).throw(_Emitted(obj, code)),
    )
    try:
        io_cli.cmd_worldbook_match(types.SimpleNamespace(query=query))
    except _Emitted as exc:
        return exc.obj, exc.code, captured
    raise AssertionError("cmd_worldbook_match did not emit")


def test_worldbook_match_posts_current_request_and_returns_match(monkeypatch):
    body = {
        "block": "<world_book>影月历</world_book>",
        "matched_names": ["历法"],
        "rejected_over_cap": [],
        "unavailable_ids": [],
    }

    obj, code, captured = _run(monkeypatch, "今天是什么日子", (200, body))

    assert captured == {
        "method": "POST",
        "url": "https://api.local/v1/worldbook/match",
        "auth": {"X-Feedling-Runtime-Token": "rt"},
        "payload": {"message": "今天是什么日子"},
        "kwargs": {"timeout": 20},
    }
    assert obj == {"ok": True, **body}
    assert code == 0


def test_worldbook_match_rejects_blank_query_before_network(monkeypatch):
    obj, code, captured = _run(monkeypatch, "   ", (200, {}))

    assert obj == {"ok": False, "error": "worldbook-match needs --query <text>"}
    assert code == 2
    assert captured == {}


def test_worldbook_match_surfaces_backend_failure(monkeypatch):
    obj, code, _captured = _run(
        monkeypatch,
        "需要设定",
        (503, {"error": "worldbook_unavailable"}),
    )

    assert obj == {
        "ok": False,
        "http_status": 503,
        "error": {"error": "worldbook_unavailable"},
    }
    assert code == 1
