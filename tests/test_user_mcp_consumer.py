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
import yaml  # noqa: F401, E402
import user_mcp_materialize as um  # noqa: F401, E402


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
    # tpl_claude pins its own allowlist → config only, no injected grant.
    assert c._user_mcp_cli_value(tpl_claude, "chat") == \
        f"--mcp-config={tmp_path}/mcp.json"
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


def test_mcp_value_codex_background_skips_legacy_sse_servers(monkeypatch, tmp_path):
    """A legacy-SSE server is comment-skipped in config.toml (codex can't speak
    the transport — codex_config_merged never emits its table), so the codex
    background lane must NOT emit a `-c mcp_servers.<name>.enabled=false` for
    it: that override would deep-merge a partial table for a server that
    otherwise doesn't exist. Only the streamable (http) servers get overrides."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:mixed", "servers": [
            {"name": "modern", "enabled": True, "url": "https://x/mcp",
             "headers": {}, "transport": "http"},
            {"name": "legacy", "enabled": True, "url": "https://mcp.map.qq.com/sse",
             "headers": {}, "transport": "sse"},
            # no explicit transport → /sse heuristic still classifies as sse
            {"name": "legacy2", "enabled": True, "url": "https://y/sse", "headers": {}},
        ]})
    tpl_codex = "codex exec --json {mcp} {message}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex)
    assert c._user_mcp_cli_value(tpl_codex, "background") == \
        "-c mcp_servers.modern.enabled=false"


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
    cmd, _ = c._render_cli_template("hello", "sid-1", lane="chat")
    # =-bound: the flags are variadic and the documented self-hosted template
    # ends in a positional prompt, which a bare `--mcp-config <path>` eats.
    assert f"--mcp-config={tmp_path}/mcp.json" in cmd
    # This template pins no allowlist, so the grant rides along — without it the
    # servers exist but every call lands in permission_denials.
    assert "--allowed-tools=mcp__jira__*" in cmd
    assert not any(t == "--mcp-config" for t in cmd)
    # background lane collapses the placeholder to nothing
    cmd_bg, _ = c._render_cli_template("hello", "sid-1", lane="background")
    assert not any(str(t).startswith("--mcp-config") for t in cmd_bg)
    assert not any(str(t).startswith("--allowed-tools=mcp__") for t in cmd_bg)


def test_render_cli_template_keeps_operator_allowlist(monkeypatch, tmp_path):
    """A template with its own allowlist (hosted's shape) gets no grant added."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    for flag in ("--allowed-tools", "--allowedTools"):
        # Both spellings are real: the repo templates use one, the official CLI
        # docs the other. Missing a spelling means injecting a duplicate flag.
        monkeypatch.setattr(
            c, "AGENT_CLI_CMD", f"claude {flag} 'Bash' {{mcp}} -p {{message}}")
        cmd, _ = c._render_cli_template("hello", "sid-1", lane="chat")
        assert f"--mcp-config={tmp_path}/mcp.json" in cmd, flag
        assert not any(str(t).startswith("--allowed-tools=mcp__") for t in cmd), flag
        assert not any(str(t).startswith("--allowedTools=mcp__") for t in cmd), flag


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


def test_apply_refuses_colliding_default_paths(monkeypatch):
    """Fail-safe for the host-all cross-user leak (PR #97's belt-and-braces).

    When FEEDLING_API_KEY is empty AND the spawner did not pin USER_MCP_* via
    env, the module-scope defaults collide to /tmp/feedling_user_mcp_<sha1("")>
    for every co-hosted user — materializing there would write THIS user's
    decrypted MCP url + auth headers into a file every co-resident agent reads.
    Any future spawn path that forgets the env pinning must degrade to
    user-MCP-off, never to a shared file.
    """
    fetched = {"n": 0}
    monkeypatch.setattr(c, "FEEDLING_API_KEY", "")
    monkeypatch.setattr(c, "_USER_MCP_PATHS_PINNED", False)
    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes",
                        lambda: fetched.__setitem__("n", fetched["n"] + 1) or {})
    monkeypatch.setattr(
        c, "_materialize_user_mcp",
        lambda servers, names: (_ for _ in ()).throw(
            AssertionError("must not materialize onto colliding shared paths")))
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "fp1"})
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    c._maybe_apply_user_mcp()

    # Refusal must not even fetch/decrypt the envelopes, must leave the applied
    # server list empty (so `{mcp}` resolves to no injection), and must record
    # the advertised fingerprint so the refusal doesn't re-log on every poll.
    assert fetched["n"] == 0
    assert c._user_mcp_applied == {"fingerprint": "fp1", "servers": []}

    # Positive control: with the paths pinned by the spawner the same call DOES
    # proceed to fetch + materialize (empty api key alone must not disable the
    # feature — host-all consumers legitimately run keyless on runtime tokens).
    seen = {}
    monkeypatch.setattr(c, "_USER_MCP_PATHS_PINNED", True)
    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", lambda: {
        "fingerprint": "fp2",
        "servers": [{"name": "s", "enabled": True, "config_envelope": {"x": 1}}],
    })
    monkeypatch.setattr(c, "_decrypt_envelope", lambda env: json.dumps(
        {"url": "https://s/mcp", "headers": {}}))
    monkeypatch.setattr(c, "_materialize_user_mcp",
                        lambda servers, names: seen.update(servers=servers))
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "fp2"})

    c._maybe_apply_user_mcp()

    assert seen["servers"][0]["url"] == "https://s/mcp"


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


def test_runtime_repo_files_covers_ca_fetch_module():
    assert "tools/user_mcp_ca_fetch.py" in c._runtime_repo_files()


# ---------------------------------------------------------------------------
# _enrich_with_fetched_ca — budgeted trust-anchor auto-fetch (Task 3)
# ---------------------------------------------------------------------------


def test_enrich_prefers_manual_ca_over_fetch():
    """手动 ca_pem 优先：给了就不该发起任何抓取。"""
    called = []

    def _fetch(url):
        called.append(url)
        return ("FETCHED", None)

    out = c._enrich_with_fetched_ca(
        [{"name": "m", "enabled": True, "url": "https://m/", "ca_pem": "MANUAL"}],
        fetch=_fetch)
    assert out[0]["ca_pem"] == "MANUAL"
    assert called == []          # 没有抓取发生


def test_enrich_fetches_when_no_manual_ca():
    out = c._enrich_with_fetched_ca(
        [{"name": "a", "enabled": True, "url": "https://a/"}],
        fetch=lambda url: ("FETCHED", None))
    assert out[0]["ca_pem"] == "FETCHED"


def test_enrich_one_failure_does_not_affect_others():
    def _fetch(url):
        if "bad" in url:
            raise OSError("boom")
        return ("FETCHED", None)

    out = c._enrich_with_fetched_ca([
        {"name": "bad", "enabled": True, "url": "https://bad/"},
        {"name": "ok", "enabled": True, "url": "https://ok/"},
    ], fetch=_fetch)
    assert out[0]["ca_pem"] == ""        # 失败者拿到空，不抛
    assert out[1]["ca_pem"] == "FETCHED" # 其它不受影响


def test_enrich_fetch_returning_none_yields_empty():
    out = c._enrich_with_fetched_ca(
        [{"name": "a", "enabled": True, "url": "https://a/"}],
        fetch=lambda url: (None, None))
    assert out[0]["ca_pem"] == ""


def test_enrich_skips_disabled_servers_entirely():
    """disabled server 不该触发抓取——它反正到不了 ca_bundle_pem
    （user_mcp_materialize._enabled() 会把它筛掉），花预算抓它纯属浪费。"""
    called = []
    out = c._enrich_with_fetched_ca(
        [{"name": "old", "enabled": False, "url": "https://old/"}],
        fetch=lambda url: (called.append(url) or "FETCHED", None))
    assert called == []
    assert out[0]["ca_pem"] == ""


def test_enrich_disabled_servers_do_not_starve_enabled_ones_budget():
    """回归测试：两台 disabled 的旧 server 排在前面，且各自的抓取都会把预算耗尽
    （如果 disabled 也被抓的话）；启用的那台必须仍然拿到锚，budget 不能被
    disabled server 白白烧光。这条直接守住"迭代域 != 消费域"那个静默失效——
    修复前，disabled server 会消耗预算，导致排在后面的 enabled server 被跳过、
    ca_pem 变成空字符串，最终两个 CA 文件被静默删除。"""
    called = []

    def _fetch(url):
        called.append(url)
        return ("FETCHED", None)

    # now() 序列只给 3 次调用的余地：一次算 deadline，两次判断。如果 disabled
    # 也走判断+抓取分支，budget 会在到达 enabled server 之前被耗尽（或 now()
    # 序列耗尽报错），从而暴露回归。
    ticks = iter([0.0, 1.0, 1.0])
    out = c._enrich_with_fetched_ca(
        [{"name": "disabled-a", "enabled": False, "url": "https://a/"},
         {"name": "disabled-b", "enabled": False, "url": "https://b/"},
         {"name": "enabled-c", "enabled": True, "url": "https://c/"}],
        budget_s=15.0, now=lambda: next(ticks), fetch=_fetch)
    assert called == ["https://c/"]           # 只抓了 enabled 的那台
    assert out[0]["ca_pem"] == ""             # disabled 原样传下去（未抓取）
    assert out[1]["ca_pem"] == ""
    assert out[2]["ca_pem"] == "FETCHED"      # enabled 仍然拿到锚


def test_enrich_stops_fetching_once_budget_spent():
    """预算耗尽后不再抓 —— 注入假 now 驱动，不要真等 15 秒。

    materialize 跑在 poll 循环里、在处理消息之前：抓取阻塞即聊天停摆。最坏情况是
    托管用户配了一串私网 IP，consumer 够不着、每个都卡满超时且一无所获。
    """
    called = []
    # 第一次 now() 算 deadline(0+15=15)，之后每个 server 判一次：
    # 5 < 15 → 抓；99 > 15 → 停；99 > 15 → 停
    ticks = iter([0.0, 5.0, 99.0, 99.0])
    out = c._enrich_with_fetched_ca(
        [{"name": "a", "enabled": True, "url": "https://a/"},
         {"name": "b", "enabled": True, "url": "https://b/"},
         {"name": "d", "enabled": True, "url": "https://d/"}],
        budget_s=15.0, now=lambda: next(ticks),
        fetch=lambda url: (called.append(url) or "FETCHED", None))
    assert called == ["https://a/"]        # 只抓了第一个
    assert out[0]["ca_pem"] == "FETCHED"
    assert out[1]["ca_pem"] == ""          # 超预算 → 空，留待下次 fingerprint 变更
    assert out[2]["ca_pem"] == ""
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


# ---------------------------------------------------------------------------
# hermes_config_merged — merge servers into config.yaml mcp_servers (Task 1)
# ---------------------------------------------------------------------------


def _srv(name, url, enabled=True, headers=None):
    return {"name": name, "enabled": enabled, "url": url, "headers": headers or {}}


def test_hermes_merge_empty_config_adds_enabled_servers():
    out = um.hermes_config_merged(
        None, [_srv("jira", "https://a.example/mcp", headers={"Authorization": "Bearer x"})], {"jira"})
    doc = yaml.safe_load(out)
    assert doc["mcp_servers"]["jira"] == {
        "url": "https://a.example/mcp", "headers": {"Authorization": "Bearer x"}}


def test_hermes_merge_preserves_other_top_level_keys_and_user_servers():
    existing = "model: gpt-4\nmcp_servers:\n  old:\n    url: https://old/mcp\n"
    out = um.hermes_config_merged(existing, [_srv("new", "https://new/mcp")], {"new"})
    doc = yaml.safe_load(out)
    assert doc["model"] == "gpt-4"                 # unrelated key untouched
    assert doc["mcp_servers"]["new"]["url"] == "https://new/mcp"
    assert doc["mcp_servers"]["old"]["url"] == "https://old/mcp"  # user's own, not managed → kept


def test_hermes_merge_prunes_only_managed_names():
    existing = ("mcp_servers:\n  mine:\n    url: https://mine/mcp\n"
                "  yours:\n    url: https://yours/mcp\n")
    out = um.hermes_config_merged(existing, [], {"mine"})   # 'mine' ours & removed; 'yours' user's
    doc = yaml.safe_load(out)
    assert "mine" not in (doc.get("mcp_servers") or {})
    assert doc["mcp_servers"]["yours"]["url"] == "https://yours/mcp"


def test_hermes_merge_skips_disabled_and_omits_empty_headers():
    out = um.hermes_config_merged(
        None, [_srv("off", "https://off/mcp", enabled=False),
               _srv("on", "https://on/mcp")], {"off", "on"})
    doc = yaml.safe_load(out)
    assert "off" not in doc["mcp_servers"]
    assert doc["mcp_servers"]["on"] == {"url": "https://on/mcp"}   # no empty headers key


def test_hermes_merge_idempotent_and_unicode():
    servers = [_srv("n", "https://n/mcp", headers={"X-Note": "你好"})]
    out1 = um.hermes_config_merged(None, servers, {"n"})
    out2 = um.hermes_config_merged(out1, servers, {"n"})
    assert yaml.safe_load(out1) == yaml.safe_load(out2)          # stable
    assert "你好" in out1                                        # allow_unicode, not \uXXXX


# ---------------------------------------------------------------------------
# hermes CA injection (Task 2: route hermes CA via SSL_CERT_FILE)
# ---------------------------------------------------------------------------


def _applied_one():
    return {"fingerprint": "x", "servers": [
        {"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}]}


def test_env_injection_hermes_uses_castore_like_codex(tmp_path, monkeypatch):
    """hermes is python — SSL_CERT_FILE (REPLACE) → concat castore, exactly like
    codex. It must NOT get NODE_EXTRA_CA_CERTS (that is Node-only, inert here)."""
    castore = tmp_path / "castore.pem"
    castore.write_text("PEM-SYSTEM+USER\n")
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(castore))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", _applied_one())
    env = c._user_mcp_child_env(["hermes", "chat", "-Q", "--source", "tool", "-q", "hi"])
    assert env == {"SSL_CERT_FILE": str(castore)}


def test_env_injection_hermes_no_castore_injects_nothing(tmp_path, monkeypatch):
    """No self-signed CA → no castore file → hermes falls back to default
    certifi (works for public HTTPS). Nothing injected."""
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "absent.pem"))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", _applied_one())
    env = c._user_mcp_child_env(["hermes", "chat", "-q", "hi"])
    assert env == {}


# ---------------------------------------------------------------------------
# hermes config.yaml materialization (Task 3: _materialize_hermes_config /
# _materialize_user_mcp's hermes target)
# ---------------------------------------------------------------------------


def _isolate_targets(tmp_path, monkeypatch):
    """Point every materialize target at tmp so the test never touches ~ or /tmp."""
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "user-mcp.json"))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(tmp_path / "no-hermes"))
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(tmp_path / "no-openclaw"))


def test_materialize_writes_hermes_config_when_dir_present(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(hermes_dir))
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    doc = yaml.safe_load((hermes_dir / "config.yaml").read_text())
    assert doc["mcp_servers"]["j"]["url"] == "https://a/mcp"


def test_materialize_backs_up_existing_hermes_config(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "config.yaml").write_text("model: gpt-4\n# a user comment\n")
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(hermes_dir))
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    bak = hermes_dir / "config.yaml.feedling-bak"
    assert bak.exists()
    assert "a user comment" in bak.read_text()       # original preserved in backup
    doc = yaml.safe_load((hermes_dir / "config.yaml").read_text())
    assert doc["model"] == "gpt-4" and "j" in doc["mcp_servers"]


def test_materialize_skips_hermes_when_dir_absent(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(tmp_path / "nonexistent"))
    # must not raise, must not create anything
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    assert not (tmp_path / "nonexistent").exists()


# ---------------------------------------------------------------------------
# openclaw.json materialization + CA guard (Task 2: _materialize_openclaw_config /
# _materialize_user_mcp's openclaw target)
# ---------------------------------------------------------------------------


def test_materialize_writes_openclaw_config_when_dir_present(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir()
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(oc_dir))
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    doc = json.loads((oc_dir / "openclaw.json").read_text())
    assert doc["mcp"]["servers"]["j"] == {"url": "https://a/mcp", "transport": "streamable-http"}


def test_materialize_backs_up_existing_openclaw_config(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir()
    (oc_dir / "openclaw.json").write_text(json.dumps({"commands": {"native": "auto"}}))
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(oc_dir))
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    bak = oc_dir / "openclaw.json.feedling-bak"
    assert bak.exists() and json.loads(bak.read_text())["commands"] == {"native": "auto"}
    doc = json.loads((oc_dir / "openclaw.json").read_text())
    assert doc["commands"] == {"native": "auto"} and "j" in doc["mcp"]["servers"]


def test_materialize_skips_openclaw_when_dir_absent(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(tmp_path / "nonexistent"))
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    assert not (tmp_path / "nonexistent").exists()


def test_env_injection_openclaw_uses_node_ca_not_ssl_cert(tmp_path, monkeypatch):
    """Guard: openclaw is Node → falls through to NODE_EXTRA_CA_CERTS (like claude/pi),
    NOT codex/hermes's SSL_CERT_FILE. No child_env code change needed; this pins it."""
    ca = tmp_path / "ca.pem"
    ca.write_text("PEM-USER\n")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", _applied_one())
    env = c._user_mcp_child_env(["openclaw", "agent", "--local", "--json", "-m", "hi"])
    assert env == {"NODE_EXTRA_CA_CERTS": str(ca)}


# ---------------------------------------------------------------------------
# openclaw_config_merged — merge servers into openclaw.json mcp.servers (Task 1)
# ---------------------------------------------------------------------------


def test_openclaw_merge_empty_adds_enabled_with_transport():
    out = um.openclaw_config_merged(
        None, [_srv("jira", "https://a.example/mcp", headers={"Authorization": "Bearer x"})], {"jira"})
    doc = json.loads(out)
    assert doc["mcp"]["servers"]["jira"] == {
        "url": "https://a.example/mcp", "transport": "streamable-http",
        "headers": {"Authorization": "Bearer x"}}


def test_openclaw_merge_preserves_other_top_level_keys_and_user_servers():
    existing = json.dumps({
        "commands": {"native": "auto"},
        "mcp": {"servers": {"old": {"url": "https://old/mcp", "transport": "streamable-http"}}},
    })
    out = um.openclaw_config_merged(existing, [_srv("new", "https://new/mcp")], {"new"})
    doc = json.loads(out)
    assert doc["commands"] == {"native": "auto"}                 # unrelated key kept
    assert doc["mcp"]["servers"]["new"]["url"] == "https://new/mcp"
    assert doc["mcp"]["servers"]["old"]["url"] == "https://old/mcp"  # user's own, not managed


def test_openclaw_merge_prunes_only_managed_names():
    existing = json.dumps({"mcp": {"servers": {
        "mine": {"url": "https://mine/mcp", "transport": "streamable-http"},
        "yours": {"url": "https://yours/mcp", "transport": "streamable-http"}}}})
    out = um.openclaw_config_merged(existing, [], {"mine"})
    doc = json.loads(out)
    assert "mine" not in doc["mcp"]["servers"]
    assert doc["mcp"]["servers"]["yours"]["url"] == "https://yours/mcp"


def test_openclaw_merge_skips_disabled_and_omits_empty_headers():
    out = um.openclaw_config_merged(
        None, [_srv("off", "https://off/mcp", enabled=False),
               _srv("on", "https://on/mcp")], {"off", "on"})
    doc = json.loads(out)
    assert "off" not in doc["mcp"]["servers"]
    assert doc["mcp"]["servers"]["on"] == {"url": "https://on/mcp", "transport": "streamable-http"}


def test_openclaw_merge_empty_result_drops_mcp_key_but_keeps_others():
    existing = json.dumps({"commands": {"native": "auto"},
                           "mcp": {"servers": {"mine": {"url": "https://mine/mcp"}}}})
    out = um.openclaw_config_merged(existing, [], {"mine"})   # prune all managed → mcp empties
    doc = json.loads(out)
    assert "mcp" not in doc               # mcp dropped when it becomes empty
    assert doc["commands"] == {"native": "auto"}


def test_openclaw_merge_idempotent_and_unicode():
    servers = [_srv("n", "https://n/mcp", headers={"X-Note": "你好"})]
    out1 = um.openclaw_config_merged(None, servers, {"n"})
    out2 = um.openclaw_config_merged(out1, servers, {"n"})
    assert json.loads(out1) == json.loads(out2)
    assert "你好" in out1                  # ensure_ascii=False


def test_user_mcp_file_written_atomically_at_0600(monkeypatch, tmp_path):
    # The claude --mcp-config file holds plaintext MCP url + auth headers. A
    # bare write_text + chmod leaves a 0644 world-readable window and a
    # non-atomic partial-JSON read; it must go through _atomic_write_text
    # (0600 temp + rename) like the CA bundles.
    seen: list = []
    real = c._atomic_write_text

    def spy(path, content, mode=0o600):
        seen.append((path, mode))
        real(path, content, mode)

    monkeypatch.setattr(c, "_atomic_write_text", spy)
    mcp = tmp_path / "mcp.json"
    monkeypatch.setattr(c, "USER_MCP_FILE", str(mcp))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))

    c._materialize_user_mcp(
        [{"name": "s", "enabled": True, "url": "https://s/mcp",
          "headers": {"Authorization": "secret"}, "ca_pem": "", "transport": ""}],
        {"s"})

    assert (str(mcp), 0o600) in seen, "USER_MCP_FILE must use _atomic_write_text"
    assert (mcp.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Self-hosted claude templates with no {mcp} placeholder
# ---------------------------------------------------------------------------


def _applied(monkeypatch, *names):
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x",
         "servers": [{"name": n, "enabled": True,
                      "url": f"https://{n}.example.com", "headers": {}}
                     for n in names]},
    )


def test_selfhosted_claude_without_placeholder_gets_config_and_grant(
    monkeypatch, tmp_path
):
    """tools/README.md's Claude example has no {mcp}; wire it anyway.

    Both flags must be =-bound: they are variadic, so a bare
    `--mcp-config <path>` eats the positional prompt that follows.
    """
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    monkeypatch.setattr(c, "USER_MCP_FILE", str(cfg))
    _applied(monkeypatch, "ombre", "jira")

    cmd = ["claude", "--print", "--output-format", "json", "the prompt"]
    out = c._inject_claude_user_mcp(cmd, "chat")

    assert out[0] == "claude"
    assert f"--mcp-config={cfg}" in out
    assert "--allowed-tools=mcp__jira__*,mcp__ombre__*" in out
    # No bare variadic form anywhere — that is the prompt-swallowing trap.
    assert "--mcp-config" not in out
    assert "--allowed-tools" not in out
    assert out[-1] == "the prompt"


def test_selfhosted_injection_is_a_pure_bypass(monkeypatch, tmp_path):
    """Every non-applicable case returns the argv unchanged, byte for byte."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    monkeypatch.setattr(c, "USER_MCP_FILE", str(cfg))
    base = ["claude", "--print", "the prompt"]

    _applied(monkeypatch, "ombre")
    assert c._inject_claude_user_mcp(base, "background") == base   # not chat
    assert c._inject_claude_user_mcp(["codex", "exec", "x"], "chat") == \
        ["codex", "exec", "x"]                                     # not claude
    already = ["claude", "--mcp-config=/own.json", "p"]
    assert c._inject_claude_user_mcp(already, "chat") == already    # operator's

    monkeypatch.setattr(c, "_user_mcp_applied", {"servers": []})
    assert c._inject_claude_user_mcp(base, "chat") == base          # none enabled

    _applied(monkeypatch, "ombre")
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "missing.json"))
    assert c._inject_claude_user_mcp(base, "chat") == base          # no file


def test_selfhosted_claude_keeps_operator_allowed_tools(monkeypatch, tmp_path):
    """An operator-pinned allowlist is never rewritten — servers still wired."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    monkeypatch.setattr(c, "USER_MCP_FILE", str(cfg))
    _applied(monkeypatch, "ombre")

    cmd = ["claude", "--allowed-tools=Bash", "--print", "p"]
    out = c._inject_claude_user_mcp(cmd, "chat")

    assert f"--mcp-config={cfg}" in out
    assert "--allowed-tools=Bash" in out
    assert not any(t.startswith("--allowed-tools=mcp__") for t in out)


def test_documented_selfhosted_claude_line_is_wired_and_authorized(
    monkeypatch, tmp_path
):
    """The exact AGENT_CLI_CMD tools/README.md tells self-hosted operators to use.

    Regression for the real product-path failure: wiring the servers without a
    grant leaves every call in permission_denials, which the model reports as
    missing permission. A self-hosted operator has no settings.json from us to
    carry the rules, so the grant has to ride on the command.

    The =-bound form is asserted too. The consumer pipes the prompt via stdin
    for claude/codex/pi (`_driver_reads_stdin`), so the variadic flags have no
    positional left to swallow HERE — but the same documented line run by hand
    does exit 1 with "Invalid MCP configuration", and the bound form is correct
    for every template shape rather than only the ones that pipe.
    """
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    monkeypatch.setattr(c, "USER_MCP_FILE", str(cfg))
    _applied(monkeypatch, "ombre")
    monkeypatch.setattr(
        c, "AGENT_CLI_CMD",
        'claude --print --output-format json {mcp} "{message}"')

    cmd, stdin_msg = c._render_cli_template("hello there", "sid-1", lane="chat")

    assert f"--mcp-config={cfg}" in cmd
    assert "--allowed-tools=mcp__ombre__*" in cmd
    # Nothing bare: a bare flag would consume the prompt that follows it.
    assert not any(t in ("--mcp-config", "--allowed-tools") for t in cmd)
    # claude reads the prompt from stdin, so it is not in argv at all.
    assert stdin_msg == "hello there"
    assert "hello there" not in cmd
