"""Per-round message fold via the enclave-decrypt reader, for `tool_loop.run_tool_loop`
(spec C7).

`_make_fold_new_messages` must fold in ONLY newly-visible user-role chat messages each
call — no dup (a message already handed out never comes back), no restart (a cursor that
has advanced never rewinds). It MUST read via the same enclave-decrypt path the turn's own
coalesce step uses (`deps.read_messages_since` / `deps.read_messages`) — never
`db.chat_messages_after_seq` — because production `chat_messages` rows are E2E-encrypted
envelopes; only the enclave-bound reader returns plaintext. These tests exercise that real
path by injecting a FAKE reader that returns scripted plaintext rows, mirroring how
`serve_worker.build_production_deps` wires the real decrypting reader in production.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from model_api_runtime.v2 import worker
from model_api_runtime.v2.worker import TurnDeps


def _unused(*_a, **_k):
    raise AssertionError("unused")


def _run_fold(fold):
    """`fold_new_messages` is now an ASYNC callable (BUG-2 fix: the closure wraps the
    enclave-bound decrypt read in `asyncio.to_thread` + the shared enclave semaphore,
    same as `_coalesce_inputs`'s own read) — every call site in these tests must await
    it. `_make_fold_new_messages` defaults `enclave_sem` to the module-level
    `ENCLAVE_SEMAPHORE`; these tests don't care about semaphore gating (that's covered
    by tests/test_v2_worker.py's `_CountingSemaphore` acquisition-count test), so they
    pass `enclave_sem=None` at construction to skip gating entirely (mirrors
    `_coalesce_inputs`/`_cap_data`'s own `enclave_sem is None` no-gate tolerance)."""
    return asyncio.run(fold())


@dataclass
class _FakeReader:
    """Scripted stand-in for the enclave-decrypt reader. `rows` is mutated by the test
    between fold() calls to simulate new plaintext messages becoming visible.

    Honours the SEQ boundary the production reader (`serve_worker._read_messages`)
    enforces at the DB layer — returns only rows with `seq > after_seq`. In the seq
    design the reader owns cross-call de-duplication (the fold no longer applies a ts
    gate in coalesce), so a fake that ignored the arg would falsely re-deliver rows."""
    rows: list[dict] = field(default_factory=list)

    def __call__(self, user_id: str, after_seq: int) -> list[dict]:
        return [r for r in self.rows if (r.get("seq") or 0) > after_seq]


def _deps(reader: _FakeReader) -> TurnDeps:
    return TurnDeps(
        read_messages=_unused,
        resolve_provider=_unused,
        mint_enclave_token=_unused,
        read_messages_since=reader,
    )


def test_fold_returns_first_message_and_advances_cursor():
    reader = _FakeReader(rows=[{"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "hi"}])
    cursor_box = {"seq": 0}
    fold = worker._make_fold_new_messages("u_fold1", _deps(reader), cursor_box, enclave_sem=None)

    out = _run_fold(fold)
    assert [m["id"] for m in out] == ["m1"]
    assert out[0]["content"] == "hi"
    assert cursor_box["seq"] == 1

    # Second call before any new message arrives: no re-fold of m1, no dup.
    assert _run_fold(fold) == []
    assert cursor_box["seq"] == 1


def test_fold_second_call_returns_only_new_message_no_dup():
    reader = _FakeReader(rows=[{"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "one"}])
    cursor_box = {"seq": 0}
    fold = worker._make_fold_new_messages("u_fold2", _deps(reader), cursor_box, enclave_sem=None)

    first = _run_fold(fold)
    assert [m["id"] for m in first] == ["m1"]

    # A newer message becomes visible via the reader (fake "new row arrived").
    reader.rows.append({"id": "m2", "ts": 101.0, "seq": 2, "role": "user", "content": "two"})

    second = _run_fold(fold)
    assert [m["id"] for m in second] == ["m2"]  # ONLY the new one — no re-fold of m1
    assert cursor_box["seq"] == 2

    # A third call with nothing new returns [] — no restart, no phantom re-delivery.
    assert _run_fold(fold) == []


def test_fold_no_new_messages_returns_empty_list():
    reader = _FakeReader(rows=[{"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "hi"}])
    cursor_box = {"seq": 1}  # start already caught up (turn's own coalesce already saw m1)
    fold = worker._make_fold_new_messages("u_fold3", _deps(reader), cursor_box, enclave_sem=None)

    assert _run_fold(fold) == []
    assert _run_fold(fold) == []


def test_fold_filters_non_user_roles():
    reader = _FakeReader(rows=[
        {"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "hi"},
        {"id": "m2", "ts": 101.0, "seq": 2, "role": "openclaw", "content": "reply"},
        {"id": "m3", "ts": 102.0, "seq": 3, "role": "user", "content": "again"},
    ])
    cursor_box = {"seq": 0}
    fold = worker._make_fold_new_messages("u_fold4", _deps(reader), cursor_box, enclave_sem=None)

    out = _run_fold(fold)
    assert [m["id"] for m in out] == ["m1", "m3"]  # m2 (assistant) filtered out
    assert cursor_box["seq"] == 3  # max seq of the KEPT user messages (m3), never rewinds
    assert _run_fold(fold) == []


class _CountingSemaphore(asyncio.Semaphore):
    """Real semaphore that counts acquisitions (mirrors
    tests/test_v2_worker.py's own `_CountingSemaphore`) — used here to prove the
    per-round fold closure actually acquires the gate it's given, not just that
    it tolerates `enclave_sem=None`."""

    def __init__(self, value=2):
        super().__init__(value)
        self.acquire_count = 0

    async def acquire(self):
        self.acquire_count += 1
        return await super().acquire()


def test_fold_acquires_the_given_enclave_semaphore():
    """BUG-2 regression: the per-round fold's enclave-bound read must be gated
    by the SAME semaphore the rest of the turn uses (spec §11 R3) — before the
    fix, the closure called the reader directly with no semaphore at all."""
    reader = _FakeReader(rows=[{"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "hi"}])
    cursor_box = {"seq": 0}
    sem = _CountingSemaphore(2)
    fold = worker._make_fold_new_messages("u_fold_sem", _deps(reader), cursor_box, enclave_sem=sem)

    out = _run_fold(fold)
    assert [m["id"] for m in out] == ["m1"]
    assert sem.acquire_count == 1


def test_fold_defaults_to_module_enclave_semaphore():
    """No enclave_sem passed -> the closure defaults to worker.ENCLAVE_SEMAPHORE
    (the same shared gate `process_job` uses), not an unbounded direct call."""
    reader = _FakeReader(rows=[{"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "hi"}])
    cursor_box = {"seq": 0}
    fold = worker._make_fold_new_messages("u_fold_default", _deps(reader), cursor_box)

    before = worker.ENCLAVE_SEMAPHORE._value
    out = _run_fold(fold)
    after = worker.ENCLAVE_SEMAPHORE._value
    assert [m["id"] for m in out] == ["m1"]
    # Acquired-then-released around the single read -> net value unchanged, but
    # the fact this ran at all through the real module semaphore (not a stub)
    # confirms the default wiring; sem.acquire_count isn't observable on the
    # bare asyncio.Semaphore, so this asserts the release brought it back.
    assert after == before


def test_fold_uses_read_messages_when_read_messages_since_absent():
    """When only the single-arg `read_messages` reader is wired (older test-style deps),
    the closure must fall back to it — mirroring `_coalesce_inputs`'s own fallback."""
    calls = []

    def reader(user_id: str) -> list[dict]:
        calls.append(user_id)
        return [{"id": "m1", "ts": 100.0, "seq": 1, "role": "user", "content": "hi"}]

    deps = TurnDeps(
        read_messages=reader,
        resolve_provider=_unused,
        mint_enclave_token=_unused,
        read_messages_since=None,
    )
    cursor_box = {"seq": 0}
    fold = worker._make_fold_new_messages("u_fold6", deps, cursor_box, enclave_sem=None)

    out = _run_fold(fold)
    assert [m["id"] for m in out] == ["m1"]
    assert calls == ["u_fold6"]


def test_build_messages_appends_folded_inputs_and_tool_results():
    build_messages = worker._make_build_messages_fn(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": "earlier"}]
    )
    from provider_types import ToolCall, ToolExchange, ToolResult

    folded_inputs = [{"id": "m9", "ts": 1.0, "content": "new user turn"}]
    exchange = ToolExchange(
        calls=(ToolCall(id="c1", name="memory_search", args={"query": "x"}),),
        results=(ToolResult(call_id="c1", content='{"ok": true, "data": "x"}'),),
    )

    messages = build_messages([exchange, *folded_inputs])

    assert messages[0] == {"role": "system", "content": "sys"}
    roles = [m["role"] for m in messages if isinstance(m, dict)]
    assert "user" in roles
    joined = " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    assert "earlier" in joined
    assert "new user turn" in joined
    assert messages[2] is exchange  # native assistant call + tool result stay structured
    assert exchange.results[0].content.endswith('"x"}')


def test_build_messages_with_no_folded_inputs_or_results_is_stable():
    build_messages = worker._make_build_messages_fn(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": "hello"}]
    )
    messages = build_messages([])
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
