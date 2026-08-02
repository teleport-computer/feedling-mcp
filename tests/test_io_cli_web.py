"""io_cli web-search / web-fetch verbs — HTTP wiring (pure, fully mocked).

These two verbs let a V1 model reach the backend's /v1/agent/web/{search,fetch}
endpoints. The contract this file pins:

  - web-search POSTs to /v1/agent/web/search with a body carrying ``query``
    (plus ``limit`` only when given).
  - web-fetch POSTs to /v1/agent/web/fetch with a body carrying ``url``.
  - the backend's CapabilityResult JSON ({"ok":...,"data"/"error":...}) is
    emitted VERBATIM (no extra {"ok": True, **body} wrapper).
  - ok=false → the full JSON still reaches stdout, but the process exits non-zero.

Everything is monkeypatched (``_http_json``, ``_auth_headers``, ``_emit``) — no
DB, no network — so this is a _PURE_UNIT test.
"""
import sys
import types as _types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


class _Emitted(Exception):
    def __init__(self, obj, code):
        self.obj, self.code = obj, code


def _run(monkeypatch, cmd, ns, response):
    """Drive one web verb with a scripted (status, body) response.

    Returns (emitted_obj, exit_code, captured) where ``captured`` records the
    single HTTP call as {"method", "url", "payload"}.
    """
    monkeypatch.setenv("FEEDLING_API_URL", "https://api.local")
    monkeypatch.setattr(io_cli, "_auth_headers", lambda: {"X-Feedling-Runtime-Token": "rt"})

    captured = {}

    def _fake_http(method, url, auth, *, payload=None, **kw):
        captured.update(method=method, url=url, payload=payload)
        return response

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)
    monkeypatch.setattr(io_cli, "_emit",
                        lambda obj, code=0: (_ for _ in ()).throw(_Emitted(obj, code)))
    try:
        cmd(ns)
    except _Emitted as e:
        return e.obj, e.code, captured
    raise AssertionError(f"{cmd.__name__} did not emit")


# ── web-search ─────────────────────────────────────────────────────────────

def test_web_search_posts_query_to_search_endpoint(monkeypatch):
    backend_json = {"ok": True, "data": {"results": [{"title": "t"}]},
                    "trace": {}, "warnings": []}
    ns = _types.SimpleNamespace(query="今天北京天气", limit=None)
    obj, code, captured = _run(monkeypatch, io_cli.cmd_web_search, ns, (200, backend_json))

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.local/v1/agent/web/search"
    assert captured["payload"] == {"query": "今天北京天气"}  # no limit key when unset
    # emitted verbatim — same object the backend returned, no extra wrapper.
    assert obj == backend_json
    assert code == 0


def test_web_search_includes_limit_when_given(monkeypatch):
    ns = _types.SimpleNamespace(query="btc price", limit=3)
    _obj, _code, captured = _run(
        monkeypatch, io_cli.cmd_web_search, ns,
        (200, {"ok": True, "data": {}, "trace": {}, "warnings": []}),
    )
    assert captured["payload"] == {"query": "btc price", "limit": 3}


def test_web_search_ok_false_still_prints_full_json_but_exits_nonzero(monkeypatch):
    backend_json = {"ok": False, "error": {"code": "web_disabled",
                                           "message": "web access is turned off"}}
    ns = _types.SimpleNamespace(query="anything", limit=None)
    obj, code, _ = _run(monkeypatch, io_cli.cmd_web_search, ns, (200, backend_json))

    assert obj == backend_json          # full CapabilityResult JSON, verbatim
    assert obj["ok"] is False
    assert code != 0                    # ok=false → non-zero exit


# ── web-fetch ──────────────────────────────────────────────────────────────

def test_web_fetch_posts_url_to_fetch_endpoint(monkeypatch):
    backend_json = {"ok": True, "data": {"text": "page body"},
                    "trace": {}, "warnings": []}
    ns = _types.SimpleNamespace(url="https://example.com/article")
    obj, code, captured = _run(monkeypatch, io_cli.cmd_web_fetch, ns, (200, backend_json))

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.local/v1/agent/web/fetch"
    assert captured["payload"] == {"url": "https://example.com/article"}
    assert obj == backend_json
    assert code == 0


def test_web_fetch_ok_false_still_prints_full_json_but_exits_nonzero(monkeypatch):
    backend_json = {"ok": False, "error": {"code": "fetch_failed",
                                           "message": "could not load url"}}
    ns = _types.SimpleNamespace(url="https://example.com/x")
    obj, code, _ = _run(monkeypatch, io_cli.cmd_web_fetch, ns, (200, backend_json))

    assert obj == backend_json
    assert code != 0


# ── non-200 (e.g. 403 before the token carries the "web" scope) ────────────

def test_web_search_non_200_surfaces_status_and_exits_nonzero(monkeypatch):
    # Until the V1 supervisor mints a token with the "web" scope, require_scope
    # 403s. No CapabilityResult comes back, so the verb reports the raw status.
    ns = _types.SimpleNamespace(query="q", limit=None)
    obj, code, _ = _run(
        monkeypatch, io_cli.cmd_web_search, ns,
        (403, {"error": "missing_scope"}),
    )
    assert obj["ok"] is False
    assert obj["http_status"] == 403
    assert "missing_scope" in str(obj["error"])
    assert code != 0
