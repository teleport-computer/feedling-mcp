# New Model API Users Default to Runtime V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route accounts registered at or after an explicit UTC cutoff directly to Hosted Runtime V2 when their first Model API route becomes active, without moving older accounts or affecting resident users.

**Architecture:** Keep the fleet-wide runtime default at `resident`. Add narrow PostgreSQL read/CAS helpers and a focused `hosted.new_user_v2_cohort` policy module; call it synchronously from successful Model API setup, while preserving the existing allowlist, generation fence, wake-schedule seed, reconciler, and manual override authority. Resident route selection writes an explicit resident desired state so an earlier automatic cohort record cannot flip the account back to V2.

**Tech Stack:** Python 3.11+, FastAPI/ASGI core functions, PostgreSQL via psycopg, pytest, Docker Compose, MDX documentation.

**Spec:** `docs/superpowers/specs/2026-08-10-new-model-api-users-default-v2-design.md`

## Global Constraints

- `FEEDLING_RUNTIME_DEFAULT_DESIRED` remains exactly `resident` in the managed test, pre, and production deployments.
- Only accounts whose `users.created_at` is at or after `FEEDLING_V2_NEW_USER_CUTOFF` and whose Model API route is tested and active are auto-admitted.
- Missing/empty/invalid cutoff or missing/unparseable registration time fails safe to resident.
- Naive historical `users.created_at` values are interpreted as UTC, matching the repository's existing user-time reporting contract.
- Any allowlist row whose `updated_by` is not `new-user-cohort` is an explicit pin and must never be overwritten by cohort admission.
- Runtime transitions must reuse `config_store.set_hosted_runtime_mode`; never update the ownership fence directly.
- Setup returns success only after route activation and V2 ownership convergence; a cutover failure returns the existing stable slug `runtime_policy_unavailable` with HTTP 503.
- Provider keys, chat plaintext, and user content must never appear in logs.
- No schema migration and no public API route or response-shape change are required.
- Implement in an isolated worktree because the shared checkout currently contains an in-progress merge and unrelated staged work.

---

## File Map

- Create `backend/hosted/new_user_v2_cohort.py`: cutoff parsing, cohort decision, insert-if-absent admission, synchronous V2 convergence, and resident desired-state pinning.
- Modify `backend/db.py`: strict single-user registration-time read, strict single-row allowlist read, and insert-if-absent allowlist CAS.
- Modify `backend/hosted/setup_core.py`: invoke the cohort policy after route activation and forced-policy reconciliation, before setup reports success.
- Modify `backend/accounts/accounts_core.py`: make both resident route-selection surfaces write an explicit resident pin and compensate on transition failure.
- Create `tests/test_new_user_v2_cohort.py`: policy, cutoff, priority, CAS, idempotency, and failure tests.
- Modify `tests/test_dual_runtime_db.py`: persistence primitive tests.
- Modify `tests/test_model_api_chat_send_routing.py`: register → setup → direct-V2 integration tests.
- Modify `tests/test_access_mode_runtime_sync_unit.py`: resident desired-state and compensation tests.
- Modify `tests/test_asgi_accounts_remaining.py`: `/v1/onboarding/route` resident synchronization parity.
- Modify `tests/test_hosted_runtime_policy.py`: managed Compose configuration contract.
- Modify `deploy/docker-compose.phala.test.yaml`, `deploy/docker-compose.phala.pre.yaml`, and `deploy/docker-compose.phala.yaml`: pass an empty-by-default cutoff only to `backend`.
- Modify `docs/HOSTED_RUNTIME_V2_ADDING_USERS.md` and `deploy/DEPLOYMENTS.md`: operator semantics, activation, observability, and rollback.
- Modify `docs-site/content/docs/architecture.mdx`, `docs-site/content/docs/workflows/chat.mdx`, and `docs-site/content/docs/changelog.mdx`: public architecture/workflow behavior and Unreleased entry.

---

### Task 1: Add race-safe cohort persistence primitives

**Files:**
- Modify: `backend/db.py:462-480`
- Modify: `backend/db.py:13151-13201`
- Test: `tests/test_dual_runtime_db.py:1-80`

**Interfaces:**
- Produces: `db.get_user_created_at_strict(user_id: str) -> str | None`
- Produces: `db.get_runtime_allowlist_entry(user_id: str) -> dict | None`
- Produces: `db.insert_runtime_allowlist_if_absent(user_id: str, desired: str, *, updated_by: str, note: str) -> bool`
- Consumes: existing `db.get_pool()` and `_RUNTIME_ALLOWLIST_DESIRED`

- [ ] **Step 1: Write failing DB tests**

Append focused tests to `tests/test_dual_runtime_db.py`:

```python
def test_single_user_created_at_read_is_strict(fresh_user):
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE users SET created_at=%s WHERE user_id=%s",
            ("2026-08-10T03:04:05+00:00", fresh_user),
        )
    assert db.get_user_created_at_strict(fresh_user) == "2026-08-10T03:04:05+00:00"
    assert db.get_user_created_at_strict("usr_missing") is None


def test_allowlist_entry_read_and_insert_if_absent_preserve_manual_pin(fresh_user):
    assert db.get_runtime_allowlist_entry(fresh_user) is None
    assert db.insert_runtime_allowlist_if_absent(
        fresh_user,
        "resident",
        updated_by="admin-api",
        note="manual rollback",
    ) is True
    assert db.insert_runtime_allowlist_if_absent(
        fresh_user,
        "v2",
        updated_by="new-user-cohort",
        note="registered-at-or-after:2026-08-10T00:00:00Z",
    ) is False
    assert db.get_runtime_allowlist_entry(fresh_user) == {
        "user_id": fresh_user,
        "desired": "resident",
        "updated_by": "admin-api",
        "note": "manual rollback",
    }
```

Make `get_runtime_allowlist_entry` omit `updated_at`; the cohort policy does not need it, and exact equality keeps the interface small.

- [ ] **Step 2: Run the tests and verify the new interfaces are missing**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_dual_runtime_db.py::test_single_user_created_at_read_is_strict tests/test_dual_runtime_db.py::test_allowlist_entry_read_and_insert_if_absent_preserve_manual_pin -q
```

Expected: FAIL with `AttributeError` for `get_user_created_at_strict` or `get_runtime_allowlist_entry`.

- [ ] **Step 3: Implement the strict reads and insert-only CAS**

Add beside `load_user` in `backend/db.py`:

```python
def get_user_created_at_strict(user_id: str) -> str | None:
    """Return the authoritative users.created_at text; DB errors propagate."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM users WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    return None if row is None else str(row[0] or "")
```

Add beside the existing allowlist helpers:

```python
def get_runtime_allowlist_entry(user_id: str) -> dict | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT user_id,desired,updated_by,note "
            "FROM v2_user_allowlist WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "user_id": str(row[0]),
        "desired": str(row[1]),
        "updated_by": str(row[2] or ""),
        "note": str(row[3] or ""),
    }


def insert_runtime_allowlist_if_absent(
    user_id: str,
    desired: str,
    *,
    updated_by: str,
    note: str,
) -> bool:
    if desired not in _RUNTIME_ALLOWLIST_DESIRED:
        raise ValueError(
            f"desired must be one of {sorted(_RUNTIME_ALLOWLIST_DESIRED)}"
        )
    with get_pool().connection() as conn:
        cur = conn.execute(
            "INSERT INTO v2_user_allowlist "
            "(user_id,desired,updated_at,updated_by,note) "
            "VALUES (%s,%s,now(),%s,%s) ON CONFLICT (user_id) DO NOTHING",
            (str(user_id), desired, updated_by, note),
        )
    return cur.rowcount > 0
```

Do not implement this operation by calling `upsert_runtime_allowlist`: an admin `resident` write racing between cohort read and insert must win.

- [ ] **Step 4: Run the focused and existing allowlist tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_dual_runtime_db.py tests/test_admin_runtime_allowlist.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the persistence primitives**

```bash
git add backend/db.py tests/test_dual_runtime_db.py
git commit -m "feat(runtime): add cohort control persistence primitives"
```

---

### Task 2: Implement the new-user V2 cohort policy

**Files:**
- Create: `backend/hosted/new_user_v2_cohort.py`
- Create: `tests/test_new_user_v2_cohort.py`

**Interfaces:**
- Consumes: `db.get_user_created_at_strict(user_id)`
- Consumes: `db.get_runtime_allowlist_entry(user_id)`
- Consumes: `db.insert_runtime_allowlist_if_absent(...)`
- Consumes: `config_store.load_active_route(store)` and `config_store.set_hosted_runtime_mode(store, mode)`
- Produces: `new_user_v2_cohort.Decision(eligible: bool, reason: str, normalized_cutoff: str)`
- Produces: `new_user_v2_cohort.decision_for_user(user_id: str) -> Decision`
- Produces: `new_user_v2_cohort.apply_default(store) -> str`
- Produces: constants `NEW_USER_V2_CUTOFF_ENV`, `AUTO_UPDATED_BY`, and `ACCESS_MODE_UPDATED_BY`

- [ ] **Step 1: Write cutoff and priority tests**

Create `tests/test_new_user_v2_cohort.py` with the normal backend path insertion and these cases:

```python
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from hosted import config_store, new_user_v2_cohort  # noqa: E402


def test_decision_uses_registration_time_and_requires_valid_utc_cutoff(monkeypatch):
    monkeypatch.setattr(
        db, "get_user_created_at_strict", lambda _uid: "2026-08-10T10:00:00"
    )
    monkeypatch.setenv(
        new_user_v2_cohort.NEW_USER_V2_CUTOFF_ENV,
        "2026-08-10T09:59:59Z",
    )
    assert new_user_v2_cohort.decision_for_user("usr_new").eligible is True

    monkeypatch.setenv(
        new_user_v2_cohort.NEW_USER_V2_CUTOFF_ENV,
        "2026-08-10T10:00:01Z",
    )
    decision = new_user_v2_cohort.decision_for_user("usr_old")
    assert (decision.eligible, decision.reason) == (False, "before_cutoff")

    monkeypatch.setenv(new_user_v2_cohort.NEW_USER_V2_CUTOFF_ENV, "not-a-time")
    assert new_user_v2_cohort.decision_for_user("usr_bad").reason == "invalid_cutoff"


def test_apply_default_never_overwrites_manual_resident_pin(monkeypatch):
    store = SimpleNamespace(user_id="usr_manual")
    monkeypatch.setattr(config_store, "hosted_runtime_policy", lambda: "dual")
    monkeypatch.setattr(
        db,
        "get_runtime_allowlist_entry",
        lambda _uid: {
            "user_id": "usr_manual",
            "desired": "resident",
            "updated_by": "admin-api",
            "note": "rollback",
        },
    )
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *_args: (_ for _ in ()).throw(AssertionError("manual pin overwritten")),
    )
    assert new_user_v2_cohort.apply_default(store) == "explicit_pin"
```

Add the real transition/idempotency test in the same file:

```python
@pytest.fixture()
def runnable_user():
    import uuid
    from conftest import configure_model_api_route

    user_id = f"usr_cohort_{uuid.uuid4().hex[:12]}"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,%s,'{}'::jsonb)",
            (user_id, "2026-08-10T10:00:00+00:00"),
        )
    configure_model_api_route(
        user_id, provider="anthropic", model="claude-3-5-sonnet-latest"
    )
    return user_id


def test_existing_automatic_row_converges_once(runnable_user, monkeypatch):
    from core import store as core_store

    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    monkeypatch.setenv(
        new_user_v2_cohort.NEW_USER_V2_CUTOFF_ENV,
        "2026-08-10T00:00:00Z",
    )
    db.insert_runtime_allowlist_if_absent(
        runnable_user,
        "v2",
        updated_by=new_user_v2_cohort.AUTO_UPDATED_BY,
        note="registered-at-or-after:2026-08-10T00:00:00Z",
    )
    store = core_store.get_store(runnable_user)
    assert new_user_v2_cohort.apply_default(store) == "converged"
    first = db.get_hosted_runtime_control_strict(runnable_user)
    assert first[:2] == ("db_action_v2", "v2")
    assert new_user_v2_cohort.apply_default(store) == "converged"
    assert db.get_hosted_runtime_control_strict(runnable_user) == first
```

The admin-wins race is covered by Task 1's `insert_runtime_allowlist_if_absent` test; do not duplicate it with mocks here.

- [ ] **Step 2: Run the new test file and verify import failure**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_new_user_v2_cohort.py -q
```

Expected: FAIL with `ImportError: cannot import name 'new_user_v2_cohort'`.

- [ ] **Step 3: Implement the focused policy module**

Create `backend/hosted/new_user_v2_cohort.py` with this shape:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import db
from hosted import config_store

NEW_USER_V2_CUTOFF_ENV = "FEEDLING_V2_NEW_USER_CUTOFF"
AUTO_UPDATED_BY = "new-user-cohort"
ACCESS_MODE_UPDATED_BY = "access-mode"


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    normalized_cutoff: str = ""


def _parse_timestamp(raw: str, *, allow_naive: bool) -> datetime:
    value = str(raw or "").strip()
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("timezone required")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def decision_for_user(user_id: str) -> Decision:
    raw_cutoff = str(os.environ.get(NEW_USER_V2_CUTOFF_ENV) or "").strip()
    if not raw_cutoff:
        return Decision(False, "no_cutoff")
    try:
        cutoff = _parse_timestamp(raw_cutoff, allow_naive=False)
    except (TypeError, ValueError):
        return Decision(False, "invalid_cutoff")
    raw_created = db.get_user_created_at_strict(user_id)
    if not raw_created:
        return Decision(False, "invalid_created_at")
    try:
        created = _parse_timestamp(raw_created, allow_naive=True)
    except (TypeError, ValueError):
        return Decision(False, "invalid_created_at")
    normalized = cutoff.isoformat().replace("+00:00", "Z")
    return Decision(created >= cutoff, "eligible" if created >= cutoff else "before_cutoff", normalized)


def _log(user_id: str, outcome: str) -> None:
    print(f"[new-user-v2:{user_id}] outcome={outcome}")


def apply_default(store) -> str:
    if config_store.hosted_runtime_policy() != config_store.HOSTED_RUNTIME_POLICY_DUAL:
        return "forced_policy"
    existing = db.get_runtime_allowlist_entry(store.user_id)
    inserted = False
    if existing is None:
        decision = decision_for_user(store.user_id)
        if not decision.eligible:
            _log(store.user_id, decision.reason)
            return decision.reason
        inserted = db.insert_runtime_allowlist_if_absent(
            store.user_id,
            "v2",
            updated_by=AUTO_UPDATED_BY,
            note=f"registered-at-or-after:{decision.normalized_cutoff}",
        )
        existing = db.get_runtime_allowlist_entry(store.user_id)
    if not existing or existing["updated_by"] != AUTO_UPDATED_BY:
        _log(store.user_id, "explicit_pin")
        return "explicit_pin"
    if existing["desired"] != "v2":
        _log(store.user_id, "automatic_resident_pin")
        return "automatic_resident_pin"
    if config_store.load_active_route(store) is None:
        raise RuntimeError("new-user V2 cohort requires an active route")
    _log(store.user_id, "record_created" if inserted else "record_already_present")
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    _log(store.user_id, "converged")
    return "converged"
```

Keep the log to identifiers and bounded enum reasons only. Do not log cutoff input, route configuration, provider key hints, or exceptions containing provider material.

- [ ] **Step 4: Run policy tests and verify all branches**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_new_user_v2_cohort.py tests/test_hosted_runtime_policy.py -q
```

Expected: PASS.

- [ ] **Step 5: Mutation-check the two safety boundaries**

Temporarily reverse `created >= cutoff` to `created < cutoff`, run the first cutoff test, and confirm it fails. Restore the comparison. Then temporarily replace `insert_runtime_allowlist_if_absent` with `upsert_runtime_allowlist`, run the manual-pin/CAS tests, and confirm they fail. Restore the production implementation before continuing.

- [ ] **Step 6: Commit the cohort policy**

```bash
git add backend/hosted/new_user_v2_cohort.py tests/test_new_user_v2_cohort.py
git commit -m "feat(runtime): define new-user V2 cohort policy"
```

---

### Task 3: Apply the cohort synchronously during Model API setup

**Files:**
- Modify: `backend/hosted/setup_core.py:874-884`
- Modify: `backend/hosted/setup_core.py:1164-1176`
- Modify: `tests/test_model_api_chat_send_routing.py:32-212`

**Interfaces:**
- Consumes: `new_user_v2_cohort.apply_default(store) -> str`
- Preserves: `_serialized_model_api_mutation`, whose ContextVar lock is reentrant when `set_hosted_runtime_mode` is called inside setup
- Produces: `_apply_new_user_v2_default_or_error(store) -> tuple[dict, int] | None`

- [ ] **Step 1: Write register-to-setup integration tests**

Add tests to `tests/test_model_api_chat_send_routing.py` that explicitly set policy `dual`:

```python
def test_dual_new_registration_setup_converges_v2_before_success(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    monkeypatch.setenv("FEEDLING_V2_NEW_USER_CUTOFF", "2000-01-01T00:00:00Z")
    user_id, api_key = _register(client)
    _setup_openrouter(client, api_key, monkeypatch)

    row = db.get_runtime_allowlist_entry(user_id)
    assert (row["desired"], row["updated_by"]) == ("v2", "new-user-cohort")
    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "db_action_v2",
        "v2",
    )


def test_dual_pre_cutoff_registration_stays_resident(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    monkeypatch.setenv("FEEDLING_V2_NEW_USER_CUTOFF", "2999-01-01T00:00:00Z")
    user_id, api_key = _register(client)
    _setup_openrouter(client, api_key, monkeypatch)
    assert db.get_runtime_allowlist_entry(user_id) is None
    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "resident_cli",
        "resident",
    )
```

Add the manual-pin, idempotency, and fail-closed cases:

```python
def test_dual_new_setup_preserves_manual_resident_pin(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    monkeypatch.setenv("FEEDLING_V2_NEW_USER_CUTOFF", "2000-01-01T00:00:00Z")
    user_id, api_key = _register(client)
    db.upsert_runtime_allowlist(
        user_id, "resident", updated_by="admin-api", note="manual rollback"
    )
    _setup_openrouter(client, api_key, monkeypatch)
    assert db.get_runtime_allowlist_entry(user_id)["updated_by"] == "admin-api"
    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "resident_cli", "resident"
    )


def test_dual_repeated_setup_does_not_rotate_generation(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    monkeypatch.setenv("FEEDLING_V2_NEW_USER_CUTOFF", "2000-01-01T00:00:00Z")
    user_id, api_key = _register(client)
    _setup_openrouter(client, api_key, monkeypatch)
    first_generation = db.get_hosted_runtime_control_strict(user_id)[2]
    _setup_openrouter(client, api_key, monkeypatch)
    assert db.get_hosted_runtime_control_strict(user_id)[2] == first_generation


def test_dual_cohort_transition_failure_is_retryable(client, monkeypatch):
    from hosted import new_user_v2_cohort

    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    monkeypatch.setenv("FEEDLING_V2_NEW_USER_CUTOFF", "2000-01-01T00:00:00Z")
    user_id, api_key = _register(client)
    real_set = config_store.set_hosted_runtime_mode
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cutover down")),
    )
    response = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "test-provider-key",
        },
        headers=_headers(api_key),
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "runtime_policy_unavailable"}
    assert db.model_api_active_route(user_id) is not None
    row = db.get_runtime_allowlist_entry(user_id)
    assert (row["desired"], row["updated_by"]) == (
        "v2", new_user_v2_cohort.AUTO_UPDATED_BY
    )
    monkeypatch.setattr(config_store, "set_hosted_runtime_mode", real_set)
    _setup_openrouter(client, api_key, monkeypatch)
    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "db_action_v2", "v2"
    )
```

Patch the external provider probe exactly as `_setup_openrouter` already does; do not replace `new_user_v2_cohort.apply_default`, because that would bypass the production boundary being tested.

- [ ] **Step 2: Run the new integration cases and verify they fail**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_model_api_chat_send_routing.py -k 'dual_new_registration or dual_pre_cutoff or cohort' -q
```

Expected: the eligible case FAILS because setup leaves the account resident.

- [ ] **Step 3: Add the setup adapter and call it at the correct durable boundary**

Import the module, following repository import style:

```python
from hosted import new_user_v2_cohort
```

Add beside `_apply_runtime_policy_or_error`:

```python
def _apply_new_user_v2_default_or_error(store) -> tuple[dict, int] | None:
    try:
        new_user_v2_cohort.apply_default(store)
    except Exception as exc:  # noqa: BLE001 — control-plane failures fail closed
        print(
            f"[new-user-v2:{store.user_id}] outcome=convergence_failed "
            f"error_type={type(exc).__name__}"
        )
        return {"error": "runtime_policy_unavailable"}, 503
    return None
```

Call it immediately after `_apply_runtime_policy_or_error(store)` succeeds and before `_save_onboarding_route(store, "model_api")`:

```python
policy_error = _apply_runtime_policy_or_error(store)
if policy_error is not None:
    return policy_error
cohort_error = _apply_new_user_v2_default_or_error(store)
if cohort_error is not None:
    return cohort_error
accounts_onboarding._save_onboarding_route(store, "model_api")
```

This location guarantees the credential and tested active route exist before cohort evaluation, and setup cannot return success before V2 convergence.

- [ ] **Step 4: Run setup, routing, and cutover regression tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_model_api_chat_send_routing.py tests/test_model_api_path.py tests/test_dual_runtime_flip_no_loss.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit setup admission**

```bash
git add backend/hosted/setup_core.py tests/test_model_api_chat_send_routing.py
git commit -m "feat(runtime): admit new Model API users to V2"
```

---

### Task 4: Make resident selection an explicit, compensating pin

**Files:**
- Modify: `backend/hosted/new_user_v2_cohort.py`
- Modify: `backend/accounts/accounts_core.py:41-94`
- Modify: `backend/accounts/accounts_core.py:381-392`
- Modify: `tests/test_access_mode_runtime_sync_unit.py:1-140`
- Modify: `tests/test_asgi_accounts_remaining.py:427-450`

**Interfaces:**
- Produces: `new_user_v2_cohort.pin_resident(store) -> None`
- Produces: `new_user_v2_cohort.restore_allowlist(user_id: str, previous: dict | None) -> None`
- Produces: `accounts_core._select_access_mode(store, mode: str) -> dict`
- Consumes: existing onboarding route persistence and `config_store.set_hosted_runtime_mode`

- [ ] **Step 1: Write unit tests for resident pin and compensation**

Extend `tests/test_access_mode_runtime_sync_unit.py` so `test_resident_switch_moves_runtime_back_to_resident` also asserts:

```python
assert desired_writes == [
    ("usr_test", "resident", "access-mode", "user-selected-resident")
]
```

Add a failure case where runtime transition raises after the resident desired write:

```python
def test_resident_switch_restores_allowlist_on_runtime_failure(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch, previous="model_api")
    previous = {
        "user_id": store.user_id,
        "desired": "v2",
        "updated_by": "new-user-cohort",
        "note": "registered-at-or-after:2026-08-10T00:00:00Z",
    }
    restored: list[dict | None] = []
    monkeypatch.setattr(
        config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2, "v2", 9),
    )
    monkeypatch.setattr(
        db, "get_runtime_allowlist_entry", lambda _uid: previous
    )
    monkeypatch.setattr(
        new_user_v2_cohort,
        "pin_resident",
        lambda _store: (_ for _ in ()).throw(RuntimeError("state write failed")),
    )
    monkeypatch.setattr(
        new_user_v2_cohort,
        "restore_allowlist",
        lambda _uid, row: restored.append(row),
    )
    selected: list[str] = []
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda _store, mode: selected.append(mode) or mode,
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "resident"}
    )

    assert (status, body) == (503, {"error": "runtime_control_unavailable"})
    assert restored == [previous]
    assert selected == [config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2]
    assert saved == ["resident", "model_api"]
```

Import `db` and `new_user_v2_cohort` at the top of the unit test, following the module-style import rule.

Extend `tests/test_asgi_accounts_remaining.py` with a real database-backed route transition rather than patching the route function or cohort producer:

```python
def test_onboarding_route_post_resident_pins_and_demotes_runtime(user):
    from conftest import configure_model_api_route
    import db
    from hosted import config_store

    user_id, api_key = user
    configure_model_api_route(
        user_id, provider="anthropic", model="claude-3-5-sonnet-latest"
    )
    store = core_store.get_store(user_id)
    db.upsert_runtime_allowlist(
        user_id,
        "v2",
        updated_by="new-user-cohort",
        note="registered-at-or-after:2026-08-10T00:00:00Z",
    )
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )

    status, body = _asgi_post(
        "/v1/onboarding/route",
        {"route": "resident"},
        {"X-API-Key": api_key},
    )

    assert status == 200
    assert body["route"] == "resident"
    row = db.get_runtime_allowlist_entry(user_id)
    assert (row["desired"], row["updated_by"]) == ("resident", "access-mode")
    assert db.get_hosted_runtime_control_strict(user_id)[:2] == (
        "resident_cli", "resident"
    )
```

- [ ] **Step 2: Run the resident synchronization tests and verify missing desired-state writes**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_access_mode_runtime_sync_unit.py tests/test_asgi_accounts_remaining.py -k 'resident or onboarding_route_post' -q
```

Expected: FAIL because current resident selection changes only the fence and leaves the prior allowlist record intact.

- [ ] **Step 3: Implement allowlist compensation helpers**

Add to `new_user_v2_cohort.py`:

```python
def restore_allowlist(user_id: str, previous: dict | None) -> None:
    if previous is None:
        db.delete_runtime_allowlist(user_id)
        return
    db.upsert_runtime_allowlist(
        user_id,
        previous["desired"],
        updated_by=previous["updated_by"],
        note=previous["note"],
    )


def pin_resident(store) -> None:
    db.upsert_runtime_allowlist(
        store.user_id,
        "resident",
        updated_by=ACCESS_MODE_UPDATED_BY,
        note="user-selected-resident",
    )
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
    )
```

The caller owns compensation because it already snapshots and restores onboarding route and runtime mode.

- [ ] **Step 4: Route both resident-selection surfaces through one transactional core helper**

Refactor `accounts_core.py` to introduce a private `_select_access_mode(store, mode) -> dict` used by both `access_modes_switch` and `onboarding_route_post`. Its resident control skeleton is:

```python
def _select_access_mode(store: UserStore, mode: str) -> dict:
    from hosted import config_store, new_user_v2_cohort

    previous_mode = onboarding._load_onboarding_route(store)
    previous_runtime_mode = None
    previous_allowlist = None
    if mode == "resident":
        previous_runtime_mode = config_store.get_hosted_runtime_control_strict(store)[0]
        previous_allowlist = db.get_runtime_allowlist_entry(store.user_id)
    data = onboarding._save_onboarding_route(store, mode)
    try:
        if mode == "resident":
            new_user_v2_cohort.pin_resident(store)
    except Exception:
        new_user_v2_cohort.restore_allowlist(store.user_id, previous_allowlist)
        if previous_runtime_mode is not None:
            config_store.set_hosted_runtime_mode(store, previous_runtime_mode)
        onboarding._save_onboarding_route(store, previous_mode)
        raise
    return data
```

Return the existing `runtime_control_unavailable` 503 if compensation is needed. Keep `model_api` and `official_import` behavior unchanged. Preserve the current response shape of each endpoint: access mode switch returns `_access_modes_payload`, while onboarding route post returns the existing onboarding document with `route` and `selected_at`.

- [ ] **Step 5: Run access-mode and account ASGI regression tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_access_mode_runtime_sync_unit.py tests/test_access_modes.py tests/test_asgi_accounts_remaining.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit resident pin semantics**

```bash
git add backend/hosted/new_user_v2_cohort.py backend/accounts/accounts_core.py tests/test_access_mode_runtime_sync_unit.py tests/test_asgi_accounts_remaining.py
git commit -m "fix(runtime): pin user-selected resident ownership"
```

---

### Task 5: Wire safe deployment configuration and documentation

**Files:**
- Modify: `deploy/docker-compose.phala.test.yaml:300-312`
- Modify: `deploy/docker-compose.phala.pre.yaml:230-242`
- Modify: `deploy/docker-compose.phala.yaml:298-310`
- Modify: `tests/test_hosted_runtime_policy.py:520-565`
- Modify: `docs/HOSTED_RUNTIME_V2_ADDING_USERS.md:6-24`
- Modify: `deploy/DEPLOYMENTS.md:136-172`
- Modify: `docs-site/content/docs/architecture.mdx:324-340`
- Modify: `docs-site/content/docs/workflows/chat.mdx:33-92`
- Modify: `docs-site/content/docs/changelog.mdx:11-18`

**Interfaces:**
- Produces: backend container environment `FEEDLING_V2_NEW_USER_CUTOFF=${FEEDLING_V2_NEW_USER_CUTOFF:-}`
- Preserves: literal `FEEDLING_HOSTED_RUNTIME_POLICY=dual` and `FEEDLING_RUNTIME_DEFAULT_DESIRED=resident`

- [ ] **Step 1: Write the Compose contract test first**

Extend the managed-compose assertions in `tests/test_hosted_runtime_policy.py`:

```python
assert env["FEEDLING_HOSTED_RUNTIME_POLICY"] == "dual"
assert env["FEEDLING_RUNTIME_DEFAULT_DESIRED"] == "resident"
assert env["FEEDLING_V2_NEW_USER_CUTOFF"] == "${FEEDLING_V2_NEW_USER_CUTOFF:-}"
```

Assert the cutoff exists on `backend` only; the `serve-worker` does not evaluate registration cohorts and must not gain a redundant copy.

- [ ] **Step 2: Run the Compose test and verify it fails**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_hosted_runtime_policy.py -k 'main_compose' -q
```

Expected: FAIL with missing `FEEDLING_V2_NEW_USER_CUTOFF`.

- [ ] **Step 3: Add the empty-by-default backend environment passthrough**

In each managed main Compose, immediately after `FEEDLING_RUNTIME_DEFAULT_DESIRED`, add:

```yaml
      # UTC registration cutoff for automatic new Model API -> Runtime V2
      # admission. Empty preserves resident-default behavior for every account.
      FEEDLING_V2_NEW_USER_CUTOFF: "${FEEDLING_V2_NEW_USER_CUTOFF:-}"
```

Do not set a date in source. Test/pre/prod activation is an environment-specific rollout decision after the code image is deployed and healthy.

- [ ] **Step 4: Update internal operator documentation**

In `docs/HOSTED_RUNTIME_V2_ADDING_USERS.md`, add sections covering:

- eligibility is checked only after a tested active Model API route exists;
- automatic rows use `updated_by=new-user-cohort`;
- manual rows override automatic admission;
- deleting an automatic row is not a durable resident pin because a later setup can recreate it;
- durable single-user rollback is `desired=resident`;
- stopping admission means clearing the cutoff or moving it into the future;
- batch rollback filters exactly `updated_by='new-user-cohort' AND desired='v2'`.

In `deploy/DEPLOYMENTS.md`, add the cutoff to the backend environment table, state that its empty value is the zero-change deployment phase, and record the order: deploy code with empty cutoff → verify → set an explicit UTC cutoff → redeploy measured Compose → observe cohort convergence.

- [ ] **Step 5: Update public docs and Unreleased changelog**

Add concise public behavior statements:

- Architecture: newly registered Model API accounts can be admitted directly to the pooled V2 runtime after successful provider-route activation; resident execution remains separate.
- Chat workflow: setup does not report success until V2 ownership is durable; failures are fail-closed and retryable.
- Changelog: cutoff-based default applies only to newly registered Model API users and does not migrate older or resident accounts.

No OpenAPI regeneration is required because no public route, request field, response field, or status slug changes.

- [ ] **Step 6: Run Compose and docs-site checks**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_hosted_runtime_policy.py -q
```

Then from `docs-site` run each command separately:

```bash
npm run types:check
npm run lint
npm run build
```

Expected: all commands PASS.

- [ ] **Step 7: Commit configuration and docs**

```bash
git add deploy/docker-compose.phala.test.yaml deploy/docker-compose.phala.pre.yaml deploy/docker-compose.phala.yaml tests/test_hosted_runtime_policy.py docs/HOSTED_RUNTIME_V2_ADDING_USERS.md deploy/DEPLOYMENTS.md docs-site/content/docs/architecture.mdx docs-site/content/docs/workflows/chat.mdx docs-site/content/docs/changelog.mdx
git commit -m "docs(runtime): roll out new-user V2 admission safely"
```

---

### Task 6: Run the end-to-end verification gate

**Files:**
- Test only; no source changes expected

**Interfaces:**
- Verifies all interfaces and invariants introduced by Tasks 1-5

- [ ] **Step 1: Run the focused ownership suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_dual_runtime_db.py tests/test_new_user_v2_cohort.py tests/test_model_api_chat_send_routing.py tests/test_model_api_path.py tests/test_access_mode_runtime_sync_unit.py tests/test_access_modes.py tests/test_asgi_accounts_remaining.py tests/test_runtime_reconciler.py tests/test_dual_runtime_flip_no_loss.py tests/test_hosted_runtime_policy.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 2: Run static checks for touched backend modules**

```bash
.venv-test/bin/python -m pyflakes backend/hosted/new_user_v2_cohort.py backend/hosted/setup_core.py backend/accounts/accounts_core.py backend/db.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Run the full local backend suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: zero new failures relative to the branch baseline.

- [ ] **Step 4: Verify no implementation drifted from the safe defaults**

Run:

```bash
rg -n 'FEEDLING_RUNTIME_DEFAULT_DESIRED|FEEDLING_V2_NEW_USER_CUTOFF' deploy/docker-compose.phala.test.yaml deploy/docker-compose.phala.pre.yaml deploy/docker-compose.phala.yaml
```

Expected: every `FEEDLING_RUNTIME_DEFAULT_DESIRED` remains literal `resident`; every backend has an empty-default cutoff passthrough; no serve-worker has the cutoff.

- [ ] **Step 5: Record deployment evidence before any production promotion**

On test, choose a cutoff after the code-only deployment has completed, register one new Model API account and one new resident account, and use the existing admin runtime-allowlist view plus encrypted chat E2E to record:

- Model API account: `updated_by=new-user-cohort`, `desired=v2`, actual state `v2`, `converged=true`, exactly one encrypted assistant reply;
- resident account: no automatic V2 row, resident ownership, exactly one resident reply;
- one pre-cutoff account: unchanged resident ownership;
- automatic V2 account pinned to resident: converges resident with no lost or duplicate reply.

Attach the test-environment evidence to the PR targeting `test`. Production promotion must originate from `test` or `pre` under the repository branch-flow rules.
