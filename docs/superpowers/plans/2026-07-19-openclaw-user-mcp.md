# 自托管 OpenClaw 用户 user-MCP 接线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让自托管 OpenClaw（独立 Node runtime）用户在 app 配的 MCP server 同步到 `~/.openclaw/openclaw.json` 并被 agent 调用；正规 HTTPS 与自签 CA 都通。

**Architecture:** OpenClaw 是 hermes 的镜像但独立：物化目标写 `openclaw.json` 的嵌套 `mcp.servers`（JSON merge），CA 天然走 Node 的 `NODE_EXTRA_CA_CERTS`（else catch-all，无需改代码）。只有 2 处实质改动。

**Tech Stack:** Python 3.11、标准库 `json`（无新依赖）、pytest。

**Spec:** `docs/superpowers/specs/2026-07-18-vps-hermes-user-mcp-design.md` §1.1、§4a（全部 docker 实证）。

## Global Constraints

- **不自动 commit（本仓 `CLAUDE.md` 规则）**：每个 Task 的 Commit step 保留，但执行者**暂停等用户显式授权**后才提交。
- **零新增依赖**：用标准库 `json`。
- **同分支**：在 `feat/hermes-user-mcp` 分支继续（同一 feature，hermes 部分已在该分支未提交）。
- **测试落点**：新测试加进 `tests/test_user_mcp_consumer.py`（已在 CI 白名单）。
- **import 约定**：`import chat_resident_consumer as c`、`import user_mcp_materialize as um`、`import json`。
- **OpenClaw MCP 格式（实证）**：`openclaw.json` 顶层 `mcp.servers`（嵌套），每条 `{url, transport:"streamable-http", headers?}`；顶层其他 key（commands/agents/cron/meta）必须原样保留。
- **CA 无需改 `_user_mcp_child_env`**：openclaw cmd 天然落 else 分支拿 `NODE_EXTRA_CA_CERTS`；只加测试守卫。
- **prune 语义**：只删 `managed_names` 里的 server，保留用户手配的——与 hermes/codex/claude 一致。

---

## File Structure

- `tools/user_mcp_materialize.py` — 新增 `openclaw_config_merged`（与 `hermes_config_merged` 并列）
- `tools/chat_resident_consumer.py` — 新增 `_materialize_openclaw_config` helper + `_materialize_user_mcp` 加 OpenClaw 目标
- `tests/test_user_mcp_consumer.py` — openclaw_config_merged 单测 + materialize 单测 + child_env 守卫

---

### Task 1: `openclaw_config_merged` 纯函数

**Files:**
- Modify: `tools/user_mcp_materialize.py`（在 `hermes_config_merged` 之后）
- Test: `tests/test_user_mcp_consumer.py`

**Interfaces:**
- Consumes: 现有 `_enabled(servers) -> list[dict]`、标准库 `json`（文件已 `import json`）
- Produces: `openclaw_config_merged(existing_text: str | None, servers: list[dict], managed_names) -> str` — 返回 merge 后的 openclaw.json 全文（结尾带换行）。

- [ ] **Step 1: 写失败测试**（`_srv` helper 已由 hermes 测试引入，复用它）

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k openclaw_merge -q`
Expected: FAIL — `AttributeError: module 'user_mcp_materialize' has no attribute 'openclaw_config_merged'`

- [ ] **Step 3: 写最小实现**

在 `tools/user_mcp_materialize.py` 的 `hermes_config_merged` 之后新增：

```python
def openclaw_config_merged(existing_text: str | None, servers: list[dict],
                           managed_names) -> str:
    """Merge enabled servers into openclaw.json's nested ``mcp.servers`` map,
    preserving every other top-level key and any server the user added by hand.

    OpenClaw is a separate Node runtime (not hermes): its config is JSON with a
    NESTED ``mcp.servers`` key (verified against openclaw@2026.7.1-2), and each
    HTTP entry needs an explicit ``transport: "streamable-http"``. ``managed_names``
    scopes the prune to server names this feature owns, same contract as the
    hermes/codex/claude targets.
    """
    doc = json.loads(existing_text) if existing_text else {}
    if not isinstance(doc, dict):
        doc = {}
    mcp = doc.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
    servers_map = mcp.get("servers")
    if not isinstance(servers_map, dict):
        servers_map = {}
    for name in list(servers_map):
        if name in managed_names:
            del servers_map[name]
    for s in _enabled(servers):
        entry: dict = {"url": s["url"], "transport": "streamable-http"}
        headers = s.get("headers") or {}
        if headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        servers_map[s["name"]] = entry
    if servers_map:
        mcp["servers"] = servers_map
        doc["mcp"] = mcp
    else:
        mcp.pop("servers", None)
        if mcp:
            doc["mcp"] = mcp
        elif "mcp" in doc:
            del doc["mcp"]
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k openclaw_merge -q`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit（等用户授权）**

```bash
git add tools/user_mcp_materialize.py tests/test_user_mcp_consumer.py
git commit -m "feat(user_mcp): openclaw_config_merged — merge servers into openclaw.json mcp.servers"
```

---

### Task 2: `_materialize_user_mcp` OpenClaw 目标 + helper + child_env 守卫

**Files:**
- Modify: `tools/chat_resident_consumer.py`（新增 `_materialize_openclaw_config`；`_materialize_user_mcp` 加 OpenClaw 目标，紧邻现有 hermes 目标之后）
- Test: `tests/test_user_mcp_consumer.py`

**Interfaces:**
- Consumes: Task 1 的 `um.openclaw_config_merged`；现有 `_atomic_write_text(path: str, text: str)`、`shutil`（hermes task 已确保 import）
- Produces: `_materialize_openclaw_config(cfg_path: Path, servers, managed_names) -> None`

- [ ] **Step 1: 写失败测试**（`_isolate_targets` / `_applied_one` helper 已由 hermes 测试引入，复用；`_isolate_targets` 需补 clear `OPENCLAW_CONFIG_DIR`）

```python
def test_materialize_writes_openclaw_config_when_dir_present(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_CONFIG_DIR", raising=False)
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir()
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(oc_dir))
    c._materialize_user_mcp(
        [{"name": "j", "enabled": True, "url": "https://a/mcp", "headers": {}}], {"j"})
    doc = json.loads((oc_dir / "openclaw.json").read_text())
    assert doc["mcp"]["servers"]["j"] == {"url": "https://a/mcp", "transport": "streamable-http"}


def test_materialize_backs_up_existing_openclaw_config(tmp_path, monkeypatch):
    _isolate_targets(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_CONFIG_DIR", raising=False)
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
    monkeypatch.delenv("HERMES_CONFIG_DIR", raising=False)
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k "openclaw_config or openclaw_uses_node" -q`
Expected: FAIL — `_materialize_user_mcp` 无 OpenClaw 目标（前三个断言 openclaw.json 不生成）。注意 `test_env_injection_openclaw_uses_node_ca_not_ssl_cert` 可能已 PASS（else 分支已 catch-all）——那是预期的守卫，保留。

- [ ] **Step 3: 写最小实现**

在 `tools/chat_resident_consumer.py` 新增 helper（放在 `_materialize_hermes_config` 之后）：

```python
def _materialize_openclaw_config(cfg_path: Path, servers: list[dict],
                                 managed_names) -> None:
    """Write the user's MCP servers into OpenClaw's ``openclaw.json``
    (nested ``mcp.servers``). OpenClaw re-loads it every ``agent --local`` turn.
    JSON has no comments to lose, but we still back up the user's file to
    ``openclaw.json.feedling-bak`` and write atomically (temp + rename)."""
    import user_mcp_materialize as _m  # noqa: PLC0415 — sibling on tools/ path
    existing = cfg_path.read_text() if cfg_path.exists() else None
    merged = _m.openclaw_config_merged(existing, servers, managed_names)
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.parent / (cfg_path.name + ".feedling-bak"))
    _atomic_write_text(str(cfg_path), merged)
```

在 `_materialize_user_mcp` 内，紧接现有 hermes 目标块之后（仍在 `_write_user_mcp_ca(servers)` 之前）插入：

```python
    openclaw_dir = os.environ.get("OPENCLAW_CONFIG_DIR") or str(Path.home() / ".openclaw")
    if Path(openclaw_dir).is_dir():
        try:
            _materialize_openclaw_config(
                Path(openclaw_dir) / "openclaw.json", servers, managed_names)
        except Exception as e:  # noqa: BLE001 — one target must never break others/chat
            log.warning("[user_mcp] openclaw.json write failed: %s: %s",
                        type(e).__name__, e)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k "openclaw_config or openclaw_uses_node" -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 全量回归**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -q`
Expected: PASS（全绿，含 hermes 与现有 pi/claude/codex 断言）

- [ ] **Step 6: Commit（等用户授权）**

```bash
git add tools/chat_resident_consumer.py tests/test_user_mcp_consumer.py
git commit -m "feat(user_mcp): materialize MCP servers into openclaw.json (Node runtime)"
```

---

## docker E2E 验证（实现完成后，控制器执行 — 非 subagent）

`scratchpad/mcp-verify/` 的 openclaw-lab 容器已就绪。步骤：

1. 容器里挂载/复制当前分支的 `tools/user_mcp_materialize.py` + `tools/chat_resident_consumer.py`
2. 用 python 直接调 `_materialize_openclaw_config`（或设 `OPENCLAW_CONFIG_DIR=/root/.openclaw` 调 `_materialize_user_mcp`），传一个 deepwiki server
3. `cat ~/.openclaw/openclaw.json` 确认 `mcp.servers.deepwiki` 带 `transport:"streamable-http"` + 备份生成
4. `openclaw agent --local --json --session-id e2e -m "用 deepwiki 工具查 modelcontextprotocol/python-sdk 的 wiki 结构"` → 确认调用 `deepwiki__read_wiki_structure`
5. **复核生效时机**：物化后不跑 `mcp reload`，直接下一 turn 看新 server 是否可见（判断 `--local` 是否每 turn 重读）

---

## Self-Review

- **Spec coverage**：§4a.1 两处改动 → Task 1（`openclaw_config_merged`）、Task 2（materialize 目标 + helper）；§4a.1 child_env「无需改代码，只加守卫」→ Task 2 的 `test_env_injection_openclaw_uses_node_ca_not_ssl_cert`。§4a.3 生效时机复核 → docker E2E step 5。§8 openclaw 单测全部映射到 Task 1/2。
- **Placeholder scan**：无 TBD；每个代码 step 有完整代码。
- **Type consistency**：`openclaw_config_merged(existing_text, servers, managed_names) -> str` Task 1 定义、Task 2 helper 调用一致；`_atomic_write_text(str, str)`、`shutil.copy2`、`_enabled` 沿用现有。
