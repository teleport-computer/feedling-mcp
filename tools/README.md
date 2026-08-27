---
document_lifecycle: current
canonical_owner: self
---
# tools/

Operator-facing utilities for Feedling. Each entry is independent — none
of these are imported by the backend at runtime.

## `chat_resident_consumer.py` — independent resident chat bridge

A long-running daemon that lets an agent backend participate in Feedling chat.
It owns the Feedling poll loop, calls the real agent entry, and posts the reply
back.

### When you need this

Pick the highest-priority path that can honestly own Live connection:

1. **Independent resident consumer** — use `chat_resident_consumer.py`. This is the normal path for Hermes / OpenClaw / Mac mini / VPS agents.
2. **HTTP/API agent backend** — still use `chat_resident_consumer.py`; it polls Feedling and POSTs user messages into your API.
3. **Long-lived desktop runtime** — only skip the bridge if that desktop/runtime process truly stays alive and keeps polling without another operator prompt.

| Your agent runtime | Use chat-resident? |
|--|--|
| Server-resident agent daemon that already owns Feedling polling itself | **No.** It is already the resident. |
| Hermes / OpenClaw / Claude Code on a Mac mini or VPS | **Yes.** Run this independent consumer and point it at the runtime's HTTP or CLI entry. |
| Hermes CLI / mcporter / any CLI that exits after one invocation | **Yes.** The consumer keeps the long-running loop and invokes the CLI per message. |
| Custom Python script that just makes HTTP requests | **Yes.** |
| Plain Anthropic / OpenAI API loop | **Yes.** |
| Local Llama / Ollama / vLLM serving a `/chat` endpoint | **Yes.** |
| A CLI tool you want to use as the agent (Hermes-CLI, etc.) | **Yes.** |

If you're in the "Yes" rows, `chat_resident_consumer.py` is the bridge. The
test is whether Feedling has a long-running poll owner, not brand name and not
whether agent tools exist in some other surface.

### What it does

1. Long-polls `GET {FEEDLING_API_URL}/v1/chat/poll` for new user messages.
2. Fetches each message's plaintext from a configured **decrypt source**
   (the enclave's `/v1/chat/history` mirror).
3. Calls your agent backend with the plaintext message and, for image
   messages, the decrypted image context (HTTP POST or CLI invocation,
   configurable).
4. Wraps the reply text into a v1 envelope using
   `backend/content_encryption.py` (imported at runtime) and POSTs it
   back to `/v1/chat/response`.
5. Maintains a checkpoint file so it never re-processes old messages
   after restart.

`/v1/chat/poll` is a responder endpoint. It claims a short lease on each
user message so two auto-reply surfaces do not both answer the same IO turn.
A read-only web chat UI should render `/v1/chat/history`; only the component
that will actually reply should poll.

For image messages (`content_type=image`), the daemon extracts `image_b64`
from the decrypt source. OpenAI-compatible HTTP backends receive a
multimodal `image_url` block, simple HTTP backends receive an `images`
array, and CLI backends receive local image file paths in the message
or in `{image_path}` / `{image_paths}` template slots.

### Quick start

```bash
# Use the latest official checkout before installing the service:
# git fetch origin main && git pull --ff-only origin main

cp deploy/chat_resident.env.example ~/feedling-chat-resident.env
chmod 600 ~/feedling-chat-resident.env
# Edit ~/feedling-chat-resident.env — fill FEEDLING_API_URL, FEEDLING_API_KEY,
# AGENT_MODE, and FEEDLING_ENCLAVE_URL.

# Install the consumer's Python deps into the same Python environment that will
# run the daemon. Proactive V2 jobs import backend DB modules, so psycopg and
# psycopg_pool must be present even when normal chat replies only use HTTP.
python -m pip install -r tools/chat_resident_requirements.txt
python -c 'import httpx, psycopg, psycopg_pool'

# Run in the foreground for testing
python tools/chat_resident_consumer.py

# Install as a systemd service for production on a root/server deployment
sudo cp deploy/feedling-chat-resident.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedling-chat-resident
sudo systemctl status feedling-chat-resident
```

For a user-space agent host such as Hermes/OpenClaw on a VPS, install it as
a user service instead of nesting it under the top-level gateway:

```ini
# ~/.config/systemd/user/feedling-chat-resident.service
[Unit]
Description=Feedling Chat Resident Consumer
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/openclaw/work/feedling-mcp
EnvironmentFile=/home/openclaw/feedling-chat-resident.env
ExecStart=/home/openclaw/.hermes/hermes-agent/venv/bin/python /home/openclaw/work/feedling-mcp/tools/chat_resident_consumer.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Then:

```bash
cd /home/openclaw/work/feedling-mcp
/home/openclaw/.hermes/hermes-agent/venv/bin/python -m pip install -r tools/chat_resident_requirements.txt
/home/openclaw/.hermes/hermes-agent/venv/bin/python -c 'import httpx, psycopg, psycopg_pool'
systemctl --user daemon-reload
systemctl --user enable --now feedling-chat-resident.service
journalctl --user -u feedling-chat-resident.service -f
```

Before Step 6 / the first IO greeting, verify that the service is running the
same checkout you just updated:

```bash
cd /home/openclaw/work/feedling-mcp
git fetch origin main
git rev-parse --short HEAD
git rev-parse --short origin/main
systemctl --user cat feedling-chat-resident.service
```

`HEAD` and `origin/main` should match, and the service `WorkingDirectory` /
`ExecStart` should point at that checkout. If not, update the checkout or point
the service at a fresh clone, then restart only `feedling-chat-resident`.

The resident consumer may call Hermes/OpenClaw through `AGENT_CLI_CMD` or
`AGENT_HTTP_URL`, but it should be supervised as its own process. Do not
make it a child job inside the current Hermes chat turn or the top-level
Hermes gateway; otherwise the IO chat loop dies or restarts with that host
process.

### Auto-update

Once installed, the consumer keeps itself on the commit the backend deploys —
you no longer have to remember to `git pull` + restart after a release.

How it works: the backend advertises its deployed commit in every chat-poll
response (`client_release.expected_consumer_commit`). At an idle poll the
consumer compares it to its own `HEAD`. **Only if** the difference actually
touches a file this consumer loads does it `git fetch` + check out that commit
and re-exec in place. The relevant-file set is auto-derived from the modules it
imports, plus `tools/io_cli.py`, the requirements files, and the lazily-imported
runtime surface (`backend/proactive/`, `backend/content_encryption.py`) that may
not be in `sys.modules` yet on a fresh idle consumer. A backend release that
doesn't touch any consumer code triggers nothing. `io_cli.py` rides along in the
same checkout — no separate update.

- **Default on.** Set `FEEDLING_AUTO_UPDATE=0` in your env file to opt out and
  manage updates manually (the verification steps above).
- **Dirty working tree is never touched.** If you have local uncommitted edits,
  the consumer logs a warning and skips the update instead of clobbering them.
  `git stash` / commit (or set `FEEDLING_AUTO_UPDATE=0`) to control this.
- **Detached HEAD after update.** Updating pins the checkout to the backend's
  exact commit (detached HEAD). To take over manually, `git checkout main`.
- **If requirements changed**, the consumer runs `pip install -r` for the
  changed requirements file before re-exec, best-effort.
- **Hosted (in-CVM) runs are excluded** — that code is baked into an attested,
  immutable image and is updated by redeploying the image, not by self-pull.

### ⚠️ Decrypt source is mandatory

The backend stores all user chat messages as v1 encrypted envelopes.
`/v1/chat/poll` returns these with `content=""` — the daemon **must**
be pointed at a decrypt source to read what the user wrote.

Set one of:

- **`FEEDLING_ENCLAVE_URL`** (recommended) — direct HTTPS to the
  enclave's decrypt proxy.
- **`FEEDLING_MCP_URL`** (legacy — no longer usable) — used to fall back
  to `feedling_chat_get_history` on the MCP server. The MCP server was
  removed on 2026-06-12, so this path is dead; use `FEEDLING_ENCLAVE_URL`.

Without either, the daemon logs `"no plaintext content"` for every
incoming message and never replies. You'd see this as: iOS app shows
your messages going out, but the agent never produces a response.

### Agent backend modes

#### `AGENT_MODE=http`

Use this when your agent exposes a JSON HTTP endpoint:

```
AGENT_MODE=http
AGENT_HTTP_URL=http://127.0.0.1:8080/chat
AGENT_HTTP_TOKEN=                            # Bearer token if your endpoint requires auth
AGENT_HTTP_FIELD=response                    # JSON field that contains the reply text
# AGENT_HTTP_CANCEL_URL=http://127.0.0.1:8080/cancel  # recommended for voice
```

The daemon POSTs `{"message": "<user text>"}` and reads the named field
from the JSON response. For image messages it also includes
`images: [{"mime_type", "data", "data_url"}]`.

During a voice turn, the same endpoint receives `"stream": true` and
`"stream_format": "ndjson"`. A simple HTTP runtime may keep returning its
normal JSON response, or opt into incremental speech with
`Content-Type: application/x-ndjson` (SSE is also accepted):

```jsonl
{"type":"text_delta","delta":"你"}
{"type":"text_delta","delta":"好"}
{"type":"result","body":{"response":"你好。"}}
```

`text_delta` is only for user-visible assistant text. Never put reasoning,
tool arguments, logs, or status text in it. The terminal `result.body` remains
the canonical complete runtime response and is parsed and stored exactly like
the non-streaming JSON response. A runtime that does not implement this
optional protocol keeps the original buffered behavior.

While a voice request is active, the consumer watches the existing claim-free
chat poll. Any newer user turn in the same call - either a revised transcript
or a real barge-in - terminates any CLI subprocess immediately. For HTTP
runtimes the consumer closes the response stream and, when
`AGENT_HTTP_CANCEL_URL` is configured, POSTs
`{"request_id":"<X-Feedling-Request-Id>"}` to that endpoint. The cancellation
endpoint should be idempotent and return after the runtime accepts the
cancellation. The obsolete request is never posted as a reply; the newer turn
is processed normally by the resident loop.

An HTTP endpoint does not receive local file authority by default. If the HTTP
agent runs on the same machine, can execute commands in this checkout, and
inherits the resident's `FEEDLING_HOME`, enable the existing `io_cli` delivery
surface explicitly:

```
FEEDLING_AGENT_HTTP_LOCAL_IO_CLI=true
FEEDLING_AGENT_HTTP_LOCAL_FILE_ROOTS=/absolute/path/to/the/agent/workspace
```

This lets the agent stage downloadable files and self-contained Canvas files
ending in `.io.html`. `send-file` may read a generated source from the explicit
workspace root and the resident leaves that original intact. Canvas delivery
also passes `--title` and `--subtitle`; both use the user's current language and
may be preserved or updated when the Canvas is revised. Keep the root as
narrow as the agent's workspace; never configure a home directory or filesystem
root. Leave both settings off for ordinary model servers, Ollama, vLLM, and
remote APIs that cannot execute `tools/io_cli.py` locally.

For Hermes' API server, use the OpenAI-compatible protocol instead of the
simple JSON shape:

```
AGENT_MODE=http
AGENT_HTTP_PROTOCOL=openai
AGENT_HTTP_URL=http://127.0.0.1:8642/v1/chat/completions
AGENT_HTTP_MODEL=hermes-agent
# AGENT_HTTP_SESSION_KEY is optional; defaults to feedling:{user_id}.
```

The daemon sends `X-Hermes-Session-Key`, stores the returned
`X-Hermes-Session-Id`, and sends it back on later turns.
On voice turns it requests standard OpenAI-compatible SSE and speaks only
`choices[0].delta.content`; `reasoning_content` and `tool_calls` are preserved
for the final response but are never sent to speech. If the endpoint rejects
streaming, the daemon retries that request once with `stream: false`.

#### `AGENT_MODE=cli`

Use this when your agent is a command-line tool:

```
AGENT_MODE=cli
AGENT_CLI_CMD=mycli ask {message}
```

If the configured CLI exposes a callable native image-generation tool, declare
that exact resident capability explicitly:

```
FEEDLING_AGENT_IMAGE_GENERATION=true
```

Do not set this merely because the selected language model or provider can
generate images through some separate API. The agent entry itself must be able
to invoke the tool and write a PNG, JPEG, or WebP for `io_cli.py send-image`.
When declared, a missing dedicated image route falls through to the resident's
native tool; a user-selected dedicated image route still takes precedence.

A known-good **claude** command that can actually see chat images (assumes
`IMAGE_TEMP_DIR=/home/agent/images`; adjust the path to yours — see the vision
notes below for why `--add-dir` and the `//` are required):

```
AGENT_MODE=cli
IMAGE_TEMP_DIR=/home/agent/images
AGENT_CLI_CMD=claude --permission-mode acceptEdits --add-dir /home/agent/images --allowed-tools 'Read(//home/agent/images/**)' -p {message}
```

`{message}` is substituted with the user's plaintext message. The
command's stdout becomes the reply. For image messages, the consumer writes
the decrypted image to `IMAGE_TEMP_DIR` and either appends the file path to
`{message}` or fills explicit `{image_path}` / `{image_paths}` placeholders
if your CLI supports image arguments.

**Getting the model to actually see the image (CLI mode).** Writing the path
into `{message}` only tells the model a file exists — it does not feed pixels.
Make sure the image reaches the model as real vision input:

- **codex** (`codex exec …`): the consumer auto-attaches each decrypted image
  with an `--image=<path>` flag (codex's native image input; the `=`-bound form
  keeps clap's variadic `--image` from swallowing the positional prompt), so no
  template change is needed. If you wire your own `-i {image_path}` /
  `{image_paths}`, the consumer respects it and does not double-attach.
- **claude** (`claude -p …`): claude opens the image via its `Read` tool, which is
  gated by **two** things — get either wrong and the read is silently denied, the
  bubble stays blank, and a vision model will often *hallucinate* the contents
  instead of admitting it saw nothing:
    1. **Workspace boundary.** Headless `claude -p` refuses to read files outside its
       current working directory (plus any `--add-dir`) *before* it even consults the
       allowlist. `IMAGE_TEMP_DIR` is almost always outside your CLI's cwd, so pass
       **`--add-dir <IMAGE_TEMP_DIR>`** (e.g. `--add-dir /home/agent/images`). This is
       the single most reliable fix — `--add-dir` makes files there readable and does
       not widen Bash. (Alternatively, launch claude with its cwd at the image dir's
       parent so `images/` is inside the workspace.)
    2. **Allow-rule path syntax.** In `--allowed-tools`, a Read rule's path is
       gitignore-style: a **single** leading slash anchors at the *settings source*
       (your cwd), **not** the filesystem root. So `Read(/home/agent/images/**)`
       silently means `<cwd>/home/agent/images/**` and never matches — you must use a
       **double** slash for a filesystem-absolute path:
       `Read(//home/agent/images/**)`. (With `--add-dir` this rule is optional, but if
       you pin a strict `--allowed-tools` allowlist, use the `//` form.)
  The managed (hosted) default command already passes `--add-dir` and the `//` rule;
  VPS operators pinning their own command must add them.
- Any CLI with a first-class image flag: use the `{image_path}` / `{image_paths}`
  template slot so pixels are attached, not just referenced.

Screen context defaults to `SCREEN_CONTEXT_MODE=tool`: ordinary turns do not
prefetch frames, and the model uses `screen-recent` / `screen-read` when the
screen matters. Set `SCREEN_CONTEXT_MODE=auto` or `always` to restore eager live
share attachment (higher latency, token cost, and privacy exposure).

When running under `systemd`, do not assume your interactive shell `PATH`
is available. Prefer an absolute executable path in `AGENT_CLI_CMD`; if that
is not stable, set `AGENT_CLI_PATH` to the directory that contains the agent
binary.

**CLI agents should produce structured stdout.** Prefer valid JSON with a
single final-answer field such as `{"reply":"..."}` plus optional
`session_id`. The daemon reads the reply field directly and treats human
terminal UI as a fallback path only. Session IDs, prompts, debug footers,
and decorative output can still leak if the CLI does not offer JSON/quiet
mode, so do not depend on text cleanup for normal operation.

For self-hosted CLI residents, the model receives a compact command catalog as
capability discovery. It calls the cataloged command directly; `--help` is only
a one-time correction when the catalog lacks detail or the command reports a
parameter error. The model still chooses whether and when to use a tool; the
resident does not route by keywords.

Cold/rebuilt sessions receive at most eight meaningful recent chat rows by
default (`FEEDLING_FOREGROUND_CHAT_CONTEXT_LIMIT=8`). Voice-call archive cards
are not replayed into that bridge; the model can inspect a relevant call with
`voice-transcript-list` / `voice-transcript-read`. Foreground World Book matching
also defaults to `FEEDLING_FOREGROUND_WORLDBOOK_CONTEXT=tool`, using
`worldbook-match --query ...` only when the model decides the setting matters.
Set the World Book mode to `eager` as a rollback.

##### Hermes example

```
AGENT_CLI_PATH=/home/openclaw/.local/bin:/home/openclaw/.hermes/hermes-agent/venv/bin
HERMES_HOME=/home/openclaw/.hermes/profiles/daily
AGENT_CLI_CMD=hermes chat -Q --source tool --max-turns 60 -q "{message}"
```

Do not put `--continue` in `AGENT_CLI_CMD`. On the first turn, Hermes creates
a session and prints `session_id`; the consumer stores it. On later turns the
consumer injects `--resume <session_id>` so Feedling is bound to the same
conversation instead of whichever local Hermes session happens to be latest.
Set `HERMES_HOME` to the same home/profile used by the user's real running
resident agent entry. Do not guess it from folder names; read it from the
actual service environment when available:

```bash
pid=$(systemctl --user show -p MainPID --value hermes-gateway)
tr '\0' '\n' < /proc/$pid/environ | grep '^HERMES_HOME='
```

Some Hermes/OpenClaw installs use `/home/openclaw/.hermes`; others use
`/home/openclaw/.hermes/profiles/daily`. The resident consumer must match the
running agent, otherwise CLI calls can fail auth or drift into the wrong
persona/session. Do not wrap `{message}` in a special persona prompt such as "You are
Dora..." or "reply naturally"; the resident should call the same agent profile
the user already trusts, with IO as only a new transport.

Before installing the daemon, run the exact Hermes command in a terminal with
a normal user message, a direct identity question, and one tool-using question.
Confirm stdout is a real model reply in the agent's voice each time. If it
returns a shell like "我看到了：<message>。你要我继续展开哪一块?", says tools are
unavailable for a normal tool-using request, prints internal reasoning, or
returns another template, the resident is correctly forwarding messages but the
configured CLI command is not reaching a production-quality agent session. Fix
`HERMES_HOME`, `AGENT_CLI_CMD`, toolset access, max-turns, or session selection
before running it as a service.

##### Claude Code CLI example

```
AGENT_CLI_PATH=/home/openclaw/.npm-global/bin:/home/openclaw/.local/bin
AGENT_CLI_CMD=claude --print --output-format json {mcp} "{message}"
```

`{mcp}` is what carries the MCP servers you enabled in the app. On a chat turn
the consumer expands it to `--mcp-config <file>`; on background turns, and when
you have no enabled servers, it expands to nothing. **A command without it still
works** — the consumer injects the same wiring itself when it sees a `claude`
command with no placeholder — but keep the placeholder if you care where the
flags land, and see "user MCP with a hand-written Claude command" below for the
one case the injection deliberately leaves alone.

The consumer reads Claude Code's `session_id` from JSON output and injects
`--resume <session_id>` on later turns. Do not use `--continue`: it means
"latest local conversation" and can attach IO to the wrong session. If the
service environment cannot find `claude`, use an absolute executable path or set
`AGENT_CLI_PATH`.

The bare command above cannot **see images / screen frames** — claude needs its
image dir in the workspace and a correctly-anchored Read grant. Add
`--add-dir <IMAGE_TEMP_DIR>` and `--allowed-tools 'Read(//<IMAGE_TEMP_DIR>/**)'`
(note the **double** slash — see "Getting the model to actually see the image"
above for why a single slash silently fails), e.g.:

```
AGENT_CLI_CMD=claude --print --output-format json --add-dir /home/agent/images --allowed-tools 'Read(//home/agent/images/**)' "{message}"
```

To also let the agent pull screen frames / photos on its own
(`io_cli screen-read --include-image`, `photo-read --include-image`), grant the
matching `Bash(python <io_cli> …)` verbs too — those tools now save the decrypted
picture into `IMAGE_TEMP_DIR` and return an `image_file` path the same Read grant
covers.

##### Codex CLI example

```
AGENT_CLI_CMD=codex exec --json --skip-git-repo-check --sandbox read-only
FEEDLING_CODEX_SESSION_RESUME=true
```

When the installed CLI supports `codex exec resume`, the consumer persists the
`thread.started` id and resumes it on later turns. Older Codex versions remain
compatible through the existing transcript fallback. Set
`FEEDLING_CODEX_SESSION_RESUME=false` for immediate rollback to that stateless
behavior.

### Session bounds and failure behavior

The resident owns IO-facing session continuity and keeps it bounded. Hermes,
Claude, Pi, and resume-capable Codex each reuse their native session; unsupported
CLI runtimes keep the transcript fallback. Later turns resume until either bound
is reached:

```
AGENT_SESSION_MAX_TURNS=40
AGENT_SESSION_MAX_BYTES=250000
AGENT_SESSION_MAX_INPUT_TOKENS=32768
```

The token bound uses provider-reported input as an exact second signal. The
32K default is based on a measured Codex voice path: roughly 18K on the cold
turn and 34K on the first resume. It keeps that cached resume, then rotates
before a third turn can grow the same context again. It does not truncate a
reply: after the completed turn crosses the bound, the next turn starts a fresh
native session and receives the bounded canonical-history bridge.

The resident also applies a capability-detected minimal voice profile by
default:

```
FEEDLING_MINIMAL_RUNTIME_PROFILE=auto
```

Codex drops optional apps/plugins/memories and skill discovery/install surfaces
while keeping configured user skills, shell, native vision, user MCP, and IO
tools. Pi drops its optional
skill/template/theme catalogs, and Claude disables slash-command discovery when
the installed CLI advertises those flags. Unknown/older CLIs receive no guessed
flags. Set the value to `off` for immediate rollback.

Codex voice turns also use App Server's native agent-message deltas when the
installed CLI supports it and the configured `codex exec` options can be mapped
exactly:

```
FEEDLING_CODEX_APP_SERVER_STREAM=auto
```

The adapter never sends a turn-level reasoning-effort override. It inherits the
user's Codex config and preserves any explicit model/reasoning config already in
`AGENT_CLI_CMD`. A custom option with no exact App Server equivalent stays on
the established `codex exec` path. Set this value to `off` for rollback.

If a CLI template contains a fixed `--session-id`, the consumer replaces it
with its own bounded session id so one hardcoded session cannot grow forever.
If a resumed session disappeared upstream, the resident clears only that cached
id and retries the current turn fresh once. If a user interrupts an in-flight
voice turn, its session id is also cleared, so the next turn reconstructs from
the canonical Enclave history instead of continuing a half-written model state.

For Enclave-backed history, connect/TLS and response-read budgets are separate:

```
FEEDLING_ENCLAVE_CONNECT_TIMEOUT_SEC=5
FEEDLING_ENCLAVE_READ_TIMEOUT_SEC=20
```

For voice streaming, only the idempotent final marker is retried:

```
FEEDLING_VOICE_STREAM_FINAL_ATTEMPTS=3
```

Agent-entry failures are user-visible by default:

```
SEND_FALLBACK_ON_AGENT_ERROR=true
FALLBACK_REPLY=我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。
```

This prevents a timeout or broken agent entry from silently dropping a user
turn. Empty plaintext caused by a missing decrypt source is still skipped rather
than answered, because the consumer cannot know what the user said.

### Image messages

Image messages are routed to the agent backend as the placeholder
configured in `IMAGE_PLACEHOLDER` plus the decrypted image context. Default:

> `[The user sent an image in IO Chat. Inspect the attached/local image before replying. If your current runtime cannot open the image, say plainly that this connector has not enabled image vision yet.]`

OpenAI-compatible HTTP backends receive the image as a standard
`image_url` block. Simple HTTP backends receive an `images` array. CLI
backends receive a local image file path; use a template such as
`AGENT_CLI_CMD='mycli ask --image "{image_path}" "{message}"'` if your
agent CLI has a first-class image flag.

If the decrypt source cannot provide image bytes, the consumer logs the
failure and routes only the honest placeholder; it should not pretend to
have seen the image.

The above delivers the pixels of the image in the **current** turn. Images
from **earlier** turns are a different case: the recent-chat transcript that
gets injected for cross-turn continuity is text-only and cannot carry pixels,
so a past image turn appears there as `[image] … io_cli chat-image --id <id>`.
An agent that wants to look at that older picture runs
`io_cli chat-image --id <message_id>`, which pulls just that one message's
decrypted image from the enclave and writes it to a Read-able file (same
`IMAGE_TEMP_DIR` + Read grant as `screen-read`/`photo-read --include-image`).
This is lazy on purpose — history images are only decrypted when the agent
actually asks. Do not point the agent at `photo-read` for chat images; that
command serves the perception photo library, a separate feed.

### Re-auth checklist

If you ran any of these on the iOS side:

- `Settings → Delete Account & Reset` (new account, new key)
- `Settings → Storage → Regenerate API Key`
- Migrated to a new self-hosted backend

… you MUST update `~/feedling-chat-resident.env` with the new
`FEEDLING_API_KEY` and `systemctl restart feedling-chat-resident`.
Otherwise tool calls return 401 `user_not_found` and the consumer logs
errors silently in the background.

Verify with:

```bash
curl -s -H "X-API-Key: <new_key>" $FEEDLING_API_URL/v1/users/whoami
# Expect: 200 with the user_id matching what iOS shows
```

### hermes / OpenClaw 用户的 MCP

自托管 hermes 用户在 app 上配置的 MCP server 会自动物化进
`$HERMES_CONFIG_DIR/config.yaml`（默认 `~/.hermes/config.yaml`）的 `mcp_servers`，
hermes 下一回合启动时经 `discover_mcp_tools` 自动发现并注册为 `mcp_<server>_<tool>`。

**前提**：hermes 的 venv 必须装 `mcp` 包（`pip install mcp`），否则 hermes 静默
禁用 MCP，配了也不生效。正规 HTTPS 与自签 CA 均支持（自签走 `SSL_CERT_FILE` 注入
的 concat 信任库）。物化会先把既有 config.yaml 备份成 `config.yaml.feedling-bak`
（pyyaml round-trip 不保留注释）。

### Claude 用户的 MCP（手写命令的那种）

hermes / OpenClaw / codex 是从各自的配置文件里读 MCP 的，命令行怎么写都不影响。
**claude 不是**——它只认命令行上的 `--mcp-config`，所以一条没有 `{mcp}` 的
`AGENT_CLI_CMD` 会让 app 里配的 server 一台都到不了 agent。旧版文档的示例就没有
这个占位符，所以照抄过的命令都中招：app 里显示"已连接、发现 N 个工具"（那是控制面
探针直连服务器测的，确实成功），而 agent 从头到尾不知道这些 server 存在，模型只能
自己编一个说法（"我没有搜索工具"／"我没有权限"）。

现在 chat 轮一定会带上两个 `=` 绑定的参数——有 `{mcp}` 的由占位符展开，没有的由
consumer 直接注入：

    --mcp-config=<file>                 让 server 真的存在
    --allowed-tools=mcp__<name>__*      让调用被预先批准

两个都必须有：实测只给 `--mcp-config`，调用会进 `permission_denials`，模型回
"这个工具需要授权"。托管用户不受影响，因为他们的授权规则本来就在我们生成的
`settings.json` 里；自托管没有那个文件。

用 `=` 绑定是因为这两个 flag 都是变参：手敲这条命令时，裸的
`--mcp-config <path>` 会把后面的提示词当成配置文件路径，claude 直接 exit 1
（`Invalid MCP configuration`）。consumer 自己是把提示词走 stdin 的，走不到这个坑，
但绑定值让任何模板形状都不会踩。

**唯一不自动处理的情况**：你自己在命令里写了 `--allowed-tools`。这时 consumer 只补
`--mcp-config`，不动你的 allowlist（合并语义不该由我们替你猜，覆盖又可能收掉你依赖
的工具），并打一条 warning 告诉你该加哪几条规则。把 `mcp__<name>__*` 加进你的
`--allowed-tools` 或 `settings.json` 即可。

已经写了 `--mcp-config` 的命令，consumer 一律不碰——那是你自己接管了。

---

## `check_chat_pipeline.py` — health check

End-to-end smoke test for the entire chat pipeline.

```bash
FEEDLING_API_URL=http://127.0.0.1:5001 \
FEEDLING_API_KEY=<your_key> \
python tools/check_chat_pipeline.py
```

Verifies four things:

| Check | OK | WARN | FAIL |
|---|---|---|---|
| Backend reachable | HTTP 200/401 | — | connection refused / 5xx |
| API key accepted | 200 | — | 401 Unauthorized |
| Resident consumer running | systemd active or process found | not running | — |
| Recent closed loop | user + assistant messages in last 10 min | unanswered user message | — |

Exit codes: `0` = OK · `1` = WARN · `2` = FAIL.

Common cases:

- "I configured the skill but nothing happens" → consumer not running (WARN on check 3).
- "Messages arrive but no replies" → consumer running but agent call failing (WARN on check 4 + check the consumer's journalctl).
- "Replies contain weird system noise" → CLI agent not configured with clean output mode.

---

## `audit_live_cvm.py` — TDX attestation CLI

Mirrors the 8 audit checks the iOS app runs. Good for CI gates,
third-party reviewers, agents doing "is this safe" checks.

```bash
export FEEDLING_CVM_APP_ID=9798850e096d770293c67305c6cfdceed68c1d28
export FEEDLING_CVM_GATEWAY_DOMAIN=dstack-pha-prod9.phala.network
export FEEDLING_ATTESTATION_URL="https://${FEEDLING_CVM_APP_ID}-5003s.${FEEDLING_CVM_GATEWAY_DOMAIN}/attestation"
export ETH_SEPOLIA_RPC_URL="https://sepolia.infura.io/v3/<key>"
export FEEDLING_APP_AUTH_CONTRACT=0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F

curl -sk "$FEEDLING_ATTESTATION_URL" > /tmp/fl_cvm_attest.json
python3 tools/audit_live_cvm.py
```

Exit code 0 = all rows pass; on prod9 row 8 is a green disclosure about
ingress-terminated MCP TLS. See `docs/AUDIT.md` for what each row proves.

---

## `dcap/` — DCAP quote parser

Python reference parser + verifier for Intel TDX DCAP quotes. Used by
`audit_live_cvm.py` and mirrors `testapp/FeedlingTest/DCAP/` (Swift) on
iOS so the audit logic is identical on both surfaces. Standalone tests
live in `tools/dcap/test_dcap_parse.py`.

---

## Envelope round-trip tests

| Tool | Verifies |
|---|---|
| `v1_envelope_roundtrip_test.py` | Current backend/iOS-compatible BoxSeal interop plus local chat envelope write/read/decrypt |
| `frame_envelope_roundtrip_test.py` | Current backend/iOS-compatible BoxSeal plus local encrypted frame ingest/persistence |
| `e2e_encryption_test.py` | Full end-to-end: write encrypted, fetch via enclave decrypt proxy, read back plaintext |

The BoxSeal contract is also covered without live services by
`tests/test_v1_envelope_roundtrip_tool.py` and
`tests/test_frame_envelope_roundtrip_tool.py`. Run those drift guards first,
then run the matching local-service tool after touching envelope crypto or
ingest behavior.
