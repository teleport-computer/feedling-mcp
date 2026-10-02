"""T779 step 1: automatic recall moved to ``memory.recall_select`` with no behaviour change.

The pre-change output is frozen in the golden file (``test_memory_recall_hybrid``
compares the enclave route against it). These tests pin the other half of the
move: the enclave entry point and the shared module give the same answer for
the same cards, the shared card builder is what the enclave's reader produced
before, and the shared module stays free of the enclave package so it can run
next to the data for plaintext accounts.
"""
from __future__ import annotations

import ast
import functools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

import recall_golden_cases as golden  # noqa: E402
from enclave import readside  # noqa: E402
from enclave.routes import chat  # noqa: E402
from memory import recall_metadata, recall_select  # noqa: E402

BACKEND = Path(__file__).parent.parent / "backend"
CARDS_PATH = Path(__file__).parent / "data" / "recall_select_cards_f47c9196.json"


@pytest.fixture(autouse=True)
def _original_recent_cards():
    if not golden._ORIG_RECENT:
        golden._ORIG_RECENT.append(recall_metadata.recent_cards)


def _shared(monkeypatch, window, args, ranker):
    monkeypatch.setattr(recall_select.time, "monotonic", lambda: golden.FIXED_MONOTONIC)
    monkeypatch.setattr(recall_metadata, "recent_cards",
                        functools.partial(golden._ORIG_RECENT[0], now=golden.FIXED_NOW))
    if ranker is None:
        monkeypatch.delenv(recall_select.RECALL_RANKER_ENV, raising=False)
    else:
        monkeypatch.setenv(recall_select.RECALL_RANKER_ENV, ranker)
    cards = [dict(c) for c in golden._garden()]
    picked, trace, log = recall_select.select_context_memories(
        cards, window, {**golden.BASE_ARGS, **args})
    return json.loads(json.dumps({"context_memories": picked, "context_memory_trace": trace,
                                  "context_memory_log": log}, ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize("name,window,args,ranker", golden.SCENARIOS,
                         ids=[s[0] for s in golden.SCENARIOS])
def test_enclave_entry_and_shared_module_select_the_same(monkeypatch, name, window, args, ranker):
    via_enclave = golden.run_scenario(chat, recall_metadata, monkeypatch, name, window, args, ranker)
    via_shared = _shared(monkeypatch, window, args, ranker)
    assert via_shared == via_enclave
    # Both also equal the pre-change golden, so the shared path is not merely
    # consistent with a changed enclave path.
    frozen = json.loads(golden.GOLDEN_PATH.read_text())["cases"][name]
    assert via_shared == frozen


OWNER = "usr_a"


def plaintext_rows():
    """Rows the frozen card fixture was generated from (reports/T779/gen_card_fixture.py
    replays the pre-change reader on exactly these)."""
    bodies = {
        "full": {"summary": "The cat naps on the green sofa", "content": "every afternoon",
                 "title": "Cat", "description": "d", "bucket": "home", "threads": ["猫", " ", "sofa"],
                 "roles": ["pet"], "type": "moment", "her_quote": "q", "context": "c",
                 "linked_dimension": "pets", "status": "Active"},
        "bare": {"summary": "Only a summary"},
        "threads_str": {"summary": "s", "threads": "single thread", "roles": "not-a-list"},
    }
    moments = [
        {"id": cid, "owner_user_id": OWNER, "visibility": "shared", "status": status,
         "body": json.dumps(body), "archived_at": archived, "superseded_by": sup,
         "source": "chat", "occurred_at": "2026-09-01T00:00:00Z",
         "created_at": "2026-09-02T00:00:00Z"}
        for (cid, body), status, archived, sup in zip(
            bodies.items(), ["", "retired", None], [None, "2026-09-03", None], [None, None, "full"])
    ]
    return bodies, moments


def test_card_from_inner_is_what_the_enclave_reader_builds_for_plaintext_rows():
    bodies, moments = plaintext_rows()
    # Frozen output of the pre-change enclave reader on these rows; comparing
    # the reader with card_from_inner alone would be circular now that the
    # reader calls it.
    frozen = json.loads(CARDS_PATH.read_text())
    assert frozen["generated_from"].startswith("f47c9196")
    cards = readside.moments_to_cards(moments, OWNER, None)
    assert cards == frozen["cards"]
    assert [recall_select.card_from_inner(bodies[m["id"]], m) for m in moments] == frozen["cards"]


def test_shared_module_does_not_import_the_enclave_package():
    tree = ast.parse((BACKEND / "memory" / "recall_select.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported, "parsed no imports; the scan is not reading the module"
    assert not [m for m in imported if m == "enclave" or m.startswith("enclave.")]



def test_encrypted_card_is_really_decrypted_then_selected_through_the_enclave_entry(monkeypatch):
    """Encrypted accounts keep the enclave path: real sealed envelope -> readside
    decrypt -> shared selection. Not a static guard: the card only reaches the
    injected set if the real decrypt produced its text."""
    import nacl.public
    from test_enclave_envelope_core import _make_envelope

    sk = nacl.public.PrivateKey.generate()
    secret = {"summary": "咪咪是只三岁的橘猫，最近不太吃饭", "content": "兽医说先观察饮水",
              "title": "猫咪", "bucket": "home"}
    other = {"summary": "The new bookshelf came from IKEA", "content": ""}
    moments = [
        {**_make_envelope(OWNER, "enc_cat", json.dumps(secret).encode(), bytes(sk.public_key)),
         "visibility": "shared", "status": "active", "created_at": "2026-01-05T08:00:00+00:00"},
        {"id": "plain_shelf", "owner_user_id": OWNER, "visibility": "shared", "status": "active",
         "body": json.dumps(other), "created_at": "2026-01-05T08:00:00+00:00"},
    ]
    monkeypatch.delenv(recall_select.RECALL_RANKER_ENV, raising=False)
    window = [{"role": "user", "content": "担心咪咪最近不吃饭"}]
    picked, trace, log = chat._build_context_memories(moments, window, {
        "context_mode": "", "authorized_user_id": OWNER, "content_sk": sk,
        "want_trace": True, "context_recent": False})
    by_id = {c["id"]: c for c in picked}
    assert "enc_cat" in by_id, log
    assert by_id["enc_cat"]["summary"] == secret["summary"]
    assert by_id["enc_cat"]["title"] == "猫咪"
    assert log["counts"]["candidate_pool"] == 2
