import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from capabilities import errors
from capabilities import worldbook as cap_worldbook


def test_worldbook_match_forwards_query_and_credentials(monkeypatch):
    observed = {}

    def match(store, payload, *, api_key, runtime_token):
        observed.update({
            "store": store,
            "payload": payload,
            "api_key": api_key,
            "runtime_token": runtime_token,
        })
        return {"block": "lore", "matched_names": ["Moon Court"]}, 200

    monkeypatch.setattr(cap_worldbook.worldbook_core, "match", match)
    result = cap_worldbook.match(
        "STORE",
        api_key="api",
        runtime_token="runtime",
        params={"query": "Luna"},
    )

    assert result.ok is True
    assert result.data == {"block": "lore", "matched_names": ["Moon Court"]}
    assert observed == {
        "store": "STORE",
        "payload": {"message": "Luna"},
        "api_key": "api",
        "runtime_token": "runtime",
    }


def test_worldbook_match_rejects_blank_query_without_read(monkeypatch):
    monkeypatch.setattr(
        cap_worldbook.worldbook_core,
        "match",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("read")),
    )
    result = cap_worldbook.match("STORE", params={"query": "  "})

    assert result.ok is False
    assert result.error["code"] == errors.INVALID
    assert result.error["retryable"] is False


def test_worldbook_match_forwards_trusted_trace_context(monkeypatch):
    observed = {}

    def match(store, payload, **kwargs):
        observed.update(store=store, payload=payload, **kwargs)
        return {"block": "", "matched_names": []}, 200

    monkeypatch.setattr(cap_worldbook.worldbook_core, "match", match)
    result = cap_worldbook.match(
        "STORE",
        params={"query": "Luna"},
        trace_context={
            "trace_id": "trace-wb",
            "job_id": "job-wb",
            "lane": "chat",
            "ignored": "private provider value",
        },
    )

    assert result.ok is True
    assert observed == {
        "store": "STORE",
        "payload": {"message": "Luna"},
        "api_key": None,
        "runtime_token": None,
        "trace_id": "trace-wb",
        "job_id": "job-wb",
        "lane": "chat",
        "actor": "host_agent_runtime",
    }


def test_worldbook_match_surfaces_structured_backend_failure(monkeypatch):
    monkeypatch.setattr(
        cap_worldbook.worldbook_core,
        "match",
        lambda *_args, **_kwargs: (
            {"error": "worldbook_match_unavailable"},
            503,
        ),
    )
    result = cap_worldbook.match("STORE", params={"query": "Luna"})

    assert result.ok is False
    assert result.error["retryable"] is True
