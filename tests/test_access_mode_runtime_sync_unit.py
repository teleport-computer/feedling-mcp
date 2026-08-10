from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from accounts import access, accounts_core, onboarding, registry  # noqa: E402
from hosted import config_store  # noqa: E402


def _stub_access_switch(monkeypatch, *, previous: str = "resident"):
    saved: list[str] = []
    monkeypatch.setattr(onboarding, "_load_onboarding_route", lambda _store: previous)

    def save(_store, mode: str):
        saved.append(mode)
        return {"route": mode}

    monkeypatch.setattr(onboarding, "_save_onboarding_route", save)
    monkeypatch.setattr(registry, "_find_user_entry_locked", lambda _uid: None)
    monkeypatch.setattr(
        access,
        "_access_modes_payload",
        lambda _store: {"active_route": saved[-1]},
    )
    return SimpleNamespace(user_id="usr_test"), saved


def test_model_api_switch_never_bypasses_allowlist_reconciler(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("access-mode must not directly flip V2")
        ),
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "model_api"}
    )

    assert status == 200
    assert body["active_route"] == "model_api"
    assert saved == ["model_api"]


def test_model_api_switch_does_not_require_an_active_route(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected switch")),
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "model_api"}
    )

    assert status == 200
    assert body["active_route"] == "model_api"
    assert saved == ["model_api"]


def test_resident_switch_moves_runtime_back_to_resident(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch, previous="model_api")
    selected: list[str] = []
    monkeypatch.setattr(
        config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (
            config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
            "v2",
            9,
        ),
    )
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda _store, mode: selected.append(mode) or mode,
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "resident"}
    )

    assert status == 200
    assert body["active_route"] == "resident"
    assert saved == ["resident"]
    assert selected == [config_store.HOSTED_RUNTIME_MODE_RESIDENT]


def _stub_route_compensation(monkeypatch, *, previous_doc):
    """Observe the seam the compensation path actually uses.

    ⚠️ 别断言 `_save_onboarding_route` 的调用序列。补偿路径**不经过它**:
    `accounts_core._select_access_mode` 失败后直接调
    `db.set_onboarding_route_strict(user_id, previous_route_doc)`
    (或 doc 为 None 时调 `db.delete_onboarding_route_strict`)。

    这条断言原本写成 `saved == ["resident", "model_api"]`,于是在
    「生产代码完全正确」的情况下长期报红 —— 它盯的是一个补偿根本不会碰的接缝。
    而该文件在 `.github/pytest-uncovered-baseline.txt` 里,CI 从不执行,
    所以这条红没有任何人看见。
    """
    restored: list[object] = []
    monkeypatch.setattr(
        db, "get_blob_strict", lambda _uid, _kind: previous_doc
    )
    monkeypatch.setattr(
        db,
        "set_onboarding_route_strict",
        lambda _uid, doc: restored.append(doc),
    )
    monkeypatch.setattr(
        db,
        "delete_onboarding_route_strict",
        lambda _uid: restored.append(_DELETED),
    )
    return restored


_DELETED = object()


def _stub_failing_runtime_transition(monkeypatch):
    """Let the resident transition fail on its first runtime write."""
    selected: list[str] = []
    monkeypatch.setattr(
        config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (
            config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
            "v2",
            9,
        ),
    )

    def fail_after_partial_write(_store, mode):
        selected.append(mode)
        if len(selected) == 1:
            raise RuntimeError("state write failed after blob write")
        return mode

    monkeypatch.setattr(
        config_store, "set_hosted_runtime_mode", fail_after_partial_write
    )
    return selected


def test_partial_runtime_failure_rolls_back_runtime_and_access_mode(monkeypatch):
    store, _saved = _stub_access_switch(monkeypatch, previous="model_api")
    selected = _stub_failing_runtime_transition(monkeypatch)
    previous_doc = {"route": "model_api"}
    restored = _stub_route_compensation(monkeypatch, previous_doc=previous_doc)

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "resident"}
    )

    assert status == 503
    assert body == {"error": "runtime_control_unavailable"}
    assert selected == [
        config_store.HOSTED_RUNTIME_MODE_RESIDENT,
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    ]
    assert restored == [previous_doc], (
        "补偿必须把 onboarding_route 还原成先前那份 doc —— "
        "否则用户会卡在一个没有真正生效的 resident 路由上"
    )


def test_partial_runtime_failure_deletes_the_route_when_there_was_none(monkeypatch):
    """先前没有路由时,补偿要删掉而不是写回一个 None。

    这条分支(`previous_route_doc is None` → `delete_onboarding_route_strict`)
    此前完全没有测试覆盖。
    """
    store, _saved = _stub_access_switch(monkeypatch, previous="model_api")
    _stub_failing_runtime_transition(monkeypatch)
    restored = _stub_route_compensation(monkeypatch, previous_doc=None)

    _body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "resident"}
    )

    assert status == 503
    assert restored == [_DELETED]
