"""v1 flow trace: beta default-on recording with deploy/per-user safety valves."""
import ast
import os
import queue
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core import enclave as core_enclave  # noqa: E402
import db  # noqa: E402
import debug_trace  # noqa: E402
from diagnostics import diagnostics_core  # noqa: E402


class _Store:
    def __init__(self, uid="usr_dbg"):
        self.user_id = uid


def _reset(monkeypatch, store):
    debug_trace._flag_cache.clear()
    monkeypatch.setattr(debug_trace, "_record_trace_stats", lambda *_args, **_kwargs: None)
    # isolate DB blobs to an in-memory dict so the test never needs Postgres
    blobs: dict = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))
    monkeypatch.setattr(db, "get_blob_strict", lambda uid, kind: blobs.get((uid, kind)))
    monkeypatch.setattr(db, "set_blob", lambda uid, kind, doc: blobs.__setitem__((uid, kind), doc))

    def patch_blob(uid, kind, patch, **_kwargs):
        persisted = dict(blobs.get((uid, kind)) or {})
        persisted.update(patch)
        blobs[(uid, kind)] = persisted
        return persisted

    monkeypatch.setattr(db, "patch_blob_strict", patch_blob)

    def append_events(uid, kind, new_events, *, cutoff_ts, max_events):
        current = blobs.get((uid, kind)) or {}
        events = list(current.get("events") or []) + list(new_events)
        events = [event for event in events if event.get("ts", 0) >= cutoff_ts]
        persisted = {"v": 1, "events": events[-max_events:]}
        blobs[(uid, kind)] = persisted
        return persisted

    monkeypatch.setattr(db, "append_blob_events_strict", append_events)
    return blobs


def _source_trace_call_contract() -> tuple[set[str], list[str]]:
    """Derive the writer contract from production callsites, not a copied list."""
    backend = Path(__file__).resolve().parents[1] / "backend"
    derived_types: set[str] = set()
    unresolved_type_expressions: list[str] = []

    def string_literals(node: ast.AST) -> set[str]:
        return {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }

    def assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {target.id for target in targets if isinstance(target, ast.Name)}

    for path in sorted(backend.rglob("*.py")):
        if path.name == "debug_trace.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        module_constants: dict[str, set[str]] = {}
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                values = string_literals(statement.value)
                for name in assigned_names(statement):
                    if values:
                        module_constants[name] = values

        for node in calls:
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "trace_event"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "debug_trace"
            ):
                continue
            type_arg = next((kw.value for kw in node.keywords if kw.arg == "type"), None)
            assert type_arg is not None, f"{path}:{node.lineno} omits trace type="
            values: set[str] = set()
            if isinstance(type_arg, ast.Constant) and isinstance(type_arg.value, str):
                values.add(type_arg.value)
            elif isinstance(type_arg, ast.Name):
                values.update(module_constants.get(type_arg.id, set()))
                enclosing = [
                    function
                    for function in functions
                    if function.lineno <= node.lineno <= (function.end_lineno or function.lineno)
                ]
                function = min(
                    enclosing,
                    key=lambda item: (item.end_lineno or item.lineno) - item.lineno,
                    default=None,
                )
                if function is not None:
                    for assignment in ast.walk(function):
                        if (
                            isinstance(assignment, (ast.Assign, ast.AnnAssign))
                            and type_arg.id in assigned_names(assignment)
                        ):
                            values.update(string_literals(assignment.value))
                    parameter_names = [arg.arg for arg in function.args.args]
                    if type_arg.id in parameter_names:
                        index = parameter_names.index(type_arg.id)
                        for caller in calls:
                            calls_helper = (
                                isinstance(caller.func, ast.Name)
                                and caller.func.id == function.name
                            ) or (
                                isinstance(caller.func, ast.Attribute)
                                and caller.func.attr == function.name
                            )
                            if not calls_helper:
                                continue
                            actual = (
                                caller.args[index]
                                if index < len(caller.args)
                                else next(
                                    (
                                        keyword.value
                                        for keyword in caller.keywords
                                        if keyword.arg == type_arg.id
                                    ),
                                    None,
                                )
                            )
                            if actual is None:
                                continue
                            values.update(string_literals(actual))
                            if isinstance(actual, ast.Name):
                                values.update(module_constants.get(actual.id, set()))
            if values:
                derived_types.update(values)
            else:
                unresolved_type_expressions.append(
                    f"{path.relative_to(backend)}:{ast.unparse(type_arg)}"
                )
    return derived_types, unresolved_type_expressions


def test_source_derived_event_types_round_trip_through_current_writer(monkeypatch):
    """Freeze the pre-migration writer/read contract without a hand-kept enum.

    Literal event types are discovered from every production callsite at test
    time.  A dynamic sentinel covers helper/HTTP callsites whose type is supplied
    at runtime.  A future table writer must preserve the same output contract.
    """
    store = _Store("usr_trace_contract")
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    monkeypatch.setenv("FEEDLING_DEBUG_VERBOSE", "1")
    derived_types, dynamic_expressions = _source_trace_call_contract()
    assert derived_types, "production trace callsite discovery found no event types"
    assert dynamic_expressions == [
        "diagnostics/diagnostics_core.py:str(ev.get('type') or '')"
    ], "only the authenticated HTTP event bridge may accept an arbitrary type"

    event_types = sorted(derived_types | {"contract.dynamic.type"})
    for index, event_type in enumerate(event_types):
        debug_trace.trace_event(
            store,
            subsystem="contract",
            type=event_type,
            actor="contract-test",
            status="warning",
            summary="summary",
            explain="explain",
            trace_id=f"trace-{index}",
            turn_id=f"turn-{index}",
            job_id=f"job-{index}",
            detail={"counts": {"items": index}, "reason": "contract"},
            content_excerpt={"reply": "visible excerpt"},
            dur_ms=12.34,
        )

    payload, status = diagnostics_core.read_trace_payload(
        store, limit=len(event_types), subsystem="contract"
    )
    assert status == 200
    assert set(payload) == {"enabled", "deploy_enabled", "verbose", "events"}
    assert payload["enabled"] is True
    assert payload["deploy_enabled"] is True
    assert payload["verbose"] is True
    assert {event["type"] for event in payload["events"]} == set(event_types)
    expected_fields = {
        "ts",
        "subsystem",
        "type",
        "actor",
        "status",
        "summary",
        "explain",
        "trace_id",
        "turn_id",
        "job_id",
        "detail",
        "dur_ms",
        "content_excerpt",
    }
    for event in payload["events"]:
        assert set(event) == expected_fields
        assert event["subsystem"] == "contract"
        assert event["actor"] == "contract-test"
        assert event["status"] == "warning"
        assert event["summary"] == "summary"
        assert event["explain"] == "explain"
        assert event["dur_ms"] == 12.3
        assert event["content_excerpt"] == {"reply": "visible excerpt"}


def test_clear_removes_persisted_events_keeps_toggle_and_allows_new_events(monkeypatch):
    store = _Store("usr_trace_clear_contract")
    blobs = _reset(monkeypatch, store)
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="route", type="before.clear")
    assert [event["type"] for event in debug_trace.read_trace(store)] == ["before.clear"]

    debug_trace.clear_trace(store)

    assert debug_trace.read_trace(store) == []
    assert blobs[(store.user_id, debug_trace.DEBUG_TRACE_FLAG_BLOB)]["enabled"] is True
    debug_trace.trace_event(store, subsystem="route", type="after.clear")
    assert [event["type"] for event in debug_trace.read_trace(store)] == ["after.clear"]


def test_default_on_records_no_env_needed(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)  # no env set (default)
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is True
    assert debug_trace.read_trace(store)[0]["type"] == "route.decided"
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) in blobs


def test_default_can_be_restored_to_opt_in_with_env(monkeypatch):
    store = _Store()
    blobs = _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", "0")
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []
    assert (store.user_id, debug_trace.DEBUG_TRACE_BLOB) not in blobs
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="route", type="route.decided", summary="x")
    assert debug_trace.read_trace(store)[0]["type"] == "route.decided"


def test_env_zero_hard_disables_even_with_flag_on(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "0")  # prod kill switch
    debug_trace.set_enabled(store, True)  # user toggled on, but...
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert debug_trace.is_enabled(store) is False
    assert debug_trace.read_trace(store) == []


def test_records_when_flag_on_no_env_needed(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    debug_trace.trace_event(store, subsystem="route", type="route.decided",
                            summary="host", detail={"mode": "agent_runtime", "reason": "text"})
    debug_trace.trace_event(store, subsystem="memory", type="memory.index.called",
                            detail={"counts": {"items": 50, "fetched": 2}})
    events = debug_trace.read_trace(store)
    assert [e["type"] for e in events] == ["memory.index.called", "route.decided"]  # newest first
    assert debug_trace.read_trace(store, subsystem="route")[0]["detail"]["mode"] == "agent_runtime"
    # Per-user opt-out still works.
    debug_trace.set_enabled(store, False)
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    assert len(debug_trace.read_trace(store)) == 2


def test_set_enabled_never_caches_a_failed_or_unreadable_write(monkeypatch):
    store = _Store("usr_trace_failure")
    debug_trace._flag_cache.clear()

    def fail_pool():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(debug_trace.db, "get_pool", fail_pool)
    with pytest.raises(RuntimeError, match="database unavailable"):
        debug_trace.set_enabled(store, True)
    assert store.user_id not in debug_trace._flag_cache

    monkeypatch.setattr(debug_trace.db, "set_blob", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(debug_trace.db, "get_blob_strict", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="debug_trace_flag_write_not_visible"):
        debug_trace.set_enabled(store, True)
    assert store.user_id not in debug_trace._flag_cache


def test_detail_is_size_bounded_metadata(monkeypatch):
    store = _Store()
    _reset(monkeypatch, store)
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    debug_trace.set_enabled(store, True)
    debug_trace.trace_event(store, subsystem="memory", type="t", detail={"big": "x" * 9999})
    ev = debug_trace.read_trace(store)[0]
    assert len(ev["detail"]["big"]) <= 200  # caller content can't bloat the buffer


def test_trace_event_does_not_wait_for_slow_blob_storage(monkeypatch):
    store = _Store()
    monkeypatch.setattr(debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(debug_trace, "verbose_enabled", lambda _store: False)

    def slow_get_blob(_uid, _kind):
        time.sleep(0.2)
        return {}

    def slow_set_blob(_uid, _kind, _doc):
        time.sleep(0.2)

    monkeypatch.setattr(debug_trace.db, "get_blob", slow_get_blob)
    monkeypatch.setattr(debug_trace.db, "set_blob", slow_set_blob)

    started = time.monotonic()
    debug_trace.trace_event(store, subsystem="route", type="route.decided")
    elapsed = time.monotonic() - started

    assert elapsed < 0.05


def test_debug_read_waits_for_worker_instead_of_becoming_a_second_writer(monkeypatch):
    """A read barrier must never steal queue items and race the worker's RMW."""
    uid = "usr_debug_read_barrier"
    isolated_queue: queue.Queue = queue.Queue()
    isolated_queue.put((uid, {"ts": time.time(), "type": "queued"}))
    monkeypatch.setattr(debug_trace, "_event_queue", isolated_queue)
    monkeypatch.setattr(debug_trace, "_pending_by_uid", {uid: 1})

    writes: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(
        debug_trace,
        "_append_events",
        lambda event_uid, events: writes.append((event_uid, events)),
    )

    debug_trace._flush_pending_for_user(uid, timeout=0.01)

    assert writes == []
    assert isolated_queue.qsize() == 1


# --- M1: explain / content_excerpt / dur_ms + verbose gate + caps -----------


class FakeStore:
    def __init__(self, uid="u1"):
        self.user_id = uid


def _reset_verbose(monkeypatch):
    """In-memory blob store + force gate ON."""
    blobs = {}
    monkeypatch.setattr(debug_trace.db, "get_blob", lambda uid, k: blobs.get((uid, k)))
    monkeypatch.setattr(debug_trace.db, "get_blob_strict", lambda uid, k: blobs.get((uid, k)))
    monkeypatch.setattr(
        debug_trace.db,
        "set_blob",
        lambda uid, k, v: blobs.__setitem__((uid, k), v),
    )

    def append_events(uid, kind, new_events, *, cutoff_ts, max_events):
        current = blobs.get((uid, kind)) or {}
        events = list(current.get("events") or []) + list(new_events)
        events = [event for event in events if event.get("ts", 0) >= cutoff_ts]
        persisted = {"v": 1, "events": events[-max_events:]}
        blobs[(uid, kind)] = persisted
        return persisted

    monkeypatch.setattr(debug_trace.db, "append_blob_events_strict", append_events)
    monkeypatch.setattr(debug_trace, "_hard_disabled", lambda: False)
    debug_trace._flag_cache.clear()
    return blobs


def test_verbose_off_strips_content_excerpt(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.setenv("FEEDLING_DEBUG_VERBOSE", "0")  # force strip
    debug_trace.trace_event(store, subsystem="agent", type="agent.model.call.done",
                            explain="模型返回", content_excerpt={"reply": "hello"}, dur_ms=12.0)
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert ev["explain"] == "模型返回"
    assert ev["dur_ms"] == 12.0
    assert ev.get("content_excerpt") in (None, {}, )  # stripped when verbose off


def test_content_excerpt_field_truncation(monkeypatch):
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)  # verbose defaults ON with gate
    big = "x" * 5000
    debug_trace.trace_event(store, subsystem="agent", type="t",
                            content_excerpt={"prompt": big})
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert len(ev["content_excerpt"]["prompt"]) <= 2048 + len("…(truncated)")
    assert ev["content_excerpt"]["prompt"].endswith("…(truncated)")


def test_verbose_ring_cap(monkeypatch):
    """环深从模块读,样本按环深推导 —— 别写死。

    这条曾写死 `== 200`。2026-08-10 把 verbose 环深从 200 提到 1000 之后它必然
    失配,而 `test_debug_trace.py` 当时既不在 conftest 的 `_PURE_UNIT`、又在
    `.github/pytest-uncovered-baseline.txt` 里 —— 两份名单都没有 ⇒ **CI 从来
    不跑它**,于是那次改动带着一条红上线,没有任何人看见。

    写死常量还有个更隐蔽的坏处:样本数(260)也是照着旧环深挑的。环深一变大,
    样本就不再超限,闸根本不会被触发 —— 那时测试会**照常变绿**,而它其实什么
    都没验到。所以样本必须由环深推出来,并显式断言确实超限。
    """
    _reset_verbose(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)

    cap = debug_trace._MAX_EVENTS_VERBOSE
    written = cap + 60
    assert written > cap, "样本没超过环深,裁剪逻辑根本不会被触发"

    for i in range(written):
        debug_trace.trace_event(store, subsystem="route", type=f"t{i}")

    assert len(debug_trace.read_trace(store, limit=written)) == cap


class _FakeEnclaveResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


def _install_enclave_client(monkeypatch, outcome):
    class _FakeEnclaveClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(core_enclave.httpx, "Client", lambda **_kwargs: _FakeEnclaveClient())


def _capture_enclave_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        core_enclave.debug_trace,
        "trace_event",
        lambda _store, **event: events.append(event),
    )
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.test")
    return events


def test_tee_replicate_success_does_not_write_enclave_trace(monkeypatch):
    events = _capture_enclave_events(monkeypatch)
    response = _FakeEnclaveResponse(body={"plaintext_b64": "cGxhaW50ZXh0"})
    _install_enclave_client(monkeypatch, response)

    plaintext = core_enclave._decrypt_envelope_via_enclave(
        {"owner_user_id": "usr_replicate", "body_ct": "ciphertext"},
        "api-key",
        purpose="tee_replicate:chat:msg_1",
    )

    assert plaintext == b"plaintext"
    assert events == []


@pytest.mark.parametrize(
    ("outcome", "expected_type"),
    [
        (_FakeEnclaveResponse(status_code=503, text="unavailable"), "enclave.call.error"),
        (core_enclave.httpx.ReadTimeout("timed out"), "enclave.call.timeout"),
    ],
)
def test_tee_replicate_failure_keeps_enclave_error_trace(monkeypatch, outcome, expected_type):
    events = _capture_enclave_events(monkeypatch)
    _install_enclave_client(monkeypatch, outcome)

    with pytest.raises(RuntimeError):
        core_enclave._decrypt_envelope_via_enclave(
            {"owner_user_id": "usr_replicate", "body_ct": "ciphertext"},
            "api-key",
            purpose="tee_replicate:memory",
        )

    assert [event["type"] for event in events] == [expected_type]
    assert events[0]["status"] == "error"
    assert events[0]["detail"]["purpose"] == "tee_replicate:memory"


def test_normal_decrypt_keeps_enclave_start_and_done_traces(monkeypatch):
    events = _capture_enclave_events(monkeypatch)
    response = _FakeEnclaveResponse(body={"plaintext_b64": "cGxhaW50ZXh0"})
    _install_enclave_client(monkeypatch, response)

    plaintext = core_enclave._decrypt_envelope_via_enclave(
        {"owner_user_id": "usr_chat", "body_ct": "ciphertext"},
        "api-key",
        purpose="chat:history",
    )

    assert plaintext == b"plaintext"
    assert [event["type"] for event in events] == ["enclave.call.start", "enclave.call.done"]
    assert all(event["detail"]["purpose"] == "chat:history" for event in events)


def test_flush_pending_waits_for_worker_in_flight_batch(monkeypatch):
    """Read-after-write consistency: unfinished_tasks must not hit zero while
    the worker still holds an un-appended batch (a concurrent reader would
    early-return stale DB), and a read must see the event once the worker's
    append + task_done complete. Deterministic: the append is gated."""
    import threading

    gate = threading.Event()
    entered = threading.Event()
    written: list = []
    real_wait = debug_trace._FLUSH_WAIT_SEC

    def blocking_append(uid, events):
        entered.set()
        assert gate.wait(5)
        written.extend(events)

    monkeypatch.setattr(debug_trace, "_append_events", blocking_append)
    monkeypatch.setattr(debug_trace, "_FLUSH_WAIT_SEC", 0.01)  # tight batch window
    try:
        debug_trace._enqueue("usr_dbg_race", {"ts": time.time(), "type": "unit.race"})
        assert entered.wait(5)  # worker popped the item and is mid-append (pre-task_done)

        t0 = time.monotonic()
        debug_trace._flush_pending_for_user("usr_dbg_race", timeout=0.2)
        blocked_wait = time.monotonic() - t0
        # Must NOT early-return while the append is in flight.
        assert blocked_wait >= 0.15
        assert not written

        gate.set()
        t1 = time.monotonic()
        debug_trace._flush_pending_for_user("usr_dbg_race", timeout=2.0)
        # Returns promptly once the worker acked, and the event is visible.
        assert time.monotonic() - t1 < 1.0
        assert written and written[0]["type"] == "unit.race"
        assert debug_trace._event_queue.unfinished_tasks == 0
    finally:
        gate.set()  # never leave the daemon worker blocked past this test
        monkeypatch.setattr(debug_trace, "_FLUSH_WAIT_SEC", real_wait)
