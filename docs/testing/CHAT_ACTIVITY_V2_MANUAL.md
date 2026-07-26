# Runtime V2 chat activity — local manual test

Run this only after both local branches are built:

- backend: `codex/chat-activity-timeline`
- iOS: `codex/chat-activity-timeline-mock`

Do not point the test app at a shared environment until the local checks pass.
The account must be owned by Runtime V2. A V1/resident account is a deliberate
negative control and must keep the old typing indicator with no activity list.

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
3. One card in a custom bucket such as `妈妈`.
4. A nonsense search term that matches no card.

Only the returned count and canonical category keys may appear in activity
metadata. Search terms, summaries, contents, threads, and custom bucket labels
must not appear.

## Cases to repeat on Codex and Pi

### A. Live activity and final placement

Send: “请先搜索与我们的关系和家人有关的记忆，再根据找到的内容回答。”

Expected:

1. The legacy dots appear first; once the backend confirms V2, the live row
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

Use a query that returns the custom `妈妈` card (it may also return canonical
cards).

Expected: total-only copy such as “涌现 2 条相关记忆”. The custom label and every
partial category breakdown are omitted.

### E. Confirmed side effect

Ask for a short reminder, then cancel it in a second turn.

Expected: schedule/cancel rows become successful only after their durable effect
has a confirmed disposition. A failed or uncertain effect must not be shown as
successful.

### F. V1 negative control

Run one resident/V1 account against the same iOS build.

Expected: the old dots and normal reply remain unchanged. The V2 activity
endpoint returns `404 turn_activity_not_found`; no phase or tool row is shown.

## Optional wire check

Take the `user_message.id` from the accepted hosted send and query:

~~~http
GET /v1/chat/turn-activity/{user_message.id}
X-API-Key: TEST_ACCOUNT_KEY
~~~

Check that:

- `runtime` is `v2`;
- every event has a backend job/call identity and a state;
- memory events have `memory_count`;
- `memory_categories`, when present, sums exactly to `memory_count`;
- no tool args, result body, assistant prose, reasoning, query, summary, content,
  thread, or raw/custom bucket label appears anywhere in the response.
