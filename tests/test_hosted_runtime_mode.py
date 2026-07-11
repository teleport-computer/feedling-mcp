"""hosted_runtime_mode 灰度开关：默认 resident_cli，可切 db_action_v2，非法值拒绝。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import store as core_store
from hosted import config_store as hosted_config_store

from conftest import configure_model_api_route


def _seed_model_api_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    # 需要一个 model_api 配置，_patch_model_api_runtime_profile 才能建 runtime profile。
    # provider config 现在落在 model_api_routes/credentials（model-api-multi-profile）。
    configure_model_api_route(uid, provider="anthropic", model="m")


def test_default_mode_is_db_action_v2():
    # Full cutover 2026-07-11: db_action_v2 is the global default. A user who has
    # never had hosted_runtime_mode set runs on the V2 pool.
    _seed_model_api_user("u_mode_1")
    store = core_store.get_store("u_mode_1")
    assert hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"


def test_set_and_get_db_action_v2():
    _seed_model_api_user("u_mode_2")
    store = core_store.get_store("u_mode_2")
    out = hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert out == "db_action_v2"
    assert hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"


def test_set_rejects_unknown_mode():
    _seed_model_api_user("u_mode_3")
    store = core_store.get_store("u_mode_3")
    with pytest.raises(ValueError):
        hosted_config_store.set_hosted_runtime_mode(store, "bogus")


def _seed_bare_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_set_without_model_api_config_raises_and_stays_default():
    # 用户没有 model_api config → set 无法落地，必须抛错（不能返回假成功），
    # 且 get 仍回退默认（全量翻转后为 db_action_v2，什么都没写进去）。
    _seed_bare_user("u_mode_4")
    store = core_store.get_store("u_mode_4")
    with pytest.raises(ValueError):
        hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"


# ------------------------------------------------------------------
# set_last_runtime_error (Task 3: v2 worker terminal-failure error surface).
# Public direct lever wrapping _patch_model_api_runtime_profile — the v2 worker
# has no `store` binding in its early-failure path, only user_id, so
# serve_worker's injected callback re-fetches the store itself and calls this.
# ------------------------------------------------------------------

def test_set_last_runtime_error_writes_profile_field():
    _seed_model_api_user("u_mode_5")
    store = core_store.get_store("u_mode_5")
    hosted_config_store.set_last_runtime_error(store, "boom")
    profile = hosted_config_store._load_model_api_runtime_profile(store)
    assert profile.get("last_runtime_error") == "boom"


def test_set_last_runtime_error_truncates_at_300_chars():
    _seed_model_api_user("u_mode_6")
    store = core_store.get_store("u_mode_6")
    long_message = "x" * 500
    hosted_config_store.set_last_runtime_error(store, long_message)
    profile = hosted_config_store._load_model_api_runtime_profile(store)
    assert profile.get("last_runtime_error") == "x" * 300
    assert len(profile.get("last_runtime_error")) == 300
