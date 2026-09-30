"""T779 step 4: keyword memory search for plaintext accounts runs in backend.

Contract: for the same corpus a plaintext account gets, byte for byte, what the
pre-T786 enclave path returned. The expected answers are frozen from base
da93d1c4 (backend ``_memory_search`` enclave branch + the real in-process
enclave ``search``) in ``fixtures/memory_search_local/``; regenerating them
from the new code would only compare the new ranking with itself.
"""
from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import conftest  # noqa: E402
import memory_readside_core as readside_core  # noqa: E402
import memory_search_contract as contract  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from memory import memory_core  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "memory_search_local"
INPUTS = json.loads((FIXTURES / "inputs.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "expected_da93d1c4.json").read_text(encoding="utf-8"))
OWNER = INPUTS["owner"]
CASES = INPUTS["cases"]


def refuse_post(*_a, **_k):
    raise AssertionError("a plaintext account must not call the enclave")


def canon(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def local_search(monkeypatch, rows, payload):
    monkeypatch.setattr(readside_core, "_plaintext_account", lambda _uid: True)
    return readside_core._memory_search("key", copy.deepcopy(rows), OWNER,
                                        dict(payload), post=refuse_post)


def sealed(mid, *, importance=0.9):
    return {"v": 1, "id": mid, "owner_user_id": OWNER, "visibility": "shared",
            "status": "active", "salience": "medium", "importance": importance,
            "occurred_at": "2026-06-20T10:00:00", "created_at": "2026-06-20T10:00:00",
            "updated_at": "2026-06-20T10:00:00", "body_ct": f"ct_{mid}",
            "nonce": f"n_{mid}", "K_user": f"ku_{mid}", "K_enclave": f"ke_{mid}"}


def test_frozen_expectations_come_from_the_base_commit():
    assert EXPECTED["_base"].startswith("da93d1c4")
    assert set(EXPECTED) - {"_base"} == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_plaintext_account_matches_frozen_enclave_answer(monkeypatch, name):
    case = CASES[name]
    new = local_search(monkeypatch, case["rows"], case["payload"])
    assert canon(new) == canon(EXPECTED[name]["result"])


def test_frozen_table_is_not_degenerate():
    got = {n: EXPECTED[n]["result"] for n in CASES}
    assert [i["id"] for i in got["late_match_in_1600"]["items"]] == ["last"]
    assert [i["id"] for i in got["ties_same_score_same_time"]["items"]] == [
        "tiea", "tieb", "tiem", "tieq"]
    assert got["k_enclave_shadow_row"]["unavailable_count"] == 1
    assert got["unreadable_rows"]["unavailable_count"] == 5
    assert got["no_hit"]["items"] == []


def test_past_hard_max_ranks_the_whole_corpus_once(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "300")
    case = CASES["late_match_in_1600"]
    seen = []
    original = readside_core.search_rank.retrieval.rank

    def observe(query, candidates, **kw):
        seen.append(len(candidates))
        return original(query, candidates, **kw)

    monkeypatch.setattr(readside_core.search_rank.retrieval, "rank", observe)
    new = local_search(monkeypatch, case["rows"], case["payload"])
    assert canon(new) == canon(EXPECTED["late_match_in_1600"]["result"])
    assert seen == [1600]


def test_sealed_rows_are_dropped_not_counted(monkeypatch):
    # Denominator: the frozen enclave answer for the same corpus without the
    # sealed rows. items, ranking and unavailable_count all equal.
    case = CASES["bucket_filter_global_idf"]
    rows = [sealed("s1"), *case["rows"][:1], sealed("s2"), *case["rows"][1:]]
    new = local_search(monkeypatch, rows, case["payload"])
    assert canon(new) == canon(EXPECTED["bucket_filter_global_idf"]["result"])


def _store_run(monkeypatch, rows, payload, *, plaintext, post):
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _s: rows)
    monkeypatch.setattr(readside_core, "_plaintext_account", lambda _uid: plaintext)
    return readside_core.memory_index_core(
        types.SimpleNamespace(user_id=OWNER), "key", payload, post_enclave=post)


def test_index_core_keeps_card_count_semantics_with_sealed_rows(monkeypatch):
    case = CASES["bucket_filter_global_idf"]
    rows = [sealed("s1"), *copy.deepcopy(case["rows"]), sealed("s2")]
    payload = {"query": "coffee repair", "bucket": "topic", "limit": 5}

    def enclave_answer(api_key, candidates, *, operation, payload):
        return {"user_id": OWNER, "unavailable_ids": [], "items": [],
                "ranking": contract.VERSION}

    old = _store_run(monkeypatch, rows, payload, plaintext=False, post=enclave_answer)
    new = _store_run(monkeypatch, rows, payload, plaintext=True, post=refuse_post)
    assert new["user_card_count"] == old["user_card_count"] == len(rows)
    assert new["truncated"] is old["truncated"] is False
    assert [i["id"] for i in new["items"]] == ["b"]


def test_sealed_content_account_still_posts_one_corpus(monkeypatch):
    calls = []

    def post(api_key, candidates, *, operation, payload):
        calls.append([m["id"] for m in candidates])
        return {"user_id": OWNER, "unavailable_ids": [], "items": [],
                "ranking": contract.VERSION}

    rows = [sealed("s1"), *copy.deepcopy(CASES["no_hit"]["rows"])]
    _store_run(monkeypatch, rows, {"query": "coffee", "limit": 5}, plaintext=False, post=post)
    # One corpus, sealed row included; order is readside_candidates' (unchanged).
    assert len(calls) == 1 and sorted(calls[0]) == ["s1", "x", "y"]


# ---- endpoint layer: memory_core.index maps errors like the enclave path ----

def _index(monkeypatch, rows, payload):
    events = []
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _s: rows)
    monkeypatch.setattr(readside_core, "_plaintext_account", lambda _uid: True)
    monkeypatch.setattr(memory_core.debug_trace, "trace_event",
                        lambda *a, **kw: events.append(kw))
    body, status = memory_core.index(types.SimpleNamespace(user_id=OWNER), "key",
                                     payload, post_enclave=refuse_post)
    return body, status, events


def test_builder_failure_is_503_readside_unavailable(monkeypatch):
    row = copy.deepcopy(CASES["no_hit"]["rows"][0])
    row["importance"] = "not-a-number"
    body, status, events = _index(monkeypatch, [row], {"query": "coffee", "limit": 5})
    assert (status, body) == (503, {"error": "readside_unavailable"})
    assert events[-1]["detail"]["upstream"] == "local_search_error"


@pytest.mark.parametrize("limit", ["MAX_CARDS", "MAX_REQUEST_BYTES", "MAX_TEXT_BYTES"])
def test_resource_limits_stay_413(monkeypatch, limit):
    rows = [copy.deepcopy(r) for r in CASES["limit_cut"]["rows"]]
    monkeypatch.setattr(contract, limit, {"MAX_CARDS": 3, "MAX_REQUEST_BYTES": 2000,
                                          "MAX_TEXT_BYTES": 100}[limit])
    body, status, _events = _index(monkeypatch, rows, {"query": "tomato", "limit": 5})
    assert (status, body) == (413, {"error": "memory_search_resource_limit"})


# ---- routing through the real registry and write gate ----

@pytest.mark.parametrize(("seed", "gate", "local"), [
    ({"content_encryption": "on"}, True, True),    # known, old "on" preference
    ({"content_encryption": "off"}, True, True),   # known, plaintext
    (None, True, False),                            # unknown user: fail-safe enclave
    ({"content_encryption": "off"}, False, False),  # plaintext writes gate closed
])
def test_route_uses_real_registry_and_gate(monkeypatch, seed, gate, local):
    uid = f"usr_t786_route_{bool(seed)}_{(seed or {}).get('content_encryption')}_{gate}"
    if seed is not None:
        conftest.seed_user(uid, **seed)
    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", gate)
    assert readside_core._plaintext_account(uid) is local


def test_failure_after_the_request_check_is_503_not_a_raw_exception(monkeypatch):
    # Readable JSON whose content holds a lone surrogate: rank's UTF-8 byte
    # count raises UnicodeEncodeError. Inside the enclave that was a 500, which
    # the backend answered with 503 readside_unavailable; locally it must too.
    row = copy.deepcopy(CASES["no_hit"]["rows"][0])
    row["body"] = json.dumps({"summary": "coffee", "content": "coffee " + chr(0xD800),
                              "bucket": "topic", "threads": []})
    body, status, events = _index(monkeypatch, [row], {"query": "coffee", "limit": 5})
    assert (status, body) == (503, {"error": "readside_unavailable"})
    assert events[-1]["detail"]["upstream"] == "local_search_error"


@pytest.mark.parametrize("name", ["bucket_filter_global_idf", "limit_cut",
                                  "late_match_in_1600"])
def test_small_hard_max_does_not_change_the_answer(monkeypatch, name):
    # The frozen answers come from one global rank. HARD_MAX also caps the result
    # count, so keep it >= every case's limit but below the corpus size: chunks
    # ranked separately would change these answers.
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "5")
    case = CASES[name]
    new = local_search(monkeypatch, case["rows"], case["payload"])
    assert canon(new) == canon(EXPECTED[name]["result"])


@pytest.mark.parametrize(("seed", "posts"), [
    ({"content_encryption": "on"}, False),   # known account: backend search
    (None, True),                             # unknown account: enclave search
])
def test_real_route_decides_whether_the_enclave_is_called(monkeypatch, seed, posts):
    uid = f"usr_t786_index_{bool(seed)}"
    if seed is not None:
        conftest.seed_user(uid, **seed)
    monkeypatch.setattr(core_envelope, "PLAINTEXT_WRITES_ACCEPTED", True)
    rows = copy.deepcopy(CASES["bucket_filter_global_idf"]["rows"])
    for row in rows:
        row["owner_user_id"] = uid
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _s: rows)
    calls = []

    def post(api_key, candidates, *, operation, payload):
        calls.append(len(candidates))
        return {"user_id": uid, "unavailable_ids": [], "items": [],
                "ranking": contract.VERSION}

    out = readside_core.memory_index_core(
        types.SimpleNamespace(user_id=uid), "key",
        {"query": "coffee repair", "bucket": "topic", "limit": 5}, post_enclave=post)
    assert bool(calls) is posts
    if not posts:
        assert [i["id"] for i in out["items"]] == ["b"]
