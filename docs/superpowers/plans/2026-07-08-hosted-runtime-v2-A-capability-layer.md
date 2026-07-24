# Hosted Runtime V2 — Plan A: Capability Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `backend/capabilities/` — a thin uniform facade over the existing domain `*_core` functions — giving the V2 worker one `CapabilityResult` shape and an action-type dispatch table, with output caps + redaction.

**Architecture:** The framework-neutral capability implementations already exist as `memory_core` / `perception_core` / `screen_read_core` / `perception_read_core` / `identity_core`. This plan does NOT reimplement them and does NOT touch the HTTP routes or `io_cli`. Each `capabilities/<verb>.py` module imports its core via `from pkg import module`, calls it, and normalizes the heterogeneous return (`(body, status)` tuple / `ScreenResult` / raised `AgentRouteError`) into `CapabilityResult`. `registry.py` maps action-type strings to these functions for the executor (Plan C).

**Tech Stack:** Python 3.11, pytest, existing backend package layout (FastAPI app assembled in `asgi_app.py`, but this plan adds no routes).

## Global Constraints

- **BYOK-only invariant:** This layer performs **no LLM calls** and takes **no LLM key**. It only threads the enclave-auth `api_key` / `runtime_token` for enclave-forwarded decrypt calls. No platform-level key anywhere. (Spec §7.3.)
- **No-filler:** capabilities never author chat text; they return data only. (Spec §2.)
- **Output caps + redaction:** every capability's `data` passes through `errors.cap_text` / `errors.cap_list` so status events and the responder never see unbounded/raw blobs. (Spec §4.1, §11.)
- **Cross-module calls use `from pkg import module` + `module.func()`** (never `from module import func`), so tests can monkeypatch the core. (CONTRIBUTING §3.)
- **Dependency direction:** `capabilities` imports only downward — `memory` / `identity` / `perception` / `screen` (all below it) + `memory_readside_core`. No imports from `hosted` / `model_api_runtime` / routes. (CONTRIBUTING §2.)
- **Tests live in repo-root `tests/`, never `backend/`.** Drive via `pytest`. (CONTRIBUTING §6.)
- **Single-file red line:** 800 lines needs justification, 1500 hard split. Each facade module is small; keep it that way.

## File Structure

```
backend/capabilities/
  __init__.py        empty package marker
  types.py           CapabilityResult + ok()/err()
  errors.py          error-code mapping + cap_text/cap_list redaction
  memory.py          index / fetch / write        (wraps memory_core)
  perception.py      snapshot / trend / history    (wraps agent/perception_core)
  screen.py          recent / read                 (wraps screen_read_core)
  photo.py           recent / read                 (wraps perception_read_core [+ screen decrypt])
  identity.py        get / patch                   (wraps identity_core)
  chat.py            image_read                     (enclave-only; mirrors io_cli cmd_chat_image)
  registry.py        CAPABILITIES dispatch + run_capability + READ/WRITE sets
tests/
  test_capabilities_types.py
  test_capabilities_memory.py
  test_capabilities_perception.py
  test_capabilities_screen.py
  test_capabilities_photo.py
  test_capabilities_identity.py
  test_capabilities_chat.py
  test_capabilities_registry.py
docs/superpowers/specs/runtime-v2-parity-matrix.md   (Phase 0 deliverable)
```

## Interfaces produced (consumed by Plan C's executor)

```python
# capabilities/types.py
@dataclass class CapabilityResult:
    ok: bool; data: dict; error: dict | None; trace: dict; warnings: list
    def to_dict(self) -> dict
def ok(data: dict | None = None, *, trace=None, warnings=None) -> CapabilityResult
def err(code: str, message: str, *, retryable: bool = False, trace=None) -> CapabilityResult

# every capabilities/<mod>.py verb:
def <verb>(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult

# capabilities/registry.py
CAPABILITIES: dict[str, Callable[..., CapabilityResult]]
READ_ACTIONS: frozenset[str]; WRITE_ACTIONS: frozenset[str]
def run_capability(action_type: str, store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult
```

---

### Task A0: Parity matrix (Phase 0 deliverable)

**Files:**
- Create: `docs/superpowers/specs/runtime-v2-parity-matrix.md`

This is the acceptance checklist that later tasks are verified against. No code/tests.

- [ ] **Step 1: Write the matrix** — one row per capability, mapping the current io_cli verb → the existing `*_core` function → the new capability function → the V2 action-type string.

```markdown
# Runtime V2 Parity Matrix

| io_cli verb | backend endpoint | existing *_core fn | capability fn | action_type | enclave? |
|---|---|---|---|---|---|
| memory-index | POST /v1/memory/index | memory_core.index | capabilities.memory.index | memory_index | yes (decrypt) |
| memory-fetch | POST /v1/memory/fetch | memory_core.fetch | capabilities.memory.fetch | memory_fetch | yes |
| (POST /v1/memory/actions) | POST /v1/memory/actions | memory_core.actions | capabilities.memory.write | memory_write | no |
| perception | GET /v1/agent/perception | perception_core.agent_perception_payload | capabilities.perception.snapshot | perception_snapshot | no |
| perception-trend | GET /v1/agent/perception/trend | perception_core.perception_trend_payload | capabilities.perception.trend | perception_trend | no |
| perception-history | GET /v1/agent/perception/history | perception_core.perception_history_payload | capabilities.perception.history | perception_history | no |
| screen-recent | GET /v1/screen/frames | screen_read_core.list_frames | capabilities.screen.recent | screen_recent | no |
| screen-read | GET /v1/screen/frames/{id}/decrypt | screen_read_core.frame_decrypt | capabilities.screen.read | screen_read | yes |
| photo-recent | GET /v1/perception/photos | perception_read_core.photos_recent | capabilities.photo.recent | photo_recent | no |
| photo-read | GET /v1/perception/photo/{id}/content | perception_read_core.photo_content | capabilities.photo.read | photo_read | yes (image) |
| chat-image | GET {ENCLAVE}/v1/chat/history | (none — enclave direct) | capabilities.chat.image_read | chat_image_read | yes |
| identity-write | POST /v1/identity/actions | identity_core.run_actions | capabilities.identity.patch | identity_patch | no |
| (GET /v1/identity/get) | GET /v1/identity/get | identity_core.get_identity | capabilities.identity.get | identity_get | no |
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/runtime-v2-parity-matrix.md
git commit -m "docs(runtime-v2): parity matrix (Phase 0)"
```

---

### Task A1: Result types + errors/redaction

**Files:**
- Create: `backend/capabilities/__init__.py`, `backend/capabilities/types.py`, `backend/capabilities/errors.py`
- Test: `tests/test_capabilities_types.py`

**Interfaces — Produces:** `CapabilityResult`, `ok()`, `err()`, `errors.code_for_status()`, `errors.retryable_for_status()`, `errors.message_for_body()`, `errors.cap_text()`, `errors.cap_list()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_types.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from capabilities.types import CapabilityResult, ok, err  # noqa: E402
from capabilities import errors  # noqa: E402


def test_ok_to_dict():
    r = ok({"n": 1})
    assert r.ok is True
    assert r.to_dict() == {"ok": True, "data": {"n": 1}, "trace": {}, "warnings": []}


def test_err_to_dict():
    r = err("capability_invalid_input", "bad", retryable=False)
    assert r.ok is False
    assert r.to_dict() == {"ok": False, "error": {"code": "capability_invalid_input", "message": "bad", "retryable": False}}


def test_code_and_retryable_for_status():
    assert errors.code_for_status(400) == "capability_invalid_input"
    assert errors.code_for_status(404) == "capability_not_found"
    assert errors.code_for_status(503) == "capability_upstream_error"
    assert errors.retryable_for_status(503) is True
    assert errors.retryable_for_status(400) is False


def test_message_for_body_extracts_and_caps():
    assert errors.message_for_body({"error": "boom"}, "d") == "boom"
    assert errors.message_for_body({"nope": 1}, "default") == "default"
    long = "x" * 5000
    assert errors.message_for_body({"message": long}, "d").endswith("…(capped)")


def test_cap_list_truncates():
    assert errors.cap_list(list(range(100)), limit=10) == list(range(10))
    assert errors.cap_list("not a list") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/__init__.py
```

```python
# backend/capabilities/types.py
"""Uniform result shape for the capability facade (Hosted Runtime V2).

Domain `*_core` functions return heterogeneous shapes — `(body, status)`
tuples, `ScreenResult` dataclasses, or raise `AgentRouteError`. The V2 worker's
planner/executor need ONE shape; `CapabilityResult` is it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CapabilityResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error: Optional[dict] = None          # {"code","message","retryable"}
    trace: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        if self.ok:
            return {"ok": True, "data": self.data, "trace": self.trace,
                    "warnings": self.warnings}
        return {"ok": False, "error": self.error}


def ok(data: Optional[dict] = None, *, trace: Optional[dict] = None,
       warnings: Optional[list] = None) -> CapabilityResult:
    return CapabilityResult(ok=True, data=data or {}, trace=trace or {},
                            warnings=warnings or [])


def err(code: str, message: str, *, retryable: bool = False,
        trace: Optional[dict] = None) -> CapabilityResult:
    return CapabilityResult(ok=False,
                            error={"code": code, "message": message,
                                   "retryable": retryable},
                            trace=trace or {})
```

```python
# backend/capabilities/errors.py
"""Error-code mapping + output caps/redaction for the capability facade."""
from __future__ import annotations

from typing import Any

UNAVAILABLE = "capability_unavailable"
INVALID = "capability_invalid_input"
NOT_FOUND = "capability_not_found"
FORBIDDEN = "capability_forbidden"
UPSTREAM = "capability_upstream_error"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

MAX_TEXT = 2000
MAX_ITEMS = 50


def code_for_status(status: int) -> str:
    if status in (400, 422):
        return INVALID
    if status in (401, 403):
        return FORBIDDEN
    if status == 404:
        return NOT_FOUND
    if status in _RETRYABLE_STATUS:
        return UPSTREAM
    return UNAVAILABLE


def retryable_for_status(status: int) -> bool:
    return status in _RETRYABLE_STATUS


def cap_text(s: Any, limit: int = MAX_TEXT) -> str:
    s = str(s or "")
    return s if len(s) <= limit else s[:limit] + "…(capped)"


def cap_list(items: Any, limit: int = MAX_ITEMS) -> list:
    if not isinstance(items, list):
        return []
    return items[:limit]


def message_for_body(body: Any, default: str) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            v = body.get(key)
            if isinstance(v, str) and v.strip():
                return cap_text(v)
            if isinstance(v, dict):
                inner = v.get("message") or v.get("error")
                if isinstance(inner, str) and inner.strip():
                    return cap_text(inner)
    return default
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_types.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/__init__.py backend/capabilities/types.py backend/capabilities/errors.py tests/test_capabilities_types.py
git commit -m "feat(capabilities): result types + error mapping + redaction"
```

---

### Task A2: Memory capabilities

**Files:**
- Create: `backend/capabilities/memory.py`
- Test: `tests/test_capabilities_memory.py`

**Interfaces:**
- Consumes: `memory_core.index(store, api_key, payload, *, post_enclave) -> (dict, int)` (`backend/memory/memory_core.py:94`); `memory_core.fetch(...) -> (dict, int)` (`:120`); `memory_core.actions(store, api_key, payload) -> (dict, int)` (`:164`); `memory_readside_core.post_enclave_readside(api_key, candidates, *, operation, payload=None, runtime_token=None) -> dict` (`backend/memory_readside_core.py:212`).
- Produces: `memory.index/fetch/write(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`.

- [ ] **Step 1: Write the failing test** (monkeypatch the core — no DB/enclave needed)

```python
# tests/test_capabilities_memory.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from memory import memory_core  # noqa: E402
from capabilities import memory as cap_memory  # noqa: E402


def test_index_wraps_core_body(monkeypatch):
    captured = {}
    def fake_index(store, api_key, payload, *, post_enclave):
        captured["payload"] = payload
        captured["post_enclave"] = post_enclave
        return {"items": [1, 2], "limit": 50}, 200
    monkeypatch.setattr(memory_core, "index", fake_index)

    r = cap_memory.index("STORE", api_key="k", runtime_token="rt", params={"limit": 50})
    assert r.ok is True
    assert r.data == {"items": [1, 2], "limit": 50}
    assert captured["payload"] == {"limit": 50}
    assert callable(captured["post_enclave"])  # closure bound to runtime_token


def test_index_maps_503_retryable(monkeypatch):
    monkeypatch.setattr(memory_core, "index",
                        lambda *a, **k: ({"error": "enclave down"}, 503))
    r = cap_memory.index("STORE", params={})
    assert r.ok is False
    assert r.error == {"code": "capability_upstream_error", "message": "enclave down", "retryable": True}


def test_write_delegates_to_actions(monkeypatch):
    seen = {}
    def fake_actions(store, api_key, payload):
        seen["payload"] = payload
        return {"applied": 1}, 200
    monkeypatch.setattr(memory_core, "actions", fake_actions)
    r = cap_memory.write("STORE", api_key="k", params={"actions": [{"type": "memory.add"}]})
    assert r.ok is True and r.data == {"applied": 1}
    assert seen["payload"] == {"actions": [{"type": "memory.add"}]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.memory'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/memory.py
"""Memory capabilities — thin facade over backend/memory/memory_core.py."""
from __future__ import annotations

from typing import Optional

from memory import memory_core
import memory_readside_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _post_enclave_for(runtime_token: Optional[str]):
    def _post(api_key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key, candidates, operation=operation, payload=payload,
            runtime_token=runtime_token)
    return _post


def _norm(body, status, *, default_msg: str) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=data)
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def index(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = memory_core.index(store, api_key, params or {},
                                     post_enclave=_post_enclave_for(runtime_token))
    return _norm(body, status, default_msg="memory index unavailable")


def fetch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = memory_core.fetch(store, api_key, params or {},
                                     post_enclave=_post_enclave_for(runtime_token))
    return _norm(body, status, default_msg="memory fetch unavailable")


def write(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = memory_core.actions(store, api_key, params or {})
    return _norm(body, status, default_msg="memory write unavailable")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_memory.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/memory.py tests/test_capabilities_memory.py
git commit -m "feat(capabilities): memory index/fetch/write facade"
```

---

### Task A3: Perception capabilities

**Files:**
- Create: `backend/capabilities/perception.py`
- Test: `tests/test_capabilities_perception.py`

**Interfaces:**
- Consumes: `perception_core.agent_perception_payload(store, *, signals_raw) -> dict` (`backend/agent/perception_core.py:214`); `perception_trend_payload(store, *, signal_raw, field_raw, days_raw) -> dict` (`:237`); `perception_history_payload(store, *, signal_raw, days_raw) -> dict` (`:249`); `perception_core.AgentRouteError` with `.status_code` + `.body` (`:32`).
- Produces: `perception.snapshot/trend/history(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_perception.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from agent import perception_core  # noqa: E402
from capabilities import perception as cap_perc  # noqa: E402


def test_snapshot_joins_signal_list_and_wraps(monkeypatch):
    seen = {}
    def fake(store, *, signals_raw):
        seen["signals_raw"] = signals_raw
        return {"ok": True, "signals": {"now": {"t": 1}}}
    monkeypatch.setattr(perception_core, "agent_perception_payload", fake)
    r = cap_perc.snapshot("STORE", params={"signals": ["now", "calendar"]})
    assert r.ok is True
    assert r.data["signals"] == {"now": {"t": 1}}
    assert seen["signals_raw"] == "now,calendar"  # list coerced to CSV


def test_snapshot_maps_agent_route_error(monkeypatch):
    def boom(store, *, signals_raw):
        raise perception_core.AgentRouteError(403, {"error": "not_permitted"})
    monkeypatch.setattr(perception_core, "agent_perception_payload", boom)
    r = cap_perc.snapshot("STORE", params={})
    assert r.ok is False
    assert r.error["code"] == "capability_forbidden"
    assert r.error["message"] == "not_permitted"
    assert r.error["retryable"] is False


def test_trend_threads_params(monkeypatch):
    seen = {}
    def fake(store, *, signal_raw, field_raw, days_raw):
        seen.update(signal=signal_raw, field=field_raw, days=days_raw)
        return {"ok": True, "trend": {}}
    monkeypatch.setattr(perception_core, "perception_trend_payload", fake)
    r = cap_perc.trend("STORE", params={"signal": "vitals", "field": "hr", "days": 30})
    assert r.ok is True
    assert seen == {"signal": "vitals", "field": "hr", "days": 30}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_perception.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.perception'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/perception.py
"""Perception capabilities — facade over backend/agent/perception_core.py."""
from __future__ import annotations

from agent import perception_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _wrap(fn, *, default_msg: str, **kwargs) -> CapabilityResult:
    try:
        body = fn(**kwargs)
    except perception_core.AgentRouteError as e:
        return err(errors.code_for_status(e.status_code),
                   errors.message_for_body(e.body, default_msg),
                   retryable=errors.retryable_for_status(e.status_code))
    return ok(data=body if isinstance(body, dict) else {"result": body})


def _csv(signals):
    if isinstance(signals, (list, tuple)):
        return ",".join(str(s) for s in signals)
    return signals


def snapshot(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    return _wrap(perception_core.agent_perception_payload,
                 default_msg="perception unavailable",
                 store=store, signals_raw=_csv(params.get("signals")))


def trend(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    return _wrap(perception_core.perception_trend_payload,
                 default_msg="perception trend unavailable",
                 store=store, signal_raw=params.get("signal"),
                 field_raw=params.get("field"), days_raw=params.get("days"))


def history(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    return _wrap(perception_core.perception_history_payload,
                 default_msg="perception history unavailable",
                 store=store, signal_raw=params.get("signal"),
                 days_raw=params.get("days"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_perception.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/perception.py tests/test_capabilities_perception.py
git commit -m "feat(capabilities): perception snapshot/trend/history facade"
```

---

### Task A4: Screen capabilities

**Files:**
- Create: `backend/capabilities/screen.py`
- Test: `tests/test_capabilities_screen.py`

**Interfaces:**
- Consumes: `screen_read_core.ScreenResult(status, json_body, raw_body, media_type, headers)` (`backend/screen/screen_read_core.py:45`); `list_frames(store, limit_raw) -> ScreenResult` (`:159`); `latest_frame(store) -> ScreenResult` (`:168`); `frame_decrypt(store, frame_id, *, include_image: str, api_key, runtime_token) -> ScreenResult` (`:209`).
- Produces: `screen.recent/read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_screen.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from screen import screen_read_core  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402
from capabilities import screen as cap_screen  # noqa: E402


def test_recent_wraps_json_body(monkeypatch):
    monkeypatch.setattr(screen_read_core, "list_frames",
                        lambda store, limit: ScreenResult(status=200, json_body={"frames": [], "total": 0}))
    r = cap_screen.recent("STORE", params={"limit": 5})
    assert r.ok is True and r.data == {"frames": [], "total": 0}


def test_read_resolves_latest_then_decrypts(monkeypatch):
    monkeypatch.setattr(screen_read_core, "latest_frame",
                        lambda store: ScreenResult(status=200, json_body={"id": "f1"}))
    seen = {}
    def fake_decrypt(store, frame_id, *, include_image, api_key, runtime_token):
        seen.update(frame_id=frame_id, include_image=include_image)
        return ScreenResult(status=200, json_body={"caption": "a cat"})
    monkeypatch.setattr(screen_read_core, "frame_decrypt", fake_decrypt)
    r = cap_screen.read("STORE", params={})  # no frame_id → resolve latest
    assert r.ok is True and r.data == {"caption": "a cat"}
    assert seen == {"frame_id": "f1", "include_image": "false"}


def test_read_binary_body_exposes_meta_only(monkeypatch):
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(status=200, raw_body=b"\xff\xd8", media_type="image/jpeg"))
    r = cap_screen.read("STORE", params={"frame_id": "f2", "include_image": True})
    assert r.ok is True
    assert r.data == {"media_type": "image/jpeg", "has_binary": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_screen.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.screen'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/screen.py
"""Screen capabilities — facade over backend/screen/screen_read_core.py."""
from __future__ import annotations

from screen import screen_read_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(res, *, default_msg: str) -> CapabilityResult:
    if res.status == 200:
        if res.json_body is not None:
            data = res.json_body if isinstance(res.json_body, dict) else {"result": res.json_body}
        else:
            # binary/opaque body (pixels): never inline into planner/status; meta only
            data = {"media_type": res.media_type, "has_binary": res.raw_body is not None}
        return ok(data=data)
    return err(errors.code_for_status(res.status),
               errors.message_for_body(res.json_body, default_msg),
               retryable=errors.retryable_for_status(res.status))


def recent(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    res = screen_read_core.list_frames(store, params.get("limit"))
    return _norm(res, default_msg="screen list unavailable")


def read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    frame_id = params.get("frame_id")
    if not frame_id:
        latest = screen_read_core.latest_frame(store)
        if latest.status != 200 or not isinstance(latest.json_body, dict):
            return _norm(latest, default_msg="no screen frame")
        frame_id = latest.json_body.get("id") or latest.json_body.get("frame_id")
        if not frame_id:
            return err(errors.NOT_FOUND, "no recent screen frame", retryable=False)
    include_image = "true" if params.get("include_image") else "false"
    res = screen_read_core.frame_decrypt(store, frame_id, include_image=include_image,
                                         api_key=api_key, runtime_token=runtime_token)
    return _norm(res, default_msg="screen read unavailable")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_screen.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/screen.py tests/test_capabilities_screen.py
git commit -m "feat(capabilities): screen recent/read facade"
```

---

### Task A5: Photo capabilities

**Files:**
- Create: `backend/capabilities/photo.py`
- Test: `tests/test_capabilities_photo.py`

**Interfaces:**
- Consumes: `perception_read_core.photos_recent(store, limit_raw) -> (dict, int)` (`backend/perception/perception_read_core.py:109`); `photo_content(store, photo_id) -> (dict, int)` (`:114`); `screen_read_core.frame_decrypt(...) -> ScreenResult` (Task A4).
- Produces: `photo.recent/read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_photo.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from perception import perception_read_core  # noqa: E402
from screen import screen_read_core  # noqa: E402
from screen.screen_read_core import ScreenResult  # noqa: E402
from capabilities import photo as cap_photo  # noqa: E402


def test_recent_wraps(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photos_recent",
                        lambda store, limit: ({"photos": [{"id": "p1"}]}, 200))
    r = cap_photo.recent("STORE", params={"limit": 3})
    assert r.ok is True and r.data == {"photos": [{"id": "p1"}]}


def test_read_requires_id():
    r = cap_photo.read("STORE", params={})
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_read_augments_with_image_meta_when_requested(monkeypatch):
    monkeypatch.setattr(perception_read_core, "photo_content",
                        lambda store, pid: ({"id": "p1", "frame_id": "f9"}, 200))
    monkeypatch.setattr(screen_read_core, "frame_decrypt",
                        lambda *a, **k: ScreenResult(status=200, raw_body=b"x", media_type="image/png"))
    r = cap_photo.read("STORE", params={"id": "p1", "include_image": True})
    assert r.ok is True
    assert r.data["image_media_type"] == "image/png" and r.data["has_image"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_photo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.photo'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/photo.py
"""Photo capabilities — facade over backend/perception/perception_read_core.py."""
from __future__ import annotations

from perception import perception_read_core
from screen import screen_read_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(body, status, *, default_msg) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=data)
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def recent(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    body, status = perception_read_core.photos_recent(store, params.get("limit"))
    return _norm(body, status, default_msg="photos unavailable")


def read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    photo_id = params.get("photo_id") or params.get("id")
    if not photo_id:
        return err(errors.INVALID, "photo read needs id", retryable=False)
    body, status = perception_read_core.photo_content(store, photo_id)
    result = _norm(body, status, default_msg="photo unavailable")
    if not result.ok or not params.get("include_image"):
        return result
    frame_id = body.get("frame_id") if isinstance(body, dict) else None
    if not frame_id:
        return result
    img = screen_read_core.frame_decrypt(store, frame_id, include_image="true",
                                         api_key=api_key, runtime_token=runtime_token)
    if img.status == 200:
        result.data = {**result.data, "image_media_type": img.media_type,
                       "has_image": img.raw_body is not None}
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_photo.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/photo.py tests/test_capabilities_photo.py
git commit -m "feat(capabilities): photo recent/read facade"
```

---

### Task A6: Identity capabilities

**Files:**
- Create: `backend/capabilities/identity.py`
- Test: `tests/test_capabilities_identity.py`

**Interfaces:**
- Consumes: `identity_core.get_identity(store) -> (dict, int)` (`backend/identity/identity_core.py:32`); `identity_core.run_actions(store, payload, *, api_key, runtime_token) -> (dict, int)` (`:44`).
- Produces: `identity.get/patch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_identity.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from identity import identity_core  # noqa: E402
from capabilities import identity as cap_identity  # noqa: E402


def test_get_wraps(monkeypatch):
    monkeypatch.setattr(identity_core, "get_identity",
                        lambda store: ({"identity": {"days_with_user": 3}}, 200))
    r = cap_identity.get("STORE")
    assert r.ok is True and r.data["identity"]["days_with_user"] == 3


def test_patch_builds_profile_patch_action(monkeypatch):
    seen = {}
    def fake_run_actions(store, payload, *, api_key, runtime_token):
        seen["payload"] = payload
        seen["runtime_token"] = runtime_token
        return {"applied": True}, 200
    monkeypatch.setattr(identity_core, "run_actions", fake_run_actions)
    r = cap_identity.patch("STORE", api_key="k", runtime_token="rt",
                           params={"self_introduction": "hi", "signature": ["a", "b"]})
    assert r.ok is True and r.data == {"applied": True}
    assert seen["payload"] == {"action": {"type": "identity.profile_patch",
                                          "patch": {"self_introduction": "hi", "signature": ["a", "b"]}}}
    assert seen["runtime_token"] == "rt"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.identity'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/identity.py
"""Identity capabilities — facade over backend/identity/identity_core.py."""
from __future__ import annotations

from identity import identity_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(body, status, *, default_msg) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=data)
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def get(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = identity_core.get_identity(store)
    return _norm(body, status, default_msg="identity unavailable")


def patch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    patch_fields = params.get("patch")
    if patch_fields is None:
        patch_fields = {k: params[k] for k in ("self_introduction", "signature") if k in params}
    payload = {"action": {"type": "identity.profile_patch", "patch": patch_fields}}
    body, status = identity_core.run_actions(store, payload, api_key=api_key,
                                             runtime_token=runtime_token or "")
    return _norm(body, status, default_msg="identity patch unavailable")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_identity.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/identity.py tests/test_capabilities_identity.py
git commit -m "feat(capabilities): identity get/patch facade"
```

---

### Task A7: Chat-image capability (enclave-only)

**Files:**
- Create: `backend/capabilities/chat.py`
- Test: `tests/test_capabilities_chat.py`

**Interfaces:**
- Consumes: `screen_read_core.enclave_forward_headers(*, api_key, runtime_token) -> dict` (`backend/screen/screen_read_core.py:61`); `httpx`; env `FEEDLING_ENCLAVE_URL`. Mirrors `io_cli.cmd_chat_image` (`tools/io_cli.py:446`) which GETs `{ENCLAVE}/v1/chat/history?since=0&limit=` and picks the message by id.
- Produces: `chat.image_read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_chat.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

import httpx  # noqa: E402
from capabilities import chat as cap_chat  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


def test_image_read_requires_id():
    r = cap_chat.image_read("STORE", params={})
    assert r.ok is False and r.error["code"] == "capability_invalid_input"


def test_image_read_finds_message(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        200, {"messages": [{"id": "m1", "image_b64": "AAAA", "image_mime": "image/png"}]}))
    r = cap_chat.image_read("STORE", runtime_token="rt", params={"id": "m1"})
    assert r.ok is True
    assert r.data == {"message_id": "m1", "image_mime": "image/png", "image_b64": "AAAA"}


def test_image_read_missing_message(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.example")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"messages": []}))
    r = cap_chat.image_read("STORE", params={"id": "nope"})
    assert r.ok is False and r.error["code"] == "capability_not_found"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_chat.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.chat'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/chat.py
"""Chat-image capability — enclave-only (no backend route). Mirrors
io_cli.cmd_chat_image: GET {ENCLAVE}/v1/chat/history and pick message by id."""
from __future__ import annotations

import os

import httpx

from screen import screen_read_core   # reuse enclave_forward_headers
from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def image_read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    message_id = params.get("message_id") or params.get("id")
    if not message_id:
        return err(errors.INVALID, "chat image needs message id", retryable=False)
    enclave = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave:
        return err(errors.UNAVAILABLE, "enclave url not configured", retryable=False)
    limit = int(params.get("limit") or 20)
    headers = screen_read_core.enclave_forward_headers(api_key=api_key, runtime_token=runtime_token)
    try:
        resp = httpx.get(f"{enclave}/v1/chat/history",
                         params={"since": 0, "limit": limit},
                         headers=headers, verify=False, timeout=30)
    except Exception as e:  # noqa: BLE001
        return err(errors.UPSTREAM, f"enclave chat history failed: {type(e).__name__}", retryable=True)
    if resp.status_code != 200:
        return err(errors.code_for_status(resp.status_code),
                   errors.message_for_body(_safe_json(resp), "chat history unavailable"),
                   retryable=errors.retryable_for_status(resp.status_code))
    body = _safe_json(resp) or {}
    messages = body.get("messages") if isinstance(body, dict) else None
    msg = next((m for m in (messages or []) if str(m.get("id")) == str(message_id)), None)
    if not msg:
        return err(errors.NOT_FOUND, f"message {message_id} not in recent history", retryable=False)
    image_b64 = msg.get("image_b64")
    if not image_b64:
        return err(errors.NOT_FOUND, "message has no image", retryable=False)
    return ok(data={"message_id": str(message_id),
                    "image_mime": msg.get("image_mime", "image/jpeg"),
                    "image_b64": image_b64})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_chat.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/capabilities/chat.py tests/test_capabilities_chat.py
git commit -m "feat(capabilities): chat-image enclave facade"
```

---

### Task A8: Registry + dispatch

**Files:**
- Create: `backend/capabilities/registry.py`
- Test: `tests/test_capabilities_registry.py`

**Interfaces:**
- Consumes: all verb modules from Tasks A2–A7.
- Produces: `CAPABILITIES: dict[str, Callable]`, `READ_ACTIONS`, `WRITE_ACTIONS`, `run_capability(action_type, store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult`. **This is the exact surface Plan C's executor drives.**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capabilities_registry.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from capabilities import registry  # noqa: E402
from capabilities import memory as cap_memory  # noqa: E402
from capabilities.types import ok  # noqa: E402


def test_all_action_types_registered():
    expected = {
        "identity_get", "identity_patch", "memory_index", "memory_fetch", "memory_write",
        "perception_snapshot", "perception_trend", "perception_history",
        "screen_recent", "screen_read", "photo_recent", "photo_read", "chat_image_read",
    }
    assert set(registry.CAPABILITIES) == expected
    assert registry.WRITE_ACTIONS == frozenset({"memory_write", "identity_patch"})
    assert "memory_index" in registry.READ_ACTIONS


def test_run_capability_dispatches(monkeypatch):
    monkeypatch.setattr(cap_memory, "index",
                        lambda store, **kw: ok({"items": [1]}))
    r = registry.run_capability("memory_index", "STORE", params={"limit": 1})
    assert r.ok is True and r.data == {"items": [1]}


def test_run_capability_unknown():
    r = registry.run_capability("does_not_exist", "STORE")
    assert r.ok is False and r.error["code"] == "capability_invalid_input"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'capabilities.registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/capabilities/registry.py
"""Action-type → capability dispatch table for the V2 executor (Plan C)."""
from __future__ import annotations

from typing import Callable

from capabilities import memory, perception, screen, photo, identity, chat
from capabilities import errors
from capabilities.types import CapabilityResult, err

CAPABILITIES: dict[str, Callable[..., CapabilityResult]] = {
    "identity_get": identity.get,
    "identity_patch": identity.patch,
    "memory_index": memory.index,
    "memory_fetch": memory.fetch,
    "memory_write": memory.write,
    "perception_snapshot": perception.snapshot,
    "perception_trend": perception.trend,
    "perception_history": perception.history,
    "screen_recent": screen.recent,
    "screen_read": screen.read,
    "photo_recent": photo.recent,
    "photo_read": photo.read,
    "chat_image_read": chat.image_read,
}

WRITE_ACTIONS = frozenset({"memory_write", "identity_patch"})
READ_ACTIONS = frozenset(set(CAPABILITIES) - WRITE_ACTIONS)


def run_capability(action_type: str, store, *, api_key=None, runtime_token=None,
                   params=None) -> CapabilityResult:
    fn = CAPABILITIES.get(action_type)
    if fn is None:
        return err(errors.INVALID, f"unknown capability: {action_type}", retryable=False)
    return fn(store, api_key=api_key, runtime_token=runtime_token, params=params)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities_registry.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the whole capability suite**

Run: `pytest tests/test_capabilities_*.py -v`
Expected: PASS (all capability tests green)

- [ ] **Step 6: Commit**

```bash
git add backend/capabilities/registry.py tests/test_capabilities_registry.py
git commit -m "feat(capabilities): action-type dispatch registry"
```

---

## Plan A done — hand-off to Plan B / C

- Plan C's executor imports **`capabilities.registry.run_capability`** and drives it by `action_type`, threading `store`, `api_key`, `runtime_token`, `params`.
- Every `data` is already capped/redacted, safe to log in status events and feed the responder.
- Enclave-bound verbs (`memory_index/fetch`, `screen_read`, `photo_read` with image, `chat_image_read`) still hit the enclave inside the core — Plan B/C must wrap those calls in the shared `ENCLAVE_SEMAPHORE` (spec §11 R3).

## Self-Review (done)

- **Spec coverage:** §4 capability layer → Tasks A1–A8; §4.4 parity matrix → A0; §11 redaction/caps → A1 `cap_text/cap_list`, applied in every `_norm`/`message_for_body`. ✅
- **Placeholder scan:** none — every step has real code + exact commands. ✅
- **Type consistency:** `CapabilityResult`/`ok`/`err` signatures identical across A1–A8; every verb is `(store, *, api_key, runtime_token, params) -> CapabilityResult`; registry keys match the action-type strings in the parity matrix and in Plan C's Consumes block. ✅
