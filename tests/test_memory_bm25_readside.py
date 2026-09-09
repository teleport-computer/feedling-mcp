"""Global ranking at the real enclave HTTP boundary, without DB/provider I/O."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import memory_bm25
import memory_readside_core
import content_encryption
import nacl.public
import memory_search_contract as contract
from asgi_test_client import _AsgiTestClient
from enclave import auth, backend_client, keys, readside, state
from enclave import routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(state._state, "ready", True)
    monkeypatch.setitem(state._state, "error", None)
    auth.reset_cache()

    async def whoami(*args, **kwargs):
        return {"user_id": "owner"}

    async def sk():
        return object()

    monkeypatch.setattr(backend_client, "backend_get", whoami)
    monkeypatch.setattr(keys, "get_content_sk", sk)
    return _AsgiTestClient(routes.build_app())


def moment(mid, text, *, bucket="topic", owner="owner"):
    return {"id": mid, "owner_user_id": owner, "visibility": "shared", "v": 1,
            "score": .5, "body": json.dumps({"summary": text, "content": text,
                                              "bucket": bucket, "threads": []})}


def search(client, rows, query="coffee repair", **extra):
    return client.post("/v1/memory/index", headers={"X-API-Key": "test"},
                       json={"moments": rows, "query": query, "limit": 1,
                             "search_protocol": contract.VERSION, **extra})


def test_global_1600_card_corpus_ranks_late_match_before_limit(client, monkeypatch):
    rows = [moment(f"m{i}", "coffee " + "noise " * 30) for i in range(1599)]
    rows.append(moment("last", "coffee grinder repair"))
    sizes = []
    original = readside.decrypt_readside_items

    def observe(chunk, *a, **kw):
        sizes.append(len(chunk))
        return original(chunk, *a, **kw)

    monkeypatch.setattr(readside, "decrypt_readside_items", observe)
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "300")
    response = search(client, rows)
    assert response.status_code == 200
    body = response.get_json()
    assert body["ranking"] == contract.VERSION
    assert [item["id"] for item in body["items"]] == ["last"]
    assert sizes == [300, 300, 300, 300, 300, 100]
    assert "_search_content" not in json.dumps(body)
    assert "_bm25_score" not in json.dumps(body)
    assert body["items"][0]["score"] == .5


def test_global_stats_include_other_buckets_and_ignore_unreadable(client, monkeypatch):
    rows = [moment("a", "coffee"), moment("b", "repair"),
            *[moment(f"other{i}", "coffee", bucket="other") for i in range(20)],
            moment("foreign", "repair repair", owner="someone_else")]
    seen = []
    original = memory_bm25.corpus_stats

    def observe(documents):
        stats = original(documents)
        seen.append(stats.documents)
        return stats

    monkeypatch.setattr(memory_bm25, "corpus_stats", observe)
    response = search(client, rows, bucket="topic")
    assert response.status_code == 200
    assert [i["id"] for i in response.get_json()["items"]] == ["b"]
    assert response.get_json()["unavailable_ids"] == ["foreign"]
    assert seen == [22]


def test_old_backend_keeps_substring_semantics_and_marker(client):
    rows = [moment("a", "coffee grinder repair")]
    body = search(client, rows, search_protocol=None).get_json()
    assert body["ranking"] == contract.LEGACY
    assert body["items"] == []  # noncontiguous terms only match the new protocol
    assert search(client, rows).get_json()["items"][0]["id"] == "a"


def test_protocol_and_limits_fail_explicitly(client, monkeypatch):
    rows = [moment("a", "coffee")]
    unknown = search(client, rows, search_protocol="future")
    assert unknown.status_code == 400
    assert unknown.get_json()["error"] == "memory_search_protocol_unsupported"
    monkeypatch.setattr(contract, "MAX_CARDS", 1)
    too_many = search(client, rows * 2)
    assert too_many.status_code == 413
    assert too_many.get_json() == {"error": "memory_search_resource_limit"}
    monkeypatch.setattr(contract, "MAX_TEXT_BYTES", 3)
    assert search(client, rows).status_code == 413
    monkeypatch.setattr(contract, "MAX_REQUEST_BYTES", 20)
    too_large = search(client, rows)
    assert too_large.status_code == 413
    assert too_large.get_json() == {"error": "memory_search_resource_limit"}


def test_nonempty_tokenless_query_returns_no_match(client):
    assert search(client, [moment("a", "coffee !!!")], query="!!!").get_json()["items"] == []


def test_real_crypto_mixed_backend_to_enclave_and_wrong_owner(client, monkeypatch):
    from types import SimpleNamespace
    secret = nacl.public.PrivateKey.generate()
    async def sk():
        return secret
    monkeypatch.setattr(keys, "get_content_sk", sk)
    plain = moment("plain", "coffee coffee " + "noise " * 15)
    sealed = content_encryption.build_envelope(
        plaintext=json.dumps({"summary": "coffee repair", "content": "CR2450 NP-4286",
                              "bucket": "topic", "threads": []}).encode(),
        owner_user_id="owner", user_pk_bytes=bytes(secret.public_key),
        enclave_pk_bytes=bytes(secret.public_key), item_id="sealed")
    wrong = {**sealed, "id": "wrong", "owner_user_id": "someone_else"}
    broken = {**sealed, "id": "broken"}  # authenticated AAD must reject changed id
    rows = [plain, sealed, wrong, broken]
    monkeypatch.setattr(memory_readside_core.memory_service, "_load_moments", lambda _: rows)

    def transport(api_key, candidates, *, operation, payload):
        result = client.post("/v1/memory/index", json={**payload, "moments": candidates},
                             headers={"X-API-Key": api_key})
        assert result.status_code == 200
        return result.get_json()

    out = memory_readside_core.memory_index_core(SimpleNamespace(user_id="owner"), "test",
        {"query": "coffee repair", "limit": 1}, post_enclave=transport)
    assert [item["id"] for item in out["items"]] == ["sealed"]
    assert out["unavailable_count"] == 1
    assert out["user_card_count"] == 3  # foreign row excluded before enclave
    assert "CR2450" not in json.dumps(out)
    assert search(client, [sealed], query="np-4286").get_json()["items"][0]["id"] == "sealed"
    assert search(client, [sealed], query="np-428").get_json()["items"] == []


def test_resource_failure_http_contract_and_v2_legacy_visibility(monkeypatch):
    from types import SimpleNamespace
    from memory import memory_core
    from capabilities import memory_results
    monkeypatch.setattr(memory_core.debug_trace, "trace_event", lambda *a, **kw: None)
    def exceed(*a, **kw):
        raise contract.SearchLimitExceeded()
    monkeypatch.setattr(memory_readside_core, "memory_index_core", exceed)
    body, status = memory_core.index(SimpleNamespace(user_id="owner"), "key",
                                    {"query": "private"}, post_enclave=None)
    assert (body, status) == ({"error": "memory_search_resource_limit"}, 413)
    compact = memory_results.index_payload({"items": [], "ranking": contract.LEGACY,
                                           "unavailable_count": 2}, tool_name="memory_search")
    assert compact["ranking"] == contract.LEGACY
    assert compact["unavailable_count"] == 2


@pytest.mark.asyncio
async def test_lifespan_prewarms_before_serving(monkeypatch):
    called = []
    monkeypatch.setattr(memory_bm25, "prewarm", lambda: called.append("warm"))
    async with routes.lifespan(None):
        assert called == ["warm"]
