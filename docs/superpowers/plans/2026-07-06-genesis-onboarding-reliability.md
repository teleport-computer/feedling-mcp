# Genesis Onboarding 可靠性(后端)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 genesis onboarding 记忆抽取对 provider 抖动健壮:临时错误重试、单块用尽跳过而不拖挂整单、大导入只采样聊天史桶、有内容必出身份(否则明确 failed)、失败可观测。

**Architecture:** 全在 `backend/genesis/`。核心是把前台"每块抽取"从"一块抛错=整挂"改成"临时错误重试→用尽跳过→记 failed window";身份加非 LLM 轻量兜底;job 结果按"有没有 identity signal"判(不看绝对记忆数)。不动 prompt、不开 combined、不后台精修身份。iOS 下一步单独做。

**Tech Stack:** Python 3.12,pytest。provider 错误分类复用 `backend/provider_client.py::classify_provider_error`(已返回 `transient`/`provider_config`/`unknown`,已覆盖 empty/no-usable/bad-json)。

## Global Constraints

- 派生 prompt(fact_map/fact_write/identity/voice/persona)**一行不动**。
- 硬错误(402/401/403/quota/key)**不重试**,立即失败。
- **不开 combined**(`FEEDLING_GENESIS_COMBINED_MAP` 不动);**不后台精修 identity**;**不做 memory core/full 拆分**。
- job 成/败判据用 **identity signal**(名字或维度),**不用绝对记忆条数**。
- venv:`/private/tmp/feedling-m2-venv/bin/python`;跑测试:`PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest <path> -p no:cacheprovider -q`。掉包则 `pip install -r backend/requirements.txt && pip install pytest`。
- 分支:`feat/genesis-onboarding-reliability`。

---

### Task 1: 抽取步支持"临时错误重试"(不只空结果)

现状:`_complete_json_retry_empty`(worker.py:537)只在结果空时重试;provider 抛异常(ReadTimeout 等)直接冲出去。改成:异常时用 `classify_provider_error` 判 —— transient/unknown 重试,provider_config 立即抛。

**Files:**
- Modify: `backend/genesis/worker.py:537-571`(`_complete_json_retry_empty`)
- Test: `tests/test_genesis_worker.py`

**Interfaces:**
- Consumes: `provider_client.classify_provider_error(exc) -> "transient"|"provider_config"|"unknown"`;`GenesisWorkerError`。
- Produces: `_complete_json_retry_empty(..., max_attempts=3)` 现在对 transient/unknown 异常也重试;provider_config 异常立即 re-raise;仍对空结果重试。签名不变。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genesis_worker.py  (追加)
import provider_client
from genesis import worker as gw

class _FakeLLM:
    def __init__(self, seq): self.seq = list(seq); self.calls = 0
    def complete(self, *a, **k):
        item = self.seq[self.calls]; self.calls += 1
        if isinstance(item, Exception): raise item
        return type("R", (), {"text": item, "usage": None})()

def test_retry_empty_also_retries_transient_then_succeeds(monkeypatch):
    # 第1次 ReadTimeout(transient)→ 第2次返回可用 JSON
    llm = _FakeLLM([provider_client.ProviderError("provider network error: ReadTimeout"),
                    '{"fact_candidates":[{"about":"user","summary":"x"}]}'])
    out = gw._complete_json_retry_empty(
        llm, user_id="u", job_id="j", task_id="fact-map-0",
        runtime=object(), messages=[{"role":"user","content":"x"}],
        max_tokens=100, idempotency_key="k", is_empty=gw._fact_write_output_empty, max_attempts=3)
    assert out["fact_candidates"][0]["summary"] == "x"
    assert llm.calls == 2

def test_retry_empty_does_not_retry_provider_config(monkeypatch):
    # 402 provider_config → 立即抛,不重试
    llm = _FakeLLM([provider_client.ProviderError("insufficient credit", status_code=402),
                    '{"fact_candidates":[]}'])
    import pytest
    with pytest.raises(provider_client.ProviderError):
        gw._complete_json_retry_empty(
            llm, user_id="u", job_id="j", task_id="fact-map-0",
            runtime=object(), messages=[{"role":"user","content":"x"}],
            max_tokens=100, idempotency_key="k", is_empty=gw._fact_write_output_empty, max_attempts=3)
    assert llm.calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis_worker.py -k retry_empty -p no:cacheprovider -q`
Expected: FAIL(现状不 catch 异常 → transient 直接抛出,第一个测试 raises 而非返回)。

- [ ] **Step 3: 改 `_complete_json_retry_empty` 支持 transient 重试**

```python
# worker.py  替换循环体(537-571)
    attempts = max(1, int(max_attempts))
    last: dict = {}
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        suffix = "" if attempt == 0 else f"-empty-retry-{attempt}"
        key_suffix = "" if attempt == 0 else f":empty_retry:{attempt}"
        try:
            last = _complete_json(
                llm, user_id=user_id, job_id=job_id, task_id=f"{task_id}{suffix}",
                runtime=runtime, messages=messages, max_tokens=max_tokens,
                idempotency_key=f"{idempotency_key}{key_suffix}", temperature=temperature)
        except provider_client.ProviderError as e:
            if provider_client.classify_provider_error(e) == "provider_config":
                raise                       # 硬错误不重试
            last_exc = e; continue          # transient/unknown → 下一轮重试
        except GenesisWorkerError as e:     # JSON 解析类 → 视为 transient(可能空/坏 json)
            last_exc = e; continue
        if not is_empty(last):
            return last                     # 有内容 → 成功
    if last_exc is not None and not last:
        raise last_exc                      # 全程只抛异常、无可用结果 → 把异常抛出去(交给上层跳块)
    return last                             # 有过结果(可能空)→ 返回空,让上层自行处理
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis_worker.py -k retry_empty -p no:cacheprovider -q`
Expected: PASS(2 passed)。

- [ ] **Step 5: Commit**

```bash
git add backend/genesis/worker.py tests/test_genesis_worker.py
git commit -m "feat(genesis): 抽取步临时错误重试(ReadTimeout/空/坏json),硬错误(402)不重试"
```

---

### Task 2: 前台每块抽取失败→跳过续跑,记 history_windows_failed

现状:前台 loop(worker.py:1202)里某块抽取抛异常 → 整个 `build_foreground_output_from_texts` 抛 → 整单 failed。改成:catch 单块失败(重试用尽后),跳过、计数,继续下一块。只对 **history** 桶计数(support 桶失败另算)。

**Files:**
- Modify: `backend/genesis/worker.py:1202-1224`(foreground loop)+ `build_foreground_output_from_texts` 返回值加 `history_windows_failed`
- Test: `tests/test_genesis_foreground_worker.py`

**Interfaces:**
- Consumes: Task 1 的 `_complete_json_retry_empty`(transient 用尽会抛异常)。
- Produces: `build_foreground_output_from_texts(...)` 返回 dict 增加键 `history_windows_total: int`、`history_windows_failed: int`。已有 memories/identity 键不变。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genesis_foreground_worker.py (追加)
from genesis import worker as gw
import provider_client

def test_foreground_skips_failed_history_chunk_and_counts(monkeypatch):
    calls = {"n": 0}
    def fake_retry(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:                          # 第2块持续失败
            raise provider_client.ProviderError("provider network error: ReadTimeout")
        return {"fact_candidates": [{"about": "user", "summary": f"m{calls['n']}"}]}
    monkeypatch.setattr(gw, "_complete_json_retry_empty", fake_retry)
    monkeypatch.setattr(gw, "_complete_json", fake_retry)
    out = gw.build_foreground_output_from_texts(
        user_id="u", job_id="j", key_prefix="k", runtime=object(),
        chunk_texts=["a", "b", "c"], source_kind="history", llm=object())
    assert out["history_windows_total"] == 3
    assert out["history_windows_failed"] == 1        # 第2块跳过
    # 另外两块的候选仍在
    assert any(m.get("summary") for m in out.get("all_fact_candidates", out.get("core_fact_candidates", [])))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis_foreground_worker.py -k skips_failed -p no:cacheprovider -q`
Expected: FAIL(现状异常直接冒出 → raises;且无 `history_windows_*` 键)。

- [ ] **Step 3: 前台 loop 加 try/except 跳块 + 计数**

```python
# worker.py foreground loop (1202 起) 改成:
    history_windows_total = 0
    history_windows_failed = 0
    for idx, text in enumerate(chunk_texts):
        is_history = (source_family == "history")
        if is_history:
            history_windows_total += 1
        try:
            if include_voice_candidates and is_history and genesis_combined_map_enabled():
                facts = _complete_json_retry_empty(
                    llm, user_id=user_id, job_id=job_id, task_id=f"combined-map-{idx}",
                    runtime=runtime, messages=prompts.combined_map_messages(text),
                    max_tokens=2400, idempotency_key=f"{shared_prefix}:combined_map:{idx}",
                    is_empty=_combined_map_empty)
                voice_candidates.append(_voice_candidate_from_combined_map(facts))
            else:
                facts = _complete_json_retry_empty(
                    llm, user_id=user_id, job_id=job_id, task_id=f"fact-map-{idx}",
                    runtime=runtime,
                    messages=prompts.fact_map_messages(_source_tagged_fact_text(source_family, text)),
                    max_tokens=1800, idempotency_key=f"{shared_prefix}:fact_map:{idx}",
                    is_empty=_fact_write_output_empty)
        except provider_client.ProviderError as e:
            if provider_client.classify_provider_error(e) == "provider_config":
                raise                                   # 硬错误 → 交上层 abort
            if is_history:
                history_windows_failed += 1             # transient 用尽 → 跳过这块 history
            continue
        except GenesisWorkerError:
            if is_history:
                history_windows_failed += 1
            continue
        if isinstance(facts.get("fact_candidates"), list):
            fact_candidates.extend(item for item in facts["fact_candidates"] if isinstance(item, dict))
```

注:非 history 桶(人物卡/档案/长期记忆)现在也用 `_complete_json_retry_empty`(带重试);它们失败会 `continue` 但不计入 history_windows_failed(support 失败由 Task 4 的身份兜底/判定处理)。

- [ ] **Step 4: 返回值加计数键**

在 `build_foreground_output_from_texts` 的 `return {...}` 里加:

```python
        "history_windows_total": history_windows_total,
        "history_windows_failed": history_windows_failed,
```

- [ ] **Step 5: Run + Commit**

Run: `PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis_foreground_worker.py -k skips_failed -p no:cacheprovider -q` → PASS

```bash
git add backend/genesis/worker.py tests/test_genesis_foreground_worker.py
git commit -m "feat(genesis): 前台 history 块抽取失败→跳过续跑+记 history_windows_failed,不拖挂整单"
```

---

### Task 3: 大导入只采样 history 桶(人物卡/档案/长期记忆全读)

现状:`_plaintext_source_groups`(plaintext.py:130)对每个桶都切到 `window_limit`。改成:**history 桶前台最多 ~8 块**(超了 `_select_evenly`),其它小桶不限。被砍的 history 后台补全(已有背景路,不改)。

**Files:**
- Modify: `backend/genesis/plaintext.py`(在 `_prepare_plaintext_import` / 传入 `_run_plaintext_genesis_v2` 的 foreground chunk 选择处,对 history group 的 chunk_texts 采样到上限)
- Test: `tests/test_genesis_plaintext_routes.py`

**Interfaces:**
- Consumes: `history_import._select_evenly(items, limit)`。
- Produces: 新函数 `_foreground_history_cap() -> int`(默认 8,可 env `FEEDLING_GENESIS_FG_HISTORY_CAP`);前台跑的 history chunk_texts ≤ cap。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genesis_plaintext_routes.py (追加)
from genesis import plaintext
from hosted import history_import

def test_foreground_history_chunks_capped_support_untouched(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_FG_HISTORY_CAP", "8")
    groups = [
        {"source_family": "history", "chunk_texts": [f"h{i}" for i in range(27)]},
        {"source_family": "ai_persona", "chunk_texts": ["persona-card"]},
    ]
    capped = plaintext._cap_foreground_history_chunks(groups)
    hist = next(g for g in capped if g["source_family"] == "history")
    persona = next(g for g in capped if g["source_family"] == "ai_persona")
    assert len(hist["chunk_texts"]) == 8          # history 采样到 8
    assert len(persona["chunk_texts"]) == 1        # 小桶不动
```

- [ ] **Step 2: Run → FAIL**（`_cap_foreground_history_chunks` 不存在）

- [ ] **Step 3: 加采样函数**

```python
# plaintext.py
import os
def _foreground_history_cap() -> int:
    try:
        return max(1, int(os.environ.get("FEEDLING_GENESIS_FG_HISTORY_CAP", "8")))
    except (TypeError, ValueError):
        return 8

def _cap_foreground_history_chunks(source_groups: list[dict]) -> list[dict]:
    """前台用:只对 history 桶采样到 cap(_select_evenly);其它桶(人物卡/档案/长期记忆)全读。
    被砍的 history 块由后台补全,不影响身份(名字来自人物卡,全读)。"""
    cap = _foreground_history_cap()
    out: list[dict] = []
    for g in source_groups:
        if str(g.get("source_family") or "") == "history":
            chunks = list(g.get("chunk_texts") or [])
            if len(chunks) > cap:
                chunks = history_import._select_evenly(chunks, cap)
            out.append({**g, "chunk_texts": chunks})
        else:
            out.append(g)
    return out
```

- [ ] **Step 4: 前台入口用采样后的 groups**

在 `_run_plaintext_genesis_v2`(plaintext.py:574)进 foreground 抽取前,把 `source_groups` 换成 `_cap_foreground_history_chunks(source_groups)`(只用于前台;后台 `_run_plaintext_background_enrichment` 仍收原始全量 source_groups)。

- [ ] **Step 5: Run + Commit**

```bash
git add backend/genesis/plaintext.py tests/test_genesis_plaintext_routes.py
git commit -m "feat(genesis): 大导入前台只采样 history 桶(cap 8),人物卡/档案/长期记忆全读;后台补全"
```

---

### Task 4: 身份非 LLM 轻量兜底 + support-only/有内容判定

现状:身份 provider 失败 → mark_failed(plaintext.py:693)。改成:derive 失败或无 signal 时,先试**非 LLM 轻量身份**(从人物卡/档案文本抓显式名字 + relationship anchor 凑维度);仍无 signal 且**输入非空** → failed("服务临时不可用");**fresh_start** → 允许无名 done。

**Files:**
- Create: `backend/genesis/lightweight_identity.py`(纯函数:从 support 文本 + anchor 凑 identity payload)
- Modify: `backend/genesis/plaintext.py`(身份分支)
- Test: `tests/test_genesis_lightweight_identity.py`

**Interfaces:**
- Produces: `lightweight_identity.derive_from_support(support_texts: list[str], *, days_with_user: int, language: str) -> dict`,返回 `{"agent_name": str, "dimensions": list, ...}`(可能 agent_name 空);`lightweight_identity.has_signal(payload) -> bool`(名字或维度非空)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genesis_lightweight_identity.py
from genesis import lightweight_identity as li

def test_lightweight_pulls_explicit_name_from_character_card():
    text = "# 阿樟 · 角色卡\n- 名字：阿樟\n- 性格：温柔但毒舌"
    p = li.derive_from_support([text], days_with_user=10, language="zh")
    assert p["agent_name"] == "阿樟"
    assert li.has_signal(p) is True

def test_lightweight_no_signal_when_no_name_no_dims():
    p = li.derive_from_support(["随便一段没有名字的话"], days_with_user=0, language="zh")
    assert li.has_signal(p) is False
```

- [ ] **Step 2: Run → FAIL**(模块不存在)

- [ ] **Step 3: 写 lightweight_identity.py**

```python
# backend/genesis/lightweight_identity.py
"""非 LLM 的身份兜底:仅在 provider 派生失败时用,从上传的人物卡/档案原文里抓显式名字。
绝不调 LLM。质量有限(启发式),只保证'有内容不至于无名空过'(见 spec §2.3)。"""
from __future__ import annotations
import re

_NAME_PATTERNS = [
    r"名字[:：]\s*([^\s（(，,。\n]{1,20})",
    r"叫\s*([^\s（(，,。\n]{1,20})\s*[，,。]",
    r"#\s*([^\s·|]{1,20})\s*[·|]\s*角色卡",
]

def _extract_name(text: str) -> str:
    for pat in _NAME_PATTERNS:
        m = re.search(pat, text or "")
        if m:
            name = m.group(1).strip(" ·|、`\"'“”")
            if name and name.lower() not in {"claude", "gpt", "gemini", "chatgpt", "hermes"}:
                return name[:40]
    return ""

def derive_from_support(support_texts: list[str], *, days_with_user: int, language: str) -> dict:
    name = ""
    for t in support_texts or []:
        name = _extract_name(str(t or ""))
        if name:
            break
    return {"agent_name": name, "dimensions": [], "self_introduction": "",
            "category": "", "signature": [], "days_with_user": max(0, int(days_with_user or 0))}

def has_signal(payload: dict | None) -> bool:
    p = payload if isinstance(payload, dict) else {}
    if str(p.get("agent_name") or "").strip():
        return True
    return bool(isinstance(p.get("dimensions"), list) and p["dimensions"])
```

- [ ] **Step 4: Run → PASS**

Run: `PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis_lightweight_identity.py -p no:cacheprovider -q`

- [ ] **Step 5: 接入 plaintext.py 身份分支**

在 `_run_plaintext_genesis_v2`(plaintext.py:687-693)身份处改成:

```python
    identity_payload, id_warnings = foreground_identity.derive_foreground_identity(
        runtime=runtime, analysis_messages=msgs, core_memories=full_memories,
        days_with_user=days, language=language)
    provider_failure = _provider_identity_failure(id_warnings)
    if provider_failure or not foreground_identity.has_identity_signal(identity_payload):
        # 轻量兜底:从人物卡/档案原文抓显式名字(不调 LLM)
        support_texts = [str(m.get("content") or "") for m in msgs
                         if history_import._is_import_support_message(m)]
        lite = lightweight_identity.derive_from_support(
            support_texts, days_with_user=days, language=language)
        if lightweight_identity.has_signal(lite):
            identity_payload = lite
        else:
            is_fresh_start = (not support_texts) and not any(
                not history_import._is_import_support_message(m) for m in msgs)
            if is_fresh_start:
                pass                       # 真空 → 允许无名继续(fresh start)
            else:
                service.mark_failed(store, job_id,
                    "onboarding_no_identity:provider_unstable")   # 有内容却凑不出身份 → failed
                return True
```

(顶部 import:`from genesis import lightweight_identity`)

- [ ] **Step 6: Run 相关测试 + Commit**

```bash
PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis_lightweight_identity.py tests/test_genesis_v2_orchestration.py -p no:cacheprovider -q
git add backend/genesis/lightweight_identity.py backend/genesis/plaintext.py tests/test_genesis_lightweight_identity.py
git commit -m "feat(genesis): 身份非LLM轻量兜底(人物卡抓名字);有内容凑不出身份→failed,fresh_start可无名"
```

---

### Task 5: job/validate 暴露 degraded 观测字段

把 Task 2 的 `history_windows_failed` 等透到 job 完成状态 + `onboarding_validation`。

**Files:**
- Modify: `backend/genesis/plaintext.py`(genesis_complete_job 的 output 带上计数)
- Modify: `backend/hosted/onboarding_validation.py`(history_import step 带上字段)
- Test: `tests/test_onboarding_validation_genesis.py`

**Interfaces:**
- Produces: validate 的 `history_import` step 增加 `history_windows_total`、`history_windows_failed`、`degraded`(= failed>0)、`support_inputs_present`。

- [ ] **Step 1: Write failing test**

```python
# tests/test_onboarding_validation_genesis.py (追加,复用 _install_model_api_harness)
def test_validate_surfaces_history_windows_failed_degraded(monkeypatch):
    _install_model_api_harness(monkeypatch, genesis_jobs=[{
        "job_id": "g1", "status": "done", "source_kind": "history_import",
        "identity_status": "initialized", "memory_action_count": 20,
        "output": {"stage": "genesis_v2_done",
                   "history_windows_total": 8, "history_windows_failed": 3},
        "metadata": {"ingest": "plaintext", "history_count": 68, "timeline_span_days": 1,
                     "history_windows_total": 8, "history_windows_failed": 3},
    }])
    body = validation._model_api_onboarding_validation_payload(_store())
    step = next(s for s in body["steps"] if s["id"] == "history_import")
    assert step["history_windows_failed"] == 3
    assert step["degraded"] is True
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: plaintext.py 完成时写计数**

在 `_run_plaintext_genesis_v2` 前台完成(`db.genesis_complete_job(... output={"stage": ...})`)处,把 `fg_merged` 里的 `history_windows_total/failed` 一并写进 output(以及 job metadata,便于 validate 读)。

- [ ] **Step 4: onboarding_validation.py 带出字段**

在拼 `history_import` step 的 dict 里加:

```python
        "history_windows_total": int(md.get("history_windows_total") or out.get("history_windows_total") or 0),
        "history_windows_failed": int(md.get("history_windows_failed") or out.get("history_windows_failed") or 0),
        "degraded": bool(int(md.get("history_windows_failed") or out.get("history_windows_failed") or 0) > 0),
        "support_inputs_present": bool((md.get("ai_persona_count") or 0) or (md.get("user_profile_count") or 0) or (md.get("memory_summary_count") or 0)),
```

(`md` = job metadata,`out` = job output;按该文件现有变量名对齐。)

- [ ] **Step 5: Run + Commit**

```bash
PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_onboarding_validation_genesis.py -p no:cacheprovider -q
git add backend/genesis/plaintext.py backend/hosted/onboarding_validation.py tests/test_onboarding_validation_genesis.py
git commit -m "feat(genesis): validate 暴露 history_windows_failed/degraded/support_inputs_present"
```

---

### Task 6: 全套回归 + 真机 e2e 冒烟

- [ ] **Step 1: 跑全套 genesis 单测**

Run: `PYTHONPATH=backend /private/tmp/feedling-m2-venv/bin/python -m pytest tests/test_genesis*.py tests/test_onboarding_validation_genesis.py -p no:cacheprovider -q`
Expected: 全 PASS(含 combined 关闭时的既有断言不回归)。

- [ ] **Step 2: 合 test 部署 + 真机 e2e(需 provider key)**

按 spec §验收 1-7,用 `tools/genesis_e2e.py acceptance`(deepseek-chat / openrouter sonnet-4.5)验:重试、部分成功 degraded、大导入采样、support-only 出身份、fresh_start 无名 done、有内容凑不出身份→failed。key 走 env,不落库。

- [ ] **Step 3: 出结果给 CC/hx review,再决定 iOS 文案(下一步)**

---

## Self-Review

- **Spec 覆盖**:§1 错误分类→Task1(复用 classify_provider_error);§2.1/2.2 重试+跳块→Task1/2;§2.4 采样→Task3;§2.3 身份轻量兜底→Task4;§2.5 support/fresh_start→Task4;§3 判定→Task4(有内容无signal→failed);§4.1 字段→Task5;iOS(§4.2)明确下一步不在本计划。✅
- **Placeholder 扫描**:无 TBD;每步有真实测试/实现代码。
- **类型一致**:`history_windows_total/failed`(Task2 产出→Task5 消费)、`lightweight_identity.derive_from_support/has_signal`(Task4 内一致)、`_cap_foreground_history_chunks`(Task3)命名前后一致。
- **注意**:Task4 Step5 的 `_provider_identity_failure`、`history_import._is_import_support_message` 均为现有函数;`foreground_identity.has_identity_signal` 现有。
