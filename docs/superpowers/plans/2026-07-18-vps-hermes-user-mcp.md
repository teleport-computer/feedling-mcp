# 自托管 hermes 用户 user-MCP 接线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让自托管 hermes（含 openclaw 别名）用户在 app 上配的 MCP server 能同步到 VPS 并被 agent 调用，且正规 HTTPS 与自签 CA 都能连通。

**Architecture:** 给 consumer 的物化层加一个 hermes 目标（pyyaml merge 进 `~/.hermes/config.yaml` 的 `mcp_servers`），并把 hermes 并进 `_user_mcp_child_env` 的 codex 分支（同为 python，走 `SSL_CERT_FILE=castore`）。数据下发链路零改动，唯一实质新代码是一个纯函数 `hermes_config_merged`。

**Tech Stack:** Python 3.11、pyyaml 6.0.3（已在 `backend/requirements.lock`）、pytest。

**Spec:** `docs/superpowers/specs/2026-07-18-vps-hermes-user-mcp-design.md`

## Global Constraints

- **不自动 commit（本仓 `CLAUDE.md` 规则）**：每个 Task 的 Commit step 保留 TDD 节奏，但执行者**必须暂停等用户显式授权**后才 `git add/commit`，绝不自作主张。
- **零新增依赖**：只用 pyyaml（已通过 `-r ../backend/requirements.txt` 到 consumer）。不加 ruamel。
- **测试落点**：新测试全部加进 `tests/test_user_mcp_consumer.py`（已在 `ci.yml` 的 pytest 白名单），无需改 `ci.yml`。
- **consumer import 约定**：测试里 `import chat_resident_consumer as c`、`import user_mcp_materialize as um`（沿用 `tests/test_user_mcp_consumer.py` 现有约定；执行者先看该文件头部确认）。
- **数据链路零改动**：不碰 backend、信封、下发、`_maybe_apply_user_mcp` 的 fetch/decrypt。
- **`_user_mcp_cli_value` 不改**：hermes 通过 config.yaml 原生发现工具，模板保持无 `{mcp}`，该函数对 hermes 继续返回空即正确。
- **hermes 工具命名** `mcp_{server}_{tool}`、**HTTP transport 格式** `{name: {url, headers}}`：均由 hermes 原生机制决定（`native-mcp.md`），我们只负责把 server 写进 `mcp_servers`。

---

## File Structure

- `tools/user_mcp_materialize.py` — 新增纯函数 `hermes_config_merged`（与 `codex_config_merged` 并列）
- `tools/chat_resident_consumer.py` — 改 `_user_mcp_child_env`（hermes 并进 codex 分支）、`_materialize_user_mcp`（加 hermes 目标）、新增 helper `_materialize_hermes_config`
- `tests/test_user_mcp_consumer.py` — 三组新测试
- `tools/README.md` + `io-onboarding/skill-resident-agent.md` + `docs/CHANGELOG.md` — 文档

---

### Task 1: `hermes_config_merged` 纯函数

**Files:**
- Modify: `tools/user_mcp_materialize.py`（在 `codex_config_merged` 之后新增）
- Test: `tests/test_user_mcp_consumer.py`

**Interfaces:**
- Consumes: 模块内现有 `_enabled(servers) -> list[dict]`
- Produces: `hermes_config_merged(existing_text: str | None, servers: list[dict], managed_names) -> str` — 返回 merge 后的 config.yaml 全文文本。`servers` 元素形如 `{"name","enabled","url","headers"}`；`managed_names` 是 server 名的集合（当前 + 曾 applied）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_user_mcp_consumer.py` 末尾加（文件头若无 `import yaml` / `import user_mcp_materialize as um` 则补上）：

```python
import yaml
import user_mcp_materialize as um


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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k hermes_merge -q`
Expected: FAIL — `AttributeError: module 'user_mcp_materialize' has no attribute 'hermes_config_merged'`

- [ ] **Step 3: 写最小实现**

在 `tools/user_mcp_materialize.py` 的 `codex_config_merged` 之后新增：

```python
def hermes_config_merged(existing_text: str | None, servers: list[dict],
                         managed_names) -> str:
    """Merge enabled servers into config.yaml's ``mcp_servers`` map, preserving
    every other top-level key and any mcp_servers entry the user added by hand.

    hermes reads ``mcp_servers`` as ONE top-level YAML map key (native-mcp.md),
    so — unlike codex's multi-table TOML — we cannot append a managed text
    block; a second ``mcp_servers:`` would be a duplicate key. We parse, splice
    the map, and re-dump. pyyaml drops comments and reflows formatting; the
    caller backs the file up first (``_materialize_hermes_config``).

    ``managed_names`` scopes the prune to server names this feature owns
    (current + previously-applied), so a server the user configured by hand in
    config.yaml is never deleted — same contract as the codex/claude targets.
    """
    import yaml  # noqa: PLC0415 — sibling dep via backend/requirements
    doc = yaml.safe_load(existing_text) if existing_text else None
    if not isinstance(doc, dict):
        doc = {}
    mcp = doc.get("mcp_servers")
    if not isinstance(mcp, dict):
        mcp = {}
    for name in list(mcp):
        if name in managed_names:
            del mcp[name]
    for s in _enabled(servers):
        entry: dict = {"url": s["url"]}
        headers = s.get("headers") or {}
        if headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        mcp[s["name"]] = entry
    if mcp:
        doc["mcp_servers"] = mcp
    elif "mcp_servers" in doc:
        del doc["mcp_servers"]
    return yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k hermes_merge -q`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit（等用户授权）**

```bash
git add tools/user_mcp_materialize.py tests/test_user_mcp_consumer.py
git commit -m "feat(user_mcp): hermes_config_merged — merge servers into config.yaml mcp_servers"
```

---

### Task 2: `_user_mcp_child_env` 把 hermes 并进 codex 的 SSL_CERT_FILE 分支

**Files:**
- Modify: `tools/chat_resident_consumer.py`（`_user_mcp_child_env` 内）
- Test: `tests/test_user_mcp_consumer.py`

**Interfaces:**
- Consumes: 现有 `_is_codex_cmd(cmd)`、`_is_hermes_chat_cmd(cmd)`、`_is_pi_cmd(cmd)`、常量 `USER_MCP_CASTORE_FILE` / `USER_MCP_CA_FILE` / `USER_MCP_FILE`
- Produces: `_user_mcp_child_env` 对 hermes 命令返回 `{"SSL_CERT_FILE": <castore>}`（castore 存在时），不再返回 `NODE_EXTRA_CA_CERTS`

- [ ] **Step 1: 写失败测试**

在 `tests/test_user_mcp_consumer.py` 加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k "hermes_uses_castore or hermes_no_castore" -q`
Expected: FAIL — hermes 当前走 else 分支，返回 `{"NODE_EXTRA_CA_CERTS": ...}`（第一个断言失败）

- [ ] **Step 3: 写最小实现**

在 `tools/chat_resident_consumer.py` 的 `_user_mcp_child_env` 内，把现有 codex 分支：

```python
    if _is_codex_cmd(cmd):
        if Path(USER_MCP_CASTORE_FILE).exists():
            env["SSL_CERT_FILE"] = USER_MCP_CASTORE_FILE   # REPLACES → concat bundle
    else:
        # claude AND pi — both Node, both ADD via NODE_EXTRA_CA_CERTS.
        if Path(USER_MCP_CA_FILE).exists():
            env["NODE_EXTRA_CA_CERTS"] = USER_MCP_CA_FILE  # ADDS → user CA only
```

改为：

```python
    if _is_codex_cmd(cmd) or _is_hermes_chat_cmd(cmd):
        # codex AND hermes are python. SSL_CERT_FILE REPLACES the trust store,
        # so it points at the concat castore (certifi system CA + user CA), not
        # the user-only bundle. httpx (hermes's mcp SDK client) reads
        # SSL_CERT_FILE, verified locally against a self-signed server.
        if Path(USER_MCP_CASTORE_FILE).exists():
            env["SSL_CERT_FILE"] = USER_MCP_CASTORE_FILE   # REPLACES → concat bundle
    else:
        # claude AND pi — both Node, both ADD via NODE_EXTRA_CA_CERTS.
        if Path(USER_MCP_CA_FILE).exists():
            env["NODE_EXTRA_CA_CERTS"] = USER_MCP_CA_FILE  # ADDS → user CA only
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k "hermes_uses_castore or hermes_no_castore" -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 回归确认 codex/claude/pi 未受影响**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -q`
Expected: PASS（全绿，含现有 pi/claude/codex env 断言）

- [ ] **Step 6: Commit（等用户授权）**

```bash
git add tools/chat_resident_consumer.py tests/test_user_mcp_consumer.py
git commit -m "feat(user_mcp): route hermes CA via SSL_CERT_FILE castore like codex"
```

---

### Task 3: `_materialize_user_mcp` 加 hermes 目标 + 原子写备份 helper

**Files:**
- Modify: `tools/chat_resident_consumer.py`（新增 `_materialize_hermes_config`；在 `_materialize_user_mcp` 内 `_write_user_mcp_ca(servers)` 之前插入 hermes 目标）
- Test: `tests/test_user_mcp_consumer.py`

**Interfaces:**
- Consumes: Task 1 的 `um.hermes_config_merged`；现有 `_atomic_write_text(path: str, text: str)`
- Produces: `_materialize_hermes_config(cfg_path: Path, servers: list[dict], managed_names) -> None` — 备份既有 config.yaml 后原子写 merge 结果；`_materialize_user_mcp` 在 `HERMES_CONFIG_DIR`（默认 `~/.hermes`）目录存在时调用它

- [ ] **Step 1: 写失败测试**

在 `tests/test_user_mcp_consumer.py` 加（`_materialize_user_mcp` 会写多个目标，测试用 monkeypatch 把无关目标指向 tmp，避免污染真实 HOME）：

```python
import os
from pathlib import Path


def _isolate_targets(tmp_path, monkeypatch):
    """Point every materialize target at tmp so the test never touches ~ or /tmp."""
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "user-mcp.json"))
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k "materialize_writes_hermes or materialize_backs_up or materialize_skips_hermes" -q`
Expected: FAIL — 无 hermes 目标，`config.yaml` 不生成（第一个断言 FileNotFoundError / KeyError）

- [ ] **Step 3: 写最小实现**

在 `tools/chat_resident_consumer.py` 顶部若无 `import shutil` 则补上。新增 helper（放在 `_materialize_user_mcp` 之前）：

```python
def _materialize_hermes_config(cfg_path: Path, servers: list[dict],
                               managed_names) -> None:
    """Write the user's MCP servers into hermes's ``config.yaml`` (mcp_servers).

    hermes discovers MCP tools by re-reading config.yaml every spawn
    (native-mcp.md), so this is all that's needed for the next turn to see the
    tools. pyyaml round-trips the file (dropping comments), so we back up the
    user's original to ``config.yaml.feedling-bak`` first, then write atomically
    (temp + rename) so a crash never leaves a half-written config.
    """
    import user_mcp_materialize as _m  # noqa: PLC0415 — sibling on tools/ path
    existing = cfg_path.read_text() if cfg_path.exists() else None
    merged = _m.hermes_config_merged(existing, servers, managed_names)
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.parent / (cfg_path.name + ".feedling-bak"))
    _atomic_write_text(str(cfg_path), merged)
```

在 `_materialize_user_mcp` 内，`_write_user_mcp_ca(servers)` 这一行**之前**插入：

```python
    hermes_dir = os.environ.get("HERMES_CONFIG_DIR") or str(Path.home() / ".hermes")
    if Path(hermes_dir).is_dir():
        try:
            _materialize_hermes_config(
                Path(hermes_dir) / "config.yaml", servers, managed_names)
        except Exception as e:  # noqa: BLE001 — one target must never break others/chat
            log.warning("[user_mcp] hermes config.yaml write failed: %s: %s",
                        type(e).__name__, e)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -k "materialize_writes_hermes or materialize_backs_up or materialize_skips_hermes" -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 全量回归**

Run: `python3 -m pytest tests/test_user_mcp_consumer.py -q`
Expected: PASS（全绿）

- [ ] **Step 6: Commit（等用户授权）**

```bash
git add tools/chat_resident_consumer.py tests/test_user_mcp_consumer.py
git commit -m "feat(user_mcp): materialize MCP servers into hermes config.yaml"
```

---

### Task 4: 文档

**Files:**
- Modify: `tools/README.md`（hermes/openclaw 自托管一节）
- Modify: `docs/CHANGELOG.md`（landmark）
- Modify: `io-onboarding` 本地克隆的 `skill-resident-agent.md`（若在手；不在手则记 TODO 交用户）

**Interfaces:** 无代码接口，纯文档。

- [ ] **Step 1: `tools/README.md` 补 hermes MCP 说明**

在 hermes/OpenClaw 相关段落补一段：

```markdown
### hermes / OpenClaw 用户的 MCP

自托管 hermes 用户在 app 上配置的 MCP server 会自动物化进
`$HERMES_CONFIG_DIR/config.yaml`（默认 `~/.hermes/config.yaml`）的 `mcp_servers`，
hermes 下一回合启动时经 `discover_mcp_tools` 自动发现并注册为 `mcp_<server>_<tool>`。

**前提**：hermes 的 venv 必须装 `mcp` 包（`pip install mcp`），否则 hermes 静默
禁用 MCP，配了也不生效。正规 HTTPS 与自签 CA 均支持（自签走 `SSL_CERT_FILE` 注入
的 concat 信任库）。物化会先把既有 config.yaml 备份成 `config.yaml.feedling-bak`
（pyyaml round-trip 不保留注释）。
```

- [ ] **Step 2: `docs/CHANGELOG.md` 记 landmark**

在最新段落加一条，说明：hermes/openclaw 此前完全没有 MCP 接线（三分支缺口）；本次给物化加 hermes 目标 + CA 复用 codex 的 castore；并记录 `hermes mcp add` 因交互式+discovery 阻塞被否决、改走 pyyaml merge 的决策。

- [ ] **Step 3: io-onboarding skill（若在手）**

若本地有 `io-onboarding` 克隆，在 `skill-resident-agent.md` 的 user-mcp 一节补 hermes 路线一句话（前提 + 自动生效）。不在手则在本 Task 输出里明确记「待用户到 io-onboarding 仓补」。

- [ ] **Step 4: Commit（等用户授权）**

```bash
git add tools/README.md docs/CHANGELOG.md
git commit -m "docs(user_mcp): document hermes/openclaw MCP wiring + prerequisites"
```

---

## 真机 E2E 验证（实现完成后，用户 VPS 执行）

单测覆盖纯函数与 env 注入；真机验证「同步」端到端（本 spec 的核心诉求）：

1. VPS 上确认 hermes venv 装了 `mcp`：`/home/ubuntu/.hermes/hermes-agent/venv/bin/pip show mcp || pip install mcp`
2. VPS 拉取本改动后重启 `feedling-chat-resident.service`（并 `export HERMES_CONFIG_DIR=~/.hermes` 或依赖默认）
3. 在 app 里给这台 VPS 对应账号加一个公共 MCP server（如 deepwiki `https://mcp.deepwiki.com/mcp`）
4. 等一个 poll 周期后 `cat ~/.hermes/config.yaml` 应见 `mcp_servers.deepwiki`，且 `~/.hermes/config.yaml.feedling-bak` 已生成
5. 发一条消息让 agent「用工具查某 GitHub 仓库的 wiki 结构」，确认回复含工具产出
6. **自签**：正规 HTTPS 上述即验证；自签端到端需一个 VPS 云可达的自签 MCP server（本地已用 httpx+`SSL_CERT_FILE` 确定性验证过 CA 路径，见 spec §2）

---

## Self-Review

- **Spec coverage**：§4.1 四处改动 → Task 1（`hermes_config_merged`）、Task 2（child_env CA）、Task 3（materialize 目标 + 备份/原子写/目录守卫/缺失创建）、Task 4（文档前提）。§4.3「config.yaml 缺失时创建最小文件」由 Task 3 实现里 `existing=None → merge → 写` 覆盖（safe_dump 出只含 mcp_servers 的文件）。§6 错误处理 → Task 3 的 try/except + 原子写 + 备份。§8 测试 → Task 1/2/3 单测；`_user_mcp_cli_value` 守卫在 Global Constraints 声明「不改」，现有测试已覆盖 hermes 无 `{mcp}` 返回空（执行者若发现无此断言则在 Task 2 补一条 `assert c._user_mcp_cli_value("hermes chat -q {message}", "chat") == ""`）。
- **Placeholder scan**：无 TBD/TODO；每个代码 step 含完整代码。Task 4 Step 2 CHANGELOG 为叙述性文档，允许自由措辞。
- **Type consistency**：`hermes_config_merged(existing_text, servers, managed_names) -> str` 在 Task 1 定义、Task 3 `_materialize_hermes_config` 调用一致；`_atomic_write_text(str, str)` 沿用现有签名；`_is_hermes_chat_cmd(cmd)` 沿用现有。
