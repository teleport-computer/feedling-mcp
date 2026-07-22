"""Coexistence contract: both runtimes fully wired under dual policy, and
v2_only remains exactly the pre-era behavior (the P7 retirement regression net).
Replaces test_hosted_resident_retirement.py for the coexistence window."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v1_implementation_present():
    for rel in (
        "backend/agent_runtime/supervisor.py",
        "backend/agent_runtime/spawners.py",
        "backend/agent_runtime/leases.py",
        "backend/agent_runtime/tokens.py",
    ):
        assert (ROOT / rel).exists(), rel


def test_v2_implementation_present():
    for rel in (
        "backend/model_api_runtime/v2/serve_worker.py",
        "backend/model_api_runtime/v2/worker.py",
        "backend/hosted/runtime_reconciler.py",
    ):
        assert (ROOT / rel).exists(), rel


def test_consumer_keeps_hosted_mode_support():
    # ec377440 的剥离已被 Task 4 反转；双运行时窗口内 consumer 必须双栈
    src = (ROOT / "tools/chat_resident_consumer.py").read_text()
    for needed in ("_HOSTED", "FEEDLING_API_KEY", "X-API-Key"):
        assert needed in src, needed


def test_v1_db_surface_present():
    import db
    for name in ("set_supervisor_heartbeat", "list_agent_runtime_enabled_users"):
        assert hasattr(db, name)


def test_dual_policy_routes_and_v2only_regresses(monkeypatch):
    from hosted import config_store
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    assert config_store.hosted_runtime_policy() == "dual"
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "v2_only")
    assert config_store.hosted_runtime_policy() == "v2_only"
    # v2_only 的 send 行为回归由 test_model_api_chat_send_routing.py 的
    # runtime_policy_not_ready 用例覆盖（Task 6 Step 3 最后一行）


def test_reconciler_is_the_only_allowlist_reader_in_send_path():
    # 设计不变量：send 热路径不读 allowlist 表
    src = (ROOT / "backend/hosted/chat_send_core.py").read_text()
    assert "runtime_allowlist" not in src
