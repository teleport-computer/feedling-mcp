# BYOK 模型目录 · 后端「列模型」接口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增旁路接口 `POST /v1/model_api/models`——给定 provider + 凭据（新填 key 或已存 credential_id），去服务商 `/models` 拉真实模型清单、归一化成 `[{id, display_name}]` 返回；供 iOS 加 key 时实时选模型。

**Architecture:** 三层，严格按 `CONTRIBUTING.md` 分层：wire 逻辑（拉取/翻页/解析/错误分类）落在叶子层 `backend/provider_client.py`，与现有各 wire 一样拆成「纯 builder + 纯 parser + 网络编排」；业务编排（XOR 校验、credential 解封、provider 归一、错误→slug）落在 `backend/hosted/setup_core.py`；HTTP 路由 boilerplate 落在 `backend/hosted/setup_routes_asgi.py`。全程**纯新增旁路**，不改任何现有函数、不动加密信封。

**Tech Stack:** Python / FastAPI（APIRouter）/ httpx（复用 `provider_client._http_client()` 共享同步 client）/ pytest（fake-httpx monkeypatch + `asgi_test_client.make_client`）。无 Pydantic（本仓契约是手写 JSON-Schema）。

## Global Constraints

以下为项目级铁律，每个 Task 隐含包含：

- **纯旁路，正常路径逐字节不变**：只新增函数/路由，不改现有 `chat_completion`/`test_provider_key`/`model_api_route_create` 等任何一行。
- **不动加密**：credential_id 路径只**读取**信封解密拿 provider key（复用 `core_enclave._decrypt_envelope_via_enclave(..., purpose="model_api_provider_key")`），绝不改 envelope id / AAD / K_enclave。
- **跨模块用模块属性调用**：`import provider_client` 后 `provider_client.list_provider_models(...)`；**禁止** `from provider_client import ...`（否则 monkeypatch 到不了所有调用点，`CONTRIBUTING.md:128-137`）。
- **错误纪律**（`CONTRIBUTING.md` §7 / `:201-206`）：所有错误返回 `{"error": "<snake_case_slug>", ...}`，动态内容只能进 `detail`；新 slug 必须同 PR 登记进 `docs/API_ERRORS.md`（`tests/test_api_errors_doc.py` 守卫）。slug 一旦上线冻结。
- **provider 鉴权失败绝不回 401**：接口自身的 401 是 Feedling 登录失效；provider key 失败一律映射成 400/429/503，避免 iOS 误判成登录过期。
- **Gemini key 走 header** `x-goog-api-key`，不进 URL query。
- **上限常量**（防自定义端点拖垮 worker）：`_CATALOG_PER_REQUEST_TIMEOUT = 15.0`、`_CATALOG_MAX_PAGES = 10`、`_CATALOG_MAX_MODELS = 2000`、`_CATALOG_MAX_BODY_BYTES = 5_000_000`、model id 上限 160（与保存约束一致）。
- **conftest 白名单**：新的**纯单元**测试文件（fake-httpx、不碰 DB）必须加进 `tests/conftest.py` 的 `_PURE_UNIT`（`:109-180`），否则无 Postgres 时静默不收集、"全绿"是假的；用 `make_client()`/真实 DB 的测试**不要**加进去（会把优雅跳过变成硬收集错误）。

**7 个 provider**（`provider_client.validate_config:299`）：`openai / openrouter / anthropic / bedrock / gemini / deepseek / openai_compatible`。各家 `/models` 拉取方式与翻页见 Task 1。

---

## 文件结构

- `backend/provider_client.py`（改）：新增叶子层函数
  - `_catalog_request(provider, api_key, base_url, cursor) -> tuple[str, dict, dict]`（纯：url, headers, params）
  - `_parse_catalog_page(provider, body) -> tuple[list[dict], str | None]`（纯：`([{id,display_name}], next_cursor)`）
  - `list_provider_models(provider, api_key, base_url="") -> dict`（网络编排：翻页/上限/去重，返回 `{"models":[...], "complete":bool, "warnings":[...], "catalog_supported":bool}`）
  - `model_catalog_error_slug(exc) -> str`（纯：ProviderError → slug）
- `backend/hosted/setup_core.py`（改）：新增 `model_api_models(store, payload, *, caller_api_key) -> tuple[dict, int]`
- `backend/hosted/setup_routes_asgi.py`（改）：新增 `@router.post("/v1/model_api/models")` handler
- `tools/public_openapi_contracts.py`（改）：登记 request/response schema + 描述
- `docs/API_ERRORS.md`（改）：登记 `model_catalog_*` slug 行
- 测试：
  - `tests/test_provider_catalog_unit.py`（新，**纯单元**，加进 `_PURE_UNIT`）：Task 1、2、3 的纯函数 + fake-httpx 编排
  - `tests/test_model_api_models_route.py`（新，`make_client`，**不进**白名单）：Task 4 的路由/core 集成

---

## Task 1: wire 层——每家 `/models` 的纯请求构造 + 纯解析

**Files:**
- Modify: `backend/provider_client.py`（在现有 wire helper 区尾部、`test_provider_key` 之前新增）
- Test: `tests/test_provider_catalog_unit.py`（新建）

**Interfaces:**
- Produces:
  - `_catalog_request(provider: str, api_key: str, base_url: str, cursor: str | None) -> tuple[str, dict, dict]` — 返回 `(url, headers, params)`。`base_url` 已是归一化后的默认或用户值。
  - `_parse_catalog_page(provider: str, body: dict) -> tuple[list[dict], str | None]` — 返回 `(models, next_cursor)`，`models` 每项 `{"id": str, "display_name": str}`，`next_cursor` 为 None 表示无下一页。
  - 常量 `_CATALOG_MODELS_PATH`（各家 path 后缀）内联在函数里即可，无需单独导出。

各家规格（实现依据）：

| provider(归一化后) | path | 认证 header | 翻页参数 | body 形状 | id 取自 |
|---|---|---|---|---|---|
| openai / openrouter / deepseek / openai_compatible | `{base}/models` | `Authorization: Bearer {key}`（openrouter 另加 `HTTP-Referer`/`X-Title`，见 `_headers`） | 无（单页） | `{"data":[{"id","name"?}]}` | `data[].id`；display=`name` or id |
| anthropic | `{base}/models?limit=1000` | `x-api-key`+`anthropic-version: 2023-06-01` | `has_more`→`?after_id={last_id}` | `{"data":[{"id","display_name"?}],"has_more","last_id"}` | `data[].id`；display=`display_name` or id |
| gemini | `{base}/models?pageSize=1000` | `x-goog-api-key: {key}` | `nextPageToken`→`?pageToken=` | `{"models":[{"name":"models/xxx","displayName"?}],"nextPageToken"?}` | `name` 去掉 `models/` 前缀；display=`displayName` or id |
| bedrock | —（不支持 bearer /models） | — | — | — | 抛 `ProviderError("model_catalog_unsupported")` |

- [ ] **Step 1: 写失败测试**（构造 + 解析，纯函数）

```python
# tests/test_provider_catalog_unit.py
import provider_client as pc


def test_catalog_request_openai_compatible_bearer():
    url, headers, params = pc._catalog_request(
        "openai_compatible", "sk-x", "https://api.example.com/v1", None)
    assert url == "https://api.example.com/v1/models"
    assert headers["Authorization"] == "Bearer sk-x"
    assert params == {}


def test_catalog_request_gemini_key_in_header_not_query():
    url, headers, params = pc._catalog_request(
        "gemini", "AIza-x", "https://generativelanguage.googleapis.com/v1beta", None)
    assert url.endswith("/models")
    assert headers["x-goog-api-key"] == "AIza-x"
    assert "AIza-x" not in url and "key" not in params  # 密钥不进 URL


def test_catalog_request_anthropic_headers_and_cursor():
    url, headers, params = pc._catalog_request(
        "anthropic", "sk-ant", "https://api.anthropic.com/v1", "msg_123")
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"
    assert params.get("after_id") == "msg_123"


def test_parse_catalog_page_openai_data_shape():
    body = {"data": [{"id": "gpt-5.4", "name": "GPT-5.4"}, {"id": "o5"}]}
    models, nxt = pc._parse_catalog_page("openai", body)
    assert models == [{"id": "gpt-5.4", "display_name": "GPT-5.4"},
                      {"id": "o5", "display_name": "o5"}]
    assert nxt is None


def test_parse_catalog_page_gemini_strips_prefix_and_paginates():
    body = {"models": [{"name": "models/gemini-3.1-pro", "displayName": "Gemini 3.1 Pro"}],
            "nextPageToken": "tok2"}
    models, nxt = pc._parse_catalog_page("gemini", body)
    assert models == [{"id": "gemini-3.1-pro", "display_name": "Gemini 3.1 Pro"}]
    assert nxt == "tok2"


def test_parse_catalog_page_anthropic_has_more():
    body = {"data": [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}],
            "has_more": True, "last_id": "claude-opus-5"}
    models, nxt = pc._parse_catalog_page("anthropic", body)
    assert models == [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}]
    assert nxt == "claude-opus-5"


def test_catalog_request_bedrock_unsupported():
    import pytest
    with pytest.raises(pc.ProviderError) as ei:
        pc._catalog_request("bedrock", "k", "", None)
    assert "model_catalog_unsupported" in str(ei.value)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_provider_catalog_unit.py -q`
Expected: FAIL（`_catalog_request` / `_parse_catalog_page` 未定义）

- [ ] **Step 3: 实现纯函数**

```python
# backend/provider_client.py —— 新增（放在 test_provider_key 之前）
from urllib.parse import quote  # 若文件顶部已 import 则复用，勿重复

_CATALOG_PER_REQUEST_TIMEOUT = 15.0
_CATALOG_MAX_PAGES = 10
_CATALOG_MAX_MODELS = 2000
_CATALOG_MAX_BODY_BYTES = 5_000_000

_CATALOG_BEARER_PROVIDERS = {"openai", "openrouter", "deepseek", "openai_compatible"}


def _catalog_request(provider: str, api_key: str, base_url: str,
                     cursor: str | None) -> tuple[str, dict, dict]:
    provider = normalize_provider(provider)
    base = (base_url or default_base_url(provider)).rstrip("/")
    if provider == "bedrock":
        raise ProviderError("model_catalog_unsupported")
    url = f"{base}/models"
    params: dict = {}
    if provider in _CATALOG_BEARER_PROVIDERS:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://feedling.app"
            headers["X-Title"] = "Feedling IO Hosted Runtime"
    elif provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        params["limit"] = 1000
        if cursor:
            params["after_id"] = cursor
    elif provider == "gemini":
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        params["pageSize"] = 1000
        if cursor:
            params["pageToken"] = cursor
    else:
        raise ProviderError("model_catalog_unsupported")
    return url, headers, params


def _parse_catalog_page(provider: str, body: dict) -> tuple[list[dict], str | None]:
    provider = normalize_provider(provider)
    out: list[dict] = []
    if provider == "gemini":
        for m in (body.get("models") or []):
            name = str(m.get("name") or "")
            mid = name.split("/", 1)[1] if name.startswith("models/") else name
            if not mid:
                continue
            out.append({"id": mid, "display_name": str(m.get("displayName") or mid)})
        nxt = body.get("nextPageToken") or None
        return out, (str(nxt) if nxt else None)
    # openai / openrouter / deepseek / openai_compatible / anthropic 都是 data[]
    for m in (body.get("data") or []):
        mid = str(m.get("id") or "")
        if not mid:
            continue
        disp = m.get("display_name") or m.get("name") or mid
        out.append({"id": mid, "display_name": str(disp)})
    nxt = None
    if provider == "anthropic" and body.get("has_more"):
        nxt = str(body.get("last_id") or "") or None
    return out, nxt
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_provider_catalog_unit.py -q`
Expected: PASS

- [ ] **Step 5: 把新测试文件加进 conftest 白名单**

在 `tests/conftest.py` 的 `_PURE_UNIT` 集合（`:109-180`）里，紧挨 `"test_provider_client.py",` 加一行：

```python
    "test_provider_catalog_unit.py",
```

Run: `python -m pytest tests/test_provider_catalog_unit.py --collect-only -q`
Expected: 6 个用例都被收集（确认白名单生效）

- [ ] **Step 6: Commit**

```bash
git add backend/provider_client.py tests/test_provider_catalog_unit.py tests/conftest.py
git commit -m "feat(provider): catalog request/parse wire helpers for /models listing"
```

---

## Task 2: `list_provider_models` 网络编排（翻页 + 上限 + 去重）

**Files:**
- Modify: `backend/provider_client.py`
- Test: `tests/test_provider_catalog_unit.py`（追加，fake-httpx）

**Interfaces:**
- Consumes: `_catalog_request`, `_parse_catalog_page`（Task 1）；`_http_client()`（现有 `:169`）。
- Produces: `list_provider_models(provider: str, api_key: str, base_url: str = "") -> dict` — 返回
  `{"models": [{"id","display_name"}], "complete": bool, "warnings": [str], "catalog_supported": bool}`。
  成功但空 → `models=[]`、`complete=True`；不支持（bedrock/无 /models）→ `catalog_supported=False`、`models=[]`；上游失败 → 抛 `ProviderError`（带 `status_code`）。

- [ ] **Step 1: 写失败测试**（fake client，覆盖翻页/去重/上限/不支持）

```python
# tests/test_provider_catalog_unit.py —— 追加
class _FakeResp:
    def __init__(self, status, body, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else "{}"
    def json(self):
        return self._body


def _install_fake_get(monkeypatch, pages):
    """pages: list of (status, body) 依次返回。"""
    seq = list(pages)
    calls = []
    class FakeClient:
        def __init__(self, *a, **k): pass
        def get(self, url, *, headers=None, params=None, timeout=None):
            calls.append({"url": url, "params": params or {}})
            status, body = seq.pop(0)
            return _FakeResp(status, body)
    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    return calls


def test_list_models_openrouter_single_page(monkeypatch):
    _install_fake_get(monkeypatch, [(200, {"data": [{"id": "a"}, {"id": "b"}]})])
    res = pc.list_provider_models("openrouter", "k", "")
    assert res["catalog_supported"] is True and res["complete"] is True
    assert [m["id"] for m in res["models"]] == ["a", "b"]


def test_list_models_anthropic_paginates_and_dedupes(monkeypatch):
    calls = _install_fake_get(monkeypatch, [
        (200, {"data": [{"id": "x"}], "has_more": True, "last_id": "x"}),
        (200, {"data": [{"id": "x"}, {"id": "y"}], "has_more": False}),
    ])
    res = pc.list_provider_models("anthropic", "k", "")
    assert [m["id"] for m in res["models"]] == ["x", "y"]   # 去重、稳定序
    assert res["complete"] is True
    assert calls[1]["params"].get("after_id") == "x"        # 第二页带 cursor


def test_list_models_bedrock_unsupported(monkeypatch):
    res = pc.list_provider_models("bedrock", "k", "")
    assert res["catalog_supported"] is False and res["models"] == []


def test_list_models_page_cap_marks_incomplete(monkeypatch):
    # 永远 has_more 的死循环，应在 _CATALOG_MAX_PAGES 处停下并 complete=False
    monkeypatch.setattr(pc, "_CATALOG_MAX_PAGES", 3)
    pages = [(200, {"data": [{"id": f"m{i}"}], "has_more": True, "last_id": f"m{i}"})
             for i in range(10)]
    _install_fake_get(monkeypatch, pages)
    res = pc.list_provider_models("anthropic", "k", "")
    assert res["complete"] is False
    assert any("truncated" in w or "不完整" in w for w in res["warnings"])


def test_list_models_upstream_401_raises(monkeypatch):
    _install_fake_get(monkeypatch, [(401, {"error": "bad key"}, "unauthorized")])
    import pytest
    with pytest.raises(pc.ProviderError) as ei:
        pc.list_provider_models("openai", "k", "")
    assert ei.value.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_provider_catalog_unit.py -q`
Expected: FAIL（`list_provider_models` 未定义）

- [ ] **Step 3: 实现编排**

```python
# backend/provider_client.py —— 新增
def list_provider_models(provider: str, api_key: str, base_url: str = "") -> dict:
    provider = normalize_provider(provider)
    warnings: list[str] = []
    try:
        first_url, _, _ = _catalog_request(provider, api_key, base_url, None)
    except ProviderError as e:
        if "model_catalog_unsupported" in str(e):
            return {"models": [], "complete": True, "warnings": [], "catalog_supported": False}
        raise

    seen: set[str] = set()
    models: list[dict] = []
    cursor: str | None = None
    complete = True
    client = _http_client()
    for _page in range(_CATALOG_MAX_PAGES):
        url, headers, params = _catalog_request(provider, api_key, base_url, cursor)
        try:
            resp = client.get(url, headers=headers, params=params,
                              timeout=_CATALOG_PER_REQUEST_TIMEOUT)
        except httpx.HTTPError as e:
            if models:                       # 后续页失败 → 部分成功
                complete = False
                warnings.append(f"catalog page fetch failed: {type(e).__name__}")
                break
            raise ProviderError(f"provider network error: {type(e).__name__}") from e
        if resp.status_code >= 400:
            if models:
                complete = False
                warnings.append(f"catalog page returned http {resp.status_code}")
                break
            raise ProviderError(f"provider_http_{resp.status_code}", status_code=resp.status_code)
        if len(resp.text or "") > _CATALOG_MAX_BODY_BYTES:
            raise ProviderError("model_catalog_invalid_response")
        try:
            body = resp.json()
        except Exception as e:
            raise ProviderError("model_catalog_invalid_response") from e
        if not isinstance(body, dict):
            raise ProviderError("model_catalog_invalid_response")
        page_models, cursor = _parse_catalog_page(provider, body)
        for m in page_models:
            mid = m["id"]
            if len(mid) > 160 or mid in seen:
                continue
            seen.add(mid)
            models.append(m)
            if len(models) >= _CATALOG_MAX_MODELS:
                complete = False
                warnings.append("model list truncated at cap")
                break
        if len(models) >= _CATALOG_MAX_MODELS or not cursor:
            break
    else:
        # for 循环正常跑满 = 还有 cursor 没取完
        if cursor:
            complete = False
            warnings.append("model list truncated: page cap reached")
    return {"models": models, "complete": complete, "warnings": warnings,
            "catalog_supported": True}
```

> 注：`_parse_catalog_page` 对单页拉取（openai 系）返回 `next_cursor=None`，循环第一轮就 `break`。`httpx` 已在文件顶部 import。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_provider_catalog_unit.py -q`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add backend/provider_client.py tests/test_provider_catalog_unit.py
git commit -m "feat(provider): list_provider_models pagination + caps + dedupe"
```

---

## Task 3: 错误→slug 映射 `model_catalog_error_slug`

**Files:**
- Modify: `backend/provider_client.py`
- Test: `tests/test_provider_catalog_unit.py`（追加）

**Interfaces:**
- Consumes: `ProviderError`（现有 `:28`，带 `.status_code`）。
- Produces: `model_catalog_error_slug(exc: BaseException) -> str` — 返回 `model_catalog_auth_failed | _access_denied | _rate_limited | _temporarily_unavailable | _unsupported | _invalid_response`。

映射规则（**注意：401 → auth_failed，但 core 会以 400 状态返回，不透传 401**）：

| 依据 | slug |
|---|---|
| `str(exc)` 含 `model_catalog_unsupported` | `model_catalog_unsupported` |
| `str(exc)` 含 `model_catalog_invalid_response` | `model_catalog_invalid_response` |
| `status_code == 401` | `model_catalog_auth_failed` |
| `status_code == 403` | `model_catalog_access_denied` |
| `status_code == 429` | `model_catalog_rate_limited` |
| `status_code in {408,500,502,503,504}` 或 `None` | `model_catalog_temporarily_unavailable` |
| 其它 4xx（400/402/404/422） | `model_catalog_auth_failed`（多为 key/账号问题） |

- [ ] **Step 1: 写失败测试**

```python
# tests/test_provider_catalog_unit.py —— 追加
def test_error_slug_mapping():
    mk = lambda msg, sc=None: pc.ProviderError(msg, status_code=sc)
    assert pc.model_catalog_error_slug(mk("provider_http_401", 401)) == "model_catalog_auth_failed"
    assert pc.model_catalog_error_slug(mk("provider_http_403", 403)) == "model_catalog_access_denied"
    assert pc.model_catalog_error_slug(mk("provider_http_429", 429)) == "model_catalog_rate_limited"
    assert pc.model_catalog_error_slug(mk("x", 503)) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("provider network error: ReadTimeout")) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("model_catalog_unsupported")) == "model_catalog_unsupported"
    assert pc.model_catalog_error_slug(mk("model_catalog_invalid_response")) == "model_catalog_invalid_response"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_provider_catalog_unit.py::test_error_slug_mapping -q`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# backend/provider_client.py —— 新增
def model_catalog_error_slug(exc: BaseException) -> str:
    msg = str(exc)
    if "model_catalog_unsupported" in msg:
        return "model_catalog_unsupported"
    if "model_catalog_invalid_response" in msg:
        return "model_catalog_invalid_response"
    sc = getattr(exc, "status_code", None)
    if sc == 401:
        return "model_catalog_auth_failed"
    if sc == 403:
        return "model_catalog_access_denied"
    if sc == 429:
        return "model_catalog_rate_limited"
    if sc in {408, 500, 502, 503, 504} or sc is None:
        return "model_catalog_temporarily_unavailable"
    return "model_catalog_auth_failed"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_provider_catalog_unit.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/provider_client.py tests/test_provider_catalog_unit.py
git commit -m "feat(provider): model_catalog_error_slug classification"
```

---

## Task 4: core + 路由 `POST /v1/model_api/models`

**Files:**
- Modify: `backend/hosted/setup_core.py`（新增 `model_api_models`）
- Modify: `backend/hosted/setup_routes_asgi.py`（新增路由）
- Test: `tests/test_model_api_models_route.py`（新建，`make_client`，**不进白名单**）

**Interfaces:**
- Consumes: `provider_client.list_provider_models`、`provider_client.model_catalog_error_slug`（Task 2/3）；`db.model_api_credential_get`（`db.py:8069`）；`core_enclave._decrypt_envelope_via_enclave`（`core/enclave.py:224`，setup_core 已 `from core import enclave as core_enclave`）。
- Produces: HTTP `POST /v1/model_api/models`。
  - 请求体：`{"provider": str, "base_url": str, "api_key": str|null, "credential_id": str|null}`（`api_key` 与 `credential_id` 二选一且仅一个）。
  - 成功 200：`{"provider": str, "models":[{"id","display_name"}], "complete": bool, "catalog_supported": bool, "warnings":[str]}`。
  - 错误：`{"error": slug, "detail"?: str}`，状态码 = `_catalog_status_for_slug(slug)`（auth_failed/access_denied/invalid_response → 400；rate_limited → 429；temporarily_unavailable → 503）。校验错误沿用现有 `api_key_or_credential_id_required`（400）。

**core 行为**：
1. 取 `provider/base_url/api_key/credential_id`；跑与 `model_api_route_create` 同款 XOR 校验（`bool(raw_key) == bool(credential_id)` → `api_key_or_credential_id_required` 400）。
2. `credential_id` 路径：`cred = db.model_api_credential_get(store.user_id, credential_id)`；无 → `credential_not_found` 404；解封 `provider_key = core_enclave._decrypt_envelope_via_enclave(cred["api_key_envelope"], caller_api_key, purpose="model_api_provider_key").decode("utf-8")`，失败 → `model_api_key_decrypt_failed` 400；**provider/base_url 以 cred 为准**覆盖 payload（与 route_create 语义一致）。
3. `api_key` 路径：`provider_key = raw_key`。
4. `provider_client.list_provider_models(provider, provider_key, base_url)` → 成功组装 200 响应（带 `provider`）。
5. `except provider_client.ProviderError as e:` → `slug = provider_client.model_catalog_error_slug(e)`；返回 `{"error": slug, "detail": str(e)[:220]}, _catalog_status_for_slug(slug)`。

- [ ] **Step 1: 写失败测试**（`make_client` 集成；monkeypatch `provider_client.list_provider_models`，不打真实网络）

```python
# tests/test_model_api_models_route.py
import provider_client
from asgi_test_client import make_client


def _client_with_user():
    c = make_client()
    c.register_user()          # 与现有 test_asgi_hosted_setup.py 同款注册；按该文件实际 helper 调整
    return c


def test_models_route_new_key_success(monkeypatch):
    monkeypatch.setattr(provider_client, "list_provider_models",
        lambda provider, key, base_url="": {
            "models": [{"id": "gpt-5.4", "display_name": "GPT-5.4"}],
            "complete": True, "warnings": [], "catalog_supported": True})
    c = _client_with_user()
    r = c.post("/v1/model_api/models", json={"provider": "openai", "api_key": "sk-x"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openai"
    assert body["models"][0]["id"] == "gpt-5.4"
    assert body["catalog_supported"] is True


def test_models_route_requires_exactly_one_credential(monkeypatch):
    c = _client_with_user()
    r = c.post("/v1/model_api/models", json={"provider": "openai"})      # 都没给
    assert r.status_code == 400
    assert r.json()["error"] == "api_key_or_credential_id_required"
    r2 = c.post("/v1/model_api/models",
                json={"provider": "openai", "api_key": "k", "credential_id": "c"})
    assert r2.status_code == 400                                         # 都给了


def test_models_route_provider_auth_failure_maps_to_400_not_401(monkeypatch):
    def boom(*a, **k):
        raise provider_client.ProviderError("provider_http_401", status_code=401)
    monkeypatch.setattr(provider_client, "list_provider_models", boom)
    c = _client_with_user()
    r = c.post("/v1/model_api/models", json={"provider": "openai", "api_key": "bad"})
    assert r.status_code == 400                                          # 不是 401
    assert r.json()["error"] == "model_catalog_auth_failed"


def test_models_route_bedrock_unsupported_is_200(monkeypatch):
    monkeypatch.setattr(provider_client, "list_provider_models",
        lambda *a, **k: {"models": [], "complete": True, "warnings": [],
                         "catalog_supported": False})
    c = _client_with_user()
    r = c.post("/v1/model_api/models", json={"provider": "bedrock", "api_key": "k"})
    assert r.status_code == 200
    assert r.json()["catalog_supported"] is False
```

> ⚠️ `register_user()` / helper 名称以 `tests/test_asgi_hosted_setup.py` 实际用法为准（该文件 `from asgi_test_client import make_client`，`:31`）。实现前先读该文件对齐注册/鉴权 helper 与 header 传法。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_model_api_models_route.py -q`
Expected: FAIL（404/路由不存在）

- [ ] **Step 3a: 实现 core**（`backend/hosted/setup_core.py`）

```python
_CATALOG_STATUS = {
    "model_catalog_auth_failed": 400,
    "model_catalog_access_denied": 400,
    "model_catalog_invalid_response": 400,
    "model_catalog_unsupported": 400,   # 不会走到（unsupported 是 200 成功分支）
    "model_catalog_rate_limited": 429,
    "model_catalog_temporarily_unavailable": 503,
}


def _catalog_status_for_slug(slug: str) -> int:
    return _CATALOG_STATUS.get(slug, 400)


def model_api_models(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]:
    provider = str(payload.get("provider") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    credential_id = str(payload.get("credential_id") or "").strip()
    if bool(raw_key) == bool(credential_id):
        return {"error": "api_key_or_credential_id_required",
                "detail": "supply exactly one of api_key / credential_id"}, 400

    if credential_id:
        cred = db.model_api_credential_get(store.user_id, credential_id)
        if not cred:
            return {"error": "credential_not_found"}, 404
        provider = cred.get("provider") or provider      # 凭据为 provider/base_url 唯一真源
        base_url = cred.get("base_url") or ""
        envelope = cred.get("api_key_envelope")
        if not isinstance(envelope, dict):
            return {"error": "model_api_key_envelope_missing"}, 404
        try:
            provider_key = core_enclave._decrypt_envelope_via_enclave(
                envelope, caller_api_key, purpose="model_api_provider_key").decode("utf-8")
        except Exception as e:
            return {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}, 400
    else:
        provider_key = raw_key

    try:
        result = provider_client.list_provider_models(provider, provider_key, base_url)
    except provider_client.ProviderError as e:
        slug = provider_client.model_catalog_error_slug(e)
        return {"error": slug, "detail": str(e)[:220]}, _catalog_status_for_slug(slug)
    return {"provider": provider, **result}, 200
```

> 确认 `setup_core.py` 顶部已 `import provider_client`、`import db`、`from core import enclave as core_enclave`（`:29`）。若 `provider_client` 未在顶部 import，加上（模块属性调用，勿 `from ... import`）。

- [ ] **Step 3b: 实现路由**（`backend/hosted/setup_routes_asgi.py`，复制 `model_api_route_create` boilerplate）

```python
@router.post("/v1/model_api/models")
async def model_api_models(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_models, auth.store, payload, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_model_api_models_route.py -q`
Expected: PASS

- [ ] **Step 5: 回归——确认没碰坏现有 model_api 路径**

Run: `python -m pytest tests/test_asgi_hosted_setup.py tests/test_model_api_path.py -q`
Expected: PASS（全绿；证明纯旁路）

- [ ] **Step 6: Commit**

```bash
git add backend/hosted/setup_core.py backend/hosted/setup_routes_asgi.py tests/test_model_api_models_route.py
git commit -m "feat(model_api): POST /v1/model_api/models catalog endpoint"
```

---

## Task 5: 公开契约 + 错误 slug 文档

**Files:**
- Modify: `tools/public_openapi_contracts.py`
- Modify: `docs/API_ERRORS.md`
- Test: 现有 OpenAPI 契约测试 + `tests/test_api_errors_doc.py`

**Interfaces:**
- Consumes: 路由 `POST /v1/model_api/models`（Task 4）。
- Produces: 该路由的 request/response schema 与描述进公开 OpenAPI；`model_catalog_*` slug 进 `docs/API_ERRORS.md`。

- [ ] **Step 1: 登记请求体 component + 映射**（`tools/public_openapi_contracts.py`）

在 `COMPONENT_SCHEMAS`（`:333`）加：

```python
    "ModelApiModelsRequest": {
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "base_url": {"type": "string"},
            "api_key": {"type": "string", "nullable": True},
            "credential_id": {"type": "string", "nullable": True},
        },
        "required": ["provider"],
        "additionalProperties": True,
        "example": {"provider": "openai", "api_key": "sk-..."},
    },
```

在 `PRECISE_JSON_BODIES`（`:1433`）加：

```python
    ("post", "/v1/model_api/models"): "ModelApiModelsRequest",
```

- [ ] **Step 2: 登记响应 override + 描述**

在 `RESPONSE_OVERRIDES`（`:1630`）加 200 响应 schema：

```python
    ("post", "/v1/model_api/models"): {
        "200": {"description": "provider 模型目录",
            "content": {"application/json": {"schema": {"type": "object", "properties": {
                "provider": {"type": "string"},
                "models": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "display_name": {"type": "string"}}}},
                "complete": {"type": "boolean"},
                "catalog_supported": {"type": "boolean"},
                "warnings": {"type": "array", "items": {"type": "string"}}}}}}},
    },
```

在 `OPERATION_DESCRIPTIONS`（`:1519`）加一句：

```python
    ("post", "/v1/model_api/models"): "列出某 provider 在该凭据下可见的模型清单（实时拉取，非 io 兼容性保证）。",
```

- [ ] **Step 3: 登记 error slug**（`docs/API_ERRORS.md` 的 `## model_api / provider 配置（设置页）` 段，`:57` 之后）

```markdown
| `model_catalog_auth_failed` | 400 | user_provider | provider key 被 /models 拒绝（含上游 401/4xx） | ✅ |
| `model_catalog_access_denied` | 400 | user_provider | 上游 403：权限/地区/项目限制 | ✅ |
| `model_catalog_rate_limited` | 429 | user_provider | 上游 429 限流 | ✅ |
| `model_catalog_temporarily_unavailable` | 503 | user_provider | 上游超时/5xx，可重试 | ✅ |
| `model_catalog_invalid_response` | 400 | user_provider | 上游非 JSON / 超限 | ✅ |
```

（`model_catalog_unsupported` 不是错误返回——它走 200 `catalog_supported:false`，无需登记为 slug。）

- [ ] **Step 4: 重新生成 + 跑契约测试**

Run:
```bash
python -m pytest tests/test_api_errors_doc.py -q
cd docs-site && npm run openapi:generate && cd ..
python -m pytest -k "openapi or contract" -q
```
Expected: PASS；`docs-site/openapi/public.json` diff 只新增本路由。

- [ ] **Step 5: Commit**

```bash
git add tools/public_openapi_contracts.py docs/API_ERRORS.md docs-site/openapi/public.json
git commit -m "docs(contract): register model_api/models route + model_catalog_* error slugs"
```

---

## Task 6: DoD——按测试标准补齐并自查

**Files:** 无新增；核对与收尾。

- [ ] **Step 1: 对照测试矩阵**

读 `docs/testing/TESTING.md` §2，本次动了「routes」+「provider client」两类。按对应行做齐必做项。

- [ ] **Step 2: 无库收集自查**（memory 教训：conftest 白名单坑）

Run: `python -m pytest tests/test_provider_catalog_unit.py --collect-only -q`
Expected: 全部用例被收集（确认新纯单元文件已在白名单、不会被静默跳过）。

- [ ] **Step 3: 全量相关测试过一遍**

Run:
```bash
python -m pytest tests/test_provider_catalog_unit.py tests/test_model_api_models_route.py \
  tests/test_asgi_hosted_setup.py tests/test_model_api_path.py tests/test_api_errors_doc.py -q
```
Expected: 全绿。

- [ ] **Step 4: credential_id 路径的真实解密核对（红线相关）**

本接口 credential_id 路径会触及 enclave 解密（只读取 key、不改信封）。按 io 红线，**这条路径建议在 test 环境真跑一次**确认解封可用（本地 fake-decrypt 只验流程）。记录结论；若暂不具备条件，在交付说明里标为待办、别当已验证。

- [ ] **Step 5: 更新看板**

在 `io/FEATURE_LOG.md` 的「模型目录」模块「关键决策/待办」回写后端进度；跑 `ops/refresh-branch-board.sh`。合并方式与合进哪条线待 hx 拍板后补记。

---

## Self-Review（对照 spec）

- **Spec §5 每一条**都有落点：接口/契约（Task 4/5）、credential XOR + 解封（Task 4）、各家拉取+header+翻页（Task 1）、上限/部分成功/空/非JSON（Task 2）、错误 slug 不透传 401（Task 3/4）、typed 契约+API_ERRORS（Task 5）、旁路不变（Task 4 Step 5 回归）。✅
- **Spec §8 测试**：纯单元（Task 1-3）+ 路由集成（Task 4）+ 契约（Task 5）+ credential_id 真跑（Task 6 Step 4）。✅
- **占位扫描**：无 TBD；`register_user()` helper 名标注了「以 test_asgi_hosted_setup.py 实际为准」——实现第一步即对齐，不是占位。
- **类型一致**：`list_provider_models` 返回结构在 Task 2 定义、Task 4 原样 `{**result}` 透传；`model_catalog_error_slug` 在 Task 3 定义、Task 4 调用；slug 集合 Task 3/4/5 三处一致。✅
- **bedrock**：Task 1 抛 unsupported → Task 2 转 `catalog_supported:false` → Task 4 走 200 → iOS 退手填。链路一致。✅
