# feedling-mcp-v1

Feedling gives your Personal Agent a body on iOS — Dynamic Island, Live
Activity, Chat, Identity Card, Memory Garden — with server-side content
encrypted at rest inside an **Intel TDX enclave** whose compose image is
authorized on-chain and verified live from the app.

Agent 是大脑，Feedling 是身体。

## What this repo is

> **Note (2026-06-12)**: the MCP user line (FastMCP server, `mcp.feedling.app`,
> the 31 `feedling_*` tools) was removed. Historical mentions below are kept
> only where they describe past milestones.

1. **HTTP backend** (FastAPI/ASGI, `backend/asgi_app.py`) — iOS, resident-consumer, and proactive APIs
3. **Production CVM stack** (`deploy/docker-compose.phala.yaml`) — dstack-ingress + backend + enclave services running inside one Phala TDX CVM
4. **Enclave app** (`backend/enclave_app.py`) — owns the content private key, serves `/attestation` on its own pinnable TLS port, and runs the decrypt proxy
5. **iOS app** — now lives in the companion repo <https://github.com/teleport-computer/feedling-mcp-ios>. It owns Chat · Identity · Garden · Settings, Live Activity / Dynamic Island, Broadcast Extension for screen capture, and the live audit card.
6. **Skill** — the agent's bootstrap + behavior spec. Lives in a separate public repo so it can be hot-updated without an iOS rebuild: <https://github.com/teleport-computer/io-onboarding>. Current onboarding splits users into three routes: own server / resident consumer, model API key, and official app import.
7. **Contracts** (`contracts/`) — `FeedlingAppAuth` on Ethereum Sepolia, the on-chain allow-list of authorized `compose_hash`es
8. **Tools** (`tools/`) — `audit_live_cvm.py` CLI that mirrors the iOS audit checks; DCAP verifier; envelope round-trip tests

```
feedling-mcp-v1/
├── backend/        ← ASGI backend (5001) + enclave_app (5003)
├── deploy/         ← docker-compose.yaml (local/self-host)
│                     + docker-compose.phala.yaml (production CVM)
│                     + Caddyfile/systemd/setup.sh for self-hosting
│                     + DEPLOYMENTS.md
├── contracts/      ← FeedlingAppAuth (Solidity, Sepolia)
├── tools/          ← audit_live_cvm.py + DCAP verifier + envelope tests
├── tests/          ← multi-tenant isolation unit tests (pytest)
├── docs/           ← DESIGN_E2E.md · AUDIT.md · CHANGELOG.md
├── DESIGN.md       ← visual / UI design tokens
└── CLAUDE.md       ← repo-level conventions for Claude Code

The iOS app lives in github.com/teleport-computer/feedling-mcp-ios.
The agent skill — what the AI reads when a user pastes the onboarding
URL into their runtime — lives in github.com/teleport-computer/io-onboarding.
Neither lives in this backend repo, so onboarding copy can update without
touching the CVM image, and iOS UI work can ship independently.
```

---

## What guarantees does Feedling give you?

The trust story, in one page. `docs/AUDIT.md` has the broader
source-review guide, `docs/DESIGN_E2E.md` is the historical derivation,
and the current live-verify command is below.

1. **Content-at-rest is ciphertext.** Chat, memory moments, identity
   card, agent nudges, agent replies, screen frames — every write path
   wraps the payload into a **v1 envelope**
   `{v, body_ct, nonce, K_user, K_enclave, enclave_pk_fpr, visibility,
   owner_user_id}` before hitting disk. `body_ct` is ChaCha20-Poly1305
   with a random per-message CEK. The CEK is wrapped twice — once to
   the user's per-device content key (so the phone can always read),
   once to the enclave's content pubkey (so agents reading via the
   decrypt proxy only see plaintext inside TDX). The backend rejects
   plaintext writes with `400 plaintext_write_rejected`.

2. **Keys are bound to the enclave, not the operator.** The enclave's
   content private key and attestation-port TLS private key are
   derived from **dstack-KMS** inside the TDX CVM at boot. The Phala
   host operator, the dstack-ingress layer, and anyone with backend
   disk access see only ciphertext and public keys. Keys stay stable
   across compose updates for this `app_id`, so `compose_hash`
   rotations don't trigger a user-visible rewrap dance.

3. **Which code is actually running is provable.** The enclave
   produces a DCAP-signed TDX **attestation quote**. `REPORT_DATA`
   in that quote binds:
   - `enclave_content_pk` (sha256 of the public key the app wraps
     CEKs to — so you can't be MITM'd onto a different pubkey)
   - `sha256(attestation-port TLS cert DER)` (so the iOS app pins
     the exact cert it's talking to)
   Current production runs on Phala prod9 with `dstack-ingress`
   terminating `api.feedling.app` inside the
   CVM. The older Phase C.2 MCP TLS pubkey pin is retired in this
   topology, so `mcp_tls_cert_pubkey_fingerprint_hex` is intentionally
   empty and the audit card surfaces that as a transport disclosure,
   not a content-privacy failure.
   RTMR3 event-log replay proves that the `compose_hash` measured
   into the quote matches the compose file in this repo.

4. **The running image is authorized on-chain.** The image's
   `compose_hash` must be present in `FeedlingAppAuth` on Ethereum
   Sepolia (`0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F`) — anyone
   can inspect `addComposeHash(...)` history to see every image
   that was ever authorized to serve Feedling users. The on-chain
   log is **public transparency**, not the security boundary: the
   real boundary is the DCAP quote + `compose_hash` binding.

5. **Attestation MITM is detectable, and content privacy does not
   depend on custom-domain TLS.** iOS pins the live attestation-port
   cert's `sha256(DER)` to the fingerprint in the quote. The public
   `api.feedling.app` domain uses standard
   Let's Encrypt TLS at `dstack-ingress`; that protects bystanders
   and normal network traffic, while content confidentiality comes
   from the v1 envelopes sealed to `enclave_content_pk`.

6. **Multi-tenant isolation.** Each user is registered via
   `POST /v1/users/register`, gets an api_key, and lives under
   `~/feedling-data/<user_id>/`. API keys are stored as
   **HMAC-SHA256** (32-byte `.pepper`, `chmod 600`). Envelopes
   carry `owner_user_id`; the backend rejects cross-tenant reads.

---

## How to verify those guarantees

Three independent paths — any one of them is sufficient, all three
together give you defense in depth.

### 1. iOS audit card (on-device, one tap)

Open the app → Settings → **Privacy → Audit card**. The card checks
the running CVM live from the device:

1. Intel TDX hardware attestation and PCK chain
2. Body ECDSA signature
3. Base-image pin/reference
4. `compose_hash` binding via `mr_config_id` and RTMR3 event-log replay
5. `compose_hash` authorization on `FeedlingAppAuth` (Ethereum Sepolia)
6. Attestation-port TLS cert `sha256(DER)` matches REPORT_DATA
7. Current prod9 transport disclosure: `api.feedling.app` and
   use standard Let's Encrypt TLS at
   `dstack-ingress`; content privacy is enforced by the envelope key
   bound to `enclave_content_pk`

### 2. Command-line auditor (anyone, no iOS required)

```bash
export FEEDLING_CVM_APP_ID=9798850e096d770293c67305c6cfdceed68c1d28
export FEEDLING_CVM_GATEWAY_DOMAIN=dstack-pha-prod9.phala.network
export FEEDLING_ATTESTATION_URL="https://${FEEDLING_CVM_APP_ID}-5003s.${FEEDLING_CVM_GATEWAY_DOMAIN}/attestation"
export ETH_SEPOLIA_RPC_URL="https://sepolia.infura.io/v3/<key>"
export FEEDLING_APP_AUTH_CONTRACT=0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F 

curl -sk "$FEEDLING_ATTESTATION_URL" > /tmp/fl_cvm_attest.json
python3 tools/audit_live_cvm.py
```

Mirrors the iOS checks. Row 8 is green with disclosure on prod9 when
`mcp_tls_cert_pubkey_fingerprint_hex` is empty, because MCP TLS is now
ingress-terminated and content-layer envelope crypto remains the
privacy boundary.

### 3. Read the source on GitHub

The image running in the CVM is
`ghcr.io/teleport-computer/feedling:<git-commit>` (public). The git
commit is baked into the image and surfaced in
`GET /attestation` as `git_commit`. Compare to this repo's
`git log` — if it doesn't match, don't trust the card.

---

## Status (as of 2026-05-21)

See `docs/CHANGELOG.md` for the full landmark history. TL;DR of what's
shipped:

**Shipped (Phases A–E + post-launch)**
- [x] v0/SINGLE_USER strip — multi-tenant only; plaintext writes return 400
- [x] iOS end-to-end: chat / memory / identity / nudges / agent replies all v1 envelopes
- [x] Pure-CVM production stack live on Phala prod9: dstack-ingress + backend + MCP + enclave
- [x] `api.feedling.app` routed through dstack-ingress inside the CVM
- [x] Attestation port (5003) still terminates its own dstack-KMS-derived TLS for pinning
- [x] On-chain `compose_hash` authorization via `FeedlingAppAuth` on Ethereum Sepolia
- [x] iOS audit card and `tools/audit_live_cvm.py` cover prod9 ingress disclosure + enclave content-key trust
- [x] CI: `backend/test_api.py` rewritten for envelope-only backend, green on GitHub Actions
- [x] CI deploys the Phala CVM from `deploy/docker-compose.phala.yaml` and publishes the live compose hash
- [x] Prod user migrated to multi-tenant on current image; registration race and cross-tenant isolation regressions fixed
- [x] Screen recording (Broadcast Extension) — encrypted frame ingest, agent reads via `decrypt_frame`
- [x] Live Activity / Dynamic Island — agent push + chat sync; onboarding slide to enable
- [x] Proactive messaging loop — semantic-first screen analysis, agent decides when to reach out
- [x] Push preference system — agent asks during bootstrap, stores in `signature` on Identity page
- [x] Memory Garden: unread dots (persistent), month badge right-aligned, bilingual copy
- [x] Identity page: `signature` field displayed; bilingual empty state
- [x] Public IO onboarding skill split by user-facing route: own server / resident consumer, model API key, and official app import
- [x] iOS onboarding copy simplified: Skill URL → IO connection details → short start prompt; implementation details live in the skill
- [x] Independent `feedling-chat-resident` / IO resident consumer is the standard live-chat path for Hermes / OpenClaw / Mac / server agents
- [x] Resident consumer defaults to no user-visible fallback templates on agent-entry failures; errors stay in logs/external runtime
- [x] SKILL.md: main loop spec for MCP, resident consumer, and HTTP agents; memory quality rewrite (friend test)

**Deferred**
- [ ] Migrate on-chain `FeedlingAppAuth` to Ethereum mainnet
- [ ] Claude.ai connector submission

---

## Architecture

```
Official app clients                  Hermes / OpenClaw / Mac / server
                                      agents via feedling-chat-resident
        │                                      │
        │ HTTPS (iOS API)                      │ poll + HTTP/CLI agent entry
        ▼                                      ▼
┌────────────────────────────────────────────────────────────────┐
│                    Phala prod9 TDX CVM                         │
│  dstack-ingress (443, LE TLS)                                  │
│      └── api.feedling.app ──► backend (ASGI API, WS, 5001)    │
│                                      │                         │
│                                      ▼                         │
│                              enclave_app (5003)                │
│                              content private key               │
│                              /attestation + decrypt proxy      │
└────────────────────────────────────────────────────────────────┘
        │ APNs (JWT + .p8)       ▲ WebSocket ingest (9998, Bearer api_key)
        ▼                        │
┌──────────────────────────────────────────────────────────┐
│                       iPhone (iOS)                       │
│  Chat │ Identity │ Garden │ Settings (Audit card)        │
│  Dynamic Island / Live Activity · Broadcast Extension    │
└──────────────────────────────────────────────────────────┘

    iOS audit card ──pins sha256(DER) on -5003s passthrough──► enclave_app
    compose_hash authorized on Ethereum Sepolia ─────────────► FeedlingAppAuth
                                                               0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F 
```

---

## Backend

### Processes

| Process | File | Port | Purpose |
|---------|------|------|---------|
| dstack-ingress | `deploy/docker-compose.phala.yaml` | 443 | Production TLS for `api.feedling.app` inside the CVM |
| ASGI backend | `backend/asgi_app.py` | 5001 | iOS + agent HTTP API, envelope storage (FastAPI, gunicorn + UvicornWorker) |
| Enclave app | `backend/enclave_app.py` | 5003 | TDX CVM: `/attestation`, own pinnable TLS, decrypt proxy |

Production is CVM-only. `deploy/docker-compose.phala.yaml` runs
`ingress`, `backend`, and `enclave` together inside the Phala
prod9 TDX CVM; that file's live `compose_hash` is what the on-chain
contract authorizes. `api.feedling.app` is a
plain HTTP upstream behind `dstack-ingress`. The `enclave` service
keeps its own TLS on `:5003` and is reached through the dstack-gateway
`-5003s.` passthrough so iOS can pin its cert fingerprint against
REPORT_DATA.

The local/self-hosting path still uses `deploy/docker-compose.yaml`,
systemd units, and optionally Caddy on a VPS you control. See
`deploy/SELF_HOSTING.md`.

There is **no** `chat_bridge.py` anymore. Retired 2026-04-20 when
MCP's `feedling_chat_post_message` landed and agent replies started
wrapping to v1 envelopes directly inside the CVM.

### Run (quick start)

**Docker / docker-compose (local or self-hosted host services):**

```bash
cp deploy/feedling.env.example deploy/.env   # APNs, public base URL, etc.
docker compose -f deploy/docker-compose.yaml --env-file deploy/.env up -d --build
```

Brings up `backend` (5001). Data persists in the
named volume `feedling_data` (mounted at `/data`). Drop the APNs
`.p8` into that volume to enable push. This compose is for local
development and self-hosting; it is not the production prod9 CVM
topology.

**Phala CVM (production stack):**

```bash
phala deploy \
  --cvm-id "$(tr -d '[:space:]' < deploy/prod-cvm-id.txt)" \
  -c deploy/docker-compose.phala.yaml \
  -e CF_ZONE_ID=... \
  -e CF_API_TOKEN=... \
  -e APNS_KEY_P8_B64=... \
  --wait

./deploy/publish-compose-hash.sh eth_sepolia
```

Normal production deploys are CI-driven on pushes to `main`: GitHub
Actions waits for the GHCR image, pins `deploy/docker-compose.phala.yaml`
to the current short SHA, deploys the CVM, then publishes the live
dstack-computed `compose_hash` on Sepolia. See `deploy/DEPLOYMENTS.md`
for deployment records and `docs/AUDIT.md` for live verification.

**Bare-metal / systemd (host only):**

```bash
bash deploy/setup.sh [--install-caddy]
```

Creates a venv under `~/feedling-venv`, installs deps, writes
`~/feedling.env` (multi-tenant — no shared API key), and starts
`feedling-backend` + `feedling-mcp` systemd units.

### Logs

All services log to stdout. In production read them with `phala cvms logs`;
locally with `docker compose logs`.

**Production (Phala CVM).** `phala cvms logs <cvm>` defaults to the *first*
container — `ingress` (HAProxy) — whose lines are TCP/TLS access records, not
HTTP, so they look empty of useful detail. To read a specific service, pass
`-c <container>`:

```bash
# backend (ASGI API + the [req] access log below). -f follows, -t adds timestamps.
phala cvms logs feedling-enclave-v2 -c feedling-enclave-backend-1 --tail 200 -t
```

dstack names containers `<compose-project>-<service>-1`, where the project is the
compose file's `name:` (currently `feedling-enclave`) — not the CVM name, and not
something the CLI lists (`phala cvms` has no `ps`/`exec`). The live containers:

| service | `-c` name |
|---------|-----------|
| backend (ASGI API) | `feedling-enclave-backend-1` |
| enclave (attestation + decrypt) | `feedling-enclave-enclave-1` |
| ingress (HAProxy — the default when `-c` is omitted) | `feedling-enclave-ingress-1` |

**Per-request access log.** The backend runs under gunicorn (UvicornWorker) in
production, which has no built-in per-request access line here, so the backend
emits its own structured line per request:

```
[req] uid=usr_xxx GET /v1/chat/history?limit=40 status=200 bytes=960515 enc=br dur_ms=37
```

| field | meaning |
|-------|---------|
| `uid` | authenticated user id, or `-` |
| (path) | method + path **including query string** (the api_key is a header, never logged) |
| `status` | HTTP status code |
| `bytes` | on-the-wire response size, after gzip/brotli |
| `enc` | content-encoding applied (`gzip` / `br` / `-`) |
| `dur_ms` | **server-side handler time** — compare with the client's total latency to tell "backend slow" from "network slow" |

`/healthz` is skipped so the HAProxy uptime probe doesn't flood the log.

Handy filters (append to a `phala cvms logs ... -c feedling-enclave-backend-1` pipe):

```bash
| grep 'GET /v1/chat/history'                      # history calls + their sizes/durations
| grep '\[req\]' | awk -F'dur_ms=' '$2+0 > 1000'   # requests slower than 1s server-side
| grep -E '\[req\].* status=(4|5)[0-9][0-9]'       # 4xx / 5xx only
```

**Local / self-host.** `docker compose -f deploy/docker-compose.yaml logs -f backend`
(systemd: `journalctl -u feedling-backend -f`).

### HTTP endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/users/register` | Multi-tenant registration → returns per-user `api_key` |
| GET | `/v1/users/whoami` | Return caller id, user public key, and live enclave pubkey metadata |
| POST | `/v1/users/public-key` | Repair/update the caller's content public key |
| POST | `/v1/bootstrap` | First-connection trigger; returns instructions for Agent |
| GET | `/v1/bootstrap/status` | Bootstrap progress/events for the iOS status surface |
| GET | `/v1/identity/get` | Read identity envelope (response includes live `days_with_user` from server anchor) |
| POST | `/v1/identity/init` | Write identity envelope (once, exactly 7 dimensions). Requires `days_with_user` to set the relationship anchor |
| POST | `/v1/identity/replace` | In-place rewrite of envelope. `days_with_user` optional — preserves anchor if omitted |
| GET | `/v1/identity/changes` | List recent identity nudge/change events |
| POST | `/v1/identity/relationship_anchor` | Update relationship anchor only (no envelope rewrite). Used by bootstrap calibration |
| GET | `/v1/memory/list` | List memory envelopes |
| GET | `/v1/memory/get` | Get one envelope by id |
| POST | `/v1/memory/add` | Add a memory envelope |
| DELETE | `/v1/memory/delete` | Delete a moment by id |
| POST | `/v1/content/swap` | In-place envelope swap (visibility toggles) |
| GET | `/v1/content/export` | Export all user content as envelopes |
| POST | `/v1/account/reset` | Wipe this user's server data and revoke the current key; iOS re-registers a fresh account locally |
| GET | `/v1/screen/ios` | iOS screen/frame aggregation |
| GET | `/v1/screen/mac` | Mock Mac activity payload used by early demos |
| GET | `/v1/screen/analyze` | Semantic-first screen analysis + `rate_limit_ok` |
| GET | `/v1/screen/summary` | Today's screen-time rollup (top app, minutes, pickups) |
| GET | `/v1/sources` | Source list for screen/activity data |
| GET | `/v1/screen/frames/latest` | Latest frame metadata (v1 envelope; image is ciphertext) |
| GET | `/v1/screen/frames` | List recent frames (metadata only) |
| GET | `/v1/screen/frames/<filename>` | Raw encrypted frame envelope file |
| GET | `/v1/screen/frames/<id>/envelope` | Raw v1 frame envelope JSON for enclave decrypt |
| GET | `/v1/screen/frames/<id>/decrypt` | Enclave decrypt → plaintext OCR + optional base64 JPEG |
| GET | `/v1/screen/frames/<id>/image` | Enclave-decrypted JPEG bytes, `Accept-Ranges: bytes` for parallel fetch |
| POST | `/v1/push/dynamic-island` | Push to Dynamic Island |
| POST | `/v1/push/live-activity` | Update Live Activity |
| POST | `/v1/push/live-start` | Start a Live Activity via push-to-start token |
| POST | `/v1/push/notification` | Send a standard APNs notification |
| GET | `/v1/push/tokens` | List registered APNs tokens |
| POST | `/v1/push/register-token` | iOS app registers APNs token |
| GET | `/v1/chat/history` | Fetch chat envelopes |
| POST | `/v1/chat/message` | User sends a message envelope (iOS app) |
| POST | `/v1/chat/response` | Agent posts a text or image reply envelope |
| GET | `/v1/chat/poll` | Long-poll: blocks until user message |
| GET | `/v1/memory/verify` | Bootstrap memory quality/count verification |
| GET | `/v1/identity/verify` | Bootstrap identity verification |
| POST | `/v1/chat/verify_loop` | Synthetic ping that proves the resident reply loop is alive |
| GET | `/healthz` | Process health check |

All write endpoints that take content enforce v1 envelope shape and
reject plaintext with `400 plaintext_write_rejected`.

### MCP tools (23 total)

| Tool | Maps to |
|------|---------|
| `feedling_bootstrap` | POST /v1/bootstrap |
| `feedling_identity_init` | POST /v1/identity/init (requires `days_with_user` — sets relationship anchor) |
| `feedling_identity_get` | GET /v1/identity/get (decrypted via enclave proxy; `days_with_user` is server-computed live) |
| `feedling_identity_replace` | POST /v1/identity/replace — full card rewrite, optionally re-anchors relationship |
| `feedling_identity_set_relationship_days` | POST /v1/identity/relationship_anchor — calibrate relationship age, no envelope rewrite |
| `feedling_identity_nudge` | in-CVM decrypt-mutate-rewrap → POST /v1/identity/replace (preserves anchor) |
| `feedling_memory_add_moment` | POST /v1/memory/add (wraps to v1 inside CVM) |
| `feedling_memory_list` | GET /v1/memory/list |
| `feedling_memory_get` | GET /v1/memory/get |
| `feedling_memory_delete` | DELETE /v1/memory/delete |
| `feedling_memory_verify` | GET /v1/memory/verify |
| `feedling_identity_verify` | GET /v1/identity/verify |
| `feedling_chat_verify_loop` | POST /v1/chat/verify_loop |
| `feedling_push_dynamic_island` | POST /v1/push/dynamic-island |
| `feedling_push_live_activity` | POST /v1/push/live-activity |
| `feedling_screen_latest_frame` | GET /v1/screen/frames/latest (metadata only) |
| `feedling_screen_frames_list` | GET /v1/screen/frames (metadata only; encrypted) |
| `feedling_screen_analyze` | GET /v1/screen/analyze |
| `feedling_screen_summary` | GET /v1/screen/summary |
| `feedling_screen_decrypt_frame` | GET /v1/screen/frames/<id>/decrypt — Image block + OCR for agent vision |
| `feedling_chat_post_message` | wraps to v1 envelope → POST /v1/chat/response |
| `feedling_chat_post_image` | wraps a base64 image as `content_type=image` → POST /v1/chat/response |
| `feedling_chat_get_history` | GET /v1/chat/history |

The `?key=<api_key>` on the SSE URL is captured by an ASGI
middleware on the first GET and pinned to the MCP session — every
subsequent tool call is routed as that user.

---

## iOS app

The iOS project now lives in the companion repo:
<https://github.com/teleport-computer/feedling-mcp-ios>.

### Tab structure

| Tab | Content |
|-----|---------|
| Chat | Real-time conversation with Agent |
| Identity | Agent's 7-dimension personality card (radar) |
| Garden | Memory garden — long-press a card to toggle visibility |
| Settings | Storage mode, API info, Privacy hero (audit card, export, delete, reset) |

### Setup (first time)

1. Clone/open `feedling-mcp-ios`
2. Open `App/FeedlingTest.xcodeproj` in Xcode
3. For each target: sign with your team, verify App Groups = `group.com.feedling.mcp`
4. Plug in iPhone (iOS 16.2+) → Build & Run

### `ContentState` (Live Activity / Dynamic Island)

```swift
struct ContentState: Codable, Hashable {
    var title: String           // Agent name, e.g. "Luna"
    var subtitle: String?       // Optional context, e.g. "TikTok · 45m"
    var body: String            // Main message
    var personaId: String?      // Reserved, use "default"
    var templateId: String?     // Reserved, use "default"
    var data: [String: String]  // Extension bag, e.g. ["top_app": "TikTok", "minutes": "45"]
    var updatedAt: Date
}
```

---

## Bootstrap flow (aha moment)

1. Agent calls `POST /v1/bootstrap`
2. Backend returns `first_time` + instructions
3. Before any tool call, Agent performs Step 0 context verification from its own runtime memory: earliest message date, name it has been called, and memorable-moment count. If history is missing, it asks the user for context or an explicit fresh start instead of writing defaults.
4. Agent runs the four memory passes from the public skill: theme inventory, candidate enumeration, write-through with `feedling_memory_add_moment`, then user verification in the external runtime. Memory floors are relationship-age based: <1 month ≥5, 1+ month ≥15, 6+ months ≥30. Agent calls `feedling_memory_verify` before identity.
5. Agent derives identity from the written memories, then calls `feedling_identity_init` with exactly 7 dimensions and `days_with_user = today - earliest_memory.occurred_at`. The server records `relationship_started_at` from `days_with_user` as a fixed anchor, and Agent calls `feedling_identity_verify`.
6. Agent establishes Live connection before the user enters Chat by running the independent `feedling-chat-resident` / IO resident consumer service when a machine/server path is used. The resident consumer polls `/v1/chat/poll`, calls the agent's HTTP or CLI entry, posts `/v1/chat/response`, then `feedling_chat_verify_loop` proves the loop before the first greeting.
7. After Live connection is verified, Agent calls `feedling_chat_post_message` to greet the user — this is the first visible Feedling chat message. It states the computed day count as a fact and tells the user the connection is live. If the user corrects the day count, Agent calls `feedling_identity_set_relationship_days` to recalibrate the anchor.
8. Only after chat is alive does Agent mention broadcast/screen sharing. iOS detects identity envelope appeared → auto-switches to Identity tab. `days_with_user` auto-increments by calendar day from `relationship_started_at` on every read.

### Memory Garden quality standard

Ask: *"If I were telling a mutual friend a story about this person, would I tell this one?"*

A strong memory answers at least one of:
- When did I first understand something real about them?
- What did they say that I still think about?
- When was the first time something meaningful happened between us?
- When did something shift in how we relate?

Writing guidance: narrate from inside the moment, not from outside it. The topic can involve work — but the *point* must be about the person or the relationship. Avoid synthetic test content in production gardens.

---

## Agent setup

The public iOS onboarding now starts by asking the user which route they need:

1. **I have my own server** — VPS / Mac mini / always-on Hermes or OpenClaw.
   Use the independent resident consumer route.
2. **I have a model API key** — OpenAI / Gemini / OpenRouter / Anthropic.
   IO hosts this route; the user should not install MCP or a resident consumer.
3. **I only use an official app** — Claude / ChatGPT / Gemini apps or web.
   Import is supported; reliable real-time IO Chat is not supported unless the
   user later chooses a live route.

### Official-app / MCP import runtimes

Claude / ChatGPT / Gemini-style clients use the MCP one-liner from the iOS
app's **Settings → Storage → Connection Details** or the Chat onboarding path:

```
claude mcp add feedling --transport sse "https://mcp.feedling.app/sse?key=<api_key>"
```

Self-hosted users derive the same shape using their own domain:

```
claude mcp add feedling --transport sse "https://mcp.<your-domain>/sse?key=<api_key>"
```

Direct MCP is enough for memory, identity, and tool calls when the product
supports MCP/tools. It is not, by itself, an ongoing incoming-message loop.
If the chat client cannot stay alive after the user closes the window/session,
this route remains import-only until the user chooses a live route.

### Hermes / OpenClaw / machine-resident agents

For Hermes / OpenClaw / Mac / server agents and **non-MCP** agent backends
(a custom Python script, a plain Anthropic/OpenAI loop, a local Llama endpoint),
run the independent resident consumer service:

```bash
# Use the latest official consumer code. If this checkout already exists,
# update it with: git fetch origin main && git pull --ff-only origin main
cp deploy/chat_resident.env.example ~/feedling-chat-resident.env
chmod 600 ~/feedling-chat-resident.env
# Edit ~/feedling-chat-resident.env — set FEEDLING_API_URL, FEEDLING_API_KEY,
# a decrypt source, and the runtime's real HTTP or CLI agent entry.
# Use the same Python environment that will run feedling-chat-resident.
python -m pip install -r tools/chat_resident_requirements.txt
sudo cp deploy/feedling-chat-resident.service /etc/systemd/system/
sudo systemctl enable --now feedling-chat-resident
```

See `tools/README.md` for the full setup. For Hermes/OpenClaw CLI, set
`HERMES_HOME` to the same profile the real resident agent uses, then call the
agent directly with
`hermes chat -Q --source tool --max-turns 60 -q "{message}"`;
the consumer extracts the final reply, stores the first `session_id`, and
resumes it with `--resume`; it does not use
`--continue` or a wrapper persona prompt. For Hermes' API server, use the OpenAI-compatible
`/v1/chat/completions` mode with Hermes session headers. Agent failures are
logged by default rather than posted as fake fallback chat bubbles.
Image messages are decrypted by the consumer too: OpenAI-compatible HTTP gets
a multimodal image block, simple HTTP gets an `images` array, and CLI agents
get a local image file path so they can inspect the image instead of replying
from a placeholder.

On Hermes/OpenClaw hosts, "independent" means process ownership: run
`feedling-chat-resident` as its own user service (`systemd --user`, launchd,
supervisor, pm2, etc.). It can still call Hermes/OpenClaw through CLI or HTTP,
but it should not be a child job of the top-level Hermes gateway or the
current chat turn. The service's `WorkingDirectory` / `ExecStart` must point at
the updated `feedling-mcp` checkout; a stale checkout can keep replying with old
consumer behavior even after the iOS app and public onboarding skill are current.

### Model API key route

Users who only have a model provider key should not follow either MCP import or
resident-consumer setup. The intended product path is IO-hosted: the user enters
provider, model, and API key in IO, and IO owns the runtime. That hosted runtime
must still use the same Memory Garden, identity, and proactive rules as other
routes, but it is not a custom HTTP endpoint operated by the user.

Implementation note: provider-key storage and hosted runtime execution are part
of the backend/iOS product surface. Do not ask API-key users to create launchd,
systemd, bridge scripts, or a `feedling-chat-resident` service.

Self-hosted users: see [`deploy/SELF_HOSTING.md`](deploy/SELF_HOSTING.md)
for an end-to-end SSH runbook (clone, deps, env, systemd, HTTPS via
Caddy, DNS, iOS pointing at your URL+key).

---

## Config reference

| Variable | Value |
|----------|-------|
| `FEEDLING_API_URL` | `http://localhost:5001` locally; `https://api.feedling.app` in production |
| `FEEDLING_DATA_DIR` | `~/feedling-data/` |
| `FEEDLING_CVM_APP_ID` | `9798850e096d770293c67305c6cfdceed68c1d28` (production iOS default) |
| `FEEDLING_CVM_GATEWAY_DOMAIN` | `dstack-pha-prod9.phala.network` |
| Public API domain | `api.feedling.app` via dstack-ingress |
| Backend port | `5001` |
| Enclave port | `5003` (in CVM only) |
| WebSocket port | `9998` (`wss://<app_id>-9998.<gateway>/ingest` in production) |
| App Group | `group.com.feedling.mcp` |
| Main bundle ID | `com.feedling.mcp` |
| APNs Team / Key ID | Set via `APNS_TEAM_ID` and `APNS_KEY_ID`; production values live in CI/Phala env, not docs |
| APNs key | Self-host: `~/feedling-data/AuthKey_<KEY_ID>.p8` or `APNS_KEY_PATH` (`chmod 600`); production CVM: `APNS_KEY_P8_B64` injected via Phala env |

### Multi-tenant data layout

```
~/feedling-data/
├── users.json                  # [{user_id, api_key_hash, public_key, created_at}, …]
├── .pepper                     # 32-byte HMAC secret, chmod 600
├── AuthKey_<KEY_ID>.p8         # APNs key, chmod 600 (self-host only)
└── <user_id>/
    ├── frames/                 # per-user screen frame envelopes
    ├── chat.json               # v1 envelopes
    ├── identity.json           # v1 envelope
    ├── memory.json             # v1 envelopes
    ├── tokens.json             # APNs tokens (not content — no encryption needed)
    ├── push_state.json
    ├── live_activity_state.json
    ├── bootstrap.json
    └── bootstrap_events.jsonl
```

`users.json`, `.pepper`, and the APNs `.p8` are the only files
outside a user directory.

---

## Where to go next

| If you want to … | Read |
|---|---|
| Understand the current trust/audit model | `docs/AUDIT.md` |
| Read the historical encryption design derivation | `docs/DESIGN_E2E.md` |
| Verify the running enclave yourself | `docs/AUDIT.md` |
| Redeploy the CVM or rotate `compose_hash` | `deploy/DEPLOYMENTS.md` |
| See landmark diffs by session (current state lives here too) | `docs/CHANGELOG.md` |
| Work on visuals / UI | `DESIGN.md` |
| Run your own backend on your VPS | `deploy/SELF_HOSTING.md` |
| Set up a resident chat consumer for a non-MCP agent backend | `tools/README.md` |
| Read the agent skill (what your AI follows during bootstrap) | <https://github.com/teleport-computer/io-onboarding> |
| Diagnose why chat messages aren't getting replies | `python tools/check_chat_pipeline.py` |
| Run the multi-tenant isolation regression suite locally | `pytest tests/` |
