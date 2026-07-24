# Hosted Runtime V2 — Multimodal (images) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make V2's model actually see the images a user sends, and the caption they send with them.

**Architecture:** `provider_client` already speaks multimodal (OpenAI-style content-block lists, with anthropic/gemini converters, all three already tested). The gap is the data entrance. Images enter **in-band via the conversation tail**, never as a capability result (a capability result would be folded into the *text* grounding context — that is exactly BUG-1). `read_tail` stays **pure text** because `compaction` shares it; a separate, capped `read_images` dep injects bytes only for the last N image messages, only on the responder path.

**Tech Stack:** Python 3.11, asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-multimodal-design.md`

## Global Constraints

- **NO-COMMIT.** Never run `git commit`, `git add`, `git stash`, `git stash pop`, `git checkout --`, `git reset`, `git clean`. The stash stack is SHARED with other live sessions and worktrees. Leave everything in the working tree.
- **Worktree:** `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2`, branch `feat/hosted-runtime-v2`. Never the main checkout.
- **`compaction`'s tail must stay pure text.** `worker._run_compaction` calls `deps.read_tail(user_id, watermark, 10_000)` and `compaction._render_old_messages` does `f"{role}: {content}"`. If image bytes ever reach `read_tail`'s return value, base64 lands in the summarizer prompt AND every historical image gets an enclave decrypt. This is the single most important constraint in this plan.
- **`chat_image_read` stays OUT of the planner vocabulary.** It remains registered in `capabilities/registry.py` and is called directly by `serve_worker._read_images`. Do not re-add it to `planner._READ_ACTIONS` or `_PLANNER_SYSTEM`.
- **Do not modify** `responder.py`, `provider_client.py`, `executor.py`, `compaction.py`, `planner.py`, or anything under `backend/capabilities/`.
- **ENCLAVE_SEMAPHORE:** image decrypts happen inside the worker's existing `async with enclave_sem` block. Per-turn new enclave round-trips ≤ `_TAIL_IMAGE_LIMIT`.
- **no-filler:** an image decrypt failure degrades to the text marker and the turn still answers. Never an error chip, never a placeholder bubble.
- **Dependency direction** (AST-guarded, `tests/test_v2_dependency_direction.py`, now derived from the directory): `context.py` stays stdlib-only; `worker.py` must not import `hosted`/`agent_runtime`. `read_images` is injected via `TurnDeps`; the production implementation lives in `serve_worker.py` (assembly layer).
- **Test baseline:** 2661 passed / 7 pre-existing failures (`test_chat_route_debug_trace` ×3, `test_debug_trace_event_route`, `test_memory_capture_trace`, `test_model_api_path` verify-ping ×2).
- **The full-suite command is:**
  ```bash
  python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
  ```
  `tests/test_api.py` and `tests/e2e_model_api_test.py` are live-server scripts that issue an HTTP request at import time; a bare `pytest tests/` aborts at collection with `Interrupted: 1 error during collection` and tests **nothing**. The same two flags are needed for any `-k` sweep. Postgres must be up: `pg_isready -h 127.0.0.1 -p 55432`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/model_api_runtime/v2/context.py` | Pure prompt assembly | **Modify** — pass list-typed `content` through; add pure `text_of()` |
| `backend/model_api_runtime/v2/worker.py` | Turn assembly | **Modify** — `TurnDeps.read_images`, `_inject_tail_images`, caps, wire into chat + wake responder paths |
| `backend/model_api_runtime/v2/serve_worker.py` | Production DI / assembly | **Modify** — caption decrypt + `has_image`/`image_mime` markers in `_read_tail`/`_read_messages`; new `_read_images` |
| `tests/test_v2_context.py` | | **Modify/Create** — list-content passthrough |
| `tests/test_v2_worker_images.py` | | **Create** — injection, caps, degradation |

---

## Task 1: `context` passes image content blocks through

Today `build_turn_messages` does `content = str(m.get("content") or "").strip()` as a truthiness gate and then appends `m["content"]`. With a list it *happens* to work (the stringified list is truthy) — by accident, and an image-only message with no caption would stringify to a non-empty list repr and pass a check that was meant to test for text. Make it explicit. Also add the pure `text_of()` helper the worker needs to decide whether a tail row has any text at all.

**Files:**
- Modify: `backend/model_api_runtime/v2/context.py`
- Test: `tests/test_v2_context.py`

**Interfaces:**
- Produces: `context.text_of(content: Any) -> str` — concatenates `text` parts of a block list, or returns the stripped string. `context.build_turn_messages` accepts `content` that is `str` or `list[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_v2_context.py` (create the file with the imports below if it does not exist):

```python
from model_api_runtime.v2 import context


def test_text_of_handles_str_list_and_none():
    assert context.text_of("  hi  ") == "hi"
    assert context.text_of(None) == ""
    assert context.text_of([
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]) == "look at this"
    # image-only block list has no text
    assert context.text_of([
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]) == ""


def test_build_turn_messages_passes_image_blocks_through_verbatim():
    blocks = [
        {"type": "text", "text": "这个报告哪里有问题"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]
    msgs = context.build_turn_messages(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": blocks}])
    assert msgs[-1]["content"] is blocks       # verbatim, not stringified
    assert msgs[-1]["role"] == "user"


def test_build_turn_messages_keeps_an_image_only_message():
    """A caption-less image must NOT be dropped — it is the entire user turn."""
    blocks = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]
    msgs = context.build_turn_messages(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": blocks}])
    assert msgs[-1]["content"] is blocks


def test_build_turn_messages_still_drops_empty_text_rows():
    msgs = context.build_turn_messages(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": "   "}])
    assert [m["role"] for m in msgs] == ["system"]


def test_needs_compaction_counts_image_rows():
    tail = [{"role": "user", "content": [{"type": "image_url",
                                          "image_url": {"url": "data:image/jpeg;base64,A"}}]}]
    assert context.needs_compaction(tail, budget=0) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_v2_context.py -q
```

Expected: FAIL — `AttributeError: module 'model_api_runtime.v2.context' has no attribute 'text_of'`.

- [ ] **Step 3: Implement**

In `backend/model_api_runtime/v2/context.py`, add after `_SUMMARY_HEADER`:

```python
def text_of(content: Any) -> str:
    """Extract the human-readable text from a tail row's ``content``.

    ``content`` is either a plain string, or an OpenAI-style content-block list
    (``[{"type":"text","text":...}, {"type":"image_url", ...}]``) once the worker
    has injected images. Mirrors ``provider_client._content_text`` but is
    replicated here to keep this module stdlib-only (dependency direction).
    """
    if isinstance(content, list):
        parts = [
            str(p.get("text") or "").strip()
            for p in content
            if isinstance(p, dict) and str(p.get("text") or "").strip()
        ]
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _has_payload(content: Any) -> bool:
    """True when the row carries anything worth sending: text, or any block at all
    (an image-only turn has no text but IS the user's entire message)."""
    if isinstance(content, list):
        return bool(content)
    return bool(str(content or "").strip())
```

Replace the tail loop inside `build_turn_messages`:

```python
    for m in tail:
        content = m.get("content")
        if not _has_payload(content):
            continue
        messages.append({"role": _norm_role(m.get("role")), "content": content})
```

Replace `needs_compaction`'s body:

```python
    count = sum(1 for m in tail if _has_payload(m.get("content")))
    return count > budget
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/test_v2_context.py tests/test_v2_compaction_integration.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 7 pre-existing failures, 0 new.

- [ ] **Step 6: Do NOT commit.**

---

## Task 2: `serve_worker` reads captions and marks image rows (still pure text)

The caption a user sends with an image is stored in `extra.caption_*` (`chat/service.py:115`) and **no V2 read path has ever read it**. Fix that here. Image rows keep a **text** `content` (the caption, or the `"[image]"` marker) plus two non-sensitive markers so the worker can find them later. **No bytes enter `read_tail`'s return value** — `compaction` calls it with `limit=10_000`.

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Test: `tests/test_v2_serve_worker_images.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `serve_worker._IMAGE_MARKER = "[image]"`
  - `serve_worker._CAPTION_DECRYPT_LIMIT = 8`
  - `serve_worker._caption_envelope(m: dict) -> dict | None` — rebuilds the caption envelope from `caption_*` keys, or `None` when absent.
  - `_read_tail` / `_read_messages` image rows now: `{"id", "ts", "role", "content": caption or "[image]", "has_image": True, "image_mime": <str>}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_serve_worker_images.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def test_caption_envelope_rebuilds_from_prefixed_keys():
    m = {
        "id": "msg1", "owner_user_id": "u1", "v": "1",
        "caption_id": "cap1", "caption_v": "1", "caption_body_ct": "CT",
        "caption_nonce": "N", "caption_K_enclave": "KE",
        "caption_owner_user_id": "u1",
    }
    env = serve_worker._caption_envelope(m)
    # AEAD AAD is owner_user_id||v||id -> MUST use the caption's own id, not the message's.
    assert env["id"] == "cap1"
    assert env["body_ct"] == "CT"
    assert env["K_enclave"] == "KE"
    assert env["owner_user_id"] == "u1"


def test_caption_envelope_none_without_ciphertext():
    assert serve_worker._caption_envelope({"id": "m1"}) is None
    assert serve_worker._caption_envelope({"id": "m1", "caption_body_ct": ""}) is None


def test_caption_envelope_falls_back_to_message_owner_and_id():
    env = serve_worker._caption_envelope(
        {"id": "m1", "owner_user_id": "u9", "v": "2", "caption_body_ct": "CT"})
    assert env["id"] == "m1"        # no caption_id -> message id
    assert env["owner_user_id"] == "u9"
    assert env["v"] == 2
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_serve_worker_images.py -q
```

Expected: FAIL — `AttributeError: ... has no attribute '_caption_envelope'`.

- [ ] **Step 3: Implement `_caption_envelope` + constants**

In `backend/model_api_runtime/v2/serve_worker.py`, add near the other module constants:

```python
_IMAGE_MARKER = "[image]"
# 只为最近 N 个图片行解 caption。compaction 用 limit=10_000 调 _read_tail，没有这个上限
# 它会把该用户历史上每一张图的 caption 都发起一次 enclave 往返（enclave 是单线程瓶颈）。
_CAPTION_DECRYPT_LIMIT = 8


def _caption_envelope(m: dict) -> dict | None:
    """从 `caption_*` 前缀字段重建 caption 信封；无密文时 None。

    镜像 `enclave/routes/chat.py:79-92`。**必须**用 `caption_id`（不是消息自己的 id）——
    enclave 的 AEAD additional-data 是 `owner_user_id||v||id`，用错 id 会 AEAD 校验失败。
    """
    ct = str(m.get("caption_body_ct") or "").strip()
    if not ct:
        return None
    v = m.get("caption_v", m.get("v", 1))
    return {
        "id": m.get("caption_id") or m.get("id"),
        "v": int(v or 1),
        "body_ct": ct,
        "nonce": m.get("caption_nonce"),
        "K_enclave": m.get("caption_K_enclave"),
        "owner_user_id": m.get("caption_owner_user_id") or m.get("owner_user_id"),
    }
```

- [ ] **Step 4: Use it in `_read_tail` and `_read_messages`**

In both functions, replace the existing image branch

```python
        if m.get("content_type") == "image":
            out.append({"id": mid, "ts": ts, "role": role, "content": "[image]"})
            continue
```

with a call to a shared helper. Add the helper next to `_caption_envelope`:

```python
def _image_row(m, *, mid, ts, role, token, caption_budget: list[int]) -> dict:
    """图片行 -> **纯文本** tail 行 + 两个非敏感标记。绝不放 b64——compaction 共用这条读路径。

    `caption_budget` 是一个单元素列表（可变计数器）：预算耗尽后不再解 caption，退化成
    `_IMAGE_MARKER`。caption 解密失败同样静默退化——看不到那句话，好过整个回合失败。
    """
    text = _IMAGE_MARKER
    cap_env = _caption_envelope(m)
    if cap_env is not None and caption_budget[0] > 0:
        caption_budget[0] -= 1
        try:
            caption = core_enclave._decrypt_envelope_via_enclave(
                cap_env, None, purpose="v2_caption_read", runtime_token=token
            ).decode("utf-8", errors="replace").strip()
            if caption:
                text = caption
        except Exception as e:  # noqa: BLE001 — 静默降级，绝不拖垮回合
            log.warning("[v2.serve_worker] caption decrypt failed msg=%s: %s", mid, e)
    return {"id": mid, "ts": ts, "role": role, "content": text,
            "has_image": True, "image_mime": m.get("image_mime") or "image/jpeg"}
```

In `_read_tail`, before the row loop, add `caption_budget = [_CAPTION_DECRYPT_LIMIT]`, and change the image branch to:

```python
        if m.get("content_type") == "image":
            out.append(_image_row(m, mid=mid, ts=ts, role=role,
                                  token=token, caption_budget=caption_budget))
            continue
```

Do the same in `_read_messages` (role is always `"user"` there; use its own `caption_budget = [_CAPTION_DECRYPT_LIMIT]`).

> Note: `_read_tail` slices `result[-limit:]` AFTER the loop, so the caption budget is spent on the *oldest* image rows first. That is acceptable (the budget is 8 and tail rows are ≤ 60), but if a reviewer objects, spending it newest-first would require restructuring the loop — do not do that in this task.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_v2_serve_worker_images.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 7 pre-existing failures, 0 new.

- [ ] **Step 6: Do NOT commit.**

---

## Task 3: `read_images` dep + capped injection into the responder tail

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`, `backend/model_api_runtime/v2/serve_worker.py`
- Test: `tests/test_v2_worker_images.py` (create)

**Interfaces:**
- Consumes: `context.text_of` (Task 1); `has_image` / `image_mime` markers (Task 2).
- Produces:
  - `worker._TAIL_IMAGE_LIMIT: int = 2` (env `FEEDLING_V2_TAIL_IMAGE_LIMIT`)
  - `worker._IMAGE_MAX_B64_CHARS: int = 2_000_000` (env `FEEDLING_V2_IMAGE_MAX_B64_CHARS`)
  - `TurnDeps.read_images: Callable[[str, list[str]], dict[str, dict]] | None = None` — `(user_id, message_ids) -> {message_id: {"image_mime": str, "image_b64": str}}`
  - `worker._inject_tail_images(tail: list[dict], *, user_id: str, read_images) -> list[dict]` — returns a NEW list; rows without images are the same dict objects.
  - `serve_worker._read_images(user_id, message_ids)` — production impl over `cap_registry.run_capability("chat_image_read", ...)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_worker_images.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker


def _img_row(mid, caption="[image]", mime="image/jpeg"):
    return {"id": mid, "ts": 1.0, "role": "user", "content": caption,
            "has_image": True, "image_mime": mime}


def _fake_reader(payload):
    def _read(user_id, message_ids):
        return {mid: payload[mid] for mid in message_ids if mid in payload}
    return _read


def test_inject_builds_openai_content_blocks_with_caption_first():
    tail = [_img_row("m1", caption="这个报告哪里有问题")]
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/png", "image_b64": "AAAA"}}))
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "这个报告哪里有问题"}
    assert blocks[1] == {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_inject_omits_text_block_for_the_bare_image_marker():
    """`[image]` is our own placeholder, not something the user wrote. Don't send it."""
    tail = [_img_row("m1")]
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": "AAAA"}}))
    assert out[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]


def test_inject_only_takes_the_most_recent_N_images():
    tail = [_img_row(f"m{i}") for i in range(5)]
    payload = {f"m{i}": {"image_mime": "image/jpeg", "image_b64": "AAAA"} for i in range(5)}
    out = worker._inject_tail_images(tail, user_id="u", read_images=_fake_reader(payload))
    injected = [i for i, r in enumerate(out) if isinstance(r["content"], list)]
    assert injected == [3, 4]                       # newest _TAIL_IMAGE_LIMIT=2
    assert out[0]["content"] == "[image]"           # older rows stay text
    assert worker._TAIL_IMAGE_LIMIT == 2


def test_inject_skips_oversized_image_and_keeps_text():
    tail = [_img_row("m1", caption="看看这个")]
    big = "A" * (worker._IMAGE_MAX_B64_CHARS + 1)
    out = worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": big}}))
    assert out[0]["content"] == "看看这个"           # degraded to text, turn still answers


def test_inject_degrades_silently_when_reader_raises():
    def _boom(user_id, message_ids):
        raise RuntimeError("enclave down")
    tail = [_img_row("m1", caption="看看这个")]
    out = worker._inject_tail_images(tail, user_id="u", read_images=_boom)
    assert out[0]["content"] == "看看这个"           # no-filler: never fail the turn


def test_inject_is_a_noop_without_a_reader_or_without_images():
    tail = [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    assert worker._inject_tail_images(tail, user_id="u", read_images=None) == tail
    assert worker._inject_tail_images(tail, user_id="u", read_images=_fake_reader({})) == tail


def test_inject_does_not_mutate_the_input_tail():
    """compaction shares read_tail's rows; mutating them would poison the summarizer."""
    tail = [_img_row("m1", caption="c")]
    original = dict(tail[0])
    worker._inject_tail_images(
        tail, user_id="u",
        read_images=_fake_reader({"m1": {"image_mime": "image/jpeg", "image_b64": "AAAA"}}))
    assert tail[0] == original
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_worker_images.py -q
```

Expected: FAIL — `AttributeError: ... has no attribute '_inject_tail_images'`.

- [ ] **Step 3: Implement in `worker.py`**

Add constants near `_TAIL_HARD_CAP`:

```python
# 每回合最多注入最近 N 张图。enclave 单线程（每张图一次往返），且无 prompt caching ——
# tail 里的图片每个回合都要重发，token 成本随图片数线性上升。
_TAIL_IMAGE_LIMIT = int(os.environ.get("FEEDLING_V2_TAIL_IMAGE_LIMIT", "2"))
# 单张图 b64 上限；超限跳过注入、退化成文本标记（不引入图像缩放依赖）。
_IMAGE_MAX_B64_CHARS = int(os.environ.get("FEEDLING_V2_IMAGE_MAX_B64_CHARS", "2000000"))
```

Add the field to `TurnDeps` (after `write_summary`):

```python
    # (user_id, message_ids) -> {message_id: {"image_mime": str, "image_b64": str}}：只对
    # 指定的图片消息做 enclave 解密。**不能**并进 read_tail —— compaction 用 limit=10_000 调
    # read_tail，b64 会进摘要器 prompt，且该用户历史上每张图都会被解密一次。默认 None：
    # worker.py 自身不 import hosted/capabilities 的装配细节；生产装配见 serve_worker。
    read_images: Callable[[str, list[str]], dict[str, dict]] | None = None
```

Add the pure-ish injector (module level, above `process_job`):

```python
def _inject_tail_images(tail: list[dict], *, user_id: str, read_images) -> list[dict]:
    """把 tail 里最近 `_TAIL_IMAGE_LIMIT` 个图片行的 content 换成 OpenAI 风格 content block
    列表（caption 文本块在前、图片块在后）。返回**新列表**，绝不原地改输入行——compaction
    共用 read_tail 产出的那些 dict。

    任何失败（无 reader / 解密抛错 / 超尺寸 / 缺字段）都静默降级成原来的文本行：用户拿到
    一条看不见图的回复，好过拿到 error chip（no-filler 铁律）。
    """
    if read_images is None:
        return tail
    targets = [r for r in tail if r.get("has_image") and r.get("id")]
    if not targets:
        return tail
    wanted = [str(r["id"]) for r in targets[-_TAIL_IMAGE_LIMIT:]]
    try:
        fetched = read_images(user_id, wanted) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("[v2.worker] read_images failed for %s: %s", user_id, e)
        return tail

    out: list[dict] = []
    for row in tail:
        got = fetched.get(str(row.get("id"))) if row.get("has_image") else None
        b64 = str((got or {}).get("image_b64") or "")
        if not b64 or len(b64) > _IMAGE_MAX_B64_CHARS:
            if got and b64:
                log.warning("[v2.worker] image too large, sending text only (msg=%s, %d chars)",
                            row.get("id"), len(b64))
            out.append(row)
            continue
        mime = str(got.get("image_mime") or "image/jpeg")
        blocks: list[dict] = []
        caption = context.text_of(row.get("content"))
        # `[image]` 是我们自己塞的占位符，不是用户写的字——别当成用户的话发给模型。
        if caption and caption != "[image]":
            blocks.append({"type": "text", "text": caption})
        blocks.append({"type": "image_url",
                       "image_url": {"url": f"data:{mime};base64,{b64}"}})
        out.append({**row, "content": blocks})
    return out
```

- [ ] **Step 4: Wire it into both responder paths**

In `process_job`'s chat path, immediately after the `summary, tail = ...` read inside `async with enclave_sem:`, still **inside** the semaphore block:

```python
                    tail = await asyncio.to_thread(
                        _inject_tail_images, tail, user_id=user_id, read_images=deps.read_images)
```

In `_run_wake`, do the same immediately after its `tail = await asyncio.to_thread(deps.read_tail, ...)` line, inside that function's `async with enclave_sem:` block (the wake nudge is appended afterwards, so it is unaffected).

Both call sites must be inside the existing `enclave_sem` block — that is the invariant that bounds enclave concurrency.

- [ ] **Step 5: Implement `_read_images` in `serve_worker.py`**

```python
def _read_images(user_id: str, message_ids: list[str]) -> dict[str, dict]:
    """按 id 取图片字节。复用**仍然注册、但 planner 词表够不到**的 chat_image_read
    capability（见 multimodal spec §4.0）—— 能力还在，只是不再由模型选择、不再流经
    responder 的文本 grounding context（那是 BUG-1 的成因）。

    单条失败只跳过那一条，绝不抛——调用方 `worker._inject_tail_images` 会把缺失的图片
    行原样留成文本。
    """
    store = core_store.get_store(user_id)
    token = _mint_runtime_token(user_id)
    out: dict[str, dict] = {}
    for mid in message_ids:
        try:
            res = cap_registry.run_capability(
                "chat_image_read", store, api_key=None, runtime_token=token,
                params={"message_id": mid})
            data = (res.to_dict() or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            log.warning("[v2.serve_worker] image read failed msg=%s: %s", mid, e)
            continue
        if data.get("image_b64"):
            out[str(mid)] = {"image_mime": data.get("image_mime") or "image/jpeg",
                             "image_b64": data["image_b64"]}
    return out
```

Add `read_images=_read_images` to `build_production_deps`'s `TurnDeps(...)` construction.

`serve_worker.py` does **not** currently import `cap_registry` — add `from capabilities import registry as cap_registry` to its import block (verified: its imports run `accounts`/`agent_runtime`/`core`/`hosted`/`model_api_runtime.v2` only). `log` already exists (`serve_worker.py:70`), `core_enclave` already exists (`:58`).

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_v2_worker_images.py tests/test_v2_worker.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 7 pre-existing failures, 0 new.

- [ ] **Step 7: Do NOT commit.**

---

## Task 4: End-to-end — the image block actually reaches the provider payload

Unit tests prove `_inject_tail_images` builds blocks. They do **not** prove the blocks survive `responder.respond` → `provider_client` → the wire. That seam is exactly where a "multimodal" change silently degrades to text.

**Files:**
- Test: `tests/test_v2_multimodal_e2e.py` (create)
- Modify: `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`

**Interfaces:** consumes everything above.

- [ ] **Step 1: Write the test**

Create `tests/test_v2_multimodal_e2e.py`:

```python
"""The block must survive responder -> provider_client -> the actual HTTP body,
for BOTH wire families. A unit test on the injector cannot show this."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx
import pytest

import provider_client
from model_api_runtime.v2 import responder as v2_responder

_BLOCKS = [
    {"type": "text", "text": "这个报告哪里有问题"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


def _resp(url, reply_json):
    return httpx.Response(200, json=reply_json, request=httpx.Request("POST", url))


def test_openai_compatible_wire_carries_the_image_block(monkeypatch):
    """openai/openai_compatible/deepseek/openrouter take the native ASYNC transport."""
    captured = []

    async def _fake_apost(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_apost)
    cfg = provider_client.ProviderConfig(
        provider="openai_compatible", model="m", api_key="k", base_url="http://x")
    reply = asyncio.run(v2_responder.respond(
        provider_config=cfg, summary="", tail=[{"role": "user", "content": _BLOCKS}]))
    assert reply == "ok"
    sent = captured[0]["messages"][-1]["content"]
    assert isinstance(sent, list)
    assert sent[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_anthropic_wire_maps_the_image_block(monkeypatch):
    """anthropic + gemini do NOT use the async transport: reliable_chat_completion_async
    bounces them through `anyio.to_thread.run_sync(chat_completion, ...)` (provider_client
    :1200-1208), i.e. the SYNC httpx.Client. Patching httpx.AsyncClient here would capture
    nothing and the test would fail on an empty list rather than on the wire shape."""
    captured = []

    def _fake_post(self, url, **kw):
        captured.append(kw.get("json"))
        return _resp(url, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-x", api_key="k", base_url="")
    reply = asyncio.run(v2_responder.respond(
        provider_config=cfg, summary="", tail=[{"role": "user", "content": _BLOCKS}]))
    assert reply == "ok"
    sent = captured[0]["messages"][-1]["content"]
    assert isinstance(sent, list)
    img = [p for p in sent if p.get("type") == "image"][0]
    assert img["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
```

Both patch targets are verified: `provider_client._async_http_client()` returns a module-level
shared `httpx.AsyncClient` (`:1156-1162`), so patching the **class**'s `post` still intercepts it.
Do not change `provider_client`. If a test captures an empty `captured` list, you patched the
wrong transport — re-read `reliable_chat_completion_async`'s provider branch, do not relax the
assertion.

- [ ] **Step 2: Run it**

```bash
python -m pytest tests/test_v2_multimodal_e2e.py -q
```

Expected: PASS. If the anthropic case fails because the image block is flattened to text, that is a **real finding** — report it, do not weaken the assertion.

- [ ] **Step 3: Update the parity matrix**

In `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`, replace the `**chat image**` row's V2 cell and verdict with:

```
| **chat image** | ✅ `io_cli chat-image`; **codex attaches images natively** (`chat_resident_consumer.py:2768`); non-native drivers get a local file path (`:371-379`) | ✅ in-band: `serve_worker._read_images` → `worker._inject_tail_images` → OpenAI content blocks → `provider_client` (openai/anthropic/gemini wires). Capped at `_TAIL_IMAGE_LIMIT=2`/turn. Caption now decrypted (was silently dropped). | aligned |
```

And in §E, mark BUG-1 resolved: append to the BUG-1 paragraph —

```
**RESOLVED (2026-07-10).** Two rounds: the agent-loop round removed `chat_image_read` from the
planner vocabulary and hardened `_fold_action_results` (blob-key strip + per-action cap); the
multimodal round routes images **in-band through the tail** as real content blocks, so no image
byte ever enters the text grounding context. `chat_image_read` remains registered and is called
directly by `serve_worker._read_images` — never by the planner.
```

- [ ] **Step 4: Full suite**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 7 pre-existing failures, 0 new.

- [ ] **Step 5: Do NOT commit.** Report the final working-tree file list.

---

## Traceability (parity matrix gate)

| Test | Parity row |
|---|---|
| `test_build_turn_messages_passes_image_blocks_through_verbatim` | §A chat image |
| `test_caption_envelope_rebuilds_from_prefixed_keys` | §A chat image (caption loss) |
| `test_inject_only_takes_the_most_recent_N_images` | spec §4.1 caps |
| `test_inject_degrades_silently_when_reader_raises` | no-filler invariant |
| `test_inject_does_not_mutate_the_input_tail` | spec §3 (compaction shares the tail) |
| `test_openai_compatible_wire_carries_the_image_block` | §A chat image, wire |
| `test_anthropic_wire_maps_the_image_block` | §A chat image, wire |

## Out of scope

Prompt caching; image resizing (would add Pillow); re-adding `chat_image_read` to the planner vocabulary (**explicitly rejected**, spec §2); BUG-2 / BUG-3; `schedule_wake`; dream / screen_watch lanes.
