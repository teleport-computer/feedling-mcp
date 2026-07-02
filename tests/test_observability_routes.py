from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from flask import Flask  # noqa: E402
from observability import routes as obs_routes  # noqa: E402


def _app(monkeypatch):
    monkeypatch.setattr(obs_routes, "require_admin", lambda: None)
    app = Flask(__name__)
    app.register_blueprint(obs_routes.bp)
    return app


def test_json_samples(monkeypatch):
    monkeypatch.setattr(obs_routes.store, "recent_samples", lambda hours: [{"ts": "x", "live_agents": 98}])
    c = _app(monkeypatch).test_client()
    r = c.get("/v1/admin/observability/samples?hours=6")
    assert r.status_code == 200 and r.get_json()[0]["live_agents"] == 98


def test_html_page(monkeypatch):
    monkeypatch.setattr(obs_routes.store, "latest_sample", lambda: {"live_agents": 98, "by_driver": {}})
    monkeypatch.setattr(obs_routes.store, "recent_samples", lambda hours: [])
    c = _app(monkeypatch).test_client()
    r = c.get("/admin/observability")
    assert r.status_code == 200 and b"observability" in r.data.lower()
