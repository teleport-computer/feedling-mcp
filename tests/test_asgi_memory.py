"""Native /v1/memory/* parity (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI routes (``memory.routes_asgi``) return the same status/body as
the Flask oracle (``memory.routes``) for every route, plus auth-failure (401) and
scope-failure (403) on the scope-gated actions surface (``/actions``).
Both sides call the
same framework-neutral ``memory.memory_core``, so a single monkeypatch on the
shared enclave / service module objects covers both paths — keeping the test
fully offline and the E2E envelope handling identical across frameworks (the
server never decrypts; the enclave call is stubbed).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from memory import memory_core  # noqa: E402
from memory import routes_asgi as memory_asgi  # noqa: E402
from memory import service as memory_service  # noqa: E402
import memory_readside_core  # noqa: E402

_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    memory_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _flask(method: str, path: str, *, headers=None, json_body=None):
    client = make_client()
    kw: dict = {"headers": headers or {}}
    if json_body is not None:
        kw["json"] = json_body
    res = client.open(path, method=method, **kw)
    return res.status_code, res.get_json(silent=True)


def _asgi(method: str, path: str, *, headers=None, json_body=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if json_body is not None:
                kw["json"] = json_body
            resp = await client.request(method, path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body

    return asyncio.run(go())


def _both(method, path, *, api_key=None, json_body=None):
    headers = _headers(api_key) if api_key else None
    f = _flask(method, path, headers=headers, json_body=json_body)
    a = _asgi(method, path, headers=headers, json_body=json_body)
    return f, a


def _envelope(user_id: str, mid: str = "mom_test", *, mem_type: str = "fact") -> dict:
    return {
        "id": mid,
        "type": mem_type,
        "body_ct": "ct",
        "nonce": "n",
        "K_user": "ku",
        "K_enclave": "ke",
        "visibility": "shared",
        "owner_user_id": user_id,
        "occurred_at": "2026-06-20T10:00:00",
        "source": "chat",
    }


def _stub_enclave(monkeypatch, fn):
    # One patch on the shared module object covers both Flask + ASGI paths.
    monkeypatch.setattr(memory_readside_core, "post_enclave_readside", fn)


# --------------------------------------------------------------------------- #
# auth-failure (401)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/memory/list", None),
    ("GET", "/v1/memory/get?id=x", None),
    ("GET", "/v1/memory/verify", None),
    ("GET", "/v1/memory/buckets", None),
    ("GET", "/v1/memory/threads", None),
    ("POST", "/v1/memory/index", {}),
    ("POST", "/v1/memory/fetch", {"ids": []}),
    ("POST", "/v1/memory/actions", {}),
    ("POST", "/v1/memory/add", {}),
    ("POST", "/v1/memory/retype", {}),
    ("DELETE", "/v1/memory/delete?id=x", None),
])
def test_no_auth_is_401_parity(user, method, path, body):
    f, a = _both(method, path, json_body=body)
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# scope-failure (403) on the scope-gated actions surface
# --------------------------------------------------------------------------- #

def _token(user_id: str, scope: list[str]) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=scope,
        ttl=900.0,
    )


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/memory/actions", {"actions": []}),
])
def test_scope_missing_is_403_parity(user, monkeypatch, method, path, body):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    uid, _api_key = user
    tok = _token(uid, ["identity"])  # NOT memory
    headers = {"X-Feedling-Runtime-Token": tok}
    f = _flask(method, path, headers=headers, json_body=body)
    a = _asgi(method, path, headers=headers, json_body=body)
    assert f == a == (403, {"error": "forbidden"})


# --------------------------------------------------------------------------- #
# list / get / delete (plain store)
# --------------------------------------------------------------------------- #

def test_list_empty_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/list", api_key=api_key)
    assert f == a == (200, {"moments": [], "total": 0, "next_cursor": None})


def test_list_invalid_limit_400_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/list?limit=abc", api_key=api_key)
    assert f == a == (400, {"error": "invalid limit"})


def _pagination_cards(count: int = 221) -> list[dict]:
    newest = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
    cards = []
    for index in range(count):
        occurred_at = newest - timedelta(seconds=index)
        # Five cards straddle the first 100-card boundary with the exact same
        # timestamp, so a time-only cursor necessarily loses or repeats rows.
        if 98 <= index <= 102:
            occurred_at = newest - timedelta(seconds=98)
        cards.append({
            "id": f"mem_{index:03d}",
            "status": "active",
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "body_ct": f"ciphertext-{index}",
        })
    cards[-2].pop("occurred_at")
    cards[-1]["occurred_at"] = "not-a-timestamp"
    # Match db.memory_load's storage order; list_moments owns presentation order.
    return sorted(cards, key=lambda card: (str(card.get("occurred_at") or ""), card["id"]))


@pytest.fixture()
def list_route_store():
    store = types.SimpleNamespace(user_id="usr_memory_list_cursor")

    async def resolved_auth():
        return memory_asgi.AuthResult(
            store=store,
            user_id=store.user_id,
            runtime_token_claims=None,
            api_key="test-api-key",
        )

    _ASGI.dependency_overrides[memory_asgi.require_auth] = resolved_auth
    try:
        yield store
    finally:
        _ASGI.dependency_overrides.pop(memory_asgi.require_auth, None)


def test_list_cursor_paginates_221_cards_without_duplicates_or_omissions(
    list_route_store, monkeypatch
):
    cards = _pagination_cards()
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: cards)

    pages = []
    cursor = ""
    for _ in range(3):
        query = "/v1/memory/list?limit=100"
        if cursor:
            query += f"&cursor={cursor}"
        status, body = _asgi("GET", query)
        assert status == 200
        assert body["total"] == 221
        pages.append(body)
        cursor = body["next_cursor"] or ""

    ids = [card["id"] for page in pages for card in page["moments"]]
    assert [len(page["moments"]) for page in pages] == [100, 100, 21]
    assert len(ids) == len(set(ids)) == 221
    assert set(ids) == {card["id"] for card in cards}
    assert pages[0]["next_cursor"]
    assert pages[1]["next_cursor"]
    assert pages[2]["next_cursor"] is None
    assert {card["id"] for card in pages[2]["moments"]} >= {"mem_219", "mem_220"}


def test_list_without_cursor_uses_server_display_order(
    list_route_store, monkeypatch
):
    cards = _pagination_cards()
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: cards)

    status, body = _asgi("GET", "/v1/memory/list?limit=100")

    assert status == 200
    # Independent oracle: this fixture is generated newest-first and its only
    # timestamp tie (98...102) has the same ascending ID order as the contract.
    assert [moment["id"] for moment in body["moments"]] == [
        f"mem_{index:03d}" for index in range(100)
    ]
    assert body["total"] == len(cards)
    assert body["next_cursor"]


def test_list_total_is_independent_of_cursor_and_limit(
    list_route_store, monkeypatch
):
    cards = _pagination_cards()
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: cards)

    first_status, first = _asgi("GET", "/v1/memory/list?limit=17")
    next_status, second = _asgi(
        "GET",
        f"/v1/memory/list?limit=3&cursor={first['next_cursor']}",
    )

    assert first_status == next_status == 200
    assert len(first["moments"]) == 17
    assert len(second["moments"]) == 3
    assert first["total"] == second["total"] == 221


def test_list_default_and_max_page_sizes(list_route_store, monkeypatch):
    max_limit = memory_core.MEMORY_LIST_MAX_LIMIT
    cards = _pagination_cards(max_limit + 1)
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: cards)

    default_status, default_page = _asgi("GET", "/v1/memory/list")
    max_status, max_page = _asgi(
        "GET", f"/v1/memory/list?limit={max_limit}"
    )

    assert default_status == max_status == 200
    assert len(default_page["moments"]) == memory_core.MEMORY_LIST_DEFAULT_LIMIT
    assert len(max_page["moments"]) == max_limit
    assert default_page["total"] == max_page["total"] == len(cards)
    assert default_page["next_cursor"] and max_page["next_cursor"]


def test_list_rejects_limit_above_declared_max(list_route_store, monkeypatch):
    max_limit = memory_core.MEMORY_LIST_MAX_LIMIT
    monkeypatch.setattr(
        memory_service,
        "_load_moments",
        lambda _store: pytest.fail("invalid limit must fail before loading memories"),
    )

    status, body = _asgi(
        "GET", f"/v1/memory/list?limit={max_limit + 1}"
    )

    assert (status, body) == (400, {"error": "invalid limit"})


@pytest.mark.parametrize(
    "cursor", ["garbage", "e30", "eyJ2IjoxLCJoIjp0cnVlfQ"]
)
def test_list_rejects_invalid_cursor_with_400(
    list_route_store, monkeypatch, cursor
):
    monkeypatch.setattr(
        memory_service, "_load_moments", lambda _store: _pagination_cards()
    )
    status, body = _asgi("GET", f"/v1/memory/list?cursor={cursor}")
    assert (status, body) == (400, {"error": "invalid cursor"})


def test_list_continues_after_cursor_anchor_disappears(
    list_route_store, monkeypatch
):
    cards = _pagination_cards()
    ordered = memory_core._sort_memory_list(cards)
    current = list(cards)
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: current)

    first_status, first = _asgi("GET", "/v1/memory/list?limit=100")
    assert first_status == 200
    vanished_id = first["moments"][-1]["id"]
    current[:] = [card for card in current if card["id"] != vanished_id]

    status, body = _asgi(
        "GET", f"/v1/memory/list?limit=100&cursor={first['next_cursor']}"
    )

    assert status == 200
    assert [card["id"] for card in body["moments"]] == [
        card["id"] for card in ordered[100:200]
    ]
    assert vanished_id not in {card["id"] for card in body["moments"]}


def test_get_missing_id_400_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/get", api_key=api_key)
    assert f == a == (400, {"error": "id required"})


def test_get_not_found_404_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/get?id=nope", api_key=api_key)
    assert f == a == (404, {"error": "not_found"})


def test_delete_missing_id_400_parity(user):
    _uid, api_key = user
    f, a = _both("DELETE", "/v1/memory/delete", api_key=api_key)
    assert f == a == (400, {"error": "id required"})


def test_delete_not_found_404_parity(user):
    _uid, api_key = user
    f, a = _both("DELETE", "/v1/memory/delete?id=nope", api_key=api_key)
    assert f == a == (404, {"error": "not_found"})


# --------------------------------------------------------------------------- #
# verify (plain store, deterministic within a day)
# --------------------------------------------------------------------------- #

def test_verify_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/verify", api_key=api_key)
    assert f == a
    assert f[0] == 200
    assert f[1]["archive_language"] == "en"
    assert f[1]["counts"]["total"] == 0


# --------------------------------------------------------------------------- #
# index / fetch / buckets / threads (enclave stubbed)
# --------------------------------------------------------------------------- #

def test_index_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    moments = [{
        "v": 1, "id": "m1", "owner_user_id": uid, "visibility": "shared",
        "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "status": "active", "importance": 0.9, "occurred_at": "2026-06-20T10:00:00",
    }]
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [dict(m) for m in moments])

    def fake_enclave(api_key_arg, candidates, *, operation, payload=None, runtime_token=None):
        assert api_key_arg == api_key
        assert operation == "index"
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    _stub_enclave(monkeypatch, fake_enclave)
    f, a = _both("POST", "/v1/memory/index", api_key=api_key, json_body={})
    assert f == a
    assert f[0] == 200
    assert [it["id"] for it in f[1]["items"]] == ["m1"]
    assert f[1]["user_card_count"] == 1


def test_index_database_failure_is_503_not_an_empty_success(user, monkeypatch):
    _uid, api_key = user
    events = []

    def fail_pool():
        raise OSError("database unavailable")

    monkeypatch.setattr(db, "get_pool", fail_pool)
    monkeypatch.setattr(
        memory_core.debug_trace,
        "trace_event",
        lambda _store, **event: events.append(event),
    )

    status, body = _asgi(
        "POST", "/v1/memory/index", headers=_headers(api_key), json_body={})

    assert (status, body) == (503, {"error": "memory_load_failed"})
    events = [e for e in events if e["type"] == "memory.index.called"]
    assert len(events) == 1
    assert events[-1]["type"] == "memory.index.called"
    assert events[-1]["status"] == "failed"
    # Both lanes now carry a closed category rather than the exception text. The
    # response above still names ``memory_load_failed`` because that message is a
    # member of ``_CLOSED_READSIDE_ERRORS`` -- a fixed literal, not upstream text.
    # The messages that *do* carry upstream payload collapse instead; see
    # ``test_readside_503_body_never_carries_the_enclave_response_text``.
    assert events[-1]["detail"]["reason"] == "readside_unavailable"
    assert events[-1]["detail"]["error_class"] == "RuntimeError"
    assert events[-1]["detail"]["upstream"] == "memory_load_failed"


# --------------------------------------------------------------------------- #
# readside error contract (T351)
#
# ``memory_readside_core.post_enclave_readside`` raises
# ``RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")``, so the
# enclave's own response body rides inside the exception message. The trace lane
# already reduced that to a closed category; the response lane returned
# ``str(e)`` verbatim. These tests pin the response lane closed and pin the
# triage signal to the place it moved to.
#
# The leaky message is *produced by the shipped function* rather than written out
# here: a literal would keep passing if the producer's format changed, which is
# the failure mode where the guard silently stops guarding.
# --------------------------------------------------------------------------- #

_ENCLAVE_CANARY = "SYNTHETIC-CANARY-9f3a decrypt_failed: owner mismatch"

# The members below are spelled out here, in the test, rather than read back out
# of ``memory_core``. An earlier version looped over the production frozensets,
# which meant deleting a member deleted its assertion along with it -- the suite
# stayed green while the contract shrank. An inventory that derives from the
# thing it is checking cannot detect a removal, so it has to be an independent
# statement of what we promise; then adding, dropping or misspelling a member is
# red, and every change to the closed set is a deliberate edit in two places.
_EXPECTED_CLOSED_READSIDE_ERRORS = {
    "api_key_unavailable",
    "enclave_invalid_readside_response",
    "enclave_unavailable",
    "memory_load_failed",
}
_EXPECTED_CLOSED_REQUEST_ERRORS = {
    "ids must be a list of non-empty strings",
    "invalid limit",
}
# The *pairs*, in order, not just the labels. Pinning only the label set leaves
# the prefixes free: renaming the ``enclave_http_429`` prefix to
# ``enclave_http_4299`` keeps every label intact, so a set comparison stays
# green while real 429s silently demote to ``enclave_http_4xx``. Order is part
# of the contract too -- the table is first-match-wins and the specific codes
# only work because they precede the ``enclave_http_4`` catch-all.
_EXPECTED_UPSTREAM_SIGNALS = (
    ("enclave_http_401", "enclave_http_401"),
    ("enclave_http_403", "enclave_http_403"),
    ("enclave_http_404", "enclave_http_404"),
    ("enclave_http_408", "enclave_http_408"),
    ("enclave_http_429", "enclave_http_429"),
    ("enclave_http_4", "enclave_http_4xx"),
    ("enclave_http_5", "enclave_http_5xx"),
    ("enclave_http_", "enclave_http_other"),
    ("enclave_error:", "enclave_error"),
    ("enclave_unavailable", "enclave_unavailable"),
    ("api_key_unavailable", "api_key_unavailable"),
    ("enclave_invalid_readside_response", "enclave_invalid_readside_response"),
    ("memory_load_failed", "memory_load_failed"),
    ("readside_local_error", "local_search_error"),
)
_EXPECTED_UPSTREAM_LABELS = {label for _prefix, label in _EXPECTED_UPSTREAM_SIGNALS}
# What the doc promises a caller can receive. The two generic codes are not
# exception messages, so they are in neither closed set, but they are the most
# load-bearing rows in the table: they are what everything else collapses to.
_EXPECTED_READSIDE_DOC_SLUGS = _EXPECTED_CLOSED_READSIDE_ERRORS | {
    "readside_unavailable",
    "request_invalid",
}


def _enclave_error_from_producer(monkeypatch, status_code: int, body_text: str):
    """Drive the real producer to a >=400 response and return what it raised."""

    class _Resp:
        status_code = 0
        text = ""

        def json(self):  # pragma: no cover - never reached on the >=400 path
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            resp = _Resp()
            resp.status_code = status_code
            resp.text = body_text
            return resp

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "http://enclave.invalid:5003")
    monkeypatch.setattr(memory_readside_core.httpx, "Client", _Client)
    with pytest.raises(RuntimeError) as caught:
        memory_readside_core.post_enclave_readside(
            "ak_synthetic", [], operation="index")
    return caught.value


def test_the_enclave_body_really_does_ride_inside_the_exception(monkeypatch):
    """Positive control for the tests below.

    If the producer ever stops embedding ``resp.text``, the leak tests would pass
    for a reason that has nothing to do with ``memory_core`` -- green because
    there was nothing to leak. This fails first in that case.
    """
    err = _enclave_error_from_producer(monkeypatch, 403, _ENCLAVE_CANARY)
    assert _ENCLAVE_CANARY in str(err), (
        "the producer no longer embeds the enclave response body; the response-"
        "contract tests below are no longer exercising a real leak")


@pytest.mark.parametrize("route,payload,event_type", [
    ("/v1/memory/index", {}, "memory.index.called"),
    ("/v1/memory/index", {"query": "synthetic"}, "memory.search.called"),
    ("/v1/memory/fetch", {"ids": ["m-synthetic"]}, "memory.fetch.called"),
])
def test_readside_503_body_never_carries_the_enclave_response_text(
    user, monkeypatch, route, payload, event_type,
):
    """The invariant, anchored on absence of the text -- not on a status code.

    Asserting ``503`` or ``{"error": <some slug>}`` would have been green before
    this change too, since the pre-fix body was ``str(e)`` with a 503 beside it.
    """
    _uid, api_key = user
    events = []
    err = _enclave_error_from_producer(monkeypatch, 403, _ENCLAVE_CANARY)

    def boom(*a, **k):
        raise err

    monkeypatch.setattr(memory_readside_core, "memory_index_core", boom)
    monkeypatch.setattr(memory_readside_core, "memory_fetch_core", boom)
    monkeypatch.setattr(
        memory_core.debug_trace, "trace_event",
        lambda _store, **event: events.append(event))

    status, body = _asgi("POST", route, headers=_headers(api_key), json_body=payload)

    assert status == 503
    serialized = json.dumps(body)
    assert _ENCLAVE_CANARY not in serialized
    assert "owner mismatch" not in serialized
    assert "decrypt_failed" not in serialized
    assert "SYNTHETIC-CANARY-9f3a" not in serialized
    # Nothing from the exception message survives anywhere in the body, not just
    # under the key we happen to expect it under.
    assert str(err) not in serialized
    assert body == {"error": "readside_unavailable"}

    # ...and the triage that used to live in the body is now in the trace.
    events = [e for e in events if e["type"] == event_type]
    assert len(events) == 1
    assert events[-1]["type"] == event_type
    assert events[-1]["detail"]["upstream"] == "enclave_http_403"
    assert _ENCLAVE_CANARY not in json.dumps(events[-1])


def _readside_failure(monkeypatch, api_key, message: str):
    """Drive one readside RuntimeError; return (status, body, triage label)."""
    events = []

    def boom(*a, **k):
        raise RuntimeError(message)

    monkeypatch.setattr(memory_readside_core, "memory_index_core", boom)
    monkeypatch.setattr(
        memory_core.debug_trace, "trace_event",
        lambda _store, **event: events.append(event))
    status, body = _asgi(
        "POST", "/v1/memory/index", headers=_headers(api_key), json_body={})
    events = [e for e in events if e["type"] == "memory.index.called"]
    assert len(events) == 1
    return status, body, events[-1]["detail"]["upstream"]


def test_every_upstream_signal_is_reachable(user, monkeypatch):
    """Each row must be the winner for at least one message.

    The equality check above pins the table's contents; this pins that the
    lookup still agrees with it. A prefix typo (``enclave_http_429`` ->
    ``enclave_http_4299``) leaves every label in place, so a set -- or even a
    tuple -- comparison against a *mutated* production table is not enough on
    its own: the probes have to be built from the test-side inventory and
    actually executed. Probing with the bare prefix is deliberate; because the
    table is first-match-wins and ordered specific-to-general, a prefix that no
    longer wins its own probe has been shadowed or misspelled.
    """
    _uid, api_key = user

    for prefix, label in _EXPECTED_UPSTREAM_SIGNALS:
        status, _body, actual = _readside_failure(monkeypatch, api_key, prefix)
        assert status == 503, prefix
        assert actual == label, f"{prefix!r} mapped to {actual!r}, expected {label!r}"


def test_upstream_status_is_mapped_through_a_table_not_passed_through(
    user, monkeypatch,
):
    """A bounded value is not automatically a safe one.

    The enclave is an upstream, so its status code is external input. The signal
    is a label looked up in ``_UPSTREAM_SIGNALS``, so a code we never listed --
    including a non-standard one -- lands on a bucket rather than being echoed.
    """
    _uid, api_key = user

    def run(message: str) -> str:
        status, body, label = _readside_failure(monkeypatch, api_key, message)
        # None of the messages below are closed-set members, so each one must
        # also collapse to the generic code in the response body.
        assert (status, body) == (503, {"error": "readside_unavailable"}), message
        return label

    # Realistic messages, i.e. prefix plus the payload the producer appends.
    assert run("enclave_http_401:synthetic") == "enclave_http_401"
    assert run("enclave_http_403:synthetic") == "enclave_http_403"
    assert run("enclave_http_404:synthetic") == "enclave_http_404"
    assert run("enclave_http_408:synthetic") == "enclave_http_408"
    assert run("enclave_http_429:synthetic") == "enclave_http_429"
    assert run("enclave_http_503:synthetic") == "enclave_http_5xx"
    # Non-standard vendor code: bucketed, never echoed.
    assert run("enclave_http_499:synthetic") == "enclave_http_4xx"
    assert run("enclave_http_299:synthetic") == "enclave_http_other"
    # A code-shaped string that is not a code at all.
    assert run("enclave_http_<script>:synthetic") == "enclave_http_other"
    assert run("enclave_error:ConnectTimeout") == "enclave_error"
    # A message from outside the known vocabulary degrades, it does not pass.
    assert run("psycopg2 could not connect to host db-internal-7") == "unknown"

    assert "unknown" not in _EXPECTED_UPSTREAM_LABELS


def test_the_closed_sets_are_exactly_the_documented_inventory():
    """Pin the membership itself, not just the behaviour of whoever is a member.

    Every other test here drives the contract through the app, so it can only
    check the members that exist. This one is the anchor: it fails if the
    production sets and the inventory above disagree in either direction, which
    is what makes a silent removal impossible.
    """
    assert set(memory_core._CLOSED_READSIDE_ERRORS) == _EXPECTED_CLOSED_READSIDE_ERRORS
    assert set(memory_core._CLOSED_REQUEST_ERRORS) == _EXPECTED_CLOSED_REQUEST_ERRORS
    # Tuple equality, so prefixes and their order are pinned as well as labels.
    assert memory_core._UPSTREAM_SIGNALS == _EXPECTED_UPSTREAM_SIGNALS
    # The two sets must stay disjoint: a request-shaped message that is also a
    # readside literal would get a 400 and a 503 depending on which lane raised.
    assert not (_EXPECTED_CLOSED_READSIDE_ERRORS & _EXPECTED_CLOSED_REQUEST_ERRORS)


def test_the_readside_contract_is_documented():
    """A closed set the caller cannot look up is not a contract."""
    doc = Path(__file__).parent.parent / "docs" / "API_ERRORS.md"
    slugs = set(re.findall(r"^\| `([a-z][a-z0-9_]+)` \|", doc.read_text(encoding="utf-8"), re.M))
    missing = _EXPECTED_READSIDE_DOC_SLUGS - slugs
    assert not missing, f"API_ERRORS.md 缺 readside slug: {sorted(missing)}"


def test_closed_readside_messages_are_not_collapsed(user, monkeypatch):
    """The mirror case: this is a redaction, not a blanket flattening.

    Without this, replacing every 503 body with one constant would also pass the
    leak test above while destroying a contract callers already depend on.
    """
    _uid, api_key = user

    def run(message: str):
        def boom(*a, **k):
            raise RuntimeError(message)

        monkeypatch.setattr(memory_readside_core, "memory_index_core", boom)
        monkeypatch.setattr(
            memory_core.debug_trace, "trace_event", lambda _store, **event: None)
        return _asgi(
            "POST", "/v1/memory/index", headers=_headers(api_key), json_body={})

    for message in sorted(_EXPECTED_CLOSED_READSIDE_ERRORS):
        assert run(message) == (503, {"error": message}), message


def test_fetch_value_error_is_adjudicated_too(user, monkeypatch):
    """The ValueError branch is not innocent by default.

    ``except ValueError`` catches more than the readside's own literals: any
    stdlib ValueError raised anywhere under the call lands here, and
    ``json.JSONDecodeError`` is a ValueError whose message is built from the
    document being parsed -- which on this lane is decrypted user plaintext.
    """
    _uid, api_key = user

    def run(exc: Exception):
        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(memory_readside_core, "memory_fetch_core", boom)
        monkeypatch.setattr(
            memory_core.debug_trace, "trace_event", lambda _store, **event: None)
        return _asgi(
            "POST", "/v1/memory/fetch", headers=_headers(api_key),
            json_body={"ids": ["m-synthetic"]})

    # Known closed literals keep their contract.
    for message in sorted(_EXPECTED_CLOSED_REQUEST_ERRORS):
        assert run(ValueError(message)) == (400, {"error": message}), message

    # Anything else collapses, including the JSONDecodeError shape.
    status, body = run(ValueError("SYNTHETIC-VE-CANARY invalid literal: 'hunter2'"))
    assert (status, body) == (400, {"error": "request_invalid"})
    assert "hunter2" not in json.dumps(body)
    assert "SYNTHETIC-VE-CANARY" not in json.dumps(body)

    decode_error = json.JSONDecodeError("Expecting value", "SYNTHETIC-PLAINTEXT", 0)
    status, body = run(decode_error)
    assert (status, body) == (400, {"error": "request_invalid"})
    assert "SYNTHETIC-PLAINTEXT" not in json.dumps(body)


def test_index_invalid_limit_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/index", api_key=api_key, json_body={"limit": -3})
    assert f == a == (400, {"error": "invalid limit"})


def test_fetch_bad_ids_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/fetch", api_key=api_key, json_body={"ids": "not-a-list"})
    assert f == a == (400, {"error": "ids must be a list of non-empty strings"})


def test_fetch_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    moments = [{
        "v": 1, "id": "m1", "owner_user_id": uid, "visibility": "shared",
        "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "status": "active", "importance": 0.5, "occurred_at": "2026-06-20T10:00:00",
    }]
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [dict(m) for m in moments])
    monkeypatch.setattr(memory_service, "_save_moments", lambda _store, _moments: None)

    def fake_enclave(api_key_arg, candidates, *, operation, payload=None, runtime_token=None):
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates], "unavailable_ids": []}

    _stub_enclave(monkeypatch, fake_enclave)
    f, a = _both("POST", "/v1/memory/fetch", api_key=api_key, json_body={"ids": ["m1", "missing"]})
    assert f == a
    assert f[0] == 200
    assert [it["id"] for it in f[1]["items"]] == ["m1"]
    assert f[1]["missing_ids"] == ["missing"]


def test_buckets_and_threads_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    moments = [{
        "v": 1, "id": "m1", "owner_user_id": uid, "visibility": "shared",
        "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "status": "active", "importance": 0.5,
    }]
    monkeypatch.setattr(
        memory_service,
        "_load_moments",
        lambda _store: [dict(moment) for moment in moments],
    )

    def fake_enclave(api_key_arg, candidates, *, operation, payload=None, runtime_token=None):
        return {"items": [
            {"id": "m1", "status": "active", "bucket": "关系", "threads": ["t1", "t2"]},
        ]}

    _stub_enclave(monkeypatch, fake_enclave)
    fb, ab = _both("GET", "/v1/memory/buckets", api_key=api_key)
    assert fb == ab == (200, {"buckets": ["关系"]})
    ft, at = _both("GET", "/v1/memory/threads", api_key=api_key)
    assert ft == at == (200, {"threads": ["t1", "t2"]})


# --------------------------------------------------------------------------- #
# add / retype (v1 envelope; deterministic via patched store)
# --------------------------------------------------------------------------- #

def _patch_empty_store(monkeypatch):
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [])
    monkeypatch.setattr(memory_service, "_save_moments", lambda _store, _moments: None)
    monkeypatch.setattr(memory_core.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)


def _blank(body, *fields):
    if not isinstance(body, dict):
        return body
    out = dict(body)
    moment = out.get("moment")
    if isinstance(moment, dict):
        moment = dict(moment)
        for f in fields:
            if f in moment:
                moment[f] = "<ts>"
        out["moment"] = moment
    return out


def test_add_missing_envelope_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/add", api_key=api_key, json_body={})
    assert f == a == (400, {"error": "envelope required (v1 encryption is mandatory)"})


def test_add_wrong_owner_403_parity(user, monkeypatch):
    _uid, api_key = user
    _patch_empty_store(monkeypatch)
    env = _envelope("someone-else")
    f, a = _both("POST", "/v1/memory/add", api_key=api_key, json_body={"envelope": env})
    assert f == a == (403, {"error": "envelope.owner_user_id does not match caller"})


def test_add_success_201_parity(user, monkeypatch):
    uid, api_key = user
    _patch_empty_store(monkeypatch)
    env = _envelope(uid)
    env["occurred_at"] = "2026-06-20T18:00:00+08:00"
    f = _flask("POST", "/v1/memory/add", headers=_headers(api_key), json_body={"envelope": env})
    a = _asgi("POST", "/v1/memory/add", headers=_headers(api_key), json_body={"envelope": env})
    assert f[0] == a[0] == 201
    # created_at is stamped with datetime.now() at add time — blank before compare.
    assert _blank(f[1], "created_at") == _blank(a[1], "created_at")
    assert f[1]["moment"]["id"] == "mom_test"
    assert f[1]["moment"]["occurred_at"] == "2026-06-20T10:00:00Z"
    assert a[1]["moment"]["occurred_at"] == "2026-06-20T10:00:00Z"
    assert f[1]["moment"]["created_at"].endswith("Z")
    assert a[1]["moment"]["created_at"].endswith("Z")
    assert f[1]["v"] == 1


def test_retype_type_invalid_400_parity(user, monkeypatch):
    _uid, api_key = user
    _patch_empty_store(monkeypatch)
    f, a = _both("POST", "/v1/memory/retype", api_key=api_key,
                 json_body={"id": "m1", "type": "bogus"})
    assert f == a
    assert f[0] == 400
    assert f[1]["error"] == "type_invalid"


def test_retype_not_found_404_parity(user, monkeypatch):
    _uid, api_key = user
    _patch_empty_store(monkeypatch)
    f, a = _both("POST", "/v1/memory/retype", api_key=api_key,
                 json_body={"id": "ghost", "type": "fact"})
    assert f == a == (404, {"error": "not_found"})


# --------------------------------------------------------------------------- #
# actions (scope no-op under api-key auth)
# --------------------------------------------------------------------------- #

def test_actions_required_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/actions", api_key=api_key, json_body={})
    assert f == a == (400, {"error": "actions required"})


@pytest.mark.parametrize("action_type", ["memory.bogus", "memory.upgrade"])
def test_actions_unsupported_type_parity(user, action_type):
    _uid, api_key = user
    body = {"actions": [{"type": action_type}]}
    f, a = _both("POST", "/v1/memory/actions", api_key=api_key, json_body=body)
    assert f == a
    # Both stacks preserve the complete per-item result, but a zero-applied
    # batch with a failure restores the outer HTTP failure signal.
    assert f[0] == 400
    assert f[1]["status"] == "failed"
    assert f[1]["error"] == "unsupported_memory_action"
    assert f[1]["failed_count"] == 1
    assert f[1]["results"][0]["http_status"] == 400
    assert f[1]["results"][0]["error"] == "unsupported_memory_action"
    assert "memory.upgrade" not in f[1]["results"][0]["supported"]


# --------------------------------------------------------------------------- #
# actions — runtime-token forwarding (hosted callers have no api_key)
# --------------------------------------------------------------------------- #

def test_actions_forwards_runtime_token_to_core(user, monkeypatch):
    """A hosted caller authenticating with a runtime token must be able to EDIT an
    existing card.

    Stage D swaps ``X-API-Key`` for ``X-Feedling-Runtime-Token`` in the resident
    consumer, so ``auth.api_key`` is None on this path. ``/actions`` was the only
    route in this file that never extracted the token (its four readside siblings
    all did), so the enclave decrypt of the OLD card raised
    ``api_key_unavailable`` and every supersede/patch came back
    ``409 memory_decrypt_failed`` — for every hosted user, V1 and V2 alike.
    """
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    uid, _api_key = user
    tok = _token(uid, ["memory"])
    seen: dict = {}

    def fake_actions(store, api_key, payload, *, runtime_token=""):
        seen["api_key"] = api_key
        seen["runtime_token"] = runtime_token
        return {"applied": 1}, 200

    monkeypatch.setattr(memory_core, "actions", fake_actions)
    status, body = _asgi(
        "POST", "/v1/memory/actions",
        headers={"X-Feedling-Runtime-Token": tok},
        json_body={"actions": [{"type": "memory.profile_patch"}]},
    )

    assert status == 200 and body == {"applied": 1}
    assert seen["api_key"] is None      # hosted callers have no per-user api key
    assert seen["runtime_token"] == tok  # ...so the token is the ONLY usable credential


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/memory/migration_state"),
    ("POST", "/v1/memory/migration_state"),
    ("POST", "/v1/memory/legacy_batch"),
])
@pytest.mark.parametrize("authenticated", [False, True])
def test_retired_memory_migration_routes_are_not_found(user, method, path, authenticated):
    """Retired endpoints must not read/write migration state, even with auth."""
    _uid, api_key = user
    status, _body = _asgi(method, path, headers=_headers(api_key) if authenticated else None,
                         json_body={} if method == "POST" else None)
    assert status == 404



# T810: exercise the real authenticated route and its actual thread bridge.
def _timing_app():
    app = _build_asgi_app()
    app.add_middleware(middleware.AccessLogMiddleware)
    return app


async def _timing_request(app, api_key, payload=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
    ) as client:
        return await client.post(
            "/v1/memory/index", headers=_headers(api_key), json=payload or {},
        )


def _timing_events(events):
    return [e for e in events if e["type"] == "memory.index.timing"]


def test_index_timing_real_readside_persists_segments_and_request_id(user, monkeypatch):
    from memory import index_timing
    import debug_trace

    uid, key = user
    store = core_store.get_store(uid)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    monkeypatch.setattr(memory_service, "_load_moments",
                        lambda _store: [_envelope(uid)])
    calls = []
    def post(api_key, candidates, **kw):
        calls.append((api_key, candidates, kw))
        return {"items": []}
    _stub_enclave(monkeypatch, post)
    ticks = iter([10.0, 12.0, 13.0, 16.0, 17.0, 20.0])
    monkeypatch.setattr(index_timing, "_now", lambda: next(ticks))
    response = asyncio.run(_timing_request(_timing_app(), key))
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0][0] == key
    # Real queue -> db writer -> database reader, not a trace_event mock.
    events = _timing_events(debug_trace.read_trace(store, limit=100))
    assert len(events) == 1
    event = events[0]
    assert event["trace_id"] == response.headers["x-request-id"]
    assert event["status"] == "ok" and event["dur_ms"] == 10000.0
    assert event["detail"]["lane"] == "index"
    assert event["detail"]["http_status"] == 200
    assert event["detail"]["segments"] == {
        "queue_ms": 2000.0, "core_ms": 5000.0, "core_state": "completed",
        "upstream_ms": 3000.0, "upstream_calls": 1,
        "upstream_state": "completed",
    }
    assert key not in json.dumps(event)


@pytest.mark.parametrize("failure", [None, "handled", "unexpected"])
def test_index_timing_preserves_result_and_content_free_failure(user, monkeypatch, failure):
    uid, key = user
    events = []
    canary = "PRIVATE-CONTENT-URL-CREDENTIAL-EXCEPTION"
    monkeypatch.setattr(memory_core.debug_trace, "trace_event",
                        lambda _store, **e: events.append(e))
    monkeypatch.setattr(memory_service, "_load_moments",
                        lambda _store: [_envelope(uid)])
    def post(*args, **kwargs):
        if failure == "handled":
            raise RuntimeError("enclave_http_503:" + canary)
        if failure == "unexpected":
            raise LookupError(canary)
        return {"items": []}
    _stub_enclave(monkeypatch, post)
    if failure == "unexpected":
        with pytest.raises(LookupError, match=canary):
            asyncio.run(_timing_request(_timing_app(), key))
    else:
        response = asyncio.run(_timing_request(_timing_app(), key))
        assert response.status_code == (503 if failure else 200)
        if failure:
            assert response.json() == {"error": "readside_unavailable"}
    timing, = _timing_events(events)
    assert timing["status"] == ("error" if failure else "ok")
    assert canary not in json.dumps(timing) and key not in json.dumps(timing)
    assert timing["detail"]["segments"]["upstream_state"] == (
        "failed" if failure else "completed")
    assert timing["detail"]["segments"]["core_state"] == (
        "failed" if failure == "unexpected" else "completed")
    assert timing["detail"]["http_status"] == (
        None if failure == "unexpected" else 503 if failure else 200)


def test_index_timing_no_upstream_is_null_and_auth_failure_has_no_timing(user, monkeypatch):
    _, key = user
    events = []
    monkeypatch.setattr(memory_core.debug_trace, "trace_event",
                        lambda _store, **e: events.append(e))
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [])
    response = asyncio.run(_timing_request(_timing_app(), key))
    assert response.status_code == 200
    timing, = _timing_events(events)
    assert timing["detail"]["segments"]["upstream_ms"] is None
    assert timing["detail"]["segments"]["upstream_state"] == "not_called"
    assert timing["detail"]["segments"]["upstream_calls"] == 0
    events.clear()
    response = asyncio.run(_timing_request(_timing_app(), "invalid"))
    assert response.status_code == 401
    assert not _timing_events(events)


@pytest.mark.parametrize("failure", [False, True])
def test_index_timing_enqueue_failure_does_not_change_business_outcome(user, monkeypatch, failure):
    import debug_trace
    _, key = user
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    attempted = []
    def broken_enqueue(uid, event):
        attempted.append(event["type"])
        raise OSError("telemetry unavailable")
    monkeypatch.setattr(debug_trace, "_enqueue", broken_enqueue)
    def read(*a, **kw):
        if failure:
            raise LookupError("original failure")
        return {"items": []}
    monkeypatch.setattr(memory_readside_core, "memory_index_core", read)
    if failure:
        with pytest.raises(LookupError, match="original failure"):
            asyncio.run(_timing_request(_timing_app(), key))
    else:
        assert asyncio.run(_timing_request(_timing_app(), key)).status_code == 200
    assert "memory.index.timing" in attempted


def test_index_timing_clock_failure_is_unknown_not_zero(user, monkeypatch):
    from memory import index_timing
    _, key = user
    events = []
    monkeypatch.setattr(memory_core.debug_trace, "trace_event",
                        lambda _store, **e: events.append(e))
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [])
    # Exercise the timer's error boundary without changing global time.
    class BrokenClock:
        @staticmethod
        def monotonic():
            raise OSError("private timer failure")
    monkeypatch.setattr(index_timing, "time", BrokenClock)
    assert asyncio.run(_timing_request(_timing_app(), key)).status_code == 200
    timing, = _timing_events(events)
    assert timing["dur_ms"] is None
    assert timing["detail"]["segments"]["queue_ms"] is None
    assert timing["detail"]["segments"]["core_ms"] is None
    assert "private" not in json.dumps(timing)


def test_index_timing_concurrent_requests_keep_identity_and_lane(monkeypatch):
    import threading
    from types import SimpleNamespace
    from accounts.auth_core import AuthResult
    from asgi.deps import require_auth
    app = _timing_app()
    async def auth(request: Request):
        key = request.headers["x-api-key"]
        return AuthResult(SimpleNamespace(user_id=key), key, None, key)
    app.dependency_overrides[require_auth] = auth
    barrier = threading.Barrier(2)
    events = []
    monkeypatch.setattr(memory_core.debug_trace, "trace_event",
                        lambda store, **e: events.append((store.user_id, e)))
    def read(store, key, payload, *, post_enclave):
        barrier.wait(timeout=5)
        if payload.get("query"):
            post_enclave(key, [], operation="index", payload=payload)
        return {"items": []}
    monkeypatch.setattr(memory_readside_core, "memory_index_core", read)
    _stub_enclave(monkeypatch, lambda *a, **kw: {"items": []})
    async def go():
        return await asyncio.gather(
            _timing_request(app, "synthetic-a"),
            _timing_request(app, "synthetic-b", {"query": "private query"}),
        )
    a, b = asyncio.run(go())
    timings = {uid: e for uid, e in events if e["type"] == "memory.index.timing"}
    assert set(timings) == {"synthetic-a", "synthetic-b"}
    for uid, response, lane, calls in [
        ("synthetic-a", a, "index", 0), ("synthetic-b", b, "search", 1),
    ]:
        event = timings[uid]
        assert response.status_code == 200
        assert event["trace_id"] == response.headers["x-request-id"]
        assert event["detail"]["lane"] == lane
        assert event["detail"]["segments"]["upstream_calls"] == calls
        assert "private query" not in json.dumps(event)
    assert timings["synthetic-a"]["trace_id"] != timings["synthetic-b"]["trace_id"]


@pytest.mark.parametrize("started", [False, True])
def test_index_timing_cancellation_keeps_incomplete_segments_unknown(user, monkeypatch, started):
    import threading
    import anyio
    from memory import index_timing
    _, key = user
    events = []
    entered = threading.Event()
    release = threading.Event()
    observations = []
    original_init = index_timing.IndexTiming.__init__
    def init(self, **kwargs):
        original_init(self, **kwargs)
        observations.append(self)
    monkeypatch.setattr(index_timing.IndexTiming, "__init__", init)
    monkeypatch.setattr(memory_core.debug_trace, "trace_event",
                        lambda _store, **e: events.append(e))
    def read(store, api_key, payload, *, post_enclave):
        return post_enclave(api_key, [], operation="index")
    def post(*a, **kw):
        entered.set()
        assert release.wait(timeout=5)
        return {"items": []}
    monkeypatch.setattr(memory_readside_core, "memory_index_core", read)
    _stub_enclave(monkeypatch, post)
    # Authenticate before occupying the worker token: the queue under test is
    # the index dispatch, not the separate authentication database dispatch.
    from accounts.auth_core import AuthResult
    from asgi.deps import require_auth
    app = _timing_app()
    store = core_store.get_store(user[0])
    async def authenticated():
        return AuthResult(store=store, user_id=user[0], runtime_token_claims=None, api_key=key)
    app.dependency_overrides[require_auth] = authenticated
    async def go():
        limiter = anyio.to_thread.current_default_thread_limiter()
        old = limiter.total_tokens
        limiter.total_tokens = 1
        if not started:
            await limiter.acquire()
        try:
            task = asyncio.create_task(_timing_request(app, key))
            async def ready():
                while not (entered.is_set() if started else observations):
                    await asyncio.sleep(0)
            await asyncio.wait_for(ready(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            timing, = _timing_events(events)
            snapshot = json.dumps(timing, sort_keys=True)
            assert timing["status"] == "cancelled"
            assert timing["detail"]["http_status"] is None
            seg = timing["detail"]["segments"]
            assert seg["core_ms"] is None and seg["upstream_ms"] is None
            assert seg["core_state"] == ("running" if started else "not_started")
            assert seg["upstream_state"] == ("running" if started else "not_called")
            if not started:
                assert seg["queue_ms"] is None
            release.set()
            if started:
                async def done():
                    while observations[0].core_state == "running":
                        await asyncio.sleep(0)
                await asyncio.wait_for(done(), timeout=3)
            assert len(_timing_events(events)) == 1
            assert json.dumps(timing, sort_keys=True) == snapshot
        finally:
            release.set()
            if not started:
                limiter.release()
            limiter.total_tokens = old
    asyncio.run(go())
