# Self-Hosting Feedling

Run your own Feedling backend on a VPS you control. Your data stays on
your VPS; the Feedling team is not in the loop.

> **What this doc covers:** the ops side — clone, deps, env, systemd,
> HTTPS, DNS, pointing iOS at your URL.
>
> **What this doc does NOT cover:** how your agent should behave during
> bootstrap, identity setup, memory writes, or chat. That's the agent
> skill, and it lives in a separate document:
> <https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md>.
> Hand that URL to your agent after the server is up.

The split is deliberate: this doc is for the human operator. The skill
above is for the agent. Don't conflate them.

---

## A note on encryption

Feedling's v1 envelope encryption runs in self-hosted mode too — the
iOS client wraps content before sending, the chat-resident daemon wraps
replies, and the enclave service runs locally (in simulator mode
without TDX hardware) and decrypts on demand for your agent.
**You don't need to operate any of this**; it's automatic.

The trust-model justification for encryption (an untrusted cloud
operator) doesn't apply when you ARE the operator — but the code path
is shared with cloud for simplicity, gives you defense-in-depth
against accidental disk exposure, backup leaks, and log pollution, and
costs you nothing operationally. If you want a plaintext mode to skip
the crypto entirely, file an issue — it's possible but a real refactor.

---

## 0. Pre-flight

Before you start:

- SSH access to a VPS: `ssh <user>@<host> "uname -a"` returns kernel + arch.
- Python 3.10+: `ssh <user>@<host> "python3 --version"` ≥ 3.10.
- You own a domain you can point at the VPS (required for HTTPS, optional
  for plain HTTP).

---

## 1. Clone the repo

```bash
ssh <user>@<host> "git clone https://github.com/teleport-computer/feedling-mcp ~/feedling-mcp"
```

**Verify:** `ssh <user>@<host> "ls ~/feedling-mcp/backend/asgi_app.py"` prints the path.

---

## 2. Install deps + (optional) APNs key

```bash
ssh <user>@<host> <<'EOF'
cd ~/feedling-mcp
python3 -m venv ~/feedling-venv
~/feedling-venv/bin/pip install -r backend/requirements.txt
mkdir -p ~/feedling-data
EOF
```

**Verify:** `ssh <user>@<host> "~/feedling-venv/bin/python -c 'import httpx, jwt, websockets, psycopg, psycopg_pool'"` exits 0.

If you have an Apple `.p8` push key, scp it onto the VPS:

```bash
scp AuthKey_<KEY_ID>.p8 <user>@<host>:~/feedling-data/
```

Without one, push endpoints log-only (chat / identity / memory still
work; just no Live Activity / Dynamic Island delivery).

---

## 3. Write the env file (multi-tenant)

```bash
ssh <user>@<host> <<EOF
cat > ~/feedling-data/.env <<INNER
FEEDLING_DATA_DIR=/home/$(whoami)/feedling-data
INNER
chmod 600 ~/feedling-data/.env
EOF
```

**Verify:** `ssh <user>@<host> "ls -l ~/feedling-data/.env"` shows `-rw-------`.

The backend is **multi-tenant only** since 2026-04-20. There is no
shared-key `SINGLE_USER` mode. The first API key is minted in step 5 by
`POST /v1/users/register`, which also creates `~/feedling-data/<user_id>/`.

---

## 4. Install + start systemd units

```bash
ssh <user>@<host> <<'EOF'
sudo cp ~/feedling-mcp/deploy/feedling-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedling-backend
EOF
```

**Verify:** `ssh <user>@<host> "sudo systemctl is-active feedling-backend"` prints `active`.

---

## 5. Register the first user

```bash
ssh <user>@<host> <<'EOF'
API_KEY=$(curl -sf -H 'content-type: application/json' \
    -d '{"public_key":"","handle":"owner"}' \
    http://127.0.0.1:5001/v1/users/register \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
echo "$API_KEY" > ~/feedling-data/.api_key
chmod 600 ~/feedling-data/.api_key
curl -s -H "X-API-Key: $API_KEY" http://127.0.0.1:5001/v1/screen/analyze
EOF
```

**Verify:** the smoke-test response includes an `"active"` field. If
you get 401, the key wasn't captured — check
`~/feedling-data/.api_key`.

---

## 6. (Optional but recommended) HTTPS via Caddy

Only run this **after** you've pointed DNS for `api.<your-domain>` and
`api.<your-domain>` at the VPS.

```bash
ssh <user>@<host> <<EOF
sudo cp ~/feedling-mcp/deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/feedling.app/<your-domain>/g' /etc/caddy/Caddyfile
sudo systemctl restart caddy
EOF
```

**Verify:** `curl -I https://api.<your-domain>/v1/screen/analyze`
returns 401 over TLS (missing-key is the right answer — connection
refused / 502 means Caddy isn't routing).

---

## 7. Point iOS at the new server

Give the user these two values:

```
URL:  https://api.<your-domain>          (or http://<host>:5001 if you skipped step 6)
Key:  <the api_key from step 5>
```

iOS app → **Settings → Storage → Self-hosted** → paste URL + Key → Save.

---

## 8. Pick an agent integration mode

Your server is up. Now decide how the user's agent talks to it.

### Option A — Independent resident consumer (recommended for machine/server agents)

Use this for Hermes / OpenClaw / Claude Code on a Mac mini or VPS, or any
agent that can be reached through a stable HTTP endpoint or CLI command.
The resident consumer owns the ongoing IO Chat loop:

```text
poll /v1/chat/poll → call the agent HTTP/CLI entry → POST /v1/chat/response
```

Install it from this repo:

```bash
cp deploy/chat_resident.env.example ~/feedling-chat-resident.env
chmod 600 ~/feedling-chat-resident.env
# Fill FEEDLING_API_URL, FEEDLING_API_KEY, a decrypt source,
# and the agent's real HTTP or CLI entry.
sudo cp deploy/feedling-chat-resident.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedling-chat-resident
```

For user-owned agent hosts, prefer a user service (`systemd --user`,
launchd, supervisor, pm2) so the consumer is not a child job of the
current agent chat turn or top-level gateway. See
**[`tools/README.md`](../tools/README.md)** for the full setup.

### ~~Option B — Chat-client MCP~~（removed 2026-06-12）

The MCP user line was removed; use Option A (resident consumer).

## 9. Verify end-to-end

Ask the user to:

1. Open the app → Settings → **Live Activity → Start**. Within ~5
   seconds, `ssh <user>@<host> "tail -f ~/feedling-data/<user_id>/tokens.json"`
   should show a `live_activity` token appear.
2. Send a chat message in the app. Watch
   `~/feedling-data/<user_id>/chat.json` grow.
3. Wait for the agent to reply through the resident consumer or a verified
   always-on MCP runtime. The reply should appear in iOS within ~30 seconds.

If the reply never arrives, see **Troubleshooting** below.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| iOS chat sends but never gets reply | No agent is connected — neither MCP nor chat-resident is running | Run `python tools/check_chat_pipeline.py` against your URL; it will tell you which layer is missing |
| 401 `user_not_found` (cloud users will see this too) | User just ran Delete Account & Reset; agent is pinned to the dead key | iOS Settings → Storage → copy new MCP String → re-import into the agent runtime |
| Live Activity never updates | `.p8` key missing or `APNS_SANDBOX=False` on a TestFlight build | Place `AuthKey_<KEY_ID>.p8` in `~/feedling-data/`; flip `APNS_SANDBOX` for App Store builds |
| Frames not arriving via WebSocket | Port 9998 blocked or WS auth failing | Open port 9998 in the VPS firewall; the broadcast extension forwards the api_key as a Bearer token |
| `chat-resident` logs "no plaintext content" for every user message | `FEEDLING_ENCLAVE_URL` is not configured | Set it in `~/feedling-chat-resident.env`. See `tools/README.md`. |

### Mandatory re-auth + E2E verification (after any of these events)

- Account reset (a new `POST /v1/users/register` ran)
- API key change / rotation
- Server re-deploy that changes the enclave public key

**Re-auth sequence** — all three steps must pass before declaring the
chat pipeline restored:

```bash
# 1. Update the key in the env file
nano ~/feedling-chat-resident.env   # set FEEDLING_API_KEY=<new_key>
sudo systemctl restart feedling-chat-resident

# 2. Verify auth (both return 200)
curl -s -H "X-API-Key: <new_key>" $FEEDLING_API_URL/v1/users/whoami
curl -s -H "X-API-Key: <new_key>" "$FEEDLING_API_URL/v1/chat/poll?timeout=1"

# 3. E2E test: send a message from iOS → confirm a non-template reply
#    arrives in the app within one poll cycle (~30s)
```

If step 3 returns no reply, inspect chat-resident logs. Production onboarding
keeps `SEND_FALLBACK_ON_AGENT_ERROR=false`, so agent-entry failures should stay
in logs instead of appearing as template chat bubbles.

---

## Self-check tool

For routine health checks:

```bash
FEEDLING_API_URL=https://api.<your-domain> \
FEEDLING_API_KEY=<api_key> \
python tools/check_chat_pipeline.py
```

Exit codes: `0` = all green · `1` = warning · `2` = failure.

---

## Reading order for the agent (after your server is up)

Hand your user's agent this URL and tell them to paste it into their
runtime:

```
https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md
```

The skill covers Step 0 verification, the 4-pass memory garden
bootstrap, identity card derivation, the main loop, and Appendix A
(HTTP-mode equivalents if your user's agent isn't MCP).
