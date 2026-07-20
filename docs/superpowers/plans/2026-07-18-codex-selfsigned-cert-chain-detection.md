# codex 自签名证书链兼容性检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** codex 用户配单张自签名证书的 MCP server（rustls 会拒）时，把无声失败变成明确信号——纯检测 + consumer 日志 + /test 用户可见警告 + 文档。

**Architecture:** 新增一个只读检测（判服务器叶子证书 `chain[0]` 是否 basicConstraints CA:TRUE），从三处露出：`tools/user_mcp_ca_fetch.py` 复用已抓到的 openssl 链、`backend/hosted/mcp_probe.py` 走 `ssl` CERT_NONE socket 取叶子 DER，两个薄实现不强抽公共层。既有 `_anchor_works`/抓取预算/双CA文件/下游物化一律不动，只新增。

**Tech Stack:** Python 3.11、`cryptography`（后端已有依赖）、pytest、uvicorn+openssl（集成靶子）；iOS Swift（独立仓 feedling-mcp-ios）、xcstrings；fumadocs（docs-site）。

## Global Constraints

- 未经明确授权不得 `git add`/`git commit`——所有 commit 步骤标「待授权」，不自行执行。
- io-onboarding 是独立公开仓，其 push 步骤单独标「待授权」。
- 跑测试前必须起 PG（容器 `feedling-test-pg`, `127.0.0.1:55432`），否则 `tests/conftest.py` 的 `collect_ignore` 静默丢掉需 DB 的模块、"全绿"是假象。
- 标准测试命令：`python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`；每改一个包再跑 `python -m pyflakes <包>`。已知 pre-existing 红 8-9 个（debug_trace/memory 簇），判据=零新增。
- 新纯单元测试文件必须登记进 `tests/conftest.py` 的 `_PURE_UNIT` 集合。
- 不动：`_anchor_works` / 抓取预算 / 双 CA 文件 / `_atomic_write_text` / fail-open / 下游物化（`ca_bundle_pem` 等）。只新增只读检测 + 露出。
- 实测事实（当依据，勿重新推理）：rustls `CaUsedAsEndEntity` 触发条件=服务器叶子证书 `chain[0]` 的 basicConstraints 是 `CA:TRUE`；`openssl req -x509` 默认生成 `CA:TRUE`；CA(CA:TRUE)+叶子(CA:FALSE) 链 codex 实测端到端通过；claude/pi 两种形状都收。
- 目标分支 test。iOS 改动在独立仓 `feedling-mcp-ios`（直接压 main，现状）。
- iOS/文案守 `IO` 非 `Feedling`；Swift view 不用裸 hex/字号，用设计令牌。

---

### Task 1: 纯检测器 + `fetch_anchor_and_leaf_ca`（tools/user_mcp_ca_fetch.py）

**Files:**
- Modify: `tools/user_mcp_ca_fetch.py`（在 `fetch_trust_anchor` 上方加 `leaf_is_ca`，把 `fetch_trust_anchor` 主体抽成 `fetch_anchor_and_leaf_ca` 并让前者变薄包装）
- Test: `tests/test_user_mcp_ca_fetch_leaf.py`（新纯单元）

**Interfaces:**
- Produces:
  - `leaf_is_ca(chain_pems: list[str]) -> bool | None` — 判 `chain_pems[0]`（叶子）是否 CA:TRUE。空链/解析失败→`None`；无 basicConstraints 扩展→`False`。
  - `fetch_anchor_and_leaf_ca(url: str, *, timeout: float = 3.0) -> tuple[str | None, bool | None]` — 返回 `(anchor_pem_or_None, leaf_is_ca_or_None)`；leaf_is_ca 从抓到的链算，即使 anchor 为 None 也照算。
  - `fetch_trust_anchor(url, *, timeout=3.0) -> str | None` — 保持原签名，收敛为 `fetch_anchor_and_leaf_ca(...)[0]`。

- [ ] **Step 1: 写失败测试**

`tests/test_user_mcp_ca_fetch_leaf.py`：
```python
"""leaf_is_ca: 判服务器叶子证书是否 basicConstraints CA:TRUE（rustls CaUsedAsEndEntity 判据）。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import user_mcp_ca_fetch as f  # noqa: E402


def _self_signed_ca_true(tmp_path: Path) -> str:
    # openssl req -x509 默认 CA:TRUE —— 业余用户最常见的单张自签名证书
    crt = tmp_path / "s.crt"
    key = tmp_path / "s.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(crt), "-days", "397", "-nodes", "-subj", "/CN=lone",
         "-addext", "subjectAltName=DNS:localhost"],
        check=True, capture_output=True)
    return crt.read_text()


def _leaf_ca_false(tmp_path: Path) -> str:
    ca_crt, ca_key = tmp_path / "ca.crt", tmp_path / "ca.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(ca_key),
         "-out", str(ca_crt), "-days", "397", "-nodes", "-subj", "/CN=ca"],
        check=True, capture_output=True)
    leaf_key, csr, leaf_crt = tmp_path / "l.key", tmp_path / "l.csr", tmp_path / "l.crt"
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-keyout", str(leaf_key),
         "-out", str(csr), "-nodes", "-subj", "/CN=leaf"],
        check=True, capture_output=True)
    ext = tmp_path / "ext.cnf"
    ext.write_text("basicConstraints=CA:FALSE\nsubjectAltName=DNS:localhost\n")
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt),
         "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(leaf_crt),
         "-days", "397", "-extfile", str(ext)],
        check=True, capture_output=True)
    return leaf_crt.read_text()


def test_lone_self_signed_leaf_is_ca_true(tmp_path):
    assert f.leaf_is_ca([_self_signed_ca_true(tmp_path)]) is True


def test_leaf_ca_false(tmp_path):
    # 链上第一张是叶子(CA:FALSE)，即使后面跟 CA 也只看 chain[0]
    assert f.leaf_is_ca([_leaf_ca_false(tmp_path)]) is False


def test_empty_chain_is_none(tmp_path):
    assert f.leaf_is_ca([]) is None


def test_garbage_pem_is_none(tmp_path):
    assert f.leaf_is_ca(["-----BEGIN CERTIFICATE-----\nnotpem\n-----END CERTIFICATE-----"]) is None


def test_fetch_trust_anchor_unchanged_signature(tmp_path):
    # 薄包装：非 https 仍返回 None
    assert f.fetch_trust_anchor("http://x") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_user_mcp_ca_fetch_leaf.py -v`
Expected: FAIL — `AttributeError: module 'user_mcp_ca_fetch' has no attribute 'leaf_is_ca'`

- [ ] **Step 3: 实现**

在 `tools/user_mcp_ca_fetch.py` 里 `fetch_trust_anchor` 上方加：
```python
def _leaf_is_ca_pem(pem: str) -> bool | None:
    """One cert: is its basicConstraints CA:TRUE? None on parse failure."""
    try:
        from cryptography import x509  # noqa: PLC0415 — backend dep, lazy
        cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed → "don't know", never raise
        return None
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        return bool(bc.ca)
    except x509.ExtensionNotFound:
        return False  # no basicConstraints → not a CA → fine as an end-entity


def leaf_is_ca(chain_pems: list[str]) -> bool | None:
    """The server's LEAF (chain_pems[0]) presented as end-entity: is it a CA
    cert? True ⇒ rustls-based agents (codex) reject it as CaUsedAsEndEntity;
    claude/pi (Node) accept it. None ⇒ empty chain or unparseable (don't warn)."""
    if not chain_pems:
        return None
    return _leaf_is_ca_pem(chain_pems[0])
```
把现有 `fetch_trust_anchor` 主体改名为 `fetch_anchor_and_leaf_ca` 并回传元组，`fetch_trust_anchor` 变薄包装：
```python
def fetch_anchor_and_leaf_ca(url: str, *, timeout: float = 3.0) -> tuple[str | None, bool | None]:
    """Like fetch_trust_anchor but ALSO reports whether the server's leaf is a
    CA cert (the codex/rustls incompatibility signal). Never raises."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None, None
        host = parsed.hostname
        if not host:
            return None, None
        port = parsed.port or 443
    except ValueError:
        return None, None

    if _verifies_against_public_roots(host, port, timeout):
        return None, None  # real cert — not pinned (see _verifies_against_public_roots)

    chain = _fetch_chain(host, port, timeout)
    leaf_ca = leaf_is_ca(chain)
    pem = _pick_trust_anchor(chain)
    if not pem:
        return None, leaf_ca
    if not _is_well_formed_ca(pem):
        return None, leaf_ca
    if not _anchor_works(pem, host, port, timeout):
        return None, leaf_ca
    return pem, leaf_ca


def fetch_trust_anchor(url: str, *, timeout: float = 3.0) -> str | None:
    """A PEM usable as a trust anchor for ``url``, or None. Never raises.
    Thin wrapper over fetch_anchor_and_leaf_ca (which also reports leaf_is_ca)."""
    return fetch_anchor_and_leaf_ca(url, timeout=timeout)[0]
```

- [ ] **Step 4: 登记 _PURE_UNIT**

在 `tests/conftest.py` 的 `_PURE_UNIT` 集合里加 `"test_user_mcp_ca_fetch_leaf"`（照该集合已有条目的字符串形式）。

- [ ] **Step 5: 跑测试确认通过 + pyflakes**

Run: `python -m pytest tests/test_user_mcp_ca_fetch_leaf.py -v`
Expected: 5 passed
Run: `python -m pyflakes tools/user_mcp_ca_fetch.py`
Expected: 无输出

- [ ] **Step 6: 回归——确认既有 fetch 测试没被改坏**

Run: `python -m pytest tests/ -q -k user_mcp_ca_fetch --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: 全绿（既有 fetch_trust_anchor 测试仍通过，签名未变）

- [ ] **Step 7: Commit（待授权）**

```bash
# 待用户授权后执行
git add tools/user_mcp_ca_fetch.py tests/test_user_mcp_ca_fetch_leaf.py tests/conftest.py
git commit -m "feat(user-mcp): detect server leaf cert used as CA (codex/rustls incompat signal)"
```

---

### Task 2: consumer 日志露出（codex 驱动 + 叶子 CA:TRUE → WARNING）

**Files:**
- Modify: `tools/chat_resident_consumer.py`（`_enrich_with_fetched_ca`）
- Test: `tests/test_user_mcp_consumer_leaf_warning.py`（需 import chat_resident_consumer；照既有 consumer 测试的 PG/import 姿势，见 `tests/test_chat_poll_client_release.py` 等）

**Interfaces:**
- Consumes: Task 1 的 `fetch_anchor_and_leaf_ca(url) -> tuple[str|None, bool|None]`；既有 `_cli_template_is_codex() -> bool`（`tools/chat_resident_consumer.py:4017`）。
- Produces: `_enrich_with_fetched_ca` 行为不变（仍返回填了 ca_pem 的 servers），但 codex 驱动下遇到叶子 CA:TRUE 的 enabled server 会 `log.warning`。

- [ ] **Step 1: 写失败测试**

`tests/test_user_mcp_consumer_leaf_warning.py`：
```python
"""codex 驱动下，某 enabled MCP server 用单张自签名证书(叶子 CA:TRUE)时 consumer 打警告。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import chat_resident_consumer as c  # noqa: E402


def _fake_fetch(leaf_ca):
    # 返回 (anchor, leaf_is_ca)——anchor 给个占位 PEM，leaf_ca 由参数控
    return lambda url: ("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----", leaf_ca)


def test_codex_lone_cert_warns(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: True)
    servers = [{"name": "probe", "enabled": True, "url": "https://h:9443/mcp", "ca_pem": ""}]
    with caplog.at_level("WARNING"):
        c._enrich_with_fetched_ca(servers, fetch=_fake_fetch(True))
    assert any("probe" in r.message and "codex" in r.message.lower()
               and ("chain" in r.message.lower() or "叶子" in r.message or "CA" in r.message)
               for r in caplog.records)


def test_codex_proper_chain_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: True)
    servers = [{"name": "probe", "enabled": True, "url": "https://h:9443/mcp", "ca_pem": ""}]
    with caplog.at_level("WARNING"):
        c._enrich_with_fetched_ca(servers, fetch=_fake_fetch(False))
    assert not any("codex" in r.message.lower() for r in caplog.records)


def test_claude_lone_cert_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: False)
    servers = [{"name": "probe", "enabled": True, "url": "https://h:9443/mcp", "ca_pem": ""}]
    with caplog.at_level("WARNING"):
        c._enrich_with_fetched_ca(servers, fetch=_fake_fetch(True))
    assert not any("codex" in r.message.lower() for r in caplog.records)


def test_manual_ca_pem_skips_fetch(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: True)
    # 手贴 ca_pem 的 server 不抓取也不判 leaf → 不警告
    called = {"n": 0}
    def fetch(url):
        called["n"] += 1
        return (None, True)
    servers = [{"name": "probe", "enabled": True, "url": "https://h/mcp", "ca_pem": "PINNED"}]
    c._enrich_with_fetched_ca(servers, fetch=fetch)
    assert called["n"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_user_mcp_consumer_leaf_warning.py -v`
Expected: FAIL — `_fake_fetch` 返回元组，但现 `_enrich_with_fetched_ca` 把返回值当 `str` 用（`ca = fetch(...) or ""`），警告断言也不成立。

- [ ] **Step 3: 实现**

改 `tools/chat_resident_consumer.py` 的 `_enrich_with_fetched_ca`：默认注入换成 `fetch_anchor_and_leaf_ca`，解包元组，codex 且 leaf_ca True 时警告。改动点（在函数体内）：
```python
    if fetch is None:
        import user_mcp_ca_fetch  # noqa: PLC0415 — sibling on tools/ path
        fetch = user_mcp_ca_fetch.fetch_anchor_and_leaf_ca
    deadline = now() + budget_s
    is_codex = _cli_template_is_codex()
    out = []
    for s in servers:
        ca = s.get("ca_pem") or ""
        if not ca and s.get("enabled") and now() < deadline:
            try:
                fetched, leaf_ca = fetch(s.get("url") or "")
                ca = fetched or ""
                if is_codex and leaf_ca is True:
                    log.warning(
                        "[user_mcp] server %r presents a single self-signed "
                        "certificate (leaf is a CA); codex (rustls) will reject "
                        "it as CaUsedAsEndEntity — regenerate it as a CA + "
                        "server-leaf chain. claude/pi accept it as-is.",
                        s.get("name"))
            except Exception as e:  # noqa: BLE001 — never wedge materialization
                log.warning("[user_mcp] ca fetch failed for %s: %s: %s",
                            s.get("name"), type(e).__name__, e)
                ca = ""
        out.append({**s, "ca_pem": ca})
    return out
```
更新该函数 docstring 里 `fetch` 注入的说明（现在返回 `(anchor, leaf_is_ca)` 元组，不再是 str）。

- [ ] **Step 4: 跑测试确认通过 + pyflakes**

Run: `python -m pytest tests/test_user_mcp_consumer_leaf_warning.py -v`
Expected: 4 passed
Run: `python -m pyflakes tools/chat_resident_consumer.py`
Expected: 无输出（或仅 pre-existing）

- [ ] **Step 5: 回归——既有 enrich/预算测试**

Run: `python -m pytest tests/ -q -k "enrich or user_mcp" --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: 全绿。特别核对既有 `_enrich_with_fetched_ca` 预算/disabled/fail-open 测试——它们若用旧的 `fetch=lambda url: "PEM"`（返回 str）会因解包失败而红；若如此，那是既有测试桩需同步改成返回 `(pem, None)`。**这是预期的桩更新，不是新回归**，一并改掉并在 commit message 注明。

- [ ] **Step 6: Commit（待授权）**

```bash
# 待授权
git add tools/chat_resident_consumer.py tests/test_user_mcp_consumer_leaf_warning.py
# 若同步改了既有 enrich 测试桩，一并 add
git commit -m "feat(consumer): warn when codex driver meets a single self-signed MCP cert"
```

---

### Task 3: 后端 /test 用户可见警告（仅 hosted codex）

**Files:**
- Modify: `backend/hosted/mcp_probe.py`（加 `leaf_is_ca(url)`；确认 `import socket`）
- Modify: `backend/hosted/mcp_core.py`（`test_server` 加 driver 查询 + 新 kind）
- Test: `tests/test_mcp_core_codex_cert_warning.py`（需 PG）

**Interfaces:**
- Produces:
  - `mcp_probe.leaf_is_ca(url: str, *, timeout: float = 3.0) -> bool | None` — CERT_NONE socket 取叶子 DER 判 CA:TRUE。SSRF 沿用 `blocked_url_kind` 前置；失败/无证书→None。
  - `test_server` 在 driver=="codex" 且 `leaf_is_ca(url) is True` 时返回 `_err("codex_cert_chain_required", ...)`, 400，盖过 `tls`。

- [ ] **Step 1: 写失败测试（probe 检测器，纯单元不需 PG）**

`tests/test_mcp_core_codex_cert_warning.py`：
```python
"""mcp_probe.leaf_is_ca + test_server 的 codex_cert_chain_required kind。"""
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from hosted import mcp_probe  # noqa: E402


def _serve_tls(tmp_path, cert_pem_files):
    """起一个 TLS server(单张 or 链)，返回 (port, stop)。用 http.server + ssl 最小化。"""
    import http.server
    import ssl as _ssl
    crt, key = cert_pem_files
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))
    httpd = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.handle_request, daemon=True)  # 一次即可
    t.start()
    return port, httpd


def _lone(tmp_path):
    crt, key = tmp_path / "s.crt", tmp_path / "s.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout",
                    str(key), "-out", str(crt), "-days", "397", "-nodes",
                    "-subj", "/CN=lone", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    return crt, key


def test_leaf_is_ca_true_for_lone_self_signed(tmp_path):
    crt, key = _lone(tmp_path)
    port, httpd = _serve_tls(tmp_path, (crt, key))
    try:
        assert mcp_probe.leaf_is_ca(f"https://127.0.0.1:{port}/mcp") is True
    finally:
        httpd.server_close()


def test_leaf_is_ca_none_when_unreachable():
    assert mcp_probe.leaf_is_ca("https://127.0.0.1:1/mcp") is None


def test_leaf_is_ca_none_for_blocked_url():
    # 非 global 地址：blocked_url_kind 前置闸 → None（不拨号）
    assert mcp_probe.leaf_is_ca("https://169.254.169.254/mcp") is None
```
（`test_server` 的 DB 级测试放 Step 7，先把纯检测器立起来。）

- [ ] **Step 2: 跑测试确认失败**

Run（先起 PG）：`python -m pytest tests/test_mcp_core_codex_cert_warning.py -v -k leaf_is_ca`
Expected: FAIL — `AttributeError: module 'hosted.mcp_probe' has no attribute 'leaf_is_ca'`

- [ ] **Step 3: 实现 mcp_probe.leaf_is_ca**

`backend/hosted/mcp_probe.py`（确认文件顶已 `import socket, ssl`；`urlparse` 已 import）：
```python
def leaf_is_ca(url: str, *, timeout: float = 3.0) -> bool | None:
    """Does the server's LEAF cert have basicConstraints CA:TRUE? Read-only:
    a CERT_NONE handshake just to inspect the presented cert (never trusts it).
    True ⇒ a rustls-based agent (codex) will reject it as CaUsedAsEndEntity.
    None ⇒ non-global/unreachable/no cert (don't warn). SSRF guard reused."""
    if blocked_url_kind(url):
        return None
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or 443
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
        if not der:
            return None
        from cryptography import x509  # noqa: PLC0415 — backend dep, lazy
        cert = x509.load_der_x509_certificate(der)
        try:
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
            return bool(bc.ca)
        except x509.ExtensionNotFound:
            return False
    except Exception:  # noqa: BLE001 — read-only probe, never raise
        return None
```

- [ ] **Step 4: 跑 probe 检测器测试确认通过**

Run: `python -m pytest tests/test_mcp_core_codex_cert_warning.py -v -k leaf_is_ca`
Expected: 3 passed

- [ ] **Step 5: 写 test_server 的 driver-gated 失败测试**

在同文件加（需 PG + 真 store；照 `tests/` 里既有 mcp_core DB 测试的 fixture 姿势——找一个已有的 `test_mcp_*` 或用 `make_client`/store fixture）：
```python
def test_test_server_codex_lone_cert_returns_kind(monkeypatch, mcp_store):
    # mcp_store: 一个已 upsert 了 name=probe（url 指向单张自签名 server）的 store fixture
    from hosted import mcp_core
    monkeypatch.setattr(mcp_core, "_user_driver", lambda store, key: "codex")
    monkeypatch.setattr(mcp_probe, "leaf_is_ca", lambda url, **k: True)
    # probe 本身对自签名会 tls 失败
    monkeypatch.setattr(mcp_probe, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(mcp_probe.ProbeError("tls", "self-signed")))
    body, status = mcp_core.test_server(mcp_store, "probe", "api-key")
    assert status == 400
    assert body["error"]["kind"] == "codex_cert_chain_required"


def test_test_server_claude_lone_cert_stays_tls(monkeypatch, mcp_store):
    from hosted import mcp_core
    monkeypatch.setattr(mcp_core, "_user_driver", lambda store, key: "claude")
    monkeypatch.setattr(mcp_probe, "leaf_is_ca", lambda url, **k: True)
    monkeypatch.setattr(mcp_probe, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(mcp_probe.ProbeError("tls", "self-signed")))
    body, status = mcp_core.test_server(mcp_store, "probe", "api-key")
    assert body["error"]["kind"] == "tls"
```
（`mcp_store` fixture：若无现成，在本测试文件里用既有 store fixture + `mcp_core.upsert_server(store, {...})` 造一条 name=probe 记录。）

- [ ] **Step 6: 跑确认失败**

Run: `python -m pytest tests/test_mcp_core_codex_cert_warning.py -v -k test_server`
Expected: FAIL — `_user_driver` 不存在 / kind 不是 codex_cert_chain_required

- [ ] **Step 7: 实现 test_server 改动**

`backend/hosted/mcp_core.py`：加 `_user_driver` 助手（lazy import 避免循环）+ 改 `test_server` 的 except 分支。
```python
def _user_driver(store: UserStore, caller_api_key: str | None) -> str:
    """This user's agent driver, or '' when it can't be determined (VPS /
    unconfigured — then no codex-specific warning is emitted)."""
    try:
        from hosted import agent_runtime_cutover, hosted_config_store  # noqa: PLC0415
        cfg = hosted_config_store._load_runtime_provider_config(store, caller_api_key)
        return agent_runtime_cutover.driver_for_provider(str((cfg or {}).get("provider") or ""))
    except Exception:  # noqa: BLE001 — driver unknown ⇒ no warning, never 500
        return ""
```
`test_server` 的 probe 调用改为：
```python
    driver = _user_driver(store, caller_api_key)
    try:
        out = mcp_probe.probe(secret["url"], secret.get("headers") or {},
                              ca_pem=secret.get("ca_pem"))
    except mcp_probe.ProbeError as e:
        # codex(rustls) 无法用单张自签名证书；此时 "agent 会自己处理" 的 tls 文案是错的。
        if (e.kind == "tls" and driver == "codex"
                and mcp_probe.leaf_is_ca(secret["url"]) is True):
            return _err("codex_cert_chain_required",
                        "single self-signed cert; codex needs a CA+leaf chain"), 400
        return _err(e.kind, e.detail), 400
    return out, 200
```

- [ ] **Step 8: 跑确认通过 + pyflakes**

Run: `python -m pytest tests/test_mcp_core_codex_cert_warning.py -v`
Expected: 全 passed
Run: `python -m pyflakes backend/hosted/mcp_probe.py backend/hosted/mcp_core.py`
Expected: 无输出

- [ ] **Step 9: 契约同步——OpenAPI/文档提及新 error kind**

若 `/v1/mcp/servers/{name}/test` 的错误 kind 在 `docs-site/openapi` 或 overrides 里有枚举，加上 `codex_cert_chain_required`。查：`grep -rn "unreachable_from_backend\|invalid_ca" docs-site/`。有则补，并 `cd docs-site && npm run openapi:generate` 后核对 diff。无枚举则跳过。

- [ ] **Step 10: 全量回归**

Run（PG 起着）：`python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: 相比基线零新增红（pre-existing 8-9 条不算）。

- [ ] **Step 11: Commit（待授权）**

```bash
# 待授权
git add backend/hosted/mcp_probe.py backend/hosted/mcp_core.py tests/test_mcp_core_codex_cert_warning.py
# 若改了 docs-site/openapi 一并 add
git commit -m "feat(mcp): /test warns hosted codex users when server uses a single self-signed cert"
```

---

### Task 4: iOS 文案映射（独立仓 feedling-mcp-ios）

**Files:**
- Modify: `feedling-mcp-ios/App/FeedlingTest/API/FeedlingAPI.swift`（`SceneErrorCopy.mcpMessage`，~273-288 那段 kind 映射）
- Modify: `feedling-mcp-ios/App/FeedlingTest/Localizable.xcstrings`（新增 `scene.error.mcp.codex_cert_chain_required`，en+zh-Hans）

**Interfaces:**
- Consumes: Task 3 的 error kind 字符串 `codex_cert_chain_required`。

- [ ] **Step 1: 加 kind 映射（specific-before-catch-all）**

在 `SceneErrorCopy.mcpMessage` 里，把新 kind 放到 `invalid_ca`/`ca_too_large` 附近、**在** `unreachable_from_backend`/`tls` 这些泛化 catch **之前**（该文件已有"specific-before-catch-all"纪律）：
```swift
if combined.contains("codex_cert_chain_required") { return "scene.error.mcp.codex_cert_chain_required".localized }
```
核对：`codex_cert_chain_required` 不是任何既有 kind 的子串、也不含任何既有 kind 作子串（避免误命中）。

- [ ] **Step 2: 加本地化串**

`Localizable.xcstrings` 新增 key `scene.error.mcp.codex_cert_chain_required`，两语（守 IO 非 Feedling）：
- en: `Saved — but this looks like a single self-signed certificate, and codex can't use it. Regenerate it as a certificate chain (a CA certificate plus a separate server certificate it signs). Claude and other agents can use a single cert as-is.`
- zh-Hans: `已保存 — 但这看起来是单张自签名证书，codex 用不了。请把它重新生成为证书链（一张 CA 证书 + 一张被它签的服务器证书）。Claude 等 agent 可以直接用单张证书。`

- [ ] **Step 3: 校验 xcstrings 合法 + 覆盖**

Run: `python3 -c "import json; d=json.load(open('App/FeedlingTest/Localizable.xcstrings')); k='scene.error.mcp.codex_cert_chain_required'; locs=d['strings'][k]['localizations']; print('en+zh:', set(locs)>={'en','zh-Hans'})"`（在 iOS 仓根跑）
Expected: `en+zh: True`

- [ ] **Step 4: 编译**

Run（iOS 仓 `App/`）：`xcodebuild -project FeedlingTest.xcodeproj -scheme FeedlingTest -destination 'platform=iOS Simulator,name=iPhone 17' -configuration Debug build`
Expected: `** BUILD SUCCEEDED **`

- [ ] **Step 5: Commit（待授权，独立仓）**

```bash
# 待授权；在 feedling-mcp-ios 仓
git add App/FeedlingTest/API/FeedlingAPI.swift App/FeedlingTest/Localizable.xcstrings
git commit -m "feat(mcp): surface codex_cert_chain_required as an amber warning"
```

---

### Task 5: 文档

**Files:**
- Modify: `docs-site/content/docs/workflows/mcp.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify（**独立公开仓 io-onboarding**）: `skill-resident-agent.md`

- [ ] **Step 1: mcp.mdx 加 codex 证书链约束**

在 mcp.mdx 的自签名/CA 章节加一段（含最小 openssl 配方）：
> codex 用户注意：codex 的 TLS 栈（rustls）拒绝把一张 CA 证书直接当服务器证书用。如果你的 MCP server 用**单张自签名证书**（`openssl req -x509` 默认生成的那种），codex 连不上（claude 等 agent 可以）。请改用**证书链**：一张自建 CA 证书 + 一张被它签发的服务器证书（`basicConstraints=CA:FALSE`，带 `subjectAltName`）。
> ```bash
> openssl req -x509 -newkey rsa:2048 -keyout ca.key -out ca.crt -days 397 -nodes -subj "/CN=my-mcp-ca"
> openssl req -newkey rsa:2048 -keyout server.key -out server.csr -nodes -subj "/CN=my-mcp"
> printf 'basicConstraints=CA:FALSE\nsubjectAltName=DNS:your.host\n' > ext.cnf
> openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 397 -extfile ext.cnf
> # server 呈上 fullchain（server.crt 后接 ca.crt），私钥用 server.key
> cat server.crt ca.crt > fullchain.crt
> ```

- [ ] **Step 2: changelog.mdx Unreleased 加条目**

在 `docs-site/content/docs/changelog.mdx` 的 Unreleased/Behavior 下加：
> **codex + self-signed MCP servers:** codex's TLS stack (rustls) rejects a CA certificate used directly as a server certificate, so a single self-signed cert works with claude/pi but not codex. Configure such servers with a certificate CHAIN (a CA cert plus a separate server cert it signs). `POST /v1/mcp/servers/{name}/test` now returns `codex_cert_chain_required` (instead of a generic `tls` result) when a codex-driven account tests a server presenting a lone self-signed cert. See [MCP servers](/docs/workflows/mcp).

- [ ] **Step 3: self-hosting.mdx 交叉引用**

在 self-hosting.mdx 里已有的 MCP/openssl 相关处加一句：自托管 codex 用户的自签名 MCP server 需 CA+叶子链（单张证书 codex 用不了），详见 [MCP servers](/docs/workflows/mcp)。

- [ ] **Step 4: docs-site 校验**

Run（`docs-site/`）：`npm run types:check && npm run lint && npm run build`
若改了 openapi：`npm run openapi:generate` 并核对 diff。
Expected: 三项绿。

- [ ] **Step 5: io-onboarding skill-resident-agent.md（独立公开仓）**

在本地 io-onboarding clone 的 `skill-resident-agent.md` 里加同样的 codex 证书链约束（面向 agent 的措辞）。**编辑可做，push 待授权**（公开仓外发动作）。

- [ ] **Step 6: Commit（待授权）**

```bash
# 待授权（本仓）
git add docs-site/content/docs/workflows/mcp.mdx docs-site/content/docs/changelog.mdx docs-site/content/docs/self-hosting.mdx
# 若 openapi 重生成一并 add
git commit -m "docs(mcp): document codex requires a CA+leaf cert chain for self-signed servers"
# io-onboarding：单独仓、单独授权后 push
```

---

## Self-Review

**1. Spec coverage:**
- 检测（叶子 CA:TRUE，cryptography，两薄实现）→ Task 1（tools）+ Task 3 Step 3（backend）✓
- 露出①consumer 日志 → Task 2 ✓
- 露出②/test 用户可见警告（driver-gated codex、盖过 tls）→ Task 3 ✓
- 露出③iOS 文案 → Task 4 ✓
- Fix 1 文档（mcp.mdx/changelog/self-hosting/io-onboarding）→ Task 5 ✓
- 不动 `_anchor_works`/预算/双CA/物化 → Task 1/2 均只新增，未触 ✓
- VPS codex 后端不知 driver 的边界 → `_user_driver` 返回 '' ⇒ 无警告（Task 3 Step 7）✓
- 集成靶子（单张 vs CA+叶子）→ Task 1/Task 3 测试用 openssl 造两套证书 ✓

**2. Placeholder scan:** 无 TBD/TODO；每个 code step 有完整代码；测试有真实断言。`mcp_store` fixture 在 Task 3 Step 5 给了兜底造法（若无现成 fixture 就地 upsert）。

**3. Type consistency:**
- `fetch_anchor_and_leaf_ca -> tuple[str|None, bool|None]`：Task 1 定义、Task 2 解包 `fetched, leaf_ca`、Task 2 测试桩 `_fake_fetch` 返回同形元组 ✓
- `leaf_is_ca`：tools 版签名 `list[str]->bool|None`、probe 版 `str->bool|None`——**同名不同参**，属两薄实现（spec 明说不强抽公共层），各自测试用各自签名，无交叉调用 ✓
- error kind 字符串 `codex_cert_chain_required`：Task 3 产出、Task 4 消费、Task 5 changelog 提及——三处逐字一致 ✓
- `_user_driver(store, caller_api_key)`：Task 3 Step 7 定义、Step 5 测试 monkeypatch 同签名 ✓

无 gap。计划可执行。
