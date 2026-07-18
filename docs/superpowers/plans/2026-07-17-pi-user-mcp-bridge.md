# pi 用户 MCP 桥 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 pi driver 托管用户（gemini / openrouter / openai_compatible）能用上自己配置的 MCP server 工具，消除 v2 spec §11 遗留的能力缺口。

**Architecture:** 写一个零依赖 pi extension，在 async factory 里读取**已经物化好的** `user-mcp.json`、连 MCP server、把每个工具 `registerTool` 成 pi 原生工具。数据链路（存储/下发/信封/物化）一行不改。pi 模板的工具白名单从 `-t bash` 换成 `-ne -xt read,edit,write`，`{mcp}` 占位符只负责在 chat 回合注入 `-e <bridge>`——与 claude 的 `--mcp-config` 完全同构，lane gating 白送。

**Tech Stack:** Node 22（runner 镜像已装）、pi 0.80.3（Dockerfile 精确 pin）、纯 JS 无 npm 依赖、pytest + node harness。

**Spec:** `docs/superpowers/specs/2026-07-17-pi-user-mcp-bridge-design.md`

## Global Constraints

以下为 spec 的项目级约束，**每个 task 的要求都隐含包含本节**：

- **pi 版本 0.80.3**：spec §6 的七条源码断言全部绑定此版本（`Dockerfile.agent-runner:42` 精确 pin）。升级 pi 必须重验 §6 断言表。
- **零 npm 依赖**：桥不得引入任何 node_modules。理由同 `mcp_probe.py` docstring：「one endpoint doesn't justify the dependency + requirements.lock churn」。
- **语言 `.js` 非 `.ts`**：本仓无 TS 工具链（无 tsconfig / tsc / 类型检查 CI）。`loader.js:416` 两者皆收。
- **MCP 协议版本 `"2025-03-26"`**：与 `backend/hosted/mcp_probe.py` 的 `_PROTOCOL_VERSION` **必须保持一致**，升级时一起动。
- **工具数上限 50**：跨所有 server 合计，超出丢弃且**必须 log 丢弃内容**。
- **连接超时 10s / server**：并发连接，失败跳过。
- **工具命名 `mcp_<server>_<tool>`**：gemini 约束 `^[a-zA-Z0-9_-]{1,64}$`，sanitize + 截断 + **确定性**去重（相同输入恒得相同输出，不依赖网络完成顺序）。
- **async factory 绝不向上抛异常**：pi await 该 factory 且阻塞启动，抛出 = 整个聊天回合挂掉。用户的 MCP server 挂了绝不能让用户失去 agent。
- **lane gating**：chat 回合注入 `-e`，background/proactive 回合不注入（结构性保证）。
- **stdout 保持干净**：pi `--mode json` 的 stdout 是 JSONL 事件流，桥的所有日志走 `console.error`（stderr）。

---

### Task 1: 订正 `route abandoned` 并让 CA 对 pi 生效

**Why first:** 这是桥的前置——CA 注入对 pi 必须先正确，否则桥通了自签名 MCP 仍然连不上。且它独立于桥，可单独 review。

**Files:**
- Modify: `tools/chat_resident_consumer.py`（删除 `_user_mcp_ca_env` 里的 pi early-return；本分支上约 :5702，**用 `grep -n "route abandoned"` 定位，勿信行号**）
- Test: `tests/test_user_mcp_consumer.py`（改 `test_env_injection_skips_pi`，约 :499）
- Modify: `docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md:5,26`
- Modify: `deploy/Dockerfile.agent-runner`（pi driver 注释去掉 deepseek，约 :38）

> **本分支范围调整（controller 记于 2026-07-17）**：spec §8 订正表中的
> `2026-07-16-user-mcp-network-relaxation-design.md` 与
> `2026-07-16-user-mcp-network-relaxation.md` **不在本分支**——它们是 `user-mcp-auto-ca-fetch`
> 那条线尚未提交的产物，只存在于主工作树。本 task **不订正这两个文件**，改由文末
> 「遗留缺口」承接。不要尝试创建它们。

**Interfaces:**
- Consumes: 无（首个 task）
- Produces: `_user_mcp_ca_env(cmd)` 对 `["pi", ...]` 返回 `{"NODE_EXTRA_CA_CERTS": USER_MCP_CA_FILE}`（当有 enabled server 且 CA 文件存在时）

- [ ] **Step 1: 改测试，让它断言正确行为**

`tests/test_user_mcp_consumer.py` 中把 `test_env_injection_skips_pi` 整体替换为：

```python
def test_env_injection_covers_pi_like_claude(tmp_path, monkeypatch):
    """pi is a Node process, so NODE_EXTRA_CA_CERTS applies to it exactly as it
    does to claude.

    This test previously asserted `== {}` with the rationale "pi: route
    abandoned". That rationale was never true: v2 spec §1 said pi was "本期不涉及"
    (a scheduling decision), which the 07-16 spec upgraded into "路线已放弃" (a
    strategy conclusion nobody ever made). pi in fact carries gemini /
    openrouter / openai_compatible on test and prod. See
    2026-07-17-pi-user-mcp-bridge-design.md §1.1.
    """
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("PEM-USER\n")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "x", "servers": [{"name": "s", "enabled": True}]})
    assert c._user_mcp_ca_env(["pi", "--mode", "json"]) == {
        "NODE_EXTRA_CA_CERTS": str(ca_file)}


def test_env_injection_pi_empty_when_no_servers(tmp_path, monkeypatch):
    """The enabled-server gate applies to pi too — no server, no CA env."""
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("PEM-USER\n")
    monkeypatch.setattr(c, "USER_MCP_CA_FILE", str(ca_file))
    monkeypatch.setattr(c, "USER_MCP_CASTORE_FILE", str(tmp_path / "castore.pem"))
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    assert c._user_mcp_ca_env(["pi", "--mode", "json"]) == {}
```

- [ ] **Step 2: 跑测试确认它红**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py -k pi -v`
Expected: FAIL — `test_env_injection_covers_pi_like_claude` 断言 `{} == {"NODE_EXTRA_CA_CERTS": ...}` 失败

- [ ] **Step 3: 删掉 `_user_mcp_ca_env` 的 pi early-return**

`tools/chat_resident_consumer.py`，删除这三行（在 docstring 之后、`enabled_servers` 之前）：

```python
    if _is_pi_cmd(cmd):
        return {}          # pi: route abandoned (v2 spec §1), no CA surface
```

删除后，pi 会自然落到函数末尾的 `NODE_EXTRA_CA_CERTS` 分支——该分支对 pi 天然正确，因为 pi 与 claude 同为 Node 进程。同时在函数 docstring 末尾追加一段：

```python
    pi is intentionally NOT special-cased: like claude it is a Node process, so
    it falls through to NODE_EXTRA_CA_CERTS. (A prior version early-returned {}
    here citing "pi: route abandoned" — that was a misreading of v2 spec §1's
    "本期不涉及"; see 2026-07-17-pi-user-mcp-bridge-design.md §1.1.)
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 订正 v2 spec 的过期表述**

`docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md` 第 5 行，改为：

```markdown
目标分支：**test**（pre 暂不稳定；test 无 pi driver，托管 driver 仅 claude/codex）
> ⚠️ **2026-07-17 订正**：本行的「test 无 pi driver」在 2026-07-13 已过期（pi driver
> 于 `1e01ef7e` 合流 test）。pi 现承载 gemini / openrouter / openai_compatible。
> 本行仅作历史记录，**不得据此推论 pi 路线状态**。§11 的 pi MCP extension 已由
> `2026-07-17-pi-user-mcp-bridge-design.md` 实施。
```

同文件第 26 行的表格行，改为：

```markdown
| pi | ~~test 分支无 pi driver，本期不涉及~~ **（2026-07-13 起过期）**；pi MCP extension 见 `2026-07-17-pi-user-mcp-bridge-design.md` |
```

- [ ] **Step 6-7: 跳过（07-16 两文档不在本分支）**

spec §8 订正表中的这两个文件**不存在于本分支**，本 task 不处理，也**不要创建它们**：

- `docs/superpowers/specs/2026-07-16-user-mcp-network-relaxation-design.md`
- `docs/superpowers/plans/2026-07-16-user-mcp-network-relaxation.md`

它们是 `user-mcp-auto-ca-fetch` 那条线的**未提交产物**，只存在于主工作树。在本分支
创建它们会伪造出一份与主工作树内容不同的副本，合并时冲突。见文末「遗留缺口」。

先确认它们确实不在，再继续：

Run: `ls docs/superpowers/specs/2026-07-16-* docs/superpowers/plans/2026-07-16-* 2>/dev/null; echo "exit=$?"`
Expected: 无输出（文件不存在）

- [ ] **Step 8: 订正 Dockerfile 的过期注释**

`deploy/Dockerfile.agent-runner` 第 38 行附近，把：

```
#  - pi CLI (`pi --mode json`)                     — pi driver: gemini/openrouter/
#    openai_compatible/deepseek, direct-native (no gateway). Exact pin only (pi
```

改为：

```
#  - pi CLI (`pi --mode json`)                     — pi driver: gemini/openrouter/
#    openai_compatible, direct-native (no gateway). NOT deepseek — it moved back
#    to the claude driver's Anthropic wire on 2026-07-14 (spawners.py:556,:834).
#    Exact pin only (pi
```

- [ ] **Step 9: 全量回归**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py ../tests/test_user_mcp_core.py ../tests/test_user_mcp_probe.py -v`
Expected: PASS（全部）

- [ ] **Step 10: Commit**

```bash
git add tools/chat_resident_consumer.py tests/test_user_mcp_consumer.py \
  docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md \
  docs/superpowers/specs/2026-07-16-user-mcp-network-relaxation-design.md \
  docs/superpowers/plans/2026-07-16-user-mcp-network-relaxation.md \
  deploy/Dockerfile.agent-runner
git commit -m "fix(user-mcp): pi gets NODE_EXTRA_CA_CERTS; retract 'route abandoned'

pi is a Node process like claude, so the CA belongs to it too. The early
return citing 'pi: route abandoned' rested on a misreading: v2 spec §1 said
'本期不涉及' (scheduling), which the 07-16 spec upgraded into '路线已放弃'
(strategy) — a decision nobody made. That claim also went stale on 07-13 when
the pi driver landed on test. pi in fact carries gemini/openrouter/
openai_compatible on test and prod.

A test asserted == {} and thereby locked the wrong rationale in as a contract.

Also drops deepseek from the Dockerfile's pi-driver comment — it moved back to
the claude driver on 07-14 and the comment never followed."
```

---

### Task 2: pi 模板改用 `-ne -xt read,edit,write` 并开 `{mcp}` 槽位

**Files:**
- Modify: `backend/agent_runtime/spawners.py:455-482`（`_default_cli_cmd` 的 pi 分支）
- Test: `tests/test_agent_runtime_spawners.py:759-765,883-891`

**Interfaces:**
- Consumes: 无
- Produces: pi 模板字符串形如 `pi --mode json -ne -xt read,edit,write {mcp} --append-system-prompt <file> [--model feedling/<m> ][--thinking <lvl> ]--session-id {session_id}`。Task 3 的 `_user_mcp_cli_value` 依赖 `{mcp}` 槽位存在。

- [ ] **Step 1: 改现有断言 + 加等价性/安全性测试**

先确保文件顶部有 `import re`（下面的 posture 测试要用）：
`grep -n "^import re" tests/test_agent_runtime_spawners.py`，没有就加。

`tests/test_agent_runtime_spawners.py`，把 `test_pi_default_cli_cmd`（约 759 行起，
**用 `grep -n "t bash"` 定位**）替换为：

```python
def test_pi_default_cli_cmd():
    cmd = spawners._default_cli_cmd("pi", "/h", model="m")
    assert "pi --mode json" in cmd
    # -t bash would ALSO filter out extension-registered tools (pi 0.80.3
    # agent-session.js:1867 filters allCustomTools through isAllowedTool), which
    # would silently kill the user-MCP bridge. -xt read,edit,write leaves the
    # same active set (["bash"]) while letting extension tools through.
    assert "-xt read,edit,write" in cmd
    assert "-t bash" not in cmd
    # -ne: with no -t, allowedToolNames is undefined and auto-discovered
    # extensions in ~/.pi/agent/extensions/ would get their tools activated by
    # includeAllExtensionTools. The agent can write files there via bash. -ne
    # closes discovery; explicit -e still works.
    assert "-ne" in cmd
    assert "{mcp}" in cmd


def test_pi_default_cli_cmd_tool_posture_equals_old_t_bash():
    """Regression: a pi user with no MCP config must keep the old active set.

    pi's defaultActiveToolNames is the hardcoded ["read","bash","edit","write"]
    (sdk.js:131). Excluding read/edit/write leaves exactly ["bash"] — which is
    what `-t bash` used to produce. Asserting this against the real command
    (rather than against a literal) is the point: it is what would catch the
    template drifting away from the posture it is supposed to preserve.
    """
    cmd = spawners._default_cli_cmd("pi", "/h", model="m")
    excluded = re.search(r"-xt (\S+)", cmd).group(1).split(",")
    # Order within -xt is not semantically meaningful; the SET is.
    assert set(excluded) == {"read", "edit", "write"}
    assert {"read", "bash", "edit", "write"} - set(excluded) == {"bash"}
```

同文件 `test_consumer_env_uses_pi_cli_and_home_for_pi_driver`（883 行起）中的：

```python
    assert cmd.startswith("pi --mode json -t bash ")
```

改为：

```python
    assert cmd.startswith("pi --mode json -ne -xt read,edit,write {mcp} ")
```

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_agent_runtime_spawners.py -k pi -v`
Expected: FAIL — `-xt read,edit,write` / `-ne` / `{mcp}` 均不在当前模板中

- [ ] **Step 3: 改模板**

`backend/agent_runtime/spawners.py` 的 pi 分支，把注释块中 `-t bash` 那段替换，并改 return。注释改为：

```python
        # -ne -xt read,edit,write: tool posture. Equivalent to the old `-t bash`
        #   for the active set — pi's defaultActiveToolNames is the hardcoded
        #   ["read","bash","edit","write"] (sdk.js:131), so excluding three
        #   leaves exactly ["bash"] — but WITHOUT -t's fatal side effect: -t is
        #   an allowlist that "applies to built-in, extension, and custom tools"
        #   (pi --help), and agent-session.js:1867 filters extension-registered
        #   tools through it BEFORE they reach the registry. Under `-t bash` the
        #   user-MCP bridge's tools would be dropped and setActiveTools() could
        #   not recover them (they're not in the registry at all).
        #   -ne closes extension auto-discovery: with no -t, allowedToolNames is
        #   undefined and includeAllExtensionTools (agent-session.js:145) would
        #   activate any extension the agent dropped into ~/.pi/agent/extensions/
        #   via bash. Explicit -e paths still load. This preserves the isolation
        #   `-t bash` used to provide.
        # {mcp}: the resident fills this per turn — `-e <bridge>` on the chat
        #   lane, empty elsewhere. Same shape as claude's --mcp-config.
```

return 语句改为：

```python
        return (
            f"pi --mode json -ne -xt read,edit,write {{mcp}} "
            f"--append-system-prompt {prompt_file} "
            f"{model_part}{thinking_part}"
            "--session-id {session_id}"
        )
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_agent_runtime_spawners.py -v`
Expected: PASS（全部；含 `test_pi_models_json_loads_and_enables_reasoning_in_real_pi`，它需要本地装有 pi 0.80.3）

- [ ] **Step 5: Commit**

```bash
git add backend/agent_runtime/spawners.py tests/test_agent_runtime_spawners.py
git commit -m "feat(pi): swap -t bash for -ne -xt read,edit,write, open {mcp} slot

-t is an allowlist that applies to extension tools too (pi --help; enforced at
agent-session.js:1867 before tools reach the registry), so -t bash would have
silently dropped every user-MCP tool the bridge registers — and setActiveTools
could not recover them.

-xt read,edit,write yields the identical active set (defaultActiveToolNames is
the hardcoded [read,bash,edit,write] at sdk.js:131, minus three = [bash]) while
letting extension tools through. -ne closes extension auto-discovery, which
otherwise widens once allowedToolNames goes undefined.

No behavior change for pi users without MCP config."
```

---

### Task 3: consumer 的 `{mcp}` pi 分支 + 桥路径 env 注入

**Files:**
- Modify: `tools/chat_resident_consumer.py`（常量区 ~258 行；`_user_mcp_cli_value` ~5788 行；`call_agent_cli` 的 child_env ~4726 行）
- Test: `tests/test_user_mcp_consumer.py`

**Interfaces:**
- Consumes: Task 2 的 `{mcp}` 槽位
- Produces:
  - 常量 `PI_MCP_BRIDGE_FILE`（默认 `/app/tools/pi_mcp_bridge/index.js`，可经同名 env 覆盖）
  - `_user_mcp_cli_value(template, lane)` 对 pi 模板返回 `f"-e {PI_MCP_BRIDGE_FILE}"`（chat + 有 enabled server）或 `""`
  - `_user_mcp_child_env(cmd)`：返回 CA env **并**在 pi 时附带 `FEEDLING_USER_MCP_FILE`。Task 6 的 `index.js` 读取该 env 名。

- [ ] **Step 1: 写失败测试**

`tests/test_user_mcp_consumer.py` 末尾追加：

```python
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
    monkeypatch.setattr(c, "PI_MCP_BRIDGE_FILE", "/app/tools/pi_mcp_bridge/index.js")
    tpl_pi = "pi --mode json -ne -xt read,edit,write {mcp} --session-id {session_id}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_pi)

    assert c._user_mcp_cli_value(tpl_pi, "chat") == \
        "-e /app/tools/pi_mcp_bridge/index.js"
    assert c._user_mcp_cli_value(tpl_pi, "background") == ""


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
```

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py -k "pi or child_env" -v`
Expected: FAIL — `AttributeError: module has no attribute 'PI_MCP_BRIDGE_FILE'` / `'_user_mcp_child_env'`

- [ ] **Step 3: 加常量**

`tools/chat_resident_consumer.py`，在 `USER_MCP_CASTORE_FILE` 定义之后追加：

```python
# The pi user-MCP bridge extension. ONE shared static file for every user —
# `COPY tools/ ./tools/` (Dockerfile.agent-runner) puts it here; the per-user
# config path rides FEEDLING_USER_MCP_FILE instead (see _user_mcp_child_env).
# Overridable for tests and for the self-hosted VPS layout.
PI_MCP_BRIDGE_FILE = os.environ.get(
    "PI_MCP_BRIDGE_FILE", "/app/tools/pi_mcp_bridge/index.js",
)
```

- [ ] **Step 4: 加 `_user_mcp_cli_value` 的 pi 分支**

在 `_user_mcp_cli_value` 中，`if _cli_template_is_codex():` 分支**之前**插入：

```python
    if _cli_template_is_pi():
        # pi has no built-in MCP (README:491) — the bridge extension registers
        # each MCP tool as a native pi tool. Same lane rule as claude: chat only.
        return f"-e {PI_MCP_BRIDGE_FILE}" if lane == "chat" else ""
```

并把 docstring 中的分支说明补一行（放在 codex 那条之后）：

```python
    - pi     → ``-e <bridge>`` ONLY on the chat lane. pi has no MCP of its own;
      the extension registers the user's MCP tools as native pi tools. A
      background turn simply loads no extension, so the tools do not exist.
```

- [ ] **Step 5: 把 `_user_mcp_ca_env` 扩成 `_user_mcp_child_env`**

`_user_mcp_ca_env` 重命名为 `_user_mcp_child_env`，并在**函数末尾的所有 return 之前**改为先算出 CA env、再按 driver 补 pi 的配置路径。整个函数体（docstring 之后）替换为：

```python
    enabled_servers = [
        s for s in _user_mcp_applied.get("servers") or [] if s.get("enabled")
    ]
    if not enabled_servers:
        return {}
    env: dict = {}
    if _is_codex_cmd(cmd):
        if Path(USER_MCP_CASTORE_FILE).exists():
            env["SSL_CERT_FILE"] = USER_MCP_CASTORE_FILE   # REPLACES → concat bundle
    else:
        # claude AND pi — both Node, both ADD via NODE_EXTRA_CA_CERTS.
        if Path(USER_MCP_CA_FILE).exists():
            env["NODE_EXTRA_CA_CERTS"] = USER_MCP_CA_FILE  # ADDS → user CA only
    if _is_pi_cmd(cmd):
        # The bridge is a shared static file; hand it this user's config path.
        env["FEEDLING_USER_MCP_FILE"] = USER_MCP_FILE
    return env
```

更新生产调用点 `tools/chat_resident_consumer.py:4726`：

```python
    child_env.update(_user_mcp_child_env(cmd))
```

- [ ] **Step 5b: 同步测试里的调用点（否则 Task 1 的测试全红）**

重命名会打断 Task 1 刚写的测试以及既有的 CA 测试——它们都还在调 `_user_mcp_ca_env`。
在 `tests/test_user_mcp_consumer.py` 中把**所有** `c._user_mcp_ca_env(` 替换为
`c._user_mcp_child_env(`。涉及（行号以 Task 1 完成后的文件为准）：

- `test_env_injection_covers_pi_like_claude`（Task 1 新写）
- `test_env_injection_pi_empty_when_no_servers`（Task 1 新写）
- `test_env_injection_empty_when_no_files`
- `test_env_injection_empty_when_no_servers_applied`
- 以及 `~398`、`~431`、`~490` 处的 codex/claude 断言

Run: `grep -c "_user_mcp_ca_env" tests/test_user_mcp_consumer.py`
Expected: `0`

claude/codex 既有的 CA 断言用 `==` 比较整个 dict——重命名后它们仍应通过，因为 pi
之外的 driver 不加 `FEEDLING_USER_MCP_FILE` 这个 key。

⚠️ **但 pi 自己那条 exact-dict 断言会破**：Task 1 写的
`test_env_injection_covers_pi_like_claude` 断言
`== {"NODE_EXTRA_CA_CERTS": str(ca_file)}`，而 pi 现在多了一个 key。
**不要把它收窄成单键检查**（`env["NODE_EXTRA_CA_CERTS"] == ...`）——那会丢掉「不多
不少」的保证，而 `test_child_env_gives_pi_the_config_path` 只证 file key（其 CA 路径
指向不存在的文件），于是**没有任何测试**能拦住将来往 agent 环境里泄漏第三个变量。
正确改法是补 mock 让两个 key 同时在场、维持整字典断言：

```python
    mcp_file = tmp_path / "user-mcp.json"          # 不必真建，只读常量值不 stat
    monkeypatch.setattr(c, "USER_MCP_FILE", str(mcp_file))
    ...
    assert c._user_mcp_child_env(["pi", "--mode", "json"]) == {
        "NODE_EXTRA_CA_CERTS": str(ca_file),
        "FEEDLING_USER_MCP_FILE": str(mcp_file),
    }
```

- [ ] **Step 6: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py -v`
Expected: PASS（全部——含 Task 1 改过的 CA 断言，它们现在走 `_user_mcp_child_env`）

- [ ] **Step 7: 全仓搜残留旧函数名**

Run: `grep -rn "_user_mcp_ca_env" tools/ tests/ backend/ docs/`
Expected: 仅 `docs/superpowers/plans/2026-07-16-*.md` 命中（历史计划，不改）。若 `tools/` 或 `tests/` 有命中，改掉。

- [ ] **Step 8: Commit**

```bash
git add tools/chat_resident_consumer.py tests/test_user_mcp_consumer.py
git commit -m "feat(user-mcp): wire the {mcp} slot for pi + hand the bridge its config path

_user_mcp_cli_value gains a pi branch shaped exactly like claude's: -e <bridge>
on the chat lane, nothing otherwise. Background turns load no extension, so the
MCP tools do not exist that turn — structural gating, not a flag.

_user_mcp_ca_env becomes _user_mcp_child_env: it still resolves the CA (claude
and pi both take NODE_EXTRA_CA_CERTS; codex takes SSL_CERT_FILE), and now also
hands pi FEEDLING_USER_MCP_FILE. The bridge is one shared static file, so the
per-user config path has to ride the environment."
```

---

### Task 4: `mcp_client.js` —— 零依赖 MCP 协议

**Files:**
- Create: `tools/pi_mcp_bridge/mcp_client.js`
- Create: `tests/pi_mcp_bridge_harness.mjs`
- Create: `tests/test_pi_mcp_bridge.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `PROTOCOL_VERSION` = `"2025-03-26"`
  - `parseRpcResponse(contentType, body) -> object`
  - `class McpClient(url, headers, {timeoutMs, fetchImpl})`，方法 `initialize()`、`listTools() -> Array<{name, description, inputSchema}>`、`callTool(name, args) -> {content: [...]}`。Task 6 的 `index.js` 依赖这三个方法名。

- [ ] **Step 1: 写 node harness（测试基建）**

Create `tests/pi_mcp_bridge_harness.mjs`：

```javascript
// Harness for tests/test_pi_mcp_bridge.py — runs a piece of the bridge under
// node and prints a JSON result on stdout.
//
// Usage:
//   node pi_mcp_bridge_harness.mjs client <url>
//     → {"tools": [...]} | {"error": "..."}
//   node pi_mcp_bridge_harness.mjs mapping <json-of-servers>
//     → {"mapped": [...], "dropped": [...]}
//   node pi_mcp_bridge_harness.mjs extension
//     → {"tools": [{name, description, parameters}], "threw": false}
//
// The bridge is plain .js precisely so this harness can import() it with no
// build step and no type-stripping — see the design doc §4.1.

import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BRIDGE = path.resolve(HERE, "..", "tools", "pi_mcp_bridge");

const [, , mode, arg] = process.argv;

async function main() {
  if (mode === "client") {
    const { McpClient } = await import(path.join(BRIDGE, "mcp_client.js"));
    const client = new McpClient(arg, {}, { timeoutMs: 5000 });
    await client.initialize();
    const tools = await client.listTools();
    const called = await client.callTool(tools[0].name, { q: "x" });
    return { tools, called };
  }
  if (mode === "mapping") {
    const { buildToolTable } = await import(path.join(BRIDGE, "tool_mapping.js"));
    return buildToolTable(JSON.parse(arg));
  }
  if (mode === "extension") {
    const mod = await import(path.join(BRIDGE, "index.js"));
    const tools = [];
    const pi = {
      registerTool: (t) => tools.push(t),
      on: () => {},
      registerCommand: () => {},
    };
    let threw = false;
    try {
      await mod.default(pi);
    } catch (err) {
      threw = true;
      return { threw, error: String(err && err.message) };
    }
    // Exercise every registered tool once so execute() paths are covered too.
    const executed = [];
    for (const t of tools) {
      const r = await t.execute("call-1", { q: "x" }, undefined, undefined, {});
      executed.push({ name: t.name, content: r.content });
    }
    return {
      threw,
      tools: tools.map((t) => ({
        name: t.name, description: t.description, parameters: t.parameters,
      })),
      executed,
    };
  }
  throw new Error(`unknown mode: ${mode}`);
}

main().then(
  (out) => { process.stdout.write(JSON.stringify(out)); },
  (err) => { process.stdout.write(JSON.stringify({ error: String(err && err.message) })); },
);
```

- [ ] **Step 2: 写失败测试（含真监听端口的 fake MCP server）**

Create `tests/test_pi_mcp_bridge.py`：

```python
"""pi user-MCP bridge tests (tools/pi_mcp_bridge/).

The bridge is JS, so behavior is exercised through a node harness
(tests/pi_mcp_bridge_harness.mjs) driven from pytest. Grep-the-source
assertions (the older feedling-io-tools style) cannot reach any of the
branches that matter here: name sanitizing, collision de-dup, the tool cap,
and — most importantly — that a dead MCP server is skipped silently instead
of taking the whole pi startup down with it.

Unlike tests/test_user_mcp_probe.py's ASGI + httpx.MockTransport fake (which
is in-process and never binds), node's fetch needs a real port, so this uses
a stdlib ThreadingHTTPServer.

Requires: node on PATH (CI: actions/setup-node).

Run with:
    cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v
"""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "pi_mcp_bridge_harness.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node required for the pi bridge harness (CI installs it via setup-node)",
)


def _make_handler(tools):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request stderr noise
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                result = {"content": [{"type": "text",
                                       "text": f"called {req['params']['name']}"}]}
            else:
                self.send_response(400)
                self.end_headers()
                return
            body = json.dumps(
                {"jsonrpc": "2.0", "id": req.get("id"), "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("mcp-session-id", "sess-1")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def fake_mcp():
    """Spin a real-port fake MCP server; yields a factory -> url."""
    servers = []

    def start(tools):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(tools))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}/mcp"

    yield start
    for srv in servers:
        srv.shutdown()


def _harness(mode, arg=None, env=None):
    cmd = ["node", str(_HARNESS), mode]
    if arg is not None:
        cmd.append(arg)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    assert proc.stdout, f"harness printed nothing; stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


def test_client_handshake_list_and_call(fake_mcp):
    url = fake_mcp([{"name": "search", "description": "find things",
                     "inputSchema": {"type": "object",
                                     "properties": {"q": {"type": "string"}}}}])
    out = _harness("client", url)
    assert "error" not in out, out
    assert [t["name"] for t in out["tools"]] == ["search"]
    assert out["called"]["content"][0]["text"] == "called search"
```

- [ ] **Step 3: 跑测试确认红**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v`
Expected: FAIL — harness 报 `Cannot find module .../mcp_client.js`

- [ ] **Step 4: 实现 `mcp_client.js`**

Create `tools/pi_mcp_bridge/mcp_client.js`：

```javascript
/**
 * Zero-dependency MCP client — single-shot JSON-RPC over streamable HTTP.
 *
 * Deliberately NOT the `mcp` SDK, for the same reason backend/hosted/mcp_probe.py
 * hand-rolled its own: four methods don't justify a node_modules tree (and its
 * supply-chain surface) inside the TEE image.
 *
 * PROTOCOL_VERSION MUST stay in sync with mcp_probe.py's _PROTOCOL_VERSION —
 * the two are the same protocol against the same user servers.
 */

export const PROTOCOL_VERSION = "2025-03-26";

/**
 * Parse a JSON-RPC reply that may arrive as plain JSON or as an SSE stream.
 * Streamable-HTTP servers may answer either way for the same request, so both
 * shapes have to work (mcp_probe.py:82 does the same on the Python side).
 */
export function parseRpcResponse(contentType, body) {
  if (String(contentType || "").includes("text/event-stream")) {
    // Take the LAST data: line that parses — earlier ones may be pings or
    // progress notifications, the final one carries the result.
    const lines = String(body).split(/\r?\n/).reverse();
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      try {
        return JSON.parse(payload);
      } catch {
        // not the JSON-RPC frame — keep scanning backwards
      }
    }
    throw new Error("no JSON-RPC payload found in SSE stream");
  }
  return JSON.parse(body);
}

export class McpClient {
  constructor(url, headers, { timeoutMs = 10000, fetchImpl = fetch } = {}) {
    this.url = url;
    this.headers = headers || {};
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
    this.sessionHeaders = {};
    this.nextId = 1;
  }

  async _post(payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const resp = await this.fetchImpl(this.url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "application/json, text/event-stream",
          ...this.headers,
          ...this.sessionHeaders,
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
        redirect: "error", // same posture as mcp_probe.py: no redirect chasing
      });
      // Always drain the body, even for notifications, so the socket is freed.
      const body = await resp.text();
      // Learn the session id from ANY reply, before the notification early-return.
      const sid = resp.headers.get("mcp-session-id");
      if (sid) this.sessionHeaders["Mcp-Session-Id"] = sid;
      // Notifications are fire-and-forget. The MCP spec requires sending
      // notifications/initialized before further requests, but servers answer it
      // however they like. mcp_probe.py:148 ignores its status outright
      // ("tolerate servers that 4xx it") and this client must match: a strict
      // check here kills the whole handshake — and therefore EVERY tool — for a
      // server whose only sin is not ack'ing a notification cleanly.
      // ORDER IS LOAD-BEARING: this return must precede the resp.ok check.
      if (payload.id === undefined) return null;
      if (!resp.ok) throw new Error(`http ${resp.status}`);
      const parsed = parseRpcResponse(resp.headers.get("content-type"), body);
      if (parsed && parsed.error) {
        const msg = parsed.error.message || JSON.stringify(parsed.error);
        throw new Error(`rpc error: ${msg}`);
      }
      return parsed ? parsed.result : null;
    } finally {
      clearTimeout(timer);
    }
  }

  async initialize() {
    await this._post({
      jsonrpc: "2.0",
      id: this.nextId++,
      method: "initialize",
      params: {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "feedling-pi-bridge", version: "1" },
      },
    });
    await this._post({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async listTools() {
    const result = await this._post({
      jsonrpc: "2.0", id: this.nextId++, method: "tools/list",
    });
    return (result && result.tools) || [];
  }

  async callTool(name, args) {
    return await this._post({
      jsonrpc: "2.0",
      id: this.nextId++,
      method: "tools/call",
      params: { name, arguments: args || {} },
    });
  }
}
```

- [ ] **Step 5: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v`
Expected: PASS

- [ ] **Step 6: 加 SSE 与协议版本一致性测试**

`tests/test_pi_mcp_bridge.py` 追加：

```python
def test_client_parses_sse_framed_reply():
    """Streamable-HTTP servers may answer the same request as SSE.

    Uses its own handler rather than the fake_mcp fixture, which only speaks the
    plain-JSON framing.
    """

    class SseHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            if req.get("method") == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if req.get("method") == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "sse", "version": "0"}}
            elif req.get("method") == "tools/list":
                result = {"tools": [{"name": "s", "description": "d",
                                     "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "called s"}]}
            frame = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                "result": result})
            body = f": ping\n\ndata: {frame}\n\n".encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SseHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = _harness("client", f"http://127.0.0.1:{srv.server_port}/mcp")
        assert "error" not in out, out
        assert [t["name"] for t in out["tools"]] == ["s"]
    finally:
        srv.shutdown()


def test_bridge_protocol_version_matches_the_probe():
    """The bridge and mcp_probe.py talk the same protocol to the same servers —
    a silent skew between them is a latent interop bug."""
    js = (Path(__file__).parent.parent / "tools" / "pi_mcp_bridge"
          / "mcp_client.js").read_text()
    py = (Path(__file__).parent.parent / "backend" / "hosted"
          / "mcp_probe.py").read_text()
    assert 'export const PROTOCOL_VERSION = "2025-03-26";' in js
    assert '_PROTOCOL_VERSION = "2025-03-26"' in py
```

- [ ] **Step 7: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 8: Commit**

```bash
git add tools/pi_mcp_bridge/mcp_client.js tests/pi_mcp_bridge_harness.mjs \
  tests/test_pi_mcp_bridge.py
git commit -m "feat(pi-mcp): zero-dependency MCP client for the pi bridge

Hand-rolled JSON-RPC over streamable HTTP (initialize → initialized →
tools/list → tools/call), mirroring backend/hosted/mcp_probe.py — four methods
don't justify pulling the mcp SDK's dependency tree into the TEE image, which
is the same call the probe made.

Handles both reply framings (plain JSON and SSE) since streamable-HTTP servers
may answer either way, and a test pins PROTOCOL_VERSION to the probe's so the
two can't silently skew.

Tests drive the JS through a node harness against a real-port stdlib fake
server — the probe's ASGI/MockTransport fake never binds, so node's fetch
can't reach it."
```

---

### Task 5: `tool_mapping.js` —— 命名 / 去重 / 上限（纯函数）

**Files:**
- Create: `tools/pi_mcp_bridge/tool_mapping.js`
- Test: `tests/test_pi_mcp_bridge.py`

**Interfaces:**
- Consumes: 无（纯函数）
- Produces:
  - `MAX_TOOLS` = `50`
  - `buildToolTable(servers) -> {mapped, dropped}`，其中 `servers` 形如 `[{name, tools: [{name, description, inputSchema}]}]`；`mapped` 元素为 `{piName, server, mcpName, description, parameters}`；`dropped` 为 `"<server>/<tool>"` 字符串数组。Task 6 的 `index.js` 依赖这些字段名。

- [ ] **Step 1: 写失败测试**

先在 `tests/test_pi_mcp_bridge.py` 顶部的 imports 追加一行（本 task 的正则断言要用）：

```python
import re
```

然后追加：

```python
def _servers(*specs):
    """specs: (server_name, [tool_name, ...])"""
    return json.dumps([
        {"name": s, "tools": [{"name": t, "description": f"desc {t}",
                               "inputSchema": {"type": "object"}} for t in tools]}
        for s, tools in specs
    ])


def test_mapping_prefixes_and_sanitizes_for_gemini():
    """pi carries gemini, whose tool names must match ^[a-zA-Z0-9_-]{1,64}$.
    MCP tool names come from the user's server and are unconstrained."""
    out = _harness("mapping", _servers(("jira", ["search.issues", "create ticket"])))
    names = [m["piName"] for m in out["mapped"]]
    assert names == ["mcp_jira_create_ticket", "mcp_jira_search_issues"]
    for n in names:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n), n


def test_mapping_is_deterministic_regardless_of_server_order():
    """The model must see the same toolset every turn. Servers finish their
    handshakes in nondeterministic order, so mapping must sort, not zip."""
    a = _harness("mapping", _servers(("alpha", ["x"]), ("beta", ["y"])))
    b = _harness("mapping", _servers(("beta", ["y"]), ("alpha", ["x"])))
    assert [m["piName"] for m in a["mapped"]] == [m["piName"] for m in b["mapped"]]


def test_mapping_dedupes_collisions_deterministically():
    """Two different MCP tool names can sanitize to the same pi name."""
    out = _harness("mapping", _servers(("s", ["a.b", "a-b", "a b"])))
    names = [m["piName"] for m in out["mapped"]]
    assert len(names) == len(set(names)), names
    again = _harness("mapping", _servers(("s", ["a.b", "a-b", "a b"])))
    assert names == [m["piName"] for m in again["mapped"]]


def test_mapping_collision_suffix_survives_a_sibling_collider_being_added():
    """The one property hash-of-pair buys over a counter.

    ⚠️ 没有这条测试，上面那条「确定性」测试是**测不出东西的**：它只用同样输入跑两次，
    而纯函数天然自我复现——一个**被明令禁止的 counter 后缀实现能原样通过它**（实测
    验证过）。真正的区别是：counter 在插入时重新编号，hash-of-pair 不动。而表在**每个
    聊天回合**都重建，所以名字一漂移，用户加个无关工具就会静默改掉别的工具的名字，
    模型记住的工具随即失效。

    "a b" / "a!b" / "a.b" / "a/b" 都 sanitize 成 "a_b" 故互相碰撞；"a-b" 保留短横不碰。
    排序按原始名，"a!b"(0x21) 落在 "a b"(0x20) 与 "a-b"(0x2D) 之间——即 "a.b" 之前。
    """
    before = _harness("mapping", _servers(("s", ["a b", "a-b", "a.b"])))
    after = _harness("mapping", _servers(("s", ["a b", "a!b", "a-b", "a.b"])))

    name_of = {m["mcpName"]: m["piName"] for m in before["mapped"]}
    name_of_after = {m["mcpName"]: m["piName"] for m in after["mapped"]}

    for mcp_name in ("a b", "a-b", "a.b"):
        assert name_of[mcp_name] == name_of_after[mcp_name], (
            f"{mcp_name} renamed by an unrelated sibling: "
            f"{name_of[mcp_name]} -> {name_of_after[mcp_name]}")


def test_mapping_caps_tools_and_reports_what_it_dropped():
    """Silent truncation would present as 'tools come and go' — the hardest
    class of bug to triage, and indistinguishable from the symptom this whole
    feature exists to fix ('the AI can't see my tool')."""
    out = _harness("mapping", _servers(("s", [f"t{i:03d}" for i in range(60)])))
    assert len(out["mapped"]) == 50
    assert len(out["dropped"]) == 10
    assert all(d.startswith("s/") for d in out["dropped"])


def test_mapping_passes_mcp_schema_through_untouched():
    """pi accepts a bare JSON Schema (no TypeBox metadata) — validation.js:257
    branches on exactly that — so the MCP inputSchema needs no conversion."""
    schema = {"type": "object", "properties": {"q": {"type": "string"}},
              "required": ["q"]}
    servers = json.dumps([{"name": "s", "tools": [
        {"name": "find", "description": "d", "inputSchema": schema}]}])
    out = _harness("mapping", servers)
    assert out["mapped"][0]["parameters"] == schema
```

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -k mapping -v`
Expected: FAIL — harness 报 `Cannot find module .../tool_mapping.js`

- [ ] **Step 3: 实现 `tool_mapping.js`**

Create `tools/pi_mcp_bridge/tool_mapping.js`：

```javascript
/**
 * Pure MCP-tool → pi-tool mapping. No I/O, no network — unit-testable alone.
 *
 * pi carries gemini, whose tool names must match ^[a-zA-Z0-9_-]{1,64}$. MCP tool
 * names come from the user's server and are unconstrained, so every name gets
 * sanitized, length-capped, and de-duplicated.
 *
 * DETERMINISM IS A HARD REQUIREMENT: the same (server, tool) set must always
 * produce the same pi names. Servers finish their handshakes in whatever order
 * the network gives us, so the table is sorted before names are assigned — a
 * name that shifts between turns makes the model see a different toolset each
 * turn, and any tool the model remembers from earlier stops resolving.
 */

export const MAX_TOOLS = 50;
const MAX_NAME_LEN = 64;

function sanitizeSegment(s) {
  return String(s == null ? "" : s).replace(/[^a-zA-Z0-9_-]/g, "_");
}

/** FNV-1a → base36. Deterministic across processes (unlike hashing objects). */
function shortHash(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(36).slice(0, 6);
}

/**
 * @param {string} server  already constrained upstream to [a-z0-9_-]{1,32}
 * @param {string} tool    arbitrary text from the user's MCP server
 * @param {Set<string>} taken  names already assigned in this pass
 */
export function piToolName(server, tool, taken) {
  const base = `mcp_${sanitizeSegment(server)}_${sanitizeSegment(tool)}`;
  const capped = base.slice(0, MAX_NAME_LEN);
  if (!taken || !taken.has(capped)) return capped;
  // Collision: derive the suffix from the FULL original pair, not from a
  // counter — a counter would depend on iteration order and drift between turns.
  // "|" is a safe separator (keeps the encoding injective): server names are
  // constrained upstream to [a-z0-9_-]{1,32}, so one can never contain it.
  const suffix = `_${shortHash(`${server}|${tool}`)}`;
  return base.slice(0, MAX_NAME_LEN - suffix.length) + suffix;
}

/**
 * @param {Array<{name: string, tools: Array<{name, description, inputSchema}>}>} servers
 * @returns {{mapped: Array<{piName, server, mcpName, description, parameters}>,
 *            dropped: string[]}}
 */
export function buildToolTable(servers) {
  const pairs = [];
  for (const s of servers || []) {
    for (const t of s.tools || []) {
      if (!t || !t.name) continue;
      pairs.push({ server: s.name, tool: t });
    }
  }
  // Sort so name assignment never depends on handshake completion order.
  pairs.sort((a, b) => {
    if (a.server !== b.server) return a.server < b.server ? -1 : 1;
    if (a.tool.name !== b.tool.name) return a.tool.name < b.tool.name ? -1 : 1;
    return 0;
  });

  const taken = new Set();
  const mapped = [];
  const dropped = [];
  for (const p of pairs) {
    if (mapped.length >= MAX_TOOLS) {
      dropped.push(`${p.server}/${p.tool.name}`);
      continue;
    }
    const piName = piToolName(p.server, p.tool.name, taken);
    taken.add(piName);
    mapped.push({
      piName,
      server: p.server,
      mcpName: p.tool.name,
      description: p.tool.description
        || `MCP tool "${p.tool.name}" from server "${p.server}"`,
      // Passed through verbatim: pi accepts a bare JSON Schema (it branches on
      // !hasTypeBoxMetadata && isJsonSchemaObject — pi-ai validation.js:257).
      parameters: p.tool.inputSchema || { type: "object", properties: {} },
    });
  }
  return { mapped, dropped };
}
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v`
Expected: PASS（8 个测试）

- [ ] **Step 5: Commit**

```bash
git add tools/pi_mcp_bridge/tool_mapping.js tests/test_pi_mcp_bridge.py
git commit -m "feat(pi-mcp): deterministic MCP→pi tool mapping with a 50-tool cap

Names are sanitized to gemini's ^[a-zA-Z0-9_-]{1,64}$ (pi carries gemini, and
MCP tool names come from the user's server, so they're unconstrained).

The table is sorted before names are assigned and collision suffixes hash the
original (server, tool) pair rather than using a counter. Both exist for the
same reason: servers complete their handshakes in nondeterministic order, and a
pi tool name that shifts between turns makes the model see a different toolset
every turn.

The cap logs exactly which tools it dropped — silent truncation would present
as 'tools come and go', which is indistinguishable from the very symptom this
feature exists to fix.

MCP inputSchema passes through untouched: pi accepts bare JSON Schema
(validation.js:257 branches on !hasTypeBoxMetadata && isJsonSchemaObject)."
```

---

### Task 6: `index.js` —— extension 装配与错误隔离

**Files:**
- Create: `tools/pi_mcp_bridge/index.js`
- Test: `tests/test_pi_mcp_bridge.py`

**Interfaces:**
- Consumes: Task 4 的 `McpClient`；Task 5 的 `buildToolTable` / `MAX_TOOLS`；Task 3 的 env 名 `FEEDLING_USER_MCP_FILE`
- Produces: default export `async function (pi)` —— pi extension factory

- [ ] **Step 1: 写失败测试**

先在 `tests/test_pi_mcp_bridge.py` 顶部的 imports 追加一行：

```python
import os
```

然后追加：

```python
def _bridge_env(tmp_path, servers_doc):
    """Build the child env the resident would hand pi: the bridge is one shared
    static file, so the per-user config path rides FEEDLING_USER_MCP_FILE."""
    cfg = tmp_path / "user-mcp.json"
    cfg.write_text(json.dumps(servers_doc))
    env = dict(os.environ)
    env["FEEDLING_USER_MCP_FILE"] = str(cfg)
    return env


def test_extension_registers_tools_from_a_live_server(fake_mcp, tmp_path):
    url = fake_mcp([{"name": "search", "description": "find things",
                     "inputSchema": {"type": "object",
                                     "properties": {"q": {"type": "string"}}}}])
    env = _bridge_env(tmp_path,
                      {"mcpServers": {"jira": {"type": "http", "url": url,
                                               "headers": {}}}})
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert [t["name"] for t in out["tools"]] == ["mcp_jira_search"]
    assert out["tools"][0]["parameters"] == {
        "type": "object", "properties": {"q": {"type": "string"}}}
    assert out["executed"][0]["content"][0]["text"] == "called search"


def test_extension_survives_a_dead_server(tmp_path):
    """THE critical path. pi awaits this factory and blocks startup on it, so an
    uncaught throw here doesn't degrade MCP — it takes the user's whole chat
    turn down. A broken third-party server must never cost someone their agent.
    """
    # Port 1 is reserved/unbound — connection refused, fast.
    env = _bridge_env(tmp_path,
                      {"mcpServers": {"dead": {"type": "http",
                                               "url": "http://127.0.0.1:1/mcp",
                                               "headers": {}}}})
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []


def test_extension_keeps_live_server_when_a_sibling_is_dead(fake_mcp, tmp_path):
    """One bad server must not deprive the user of the good ones."""
    url = fake_mcp([{"name": "ok", "description": "d",
                     "inputSchema": {"type": "object"}}])
    env = _bridge_env(tmp_path, {"mcpServers": {
        "good": {"type": "http", "url": url, "headers": {}},
        "dead": {"type": "http", "url": "http://127.0.0.1:1/mcp", "headers": {}},
    }})
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert [t["name"] for t in out["tools"]] == ["mcp_good_ok"]


def test_extension_noop_without_env_or_config(tmp_path):
    env = dict(os.environ)
    env.pop("FEEDLING_USER_MCP_FILE", None)
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []

    env["FEEDLING_USER_MCP_FILE"] = str(tmp_path / "missing.json")
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []


def test_extension_survives_malformed_config(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text("{not json")
    env = dict(os.environ)
    env["FEEDLING_USER_MCP_FILE"] = str(cfg)
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []


def test_extension_tool_call_error_returns_content_not_throw(tmp_path):
    """A failing tools/call must come back to the model as text it can react to;
    throwing would surface as a broken turn instead of a tool error."""

    class DyingHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "d", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "boom", "description": "d",
                                     "inputSchema": {"type": "object"}}]}
            else:  # tools/call → JSON-RPC error
                body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                   "error": {"code": -32000,
                                             "message": "upstream exploded"}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                               "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), DyingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = _bridge_env(tmp_path, {"mcpServers": {"s": {
            "type": "http", "url": f"http://127.0.0.1:{srv.server_port}/mcp",
            "headers": {}}}})
        out = _harness("extension", None, env)
        assert out["threw"] is False, out
        payload = json.loads(out["executed"][0]["content"][0]["text"])
        assert payload["ok"] is False
        assert "upstream exploded" in payload["error"]
    finally:
        srv.shutdown()
```

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -k extension -v`
Expected: FAIL — harness 报 `Cannot find module .../index.js`

- [ ] **Step 3: 实现 `index.js`**

Create `tools/pi_mcp_bridge/index.js`：

```javascript
/**
 * feedling user-MCP bridge — a pi extension.
 *
 * pi has no built-in MCP (README:491: "No MCP. ... or build an extension that
 * adds MCP support"). This reads the ALREADY-MATERIALIZED user-mcp.json that the
 * resident consumer writes, connects to each server, and registers every MCP
 * tool as a native pi tool. Nothing in the storage/delivery/materialization
 * chain changes.
 *
 * Loaded per-turn via `-e` on the chat lane only (chat_resident_consumer.py's
 * _user_mcp_cli_value). A background turn simply loads no extension, so the MCP
 * tools do not exist that turn — that's what keeps proactive turns from silently
 * spending the user's third-party quota (v2 spec §1).
 *
 * ERROR POLICY — the single most important thing in this file: pi awaits this
 * factory and BLOCKS STARTUP on it (docs/extensions.md). An uncaught throw here
 * does not degrade MCP; it takes the user's entire chat turn down. Every failure
 * path below therefore logs and continues. A broken third-party server must
 * never cost someone their agent.
 *
 * All logging goes to stderr: pi --mode json emits its JSONL event stream on
 * stdout, and the resident parses it.
 */

import { readFileSync } from "node:fs";
import { McpClient } from "./mcp_client.js";
import { buildToolTable, MAX_TOOLS } from "./tool_mapping.js";

// Matches mcp_probe.py's _CONNECT_TIMEOUT — same servers, same patience.
const CONNECT_TIMEOUT_MS = 10000;

function loadServers(path) {
  const doc = JSON.parse(readFileSync(path, "utf8"));
  const out = [];
  for (const [name, cfg] of Object.entries((doc && doc.mcpServers) || {})) {
    if (!cfg || !cfg.url) continue;
    out.push({ name, url: cfg.url, headers: cfg.headers || {} });
  }
  return out;
}

export default async function feedlingUserMcpBridge(pi) {
  try {
    const file = process.env.FEEDLING_USER_MCP_FILE;
    if (!file) return; // not a pi user-MCP turn

    let servers;
    try {
      servers = loadServers(file);
    } catch (err) {
      console.error(`[user_mcp] cannot read ${file}: ${err && err.message}`);
      return;
    }
    if (!servers.length) return;

    const clients = new Map();
    const connected = await Promise.all(servers.map(async (s) => {
      const client = new McpClient(s.url, s.headers,
                                   { timeoutMs: CONNECT_TIMEOUT_MS });
      try {
        await client.initialize();
        const tools = await client.listTools();
        clients.set(s.name, client);
        return { name: s.name, tools };
      } catch (err) {
        // Skip this server, keep the others, let pi start.
        console.error(
          `[user_mcp] server "${s.name}" unreachable, skipped: ${err && err.message}`);
        return { name: s.name, tools: [] };
      }
    }));

    const { mapped, dropped } = buildToolTable(connected);
    if (dropped.length) {
      console.error(
        `[user_mcp] tool cap ${MAX_TOOLS} reached — dropped ${dropped.length}: `
        + dropped.join(", "));
    }

    for (const t of mapped) {
      pi.registerTool({
        name: t.piName,
        label: `${t.server}: ${t.mcpName}`,
        description: t.description,
        parameters: t.parameters,
        async execute(_toolCallId, params) {
          const client = clients.get(t.server);
          if (!client) {
            return toolError(t.piName, `server "${t.server}" is not connected`);
          }
          try {
            const result = await client.callTool(t.mcpName, params || {});
            const content = (result && result.content) || [];
            return {
              content: content.length ? content : [{ type: "text", text: "" }],
              details: { server: t.server, tool: t.mcpName },
            };
          } catch (err) {
            // Hand the model a readable failure instead of throwing — a throw
            // surfaces as a broken turn rather than a tool it can retry/route
            // around.
            return toolError(t.piName, String(err && err.message));
          }
        },
      });
    }

    console.error(
      `[user_mcp] registered ${mapped.length} tool(s) from `
      + `${clients.size}/${servers.length} server(s)`);
  } catch (err) {
    // Last line of defense: pi must still start.
    console.error(`[user_mcp] bridge disabled (unexpected): ${err && err.message}`);
  }
}

function toolError(tool, message) {
  return {
    content: [{ type: "text",
                text: JSON.stringify({ ok: false, error: message, tool }) }],
    details: { ok: false },
  };
}
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v`
Expected: PASS（14 个测试）

- [ ] **Step 5: Commit**

```bash
git add tools/pi_mcp_bridge/index.js tests/test_pi_mcp_bridge.py
git commit -m "feat(pi-mcp): the bridge extension — assemble, register, isolate failures

Reads the already-materialized user-mcp.json, connects to each server
concurrently, and registers every MCP tool as a native pi tool. The storage /
delivery / materialization chain is untouched.

Error policy is the load-bearing part: pi awaits this factory and blocks
startup on it, so an uncaught throw wouldn't degrade MCP — it would take the
user's whole chat turn down. Every path logs and continues: unreadable config,
malformed JSON, dead server, failing tools/call. A dead server is skipped and
its siblings still register. Tests cover each of those paths.

Logs go to stderr; stdout is pi's JSONL event stream, which the resident parses."
```

---

### Task 7: CI 接线

**Why this task exists:** 前六个 task 的测试**目前一条都不会在 CI 里跑**。`ci.yml` 的 pytest 目标是手工白名单，止于 `tests/test_dream_prompt_v1.py`——`test_user_mcp_consumer.py`、`test_agent_runtime_spawners.py`、新建的 `test_pi_mcp_bridge.py` 都不在其中。不做这一步，前面所有测试都只是本地资产。

**Files:**
- Modify: `.github/workflows/ci.yml`（test job：加 `setup-node`；pytest 列表加两个文件）

**Interfaces:**
- Consumes: Task 1/3 的 `tests/test_user_mcp_consumer.py`、Task 4/5/6 的 `tests/test_pi_mcp_bridge.py`
- Produces: 无（CI 配置）

- [ ] **Step 1: 加 setup-node**

`.github/workflows/ci.yml` 的 test job，在 `Install pytest-asyncio (test-only)` 步骤**之前**插入：

```yaml
      # The pi user-MCP bridge is JS; tests/test_pi_mcp_bridge.py drives it
      # through a node harness. Pinned major so the harness never silently
      # lands on a node without the APIs it uses (fetch/AbortController).
      - name: Set up Node (pi bridge harness)
        uses: actions/setup-node@v4
        with:
          node-version: '22'
```

- [ ] **Step 2: 把测试文件加进 pytest 列表**

同 job 的 `Run Round 3 V2 regression suite` 步骤，在 `tests/test_dream_prompt_v1.py \` 之后追加两行：

```yaml
            tests/test_user_mcp_consumer.py \
            tests/test_pi_mcp_bridge.py \
```

- [ ] **Step 3: 验证 node 在 harness 里可用**

Run: `node --version`
Expected: `v22.x`（本地）。CI 由 setup-node 保证。

- [ ] **Step 4: 本地跑一遍 CI 将要跑的两个文件**

Run:
```bash
cd backend && PYTHONPATH=. python -m pytest \
  ../tests/test_user_mcp_consumer.py ../tests/test_pi_mcp_bridge.py -v
```
Expected: PASS（全部）

- [ ] **Step 5: 确认 skipif 不会造成假绿**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_pi_mcp_bridge.py -v -rs`
Expected: **无 skipped**。若出现 `SKIPPED [.] node required`，说明 node 不在 PATH——本仓有过「零 skipped 的静默丢弃」导致假绿的前科（conftest 无 PG 时丢掉整个 DB 模块，「391 passed」实为 2440），此处必须确认真跑。

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: actually run the user-MCP consumer + pi bridge suites

ci.yml's pytest target is a hand-maintained allowlist, and neither
test_user_mcp_consumer.py nor the new test_pi_mcp_bridge.py was on it — every
test in this feature would have been a local-only asset while CI reported green.
That's the same failure shape as conftest's collect_ignore silently dropping the
DB modules with zero skipped ('391 passed' against a real baseline of 2440),
just via a different mechanism: not skipped, simply never collected.

setup-node pins the harness's runtime so it can't land on a node without fetch."
```

- [ ] **Step 7: 记录遗留缺口（不静默）**

在 `docs/superpowers/plans/2026-07-17-pi-user-mcp-bridge.md`（本文件）末尾的「遗留缺口」一节确认内容仍然准确（见文末）。**不要**把 `tests/test_agent_runtime_spawners.py` 加进 CI——它含 `test_pi_models_json_loads_and_enables_reasoning_in_real_pi`，会 `subprocess` 真跑 `pi --list-models` 且不 skip，CI 未装 pi 会直接红。该文件的 CI 覆盖需先装 pi，属独立决策，见文末。

---

### Task 8: 手工 E2E + CHANGELOG

**Files:**
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1-7 全部
- Produces: 无

- [ ] **Step 1: 本地全量回归**

Run:
```bash
cd backend && PYTHONPATH=. python -m pytest \
  ../tests/test_user_mcp_consumer.py ../tests/test_user_mcp_core.py \
  ../tests/test_user_mcp_probe.py ../tests/test_user_mcp_routes.py \
  ../tests/test_user_mcp_poll.py ../tests/test_pi_mcp_bridge.py \
  ../tests/test_agent_runtime_spawners.py ../tests/test_chat_resident_consumer.py -v
```
Expected: PASS（全部）。任何红都必须先修再继续——`test_agent_runtime_spawners.py` 需要本地装有 pi 0.80.3。

- [ ] **Step 2: 确认 test 环境部署所需的镜像重建**

桥是 `COPY tools/` 带进镜像的，**Dockerfile 不变但镜像必须重建**（新文件）。确认部署流程会重建 runner 镜像并 bump tag。

- [ ] **Step 3: 手工 E2E（test 环境）**

对齐 v2 spec §10 的做法：

1. 选一个 pi 路线用户（provider ∈ {gemini, openrouter, openai_compatible}）
2. `curl` 建一个 MCP server 配置，指向一个真实公共 MCP server
3. 聊一轮，让模型列出可用工具 → **应看到 `mcp_<server>_<tool>`**
4. 让模型真的调用一次该工具 → 应返回真实结果
5. 检查 runner 日志中的 `[user_mcp] registered N tool(s) from M/M server(s)`
6. **验证 lane gating**：等一个 proactive 回合，确认该回合日志无 `[user_mcp] registered`，且模型看不到 MCP 工具
7. **验证故障隔离**：把配置改成一个不可达 URL，再聊一轮 → 聊天必须正常，日志出现 `server "<name>" unreachable, skipped`

- [ ] **Step 4: 写 CHANGELOG**

`docs/CHANGELOG.md` 顶部追加（landmark 惯例）：

```markdown
- **pi 路线终于能用用户 MCP 了**（v2 spec §11 的后续项，欠了 4 天）：pi 官方无 MCP，
  写了个 extension 桥（`tools/pi_mcp_bridge/`，零依赖手写 MCP client，与
  `mcp_probe.py` 同协议同理由）读**已物化**的 `user-mcp.json`、把每个 MCP 工具注册成
  pi 原生工具。**数据链路一行没改**。pi 模板 `-t bash` → `-ne -xt read,edit,write`
  ——`-t` 是 allowlist 且对 extension 工具同样生效（`agent-session.js:1867` 在工具进
  registry 前就过滤），不换的话桥注册的工具会被静默丢弃。影响 gemini/openrouter/
  openai_compatible 全部托管用户。
- **流程教训（比功能本身更值得记）**：这个洞之所以存在 4 天，是因为一句**过期事实被
  升级成了错误结论**。v2 spec §1（07-08）说「test 无 pi driver，本期不涉及」——当时属
  实；07-13 pi driver 合流，该句过期但没人回头改；07-16 的 spec 读到它，写成「路线已
  放弃」；07-17 该结论被 `8cb9314b` 刻进代码注释。§11 那个「等 pi driver 合流就做」的
  待办在 07-13 到期，却被反向**误销案**。本次一并订正四处 + 一句 Dockerfile 过期注释
  （它称 deepseek 走 pi，实际 07-14 已改回 claude driver——同一种病）。新 spec §6 因此
  给每条源码断言都标了行号证据 + 时效性声明（绑定 pi 0.80.3，升级必须重验）。
```

- [ ] **Step 5: Commit（本仓）**

```bash
git add docs/CHANGELOG.md
git commit -m "docs(changelog): pi user-MCP bridge + the stale-fact lesson"
```

- [ ] **Step 6: 更新 io-onboarding（另一个仓库，单独 push）**

spec §10 要求：`io-onboarding` 的 resident-agent skill 文档中，user-mcp.json 一节需
补充 pi 路线说明（v2 spec §12 建的那一节，当时只写了 claude/codex）。

⚠️ **这不在本仓**。按仓库规约（`CLAUDE.md`「Public docs mirror」），这些公开文档住在
`github.com/teleport-computer/io-onboarding`，必须在该仓的本地 clone 里改并 push 到
那边——**本仓的任何 commit 都不会影响它**。

1. 在 io-onboarding clone 里定位讲 user-mcp.json 的那一节（v2 spec §12 记为
   `skill-resident-agent.md` §2.3；**以该仓实际文件名/章节号为准**，不要凭本文假设）
2. 把「claude 用 `--mcp-config`、codex 用 `config.toml`」的二选一表述，补成三条：
   **pi 由 feedling 的 bridge extension 读取同一个 `user-mcp.json`，把每个 MCP 工具注册
   成 pi 原生工具；对自托管 VPS 用户而言，该文件的位置与格式不变**
3. 在该仓 commit + push

若该仓 clone 不在本机，**不要跳过**——把它作为未完成项报告给用户，不要标记 Task 8 完成。

---

## 遗留缺口（明写不藏）

- **07-16 两文档的订正未做**（spec §8 订正表第 2、3 行）：
  `2026-07-16-user-mcp-network-relaxation-design.md` §2 决策表的「pi 不注入…路线已放弃」，
  以及同名 plan 的 `:891` / `:998` 两处片段。这两个文件是 `user-mcp-auto-ca-fetch` 那条
  线**尚未提交**的产物，只存在于主工作树，不在本分支，因此本次无法订正。
  **承接方式**：待 ca-fetch 收尾提交后，在主工作树按 spec §8 订正表处理——注意那时
  `_user_mcp_ca_env` 已更名为 `_user_mcp_child_env`（本计划 Task 3），历史 plan 片段
  应**加订正注记而非重写**，否则会立刻制造一处新的过期事实。
  ⚠️ 这条**不得**因为「pi 桥已上线」就当作已解决——那句「路线已放弃」还留在 07-16 的
  spec 里，正是它当初误导了实现。

- **`tests/test_agent_runtime_spawners.py` 仍不在 CI**。它含
  `test_pi_models_json_loads_and_enables_reasoning_in_real_pi`，会 `subprocess` 真跑
  `pi --list-models` 且**不 skip**，CI 未装 pi 会直接红。因此本计划改的 pi 模板断言
  （Task 2）**只在本地跑**。要纳入 CI 需先装 pi，可仿 `npm install -g phala@1.1.19
  # pinned (§8 supply-chain)` 的既有模式 pin 到 0.80.3——属独立决策，不在本计划范围。
  **不得**因为 Task 7 加了 setup-node 就认为该文件已被覆盖。
- **typebox 对复杂 JSON Schema 的支持度未逐一验证**。spec §4.2 已记：
  `getValidator` 走 `Compile(schema)`，`$ref` / `oneOf` 等构造的支持度未测。若真实
  用户的 MCP server 用了这类 schema 导致 `validateToolArguments` 抛错，缓解是在
  `tool_mapping.js` 里清洗 schema（而非放弃校验）。**这不是理论风险**——公共 MCP
  server 用 `$ref` 并不罕见，建议 E2E 时特意挑一个 schema 复杂的 server 试。
- **每 chat 回合重做 MCP 握手**的延迟未实测（spec §5 判定可接受，因与 claude 同构）。
  若 E2E 观察到明显启动变慢，再评估在 resident 侧缓存 `tools/list` 结果。
