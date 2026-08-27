"""Static contract for explicit production UserStore section dependencies."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))


@dataclass(frozen=True)
class CallSite:
    path: str
    lineno: int
    has_require_keyword: bool = False


# Every implicit get_store call retained after migration must be reviewed here.
# The line number intentionally makes code movement require a fresh review.
SHELL_ONLY_GET_STORE_SITES: dict[tuple[str, int], str] = {
    ("backend/accounts/accounts_core.py", 226): "access-mode mutation uses DB-backed control state",
    ("backend/accounts/accounts_core.py", 263): "onboarding route is a direct blob read",
    ("backend/accounts/auth_core.py", 166): "authentication returns identity, locks, and waiters",
    ("backend/accounts/auth_core.py", 179): "authentication returns identity, locks, and waiters",
    ("backend/admin/admin_core.py", 879): "runtime mode control is DB-backed",
    ("backend/admin/admin_core.py", 899): "runtime mode control is DB-backed",
    ("backend/admin/admin_core.py", 935): "runtime allowlist reconciliation is DB-backed",
    ("backend/agent_runtime/spawners.py", 1110): "web settings are direct blob reads",
    ("backend/agent_runtime/supervisor.py", 242): "notices are durable log writes",
    ("backend/agent_runtime/supervisor.py", 268): "notice resolution is a durable log write",
    ("backend/asgi_app.py", 178): "debug trace is a durable log write",
    ("backend/genesis/worker.py", 1910): "genesis state and tracing use direct DB/blob helpers",
    ("backend/genesis/worker.py", 2036): "genesis reaper uses direct DB/blob helpers",
    ("backend/genesis/worker.py", 2097): "genesis reclaim uses direct DB/blob helpers",
    ("backend/genesis/worker.py", 2141): "resident genesis reaper uses direct DB/blob helpers",
    ("backend/genesis/worker.py", 2210): "unclaimed genesis reaper uses direct DB/blob helpers",
    ("backend/genesis/worker.py", 2272): "genesis failure handling uses direct DB/blob helpers",
    ("backend/hosted/runtime_reconciler.py", 54): "runtime control tuple is DB-backed",
    ("backend/model_api_runtime/v2/jobs_store.py", 4324): "terminal reply is a cold-safe committed write",
    ("backend/model_api_runtime/v2/profile_store.py", 227): "envelope construction needs identity only",
    ("backend/model_api_runtime/v2/serve_worker.py", 839): "provider configuration is a direct blob read",
    ("backend/model_api_runtime/v2/serve_worker.py", 1425): "memory quoted-card reads are DB/readside backed",
    ("backend/model_api_runtime/v2/serve_worker.py", 1785): "summary envelope construction needs identity only",
    ("backend/model_api_runtime/v2/serve_worker.py", 1844): "checkpoint envelope construction needs identity only",
    ("backend/model_api_runtime/v2/serve_worker.py", 1886): "wake gate state uses direct DB/blob helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 1916): "scheduled wake control is DB-backed",
    ("backend/model_api_runtime/v2/serve_worker.py", 2060): "image capability reads exact DB rows",
    ("backend/model_api_runtime/v2/serve_worker.py", 2162): "screen decrypt reads an exact frame row",
    ("backend/model_api_runtime/v2/serve_worker.py", 2486): "file capability reads exact DB/object rows",
    ("backend/model_api_runtime/v2/serve_worker.py", 2694): "image generation output is a cold-safe write",
    ("backend/model_api_runtime/v2/serve_worker.py", 2882): "profile memory reads use DB/readside helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 2978): "turn memory reads use DB/readside helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 3141): "memory actions use durable helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 3204): "memory envelope construction needs identity only",
    ("backend/model_api_runtime/v2/serve_worker.py", 3219): "capture scheduler state is DB/blob backed",
    ("backend/model_api_runtime/v2/serve_worker.py", 3250): "dream scheduler state is DB/blob backed",
    ("backend/model_api_runtime/v2/serve_worker.py", 3346): "screen-watch runtime fence is DB-backed",
    ("backend/model_api_runtime/v2/serve_worker.py", 3609): "reply effect is a cold-safe committed write",
    ("backend/model_api_runtime/v2/serve_worker.py", 3623): "legacy reply effect is a cold-safe committed write",
    ("backend/model_api_runtime/v2/serve_worker.py", 3647): "transactional reply uses cold-safe post-commit reconciliation",
    ("backend/model_api_runtime/v2/serve_worker.py", 3932): "identity capability uses durable helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 4027): "schedule capability uses durable helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 4081): "workspace capability uses exact DB rows",
    ("backend/model_api_runtime/v2/serve_worker.py", 4163): "workspace batch capability uses exact DB rows",
    ("backend/model_api_runtime/v2/serve_worker.py", 4681): "trajectory envelope construction needs identity only",
    ("backend/model_api_runtime/v2/serve_worker.py", 4730): "capture state is a direct blob read",
    ("backend/model_api_runtime/v2/serve_worker.py", 4772): "dream status uses direct DB/blob helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 4802): "capture status uses direct DB/blob helpers",
    ("backend/model_api_runtime/v2/serve_worker.py", 5011): "debug trace is a durable log write",
    ("backend/model_api_runtime/v2/serve_worker.py", 5070): "runtime mode dependency is DB-backed",
    ("backend/model_api_runtime/v2/serve_worker.py", 5079): "web-tools setting is a direct blob read",
    ("backend/model_api_runtime/v2/serve_worker.py", 5154): "scheduler runtime mode is DB-backed",
    ("backend/model_api_runtime/v2/worker.py", 8614): "wake lane reads bounded DB inputs and durable cursors",
    ("backend/model_api_runtime/v2/worker.py", 13397): "chat lane reads bounded DB inputs and durable cursors",
    ("backend/perception/service.py", 89): "perception runtime fence is DB-backed",
    ("backend/perception/service.py", 652): "proactive settings are a direct blob read",
    ("backend/perception/service.py", 720): "activation readiness uses direct DB/blob helpers",
    ("backend/perception/service.py", 858): "V2 perception enqueue uses durable helpers",
    ("backend/perception/service.py", 1018): "legacy perception enqueue is a cold-safe write",
    ("backend/voice/routes_asgi.py", 595): "voice archive/card writes are cold-safe before cleanup refresh",
}


def _python_files():
    yield from sorted(BACKEND_ROOT.rglob("*.py"))


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
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
                        has_require_keyword=any(
                            keyword.arg == "require" for keyword in node.keywords
                        ),
                    )
                )
    return sorted(sites, key=lambda site: (site.path, site.lineno))


def test_no_direct_user_store_construction():
    assert _find_calls(
        "UserStore", exclude={"backend/core/store.py"}
    ) == []


def test_get_store_sites_are_explicit_or_reviewed_shell_only():
    implicit = {
        (site.path, site.lineno)
        for site in _find_calls("get_store")
        if site.path != "backend/core/store.py" and not site.has_require_keyword
    }
    assert implicit == set(SHELL_ONLY_GET_STORE_SITES), (
        "classify each implicit get_store call as an explicit section load or "
        "a reviewed shell-only site"
    )
    assert all(reason.strip() for reason in SHELL_ONLY_GET_STORE_SITES.values())


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
            AssertionError("shell-only path loaded Chat hot snapshot")
        ),
    )
    return core_store


def test_auth_resolution_is_shell_only(monkeypatch):
    core_store = _forbid_chat_snapshot(monkeypatch)
    from accounts import auth_core

    monkeypatch.setattr(auth_core, "resolve_runtime_claims", lambda _headers: None)
    monkeypatch.setattr(auth_core.registry, "_resolve_user", lambda _key: "u-auth")

    result = auth_core.resolve_user({"X-API-Key": "test-key"})

    assert result.user_id == "u-auth"
    assert result.store is core_store._stores["u-auth"]
    assert result.store.loaded_sections() == frozenset()


def test_admin_and_reconciler_runtime_control_are_shell_only(monkeypatch):
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


def test_perception_settings_and_activation_are_shell_only(monkeypatch):
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


def test_profile_sealing_and_debug_trace_are_shell_only(monkeypatch):
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


def test_genesis_early_failure_is_shell_only(monkeypatch):
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
