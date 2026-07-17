"""
User-MCP consumer wiring tests for tools/chat_resident_consumer.py
==================================================================

Covers Task 6 of the user-MCP-servers feature:
  - `{mcp}` placeholder value per lane/driver (`_user_mcp_cli_value`)
  - poll fingerprint sensing (`_update_user_mcp_advertised`)
  - fetch + decrypt + materialize (`_maybe_apply_user_mcp` / `_materialize_user_mcp`)
  - self-update whitelist covers the new pure module

Network + enclave decrypt are mocked — no real backend/enclave needed.

Run with:
    cd backend && PYTHONPATH=. /path/to/venv/python -m pytest \
        ../tests/test_user_mcp_consumer.py -v
"""

import base64  # noqa: F401  (kept for parity with sibling suites / future use)
import json
import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars BEFORE importing consumer.
# consumer reads env at module scope; these must exist first.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    # Shared suite convention. Must match the other consumer test modules'
    # bootstrap value: these are module-scope os.environ.setdefault()s, so the
    # first-imported module wins the key and it leaks cross-module — a divergent
    # value here breaks test_chat_resident_consumer's X-API-Key assertions when
    # this module is collected first.
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_CLI_CMD": "claude --allowed-tools 'x' {mcp} -p {message}",
    "CHECKPOINT_FILE": "/tmp/feedling_test_user_mcp_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Ensure repo root + backend + tools on path (mirrors existing test suite).
# tools/ is needed so the consumer's bare `import user_mcp_materialize` resolves
# when the consumer itself is imported as the `tools.chat_resident_consumer`
# package (tools/ is NOT sys.path[0] in that case, unlike the CLI entrypoint).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

# Stub content_encryption when backend tree is absent.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as c  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# {mcp} placeholder value — per lane, per driver
# ---------------------------------------------------------------------------


def test_mcp_value_claude_and_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(
        c,
        "_user_mcp_applied",
        {"fingerprint": "sha256:x", "servers": [
            {"name": "jira", "enabled": True,
             "url": "https://a.example.com", "headers": {}}]},
    )
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    tpl_claude = "claude --allowed-tools 'x' {mcp} -p {message}"
    tpl_codex = "codex exec --json {mcp} {message}"

    # codex-ness is read from the global AGENT_CLI_CMD (canonical
    # _cli_template_is_codex helper), not from the `template` argument, so
    # each branch below keeps AGENT_CLI_CMD in sync with the template it's
    # exercising — mirroring the one real call site (_render_cli_template),
    # which always passes template=AGENT_CLI_CMD.

    # claude → --mcp-config only on chat lane
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_claude)
    assert c._user_mcp_cli_value(tpl_claude, "chat") == \
        f"--mcp-config {tmp_path}/mcp.json"
    assert c._user_mcp_cli_value(tpl_claude, "background") == ""

    # codex → per-server `enabled=false` override only on non-chat (background)
    # lane. `-c mcp_servers={}` is a no-op (codex deep-merges -c onto config, so
    # an empty parent table leaves each [mcp_servers.<name>] enabled); only an
    # explicit per-server enabled=false actually disables it.
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex)
    assert c._user_mcp_cli_value(tpl_codex, "chat") == ""
    assert c._user_mcp_cli_value(tpl_codex, "background") == \
        "-c mcp_servers.jira.enabled=false"

    # no {mcp} placeholder → empty regardless of lane
    assert c._user_mcp_cli_value("claude -p {message}", "chat") == ""


def test_mcp_value_codex_background_disables_every_enabled_server(monkeypatch, tmp_path):
    """codex background lane must emit one `enabled=false` override per enabled
    server (not one empty map), so every advertised server is hard-off on
    proactive/background turns. Order is stable (sorted by name)."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:multi", "servers": [
            {"name": "jira", "enabled": True, "url": "u", "headers": {}},
            {"name": "confluence", "enabled": True, "url": "u", "headers": {}},
            {"name": "disabled_one", "enabled": False, "url": "u", "headers": {}},
        ]})
    tpl_codex = "codex exec --json {mcp} {message}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex)

    # sorted by name → confluence before jira; the disabled server is skipped.
    assert c._user_mcp_cli_value(tpl_codex, "background") == (
        "-c mcp_servers.confluence.enabled=false "
        "-c mcp_servers.jira.enabled=false"
    )
    # chat lane still relies on config.toml (no per-turn override).
    assert c._user_mcp_cli_value(tpl_codex, "chat") == ""


def test_mcp_value_empty_when_no_enabled_server(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    # no servers at all
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    assert c._user_mcp_cli_value("claude {mcp} -p {message}", "chat") == ""
    # a server that is present but disabled must not gate the placeholder open
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x",
         "servers": [{"name": "jira", "enabled": False, "url": "u", "headers": {}}]})
    assert c._user_mcp_cli_value("codex exec {mcp} {message}", "background") == ""


def test_mcp_value_codex_absolute_path_is_recognized(monkeypatch, tmp_path):
    """codex-ness must be driven by the canonical _cli_template_is_codex()
    helper (Path(cmd[0]).name == "codex"), not a naive
    startswith/substring check on the template string — otherwise a
    systemd unit that pins an absolute path (e.g. /usr/local/bin/codex,
    common when PATH isn't guaranteed under a service) would be
    misdetected as the claude driver."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x", "servers": [
            {"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    tpl_codex_abs = "/usr/local/bin/codex exec {mcp} {message}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex_abs)

    assert c._user_mcp_cli_value(tpl_codex_abs, "background") == \
        "-c mcp_servers.jira.enabled=false"
    assert c._user_mcp_cli_value(tpl_codex_abs, "chat") == ""


# ---------------------------------------------------------------------------
# {mcp} injection through _render_cli_template (chat lane splits into 2 args)
# ---------------------------------------------------------------------------


def test_render_cli_template_injects_mcp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(c, "AGENT_CLI_CMD",
                        "claude {mcp} -p {message}")
    cmd = c._render_cli_template("hello", "sid-1", lane="chat")
    assert "--mcp-config" in cmd
    assert f"{tmp_path}/mcp.json" in cmd
    # background lane collapses the placeholder to nothing
    cmd_bg = c._render_cli_template("hello", "sid-1", lane="background")
    assert "--mcp-config" not in cmd_bg


# ---------------------------------------------------------------------------
# poll fingerprint sensing
# ---------------------------------------------------------------------------


def test_update_user_mcp_advertised(monkeypatch):
    monkeypatch.setattr(c, "_user_mcp_advertised", {})
    c._update_user_mcp_advertised({"fingerprint": "sha256:abc"})
    assert c._user_mcp_advertised == {"fingerprint": "sha256:abc"}
    # non-dict payloads are ignored (no crash, keeps prior state)
    c._update_user_mcp_advertised(None)
    assert c._user_mcp_advertised == {"fingerprint": "sha256:abc"}


# ---------------------------------------------------------------------------
# fetch + decrypt + materialize
# ---------------------------------------------------------------------------


def test_apply_user_mcp_materializes_files(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "claude-home").mkdir()
    (tmp_path / "codex-home").mkdir()
    (tmp_path / "claude-home" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(x:*)"]}}))

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", lambda: {
        "fingerprint": "sha256:new",
        "servers": [{"name": "jira", "enabled": True,
                     "config_envelope": {"id": "e1"}}]})
    monkeypatch.setattr(c, "_decrypt_envelope", lambda env: json.dumps(
        {"url": "https://a.example.com/mcp", "headers": {"X-K": "v"}}).encode())

    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "sha256:new"})
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    c._maybe_apply_user_mcp()

    # generic --mcp-config file
    doc = json.loads((tmp_path / "mcp.json").read_text())
    assert doc["mcpServers"]["jira"]["url"] == "https://a.example.com/mcp"
    # claude settings.json permission rule merged in
    settings = json.loads((tmp_path / "claude-home" / "settings.json").read_text())
    assert "mcp__jira__*" in settings["permissions"]["allow"]
    # pre-existing non-mcp rule preserved
    assert "Bash(x:*)" in settings["permissions"]["allow"]
    # codex config.toml managed block
    codex_config = tmp_path / "codex-home" / "config.toml"
    assert "[mcp_servers.jira]" in codex_config.read_text()
    # applied state advanced
    assert c._user_mcp_applied["fingerprint"] == "sha256:new"
    # both plaintext-secret files are chmod 0600
    assert (os.stat(tmp_path / "mcp.json").st_mode & 0o777) == 0o600
    assert (os.stat(codex_config).st_mode & 0o777) == 0o600


def test_apply_user_mcp_skips_when_fingerprint_unchanged(monkeypatch):
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "sha256:same"})
    monkeypatch.setattr(
        c, "_user_mcp_applied", {"fingerprint": "sha256:same", "servers": []})

    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise AssertionError("should not fetch when fingerprint is unchanged")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _boom)
    c._maybe_apply_user_mcp()
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Task 5: per-runtime CA bundles (claude adds, codex replaces)
# ---------------------------------------------------------------------------


def test_ca_pem_survives_decrypt_to_materialize(monkeypatch):
    """回归守卫：ca_pem 必须从解密后的信封一路传到 materialize。

    _maybe_apply_user_mcp 原本只取 url/headers。漏掉 ca_pem 透传 → ca_bundle_pem
    永远收不到 CA → 两个文件永不生成 → 功能全链路静默失效，**而其它所有测试照样
    全绿**（它们直接喂 servers dict，绕过了这一段）。这条是唯一能抓住它的测试。
    """
    seen = {}
    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", lambda: {
        "fingerprint": "fp1",
        "servers": [{"name": "s", "enabled": True, "config_envelope": {"x": 1}}],
    })
    monkeypatch.setattr(c, "_decrypt_envelope", lambda env: json.dumps(
        {"url": "https://s/mcp", "headers": {}, "ca_pem": "PEM-USER"}))
    monkeypatch.setattr(c, "_materialize_user_mcp",
                        lambda servers, names: seen.update(servers=servers))
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "fp1"})
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    c._maybe_apply_user_mcp()

    assert seen["servers"][0]["ca_pem"] == "PEM-USER"


def test_ca_files_have_different_content(tmp_path, monkeypatch):
    """claude 拿纯用户 CA，codex 拿 certifi+用户 CA。喂反了 codex 会丢公共根。"""
    import certifi
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))

    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/",
                           "ca_pem": "PEM-USER"}])

    system_ca = Path(certifi.where()).read_text()
    # claude 的包：只有用户 CA，绝不含公共根
    assert ca_file.read_text() == "PEM-USER\n"
    # codex 的包：公共根 + 用户 CA 都在
    castore = castore_file.read_text()
    assert "PEM-USER" in castore
    assert castore.startswith(system_ca.rstrip("\n")[:200])
    assert len(castore) > len(system_ca)


def test_no_ca_removes_both_files(tmp_path, monkeypatch):
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    ca_file.write_text("stale")
    castore_file.write_text("stale")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))

    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/"}])

    assert not ca_file.exists()
    assert not castore_file.exists()


def test_ca_files_are_0600(tmp_path, monkeypatch):
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))
    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/",
                           "ca_pem": "PEM-USER"}])
    assert oct(ca_file.stat().st_mode)[-3:] == "600"
    assert oct(castore_file.stat().st_mode)[-3:] == "600"


def test_certifi_failure_fails_open(tmp_path, monkeypatch):
    """读 certifi 失败 → 不写 castore → codex 退回原生信任库。
    宁可这个 MCP server 不通，也不能把用户整个 agent 的 TLS 弄挂（spec §9）。"""
    import certifi
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))
    monkeypatch.setattr(certifi, "where", lambda: "/nonexistent/ca.pem")

    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/",
                           "ca_pem": "PEM-USER"}])

    assert ca_file.exists()          # claude 仍可用
    assert not castore_file.exists() # codex 退回原生，不被半个包毒死


def test_castore_write_failure_fails_open_no_truncated_file(tmp_path, monkeypatch):
    """FINAL FIX WAVE ①: a write failure mid-castore-write (ENOSPC, SIGKILL,
    two consumers racing the same path) must never leave a truncated file on
    disk. codex's SSL_CERT_FILE has REPLACE semantics — a half-written
    castore kills ALL of codex's outbound TLS, not just the user's MCP
    server. ``_write_user_mcp_ca`` must also fail OPEN (not raise), matching
    the existing certifi-read-failure branch immediately below it.

    The failure is injected at the ``_atomic_write_text`` seam (the actual
    disk-write primitive `_write_user_mcp_ca` calls) rather than
    ``Path.write_text``, so this stays a meaningful test of the fixed
    implementation instead of a dead mock the new code path never touches.
    Atomicity of ``_atomic_write_text`` itself (no partial file ever reaches
    the target path) is covered separately by
    ``test_atomic_write_text_leaves_target_untouched_on_failure``.
    """
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    castore_file.write_text("STALE-FROM-A-PREVIOUS-RUN")  # must not survive
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))

    real_atomic_write = c._atomic_write_text

    def _boom(path, content, *a, **kw):
        if str(path) == str(castore_file):
            raise OSError(28, "No space left on device")
        return real_atomic_write(path, content, *a, **kw)

    monkeypatch.setattr(c, "_atomic_write_text", _boom)

    # Must not raise — chat must keep working even if CA plumbing breaks.
    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/",
                           "ca_pem": "PEM-USER"}])

    # The stale/truncated castore must be gone, not left half-written.
    assert not castore_file.exists(), \
        "a truncated/stale castore was left on disk after a write failure"
    # claude's CA file is an independent write path — one runtime's CA
    # write failure shouldn't sacrifice the other's.
    assert ca_file.exists()

    # And the absent file must not get injected into codex's env either.
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})
    env = c._user_mcp_child_env(["codex", "exec", "hi"])
    assert "SSL_CERT_FILE" not in env


def test_ca_file_write_failure_fails_open_no_truncated_file(tmp_path, monkeypatch):
    """Symmetric case for claude's CA file: same atomic-write path, same
    fail-open contract. A truncated PEM makes Node error out at startup."""
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    ca_file.write_text("STALE-FROM-A-PREVIOUS-RUN")  # must not survive
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))

    real_atomic_write = c._atomic_write_text

    def _boom(path, content, *a, **kw):
        if str(path) == str(ca_file):
            raise OSError(28, "No space left on device")
        return real_atomic_write(path, content, *a, **kw)

    monkeypatch.setattr(c, "_atomic_write_text", _boom)

    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/",
                           "ca_pem": "PEM-USER"}])  # must not raise

    assert not ca_file.exists(), \
        "a truncated/stale CA file was left on disk after a write failure"
    # castore is an independent write path — still written normally.
    assert castore_file.exists()

    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})
    env = c._user_mcp_child_env(["claude", "-p", "hi"])
    assert "NODE_EXTRA_CA_CERTS" not in env


def test_atomic_write_text_leaves_target_untouched_on_failure(tmp_path, monkeypatch):
    """Unit-level coverage for the atomic-write helper itself: a failure
    partway through (simulated via a failing fsync, e.g. ENOSPC at flush
    time) must leave the target file exactly as it was before the call —
    never partially overwritten — and must not leave a stray temp file
    behind in the directory."""
    target = tmp_path / "out.pem"
    target.write_text("PREVIOUS-GOOD-CONTENT")

    def _boom_fsync(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(c.os, "fsync", _boom_fsync)

    with pytest.raises(OSError):
        c._atomic_write_text(str(target), "X" * 1000)

    assert target.read_text() == "PREVIOUS-GOOD-CONTENT"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.pem"]
    assert leftovers == [], f"stray temp files left behind: {leftovers}"


def test_castore_parent_dir_created(tmp_path, monkeypatch):
    """Both CA files' parent dirs must be created — previously only
    USER_MCP_CA_FILE's parent got mkdir(parents=True)."""
    ca_file = tmp_path / "claude_home" / "ca.pem"
    castore_file = tmp_path / "codex_home" / "castore.pem"
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))

    c._write_user_mcp_ca([{"name": "s", "enabled": True, "url": "https://s/",
                           "ca_pem": "PEM-USER"}])

    assert ca_file.exists()
    assert castore_file.exists()


@pytest.mark.parametrize("cmd,expect_var,expect_file", [
    (["claude", "-p", "hi"], "NODE_EXTRA_CA_CERTS", "ca"),
    (["codex", "exec", "hi"], "SSL_CERT_FILE", "castore"),
])
def test_env_injection_per_runtime(tmp_path, monkeypatch,
                                   cmd, expect_var, expect_file):
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    ca_file.write_text("PEM-USER\n")
    castore_file.write_text("SYSTEM\nPEM-USER\n")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))
    # _user_mcp_child_env gates on the in-memory applied state, not just file
    # existence (FINAL FIX WAVE ③) — an enabled server must be "applied".
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})

    env = c._user_mcp_child_env(cmd)

    expected_path = str(ca_file if expect_file == "ca" else castore_file)
    assert env == {expect_var: expected_path}
    # 不可指反：codex 绝不能拿到纯用户 CA 的那个文件
    if expect_var == "SSL_CERT_FILE":
        assert env["SSL_CERT_FILE"] != str(ca_file)


def test_env_injection_covers_pi_like_claude(tmp_path, monkeypatch):
    """pi is a Node process, so NODE_EXTRA_CA_CERTS applies to it exactly as it
    does to claude.

    This test previously asserted `== {}` with the rationale "pi: route
    abandoned". That rationale was never true: v2 spec §1 said pi was "本期不涉及"
    (a scheduling decision), which the 07-16 spec upgraded into "路线已放弃" (a
    strategy conclusion nobody ever made). pi in fact carries gemini /
    openrouter / openai_compatible on test and prod. See
    2026-07-17-pi-user-mcp-bridge-design.md §1.1.

    Asserted as a whole dict on purpose: pi's child env must be exactly the CA
    plus its own bridge config path — nothing else. A single-key check would not
    catch a future refactor leaking an extra var into the agent's environment.
    """
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("PEM-USER\n")
    mcp_file = tmp_path / "user-mcp.json"
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.setattr(c, "USER_MCP_FILE", str(mcp_file))
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})
    assert c._user_mcp_child_env(["pi", "--mode", "json"]) == {
        "NODE_EXTRA_CA_CERTS": str(ca_file),
        "FEEDLING_USER_MCP_FILE": str(mcp_file),
    }


def test_env_injection_pi_empty_when_no_servers(tmp_path, monkeypatch):
    """The enabled-server gate applies to pi too — no server, no CA env."""
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("PEM-USER\n")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    assert c._user_mcp_child_env(["pi", "--mode", "json"]) == {}


def test_env_injection_empty_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "nope.pem"))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "nope2.pem"))
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})
    assert c._user_mcp_child_env(["claude", "-p"]) == {}
    assert c._user_mcp_child_env(["codex", "exec"]) == {}


def test_env_injection_empty_when_no_servers_applied(tmp_path, monkeypatch):
    """No enabled server in ``_user_mcp_applied`` → inject nothing, even if
    the file check alone would say otherwise (covered separately)."""
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("PEM-USER\n")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    assert c._user_mcp_child_env(["claude", "-p"]) == {}


def test_env_injection_ignores_stale_files_after_restart(tmp_path, monkeypatch):
    """FINAL FIX WAVE ③: reproduces the stale-file-survives-a-restart bug.

    Scenario: the user deletes every MCP server while the consumer is down.
    On restart, ``_user_mcp_applied`` starts fresh/empty (in-memory state
    doesn't persist across process restarts) and the advertised fingerprint
    is also empty, so ``_maybe_apply_user_mcp`` early-returns without ever
    re-materializing — the OLD CA/castore files from the previous run are
    still sitting on disk. Gating on ``Path.exists()`` alone would keep
    injecting them forever; gating on the in-memory applied state (which
    ``_user_mcp_cli_value`` already does for USER_MCP_FILE) fixes it.
    """
    ca_file = tmp_path / "ca.pem"
    castore_file = tmp_path / "castore.pem"
    ca_file.write_text("PEM-USER\n")               # stale from before restart
    castore_file.write_text("SYSTEM\nPEM-USER\n")  # stale from before restart
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore_file))
    # Fresh process state: nothing applied yet, exactly what a just-restarted
    # consumer looks like before its first poll response arrives.
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    assert c._user_mcp_child_env(["claude", "-p", "hi"]) == {}
    assert c._user_mcp_child_env(["codex", "exec", "hi"]) == {}


def test_apply_user_mcp_failure_does_not_raise(monkeypatch):
    """A fetch/decrypt failure logs and returns — never blocks the chat loop —
    and leaves applied state untouched so the next poll retries."""
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "sha256:z"})
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    def _explode():
        raise RuntimeError("network down")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _explode)
    c._maybe_apply_user_mcp()  # must not raise
    assert c._user_mcp_applied["fingerprint"] is None


def test_apply_user_mcp_deletion_prunes_only_managed_rule(monkeypatch, tmp_path):
    """修复 1 回归：删除一个 server 后，settings.json 里它自己的
    mcp__<name>__* 规则要被清掉，但用户在 .mcp.json 里自行配置的、本功能
    不管理的 mcp__othersrv__* 规则必须原样保留（不能被整体 prune 逻辑误删）。
    """
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / "claude-home").mkdir()
    (tmp_path / "claude-home" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": [
            "Bash(x:*)",
            "mcp__othersrv__*",  # unrelated — not managed by this feature
            "mcp__jira__*",      # stale rule from when jira was applied
        ]}}))

    # jira was previously applied; the new advertised fingerprint carries no
    # servers at all (user deleted the last managed server).
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:old",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": ""})

    def _boom():
        raise AssertionError("empty fingerprint must not fetch envelopes")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _boom)
    c._maybe_apply_user_mcp()

    settings = json.loads((tmp_path / "claude-home" / "settings.json").read_text())
    allow = settings["permissions"]["allow"]
    assert "mcp__jira__*" not in allow          # deleted server's rule pruned
    assert "mcp__othersrv__*" in allow          # unrelated rule untouched
    assert "Bash(x:*)" in allow


def test_apply_user_mcp_clears_to_empty(monkeypatch, tmp_path):
    """Advertised fingerprint empty (all servers removed) materializes an empty
    config without hitting the network."""
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": ""})
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:old",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})

    def _boom():
        raise AssertionError("empty fingerprint must not fetch envelopes")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _boom)
    c._maybe_apply_user_mcp()
    assert c._user_mcp_applied["fingerprint"] == ""
    assert json.loads((tmp_path / "mcp.json").read_text()) == {"mcpServers": {}}


# ---------------------------------------------------------------------------
# self-update whitelist covers the new pure module
# ---------------------------------------------------------------------------


def test_runtime_repo_files_covers_materialize_module():
    assert "tools/user_mcp_materialize.py" in c._runtime_repo_files()


def test_runtime_repo_files_covers_pi_bridge_files():
    files = c._runtime_repo_files()
    assert "tools/pi_mcp_bridge/index.js" in files
    assert "tools/pi_mcp_bridge/mcp_client.js" in files
    assert "tools/pi_mcp_bridge/tool_mapping.js" in files


# ---------------------------------------------------------------------------
# pi: {mcp} → -e <bridge>, and the bridge's config path via env
# ---------------------------------------------------------------------------


def test_mcp_value_pi_injects_extension_on_chat_lane_only(monkeypatch, tmp_path):
    """pi mirrors claude: inject on the chat lane, nothing on background.

    Structural gating — a background turn simply has no extension loaded, so the
    MCP tools do not exist that turn (v2 spec §1: never silently spend the
    user's third-party quota).
    """
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x", "servers": [
            {"name": "jira", "enabled": True,
             "url": "https://a.example.com", "headers": {}}]})
    bridge_file = tmp_path / "index.js"
    bridge_file.write_text("// bridge")
    monkeypatch.setattr(c, "PI_MCP_BRIDGE_FILE", str(bridge_file))
    tpl_pi = "pi --mode json -ne -xt read,edit,write {mcp} --session-id {session_id}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_pi)

    assert c._user_mcp_cli_value(tpl_pi, "chat") == f"-e {bridge_file}"
    assert c._user_mcp_cli_value(tpl_pi, "background") == ""


def test_mcp_value_pi_degrades_to_empty_when_bridge_file_missing(
        monkeypatch, tmp_path):
    """Finding 1: pi exits 1 with empty stdout when `-e <path>` points at a
    missing file — no model call at all — on the chat-only lane. That's the
    same failure shape _strip_missing_mcp_config guards against for claude's
    --mcp-config; the chat turn must survive by degrading to no-MCP instead
    of dying."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x", "servers": [
            {"name": "jira", "enabled": True,
             "url": "https://a.example.com", "headers": {}}]})
    monkeypatch.setattr(c, "PI_MCP_BRIDGE_FILE", str(tmp_path / "missing" / "index.js"))
    tpl_pi = "pi --mode json -ne -xt read,edit,write {mcp} --session-id {session_id}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_pi)

    assert c._user_mcp_cli_value(tpl_pi, "chat") == ""


def test_mcp_value_pi_empty_without_enabled_servers(monkeypatch):
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    tpl_pi = "pi --mode json {mcp} --session-id {session_id}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_pi)
    assert c._user_mcp_cli_value(tpl_pi, "chat") == ""


def test_child_env_gives_pi_the_config_path(tmp_path, monkeypatch):
    """The bridge is one shared static file; the config path is per-user, so it
    must ride an env var rather than be baked into the extension."""
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text("{}")
    monkeypatch.setattr(c, "USER_MCP_FILE", str(mcp_file))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "nope.pem"))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "nope2.pem"))
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})

    env = c._user_mcp_child_env(["pi", "--mode", "json"])
    assert env["FEEDLING_USER_MCP_FILE"] == str(mcp_file)
    # claude/codex have their own config channels and must not get this var
    assert "FEEDLING_USER_MCP_FILE" not in c._user_mcp_child_env(["claude", "-p"])
    assert "FEEDLING_USER_MCP_FILE" not in c._user_mcp_child_env(["codex", "exec"])


def test_child_env_pi_no_config_path_without_servers(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "nope.pem"))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "nope2.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    assert c._user_mcp_child_env(["pi", "--mode", "json"]) == {}
