"""Static contract for explicit production UserStore section dependencies."""

from __future__ import annotations

import ast
import difflib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from tools.store_per_load_mode_inventory import (  # noqa: E402
    derive_reviewed_sites,
    render_inventory,
)


PER_LOAD_MODE_SNAPSHOT = REPO_ROOT / "tests/fixtures/store_per_load_mode_sites.json"


@dataclass(frozen=True)
class CallSite:
    path: str
    lineno: int
    has_effective_require: bool = False
    review_reason: str = ""


# Reviewed store calls carry their non-empty review reason at the call site.
# The scanner below derives the complete inventory directly from production AST.


def _python_files():
    yield from sorted(BACKEND_ROOT.rglob("*.py"))


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_statically_falsy(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return not bool(node.value)
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return (
        isinstance(node, ast.Call)
        and _call_name(node) in {"dict", "frozenset", "list", "set", "tuple"}
        and not node.args
        and not node.keywords
    )


def _has_effective_require(call: ast.Call) -> bool:
    return any(
        keyword.arg == "require" and not _is_statically_falsy(keyword.value)
        for keyword in call.keywords
    )


def _review_reason(call: ast.Call) -> str:
    for keyword in call.keywords:
        if (
            keyword.arg == "reason"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value.strip()
    return ""


def _find_calls(name: str, *, exclude: set[str] | None = None) -> list[CallSite]:
    excluded = exclude or set()
    sites = []
    for file_path in _python_files():
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in excluded:
            continue
        tree = ast.parse(file_path.read_text(), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == name:
                sites.append(
                    CallSite(
                        path=relative,
                        lineno=node.lineno,
                        has_effective_require=_has_effective_require(node),
                        review_reason=_review_reason(node),
                    )
                )
    return sorted(sites, key=lambda site: (site.path, site.lineno))


def test_no_direct_user_store_construction():
    assert _find_calls("UserStore", exclude={"backend/core/store.py"}) == []


def test_get_store_sites_are_explicit_or_reviewed():
    implicit = {
        (site.path, site.lineno)
        for site in _find_calls("get_store")
        if site.path != "backend/core/store.py" and not site.has_effective_require
    }
    assert implicit == set(), (
        "replace each implicit get_store call with a real section requirement "
        "or get_store_per_load_mode(..., reason=...)"
    )
    per_load_mode = _find_calls("get_store_per_load_mode", exclude={"backend/core/store.py"})
    assert per_load_mode, "reviewed scanner must find production declarations"
    assert {
        "backend/accounts/accounts_core.py",
        "backend/model_api_runtime/v2/serve_worker.py",
        "backend/voice/routes_asgi.py",
    } <= {site.path for site in per_load_mode}
    assert all(site.review_reason for site in per_load_mode)


def test_reviewed_inventory_matches_snapshot():
    expected = PER_LOAD_MODE_SNAPSHOT.read_text()
    actual = render_inventory(REPO_ROOT)
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="reviewed snapshot",
            tofile="derived inventory",
        )
    )

    assert actual == expected, (
        "reviewed Store call inventory changed; review the semantic diff, then "
        "run `python tools/store_per_load_mode_inventory.py --write` to accept it:\n"
        f"{diff}"
    )


def test_reviewed_snapshot_matches_independent_scanner():
    snapshot = json.loads(PER_LOAD_MODE_SNAPSHOT.read_text())
    snapshot_counts = Counter(
        {
            (entry["path"], entry["reason"]): entry["count"]
            for entry in snapshot["sites"]
        }
    )
    independently_scanned = Counter(
        (site.path, site.review_reason)
        for site in _find_calls(
            "get_store_per_load_mode", exclude={"backend/core/store.py"}
        )
    )

    assert snapshot_counts == independently_scanned


def test_reviewed_inventory_identity_ignores_file_local_refactors(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    source = backend / "worker.py"
    source.write_text(
        "def run(user_id):\n"
        "    return get_store_per_load_mode(user_id, reason='direct DB helper')\n"
    )
    baseline = derive_reviewed_sites(tmp_path)

    source.write_text(
        "# unrelated heading\n"
        "# another unrelated line\n"
        "def renamed_run(user_id):\n"
        "    return get_store_per_load_mode(\n"
        "        user_id, reason='direct DB helper'\n"
        "    )\n"
    )

    assert derive_reviewed_sites(tmp_path) == baseline


def test_reviewed_inventory_identity_detects_a_new_site(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    source = backend / "worker.py"
    source.write_text(
        "def first(user_id):\n"
        "    return get_store_per_load_mode(user_id, reason='first helper')\n"
    )
    baseline = derive_reviewed_sites(tmp_path)

    source.write_text(
        source.read_text()
        + "\ndef second(user_id):\n"
        + "    return get_store_per_load_mode(user_id, reason='first helper')\n"
    )
    updated = derive_reviewed_sites(tmp_path)

    assert len(updated) == len(baseline) == 1
    assert baseline[0].count == 1
    assert updated[0].count == 2


def test_empty_require_does_not_leave_the_implicit_audit_surface():
    for expression in ("()", "[]", "None", "False", "set()"):
        call = ast.parse(f"get_store('user', require={expression})").body[0].value
        assert isinstance(call, ast.Call)
        assert not _has_effective_require(call), expression


def test_reviewed_store_requires_a_nonempty_review_reason():
    from core import store as core_store

    for reason in ("", "   ", None):
        try:
            core_store.get_store_per_load_mode("audit-user", reason=reason)
        except ValueError as exc:
            assert str(exc) == "shell-only store reason required"
        else:
            raise AssertionError(f"accepted empty reviewed reason: {reason!r}")


def test_no_production_legacy_compatibility_calls():
    assert _find_calls(
        "get_store_legacy", exclude={"backend/core/store.py"}
    ) == []


def test_production_never_requests_all_store_sections():
    offenders = []
    for file_path in _python_files():
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative == "backend/core/store.py":
            continue
        tree = ast.parse(file_path.read_text(), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "ALL_STORE_SECTIONS":
                offenders.append((relative, node.lineno))
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "ALL_STORE_SECTIONS"
            ):
                offenders.append((relative, node.lineno))
    assert offenders == []


def _forbid_chat_snapshot(monkeypatch):
    from core import store as core_store

    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    monkeypatch.setattr(core_store, "_stores", {})
    monkeypatch.setattr(
        core_store.db,
        "chat_load_hot_snapshot_strict",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reviewed path loaded Chat hot snapshot")
        ),
    )
    return core_store


def test_auth_resolution_loads_no_sections(monkeypatch):
    core_store = _forbid_chat_snapshot(monkeypatch)
    from accounts import auth_core

    monkeypatch.setattr(auth_core, "resolve_runtime_claims", lambda _headers: None)
    monkeypatch.setattr(auth_core.registry, "_resolve_user", lambda _key: "u-auth")

    result = auth_core.resolve_user({"X-API-Key": "test-key"})

    assert result.user_id == "u-auth"
    assert result.store is core_store._stores["u-auth"]
    assert result.store.loaded_sections() == frozenset()


def test_admin_and_reconciler_runtime_control_load_no_sections(monkeypatch):
    _forbid_chat_snapshot(monkeypatch)
    from admin import admin_core
    from hosted import config_store, runtime_reconciler

    monkeypatch.setattr(
        config_store,
        "get_hosted_runtime_mode_strict",
        lambda _store: config_store.HOSTED_RUNTIME_MODE_RESIDENT,
    )
    body, status = admin_core.get_runtime_mode("u-admin")
    assert status == 200
    assert body["hosted_runtime_mode"] == config_store.HOSTED_RUNTIME_MODE_RESIDENT

    monkeypatch.setattr(
        config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (
            config_store.HOSTED_RUNTIME_MODE_RESIDENT,
            "resident",
            1,
        ),
    )
    assert runtime_reconciler._current_actual("u-reconcile") == "resident"


def test_perception_settings_and_activation_load_no_sections(monkeypatch):
    core_store = _forbid_chat_snapshot(monkeypatch)
    from perception import service as perception_service

    monkeypatch.setattr(
        core_store.UserStore,
        "load_proactive_settings",
        lambda _store: {"enabled": True},
    )
    monkeypatch.setattr(
        core_store.UserStore,
        "proactive_activation_ready",
        lambda _store: True,
    )

    assert perception_service._app_proactive_settings("u-perception") == {
        "enabled": True
    }
    assert perception_service._proactive_activation_ready("u-perception") is True


def test_profile_sealing_and_debug_trace_load_no_sections(monkeypatch):
    _forbid_chat_snapshot(monkeypatch)
    from model_api_runtime.v2 import profile_store, serve_worker

    monkeypatch.setattr(
        profile_store.core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, raw: ({"owner_user_id": store.user_id, "body": raw}, None),
    )
    assert profile_store._seal_text("u-profile", "hello")["owner_user_id"] == (
        "u-profile"
    )

    seen = []
    monkeypatch.setattr(
        serve_worker,
        "_emit_v2_debug_trace",
        lambda store, event_type, **_kwargs: seen.append(
            (store.user_id, event_type)
        ),
    )
    serve_worker._emit_v2_debug_trace_for_user("u-trace", "turn.started")
    assert seen == [("u-trace", "turn.started")]


def test_v2_bounded_tail_does_not_load_chat_snapshot(monkeypatch):
    _forbid_chat_snapshot(monkeypatch)
    from model_api_runtime.v2 import serve_worker

    monkeypatch.setattr(
        serve_worker,
        "_read_tail_window",
        lambda user_id, after_ts, limit, *, oldest_first: [
            {"id": "m1", "seq": 1, "content": "one"},
            {"id": "m2", "seq": 2, "content": "two"},
        ],
    )

    assert [row["id"] for row in serve_worker._read_tail("u-tail", 0, 2)] == [
        "m1",
        "m2",
    ]


def test_genesis_early_failure_loads_no_sections(monkeypatch):
    _forbid_chat_snapshot(monkeypatch)
    from genesis import worker

    monkeypatch.setattr(worker, "_trace_genesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker.service,
        "write_genesis_state",
        lambda *_args, **_kwargs: None,
    )
    try:
        worker._process_job(
            {"user_id": "u-genesis", "job_id": "g1", "total_chunks": 0},
            api_url="",
            enclave_url="",
            mint_runtime_token=lambda _user_id: "token",
        )
    except worker.GenesisWorkerError as exc:
        assert str(exc) == "empty_import"
    else:
        raise AssertionError("expected empty genesis import to fail")


def test_v2_chat_cache_scan_explicitly_loads_one_snapshot(monkeypatch):
    core_store = _forbid_chat_snapshot(monkeypatch)
    from model_api_runtime.v2 import serve_worker

    calls = []
    monkeypatch.setattr(
        core_store.db,
        "chat_load_hot_snapshot_strict",
        lambda user_id, limit: (
            calls.append((user_id, limit))
            or (
                3,
                [
                    {"id": "a", "seq": 1, "role": "assistant", "ts": 1.0},
                    {"id": "u", "seq": 2, "role": "user", "ts": 2.0},
                    {"id": "h", "seq": 3, "role": "human", "ts": 3.0},
                ],
            )
        ),
    )

    assert serve_worker._last_user_msg_ts("u-chat-scan") == 3.0
    assert len(calls) == 1
