# V1 and V2 chat activity — local manual test

> **状态说明(2026-08-15,Seven 定)**:V1 部分作为**对照组**保留 —— 查 V2 的问题时
> 先拿 V1 当对照(V1 大多数功能是好的,对比能快速定位差异在哪一层)。
> V1 **不是必测路径**:README 里「只剩两条路径需要测」的口径不变,与本文档不矛盾。

Run this only after both local branches are built:

- backend: `codex/chat-activity-timeline`
- iOS: `codex/chat-activity-timeline-mock`

Do not point the test app at a shared environment until the local checks pass.
Run the same cases on a resident-owned V1 account and a Runtime V2 account.

## Driver matrix

Use two test accounts or switch the active model route and repeat the same
cases. Confirm the derived driver before each run:

~~~http
POST /v1/model_api/driver
X-API-Key: TEST_ACCOUNT_KEY
Content-Type: application/json

{}
~~~

| Provider route | Expected driver |
| --- | --- |
| OpenAI | `codex` |
| Gemini, OpenRouter, or openai_compatible | `pi` |

The activity contract is driver-independent: both drivers must produce the same
backend-confirmed event shape for the same native tool.

## Seed data

Prepare memories that can be recognized without reading private text in logs:

1. Three cards in the canonical `我们的关系` / `Our relationship` bucket.
2. One card in the canonical `家庭` / `Family` bucket.
3. A mixed result set containing at least one custom bucket such as `妈妈`; the
   other cards may use canonical buckets. Eleven returned cards is one useful
   example, not a fixed product threshold.
4. A nonsense search term that matches no card.

Only the returned count and canonical category keys may appear in activity
metadata. Search terms, summaries, contents, threads, and custom bucket labels
must not appear.

## Cases to repeat on V1 Codex, Claude, Pi, and V2 routes

### A. Live activity and final placement

Send: “请先搜索与我们的关系和家人有关的记忆，再根据找到的内容回答。”

Expected:

1. The legacy dots appear first; once the backend confirms the turn, the live row
   changes to Working / 正在处理.
2. A real `memory_search` or `memory_fetch` row appears only after that tool
   starts. There is no locally invented “理解问题” or “正在思考” activity row.
3. With the four canonical cards returned, the completed row reads like
   “涌现 3 条关于亲密关系的记忆和 1 条关于家人的记忆” (English locale uses the
   equivalent English copy).
4. If a display-safe reasoning summary exists, it is above the assistant reply.
   Activity is below the reply. They are never interleaved or merged.
5. Reopen Chat. The same completed activity remains on the history item.

### B. One result

Use a query known to return one canonical card.

Expected: the row uses singular copy and the category count is exactly one.

### C. Zero results

Ask the agent to search the nonsense term.

Expected: “没有涌现相关记忆” / “No related memories emerged”. It must not claim a
category.

### D. Unknown or custom category

Use a query that returns a mixed result set, including the custom `妈妈` card.

Expected: total-only copy “涌现 N 条相关记忆”, where `N` is the actual number
of returned memories. For example, eleven returned cards display “涌现 11 条相关
记忆”. The custom label and every partial category breakdown are omitted.

### E. Combined memory-to-download flow

Send a natural request such as:

“请回忆我们相处过程中与亲密关系和家人有关的内容，帮我整理成一份《我们的关系小档案》，生成 PDF 给我下载。”

Repeat once with Markdown as the requested format.

Expected:

1. A confirmed memory row appears first. Complete canonical results use
   “涌现 3 条关于亲密关系的记忆和 1 条关于家人的记忆”; incomplete or custom
   classification uses the total-only fallback with the actual returned count.
2. File-generation activity follows without exposing memory text, search terms,
   tool arguments, or local paths.
3. The final assistant text is followed by a real downloadable PDF/Markdown
   card, grouped under one agent name and linked to the original user request.
4. The downloaded file has the requested format and its content reflects the
   selected memories.

### F. Confirmed side effect

Ask for a short reminder, then cancel it in a second turn.

Expected: schedule/cancel rows become successful only after their durable effect
has a confirmed disposition. A failed or uncertain effect must not be shown as
successful.

### F. Runtime parity

Repeat A–E on resident/V1 and V2. V1 `io_cli` tools and V2 native capability
tools must produce the same display labels and memory-count fallback rules.

## Optional wire check

Take the `user_message.id` from the accepted hosted send and query:

~~~http
GET /v1/chat/turn-activity/{user_message.id}
X-API-Key: TEST_ACCOUNT_KEY
~~~

Check that:

- `runtime` is `v1` or `v2` and matches the selected route;
- every event has a backend job/call identity and a state;
- memory events have `memory_count`;
- `memory_categories`, when present, sums exactly to `memory_count`;
- no tool args, result body, assistant prose, reasoning, query, summary, content,
  thread, or raw/custom bucket label appears anywhere in the response.
