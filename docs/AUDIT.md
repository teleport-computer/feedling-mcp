# Is this safe? — an audit guide for your agent

You (the user) handed this repo URL to your agent and asked "is this
safe?" — this document is the answer your agent can walk through
with you. It is intentionally agent-consumable: concrete claims,
concrete files to read, concrete commands to run, concrete
addresses and transaction hashes to verify.

If your agent disagrees with anything here after reading the code,
**trust your agent**. This document describes what the code is
supposed to do; the code itself is the source of truth.

> Repo: <https://github.com/teleport-computer/feedling-mcp>
> Running enclave: `app_id=9798850e096d770293c67305c6cfdceed68c1d28` on Phala Cloud prod9
> On-chain release log: [FeedlingAppAuth @ `0x6c8A6f1e…49b88F`](https://sepolia.etherscan.io/address/0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F) (Eth Sepolia)

---

## 1. Plain-English trust model

Feedling takes sensitive data from the user's phone — chat,
memory garden, identity card, and optionally screen recordings —
and stores it on a server. Naively, this is scary: you hand your
life to someone else's machine.

Feedling's answer has three layers:

1. **Content is encrypted before it leaves the phone.** Chat messages,
   memory moments, and identity cards are wrapped in a v1 envelope
   client-side. The server stores ciphertext only. An operator with
   root on the backend disk sees base64 blobs — no plaintext.
   Frames (screen recordings) are wrapped by the iOS broadcast
   extension before the WebSocket ingest.

2. **The decryption key lives in a TDX enclave.** Envelopes contain
   two sealed copies of the per-item symmetric key: one for the user
   (on their phone), one for an enclave. The enclave's private key
   is derived from dstack-KMS inside an Intel-TDX attested virtual
   machine. Dstack-KMS will only release this key to a VM whose
   measurements match an authorized `compose_hash`.

3. **Every released app version is published on-chain.** When the
   Feedling team ships a new version, they publish its `compose_hash`
   to a public Ethereum contract. An agent can list all historical
   releases and flag any that don't match expectations.

So: operator with backend root = ciphertext only. Operator with
root on the TDX host = can't extract the enclave key (TDX memory
isolation). Operator publishing a malicious compose = visible
on-chain, and users who've accepted an older `compose_hash` see the
change in the app's consent modal before any new release runs.

The residual trust surface: Intel's TDX silicon, Phala/dstack
infrastructure, standard CA/DNS trust for `api.feedling.app`,
and the Feedling team's Ethereum key used to
publish `addComposeHash` transactions. The attestation endpoint still
uses enclave-owned TLS on the `-5003s.` route so the app can pin the
certificate fingerprint signed into the TDX quote.

---

## 2. What you (the agent) should verify

A checklist. Numbers in parens are rough effort for an agent with
shell + code-read + web access.

| # | Claim | How to verify | Effort |
|---|---|---|---|
| 1 | The repo you're reading matches the deployed binary. | Check the running `/attestation` endpoint's `git_commit` field; clone that commit; compute its docker image digest locally (`docker buildx` reproducible build); compare to the digest on `ghcr.io/teleport-computer/feedling:<commit>`. Script: `deploy/BUILD.md`. | 30 min |
| 2 | The deployed `compose_hash` is what this repo's `docker-compose.phala.yaml` would produce. | Run `deploy/publish-compose-hash.sh` locally and compare to `/attestation`'s `compose_hash`. | 5 min |
| 3 | The deployed `compose_hash` is in the on-chain release log. | `cast call --rpc-url <sepolia> 0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F "isAppAllowed(bytes32)(bool)" 0x<compose_hash>`. Expect `true`. | 1 min |
| 4 | The TDX quote was signed by Intel hardware. | Run `tools/audit_live_cvm.py`; rows 1–3 are Intel's signature chain + measurement integrity. The script is small enough to read end to end. | 5 min |
| 5 | The attestation TLS cert on the `-5003s.` URL is the one the enclave attested. | `tools/audit_live_cvm.py` row 7 pins `sha256(cert.DER)` vs the attested fingerprint. Row 8 is now a disclosure row: the MCP service was removed 2026-06-12 (`mcp_tls_cert_pubkey_fingerprint_hex` stays empty in the bundle); content privacy is enforced by envelopes sealed to `enclave_content_pk`. | 1 min |
| 6 | The backend code doesn't decrypt content. | Read the route modules (`backend/*/routes_asgi.py`, assembled by `backend/asgi_app.py`). `/v1/chat/message`, `/v1/memory/add`, `/v1/identity/init`, `/v1/content/swap` all require v1 envelopes and store them verbatim — no crypto primitives called, and plaintext bodies now 400. The only place envelope bodies are decrypted is `backend/enclave_app.py`, which runs inside the TDX container. | 20 min |
| 7 | Identity.nudge can't silently mutate encrypted cards. | The HTTP `/v1/identity/nudge` endpoint was removed in the 2026-04-20 v0 strip; the MCP nudge tool was removed with the MCP line on 2026-06-12. Identity mutation now only happens via `/v1/identity/actions` envelope-rewrap actions (decrypt happens in the enclave). Plaintext mutation is not expressible on the wire. | 2 min |
| 8 | The iOS app actually pins the TLS cert, not just displays a green check. | Read `testapp/FeedlingTest/AuditCardView.swift` `PinningCaptureDelegate` + `AuditViewModel.run`. The delegate captures the server's `sha256(cert.DER)` during the TLS handshake; the viewmodel compares it to `bundle.enclave_tls_cert_fingerprint_hex`. Mismatch ⇒ red row + "MITM detected". | 10 min |
| 9 | The iOS app decrypts client-side, not via the server. | Read `testapp/FeedlingTest/ContentEncryption.swift` + `ChatMessage.decryptedIfNeeded`. Envelopes land in view models, are unsealed with `user_sk` from Keychain, body AEAD-opened with the recovered `K`. | 15 min |
| 10 | Reset actually deletes the ciphertext (not just local). | Read `backend/content/routes_asgi.py` `/v1/account/reset`. Calls `shutil.rmtree(store.dir)` + removes the user from `users.json` + evicts the `api_key_hash` cache. Second call with the same key 401s because the user no longer exists. | 5 min |

If rows 4, 5, and 3 pass in that order, the "something real is
running, and the Feedling team authorized it" claim is cryptographic.
Rows 6–10 are the "code actually does what we say" claim and are
source-review work.

---

## 3. Key files by concern

### Cryptography & enclave

- `backend/enclave_app.py` — the in-enclave service. Key derivation
  (`derive_keys`), attestation bundle assembly, per-user content
  decrypt handlers. Read top-to-bottom; ~800 lines.
- `backend/dstack_tls.py` — deterministic TLS cert derivation from
  dstack-KMS for the attestation port. (Earlier MCP-in-enclave TLS
  modes used the same helper; the MCP service was removed 2026-06-12.)
- `backend/content_encryption.py` — Python mirror of iOS's
  `ContentEncryption.swift`. Same primitives on both sides; if they
  drift, AEAD verification fails on read-back.
- `testapp/FeedlingTest/ContentEncryption.swift` — iOS client
  envelope builder + unsealer.
- `testapp/FeedlingTest/DCAP/` — Intel TDX quote parser + verifier,
  runs fully on-device.

### Attestation & trust UI

- `testapp/FeedlingTest/AuditCardView.swift` — the audit card the
  user sees in Settings → Privacy. Worth reading end-to-end;
  surfaces every check in a labelled row + a tap-to-expand
  mechanism reveal per row.
- `tools/audit_live_cvm.py` — the CLI auditor. It runs the same
  security checks as the iOS card; row 8 is a green disclosure row
  (MCP service removed 2026-06-12, fingerprint field stays empty).

### On-chain

- `contracts/src/FeedlingAppAuth.sol` — the contract that holds the
  release log.
- `contracts/script/AddComposeHash.s.sol` — the Foundry script we
  use to publish a new `compose_hash`.
- `deploy/publish-compose-hash.sh` — wraps `cast send` for
  publishing. Shows exactly what gets sent on-chain.

### Backend data handling

- `backend/asgi_app.py` + `backend/*/routes_asgi.py` — the ASGI API
  (FastAPI; assembly in `asgi_app.py`, routes in the domain packages).
  **Read these carefully if you're auditing data handling.** Every
  endpoint that accepts user data either stores ciphertext verbatim or
  is explicitly marked as a plaintext metadata field.

### Deploy

- `deploy/docker-compose.phala.yaml` — the **exact** compose file
  the CVM runs. Every literal in this file goes into the
  `compose_hash`. Env vars are either baked security values or
  explicitly non-security-relevant.
- `deploy/Dockerfile` — hash-pinned Python base image + hash-pinned
  requirements via `pip install --require-hashes`. Supply chain is
  pinned at both layers.
- `deploy/DEPLOYMENTS.md` — one-line-per-deploy record. Every
  compose_hash the CVM has ever run is logged here with the
  Sepolia tx that authorized it.

### Docs that name the honest asterisks

- `docs/DESIGN_E2E.md` — historical encryption-design derivation,
  including §10 "Threat model". Use `README.md` and this audit guide
  for current production topology.
- `docs/CHANGELOG.md` — landmark diffs by session, including what
  was encrypted at each phase and which paths are now fully v1.

---

## 4. Known caveats (things we say, things we don't)

Read this section carefully — it's the list of claims we
*don't* make.

### We don't claim

- **"Feedling can never see your data."** Our operators can see
  ciphertext on disk. If Intel's silicon is compromised, or if
  dstack's KMS releases keys to a compose we haven't authorized
  on-chain, or if our Ethereum key is compromised — the guarantee
  weakens. Each of those is documented in
  `docs/DESIGN_E2E.md §10`.
- **"We don't log metadata."** Timestamps, message counts, push
  tokens, and per-user storage paths all live on the backend in
  plaintext. These are intentional — sorting chat history
  requires timestamps, Dynamic Island requires push tokens.
- **"The base image is bit-for-bit reproducible."** The Python
  wheel install is `--require-hashes`-verified, but system apt
  packages aren't hash-pinned. That's a known gap; auditors who
  ask will get it closed, just ask.
- **"Self-hosted users get the same guarantees."** They don't —
  they own the whole stack. Self-hosted is the "you don't have
  to trust anyone including Feedling" option, not the
  "same crypto" option.

### We do claim

- **Content you write through the app, post-Phase-A, is stored as
  ciphertext on our disk.** If the backend is imaged, rolled back,
  leaked — attackers get envelopes, not plaintext. Your `user_sk`
  is bound to your Apple account via iCloud Keychain.
- **The running `compose_hash` is what the Feedling team
  authorized.** It's in a public contract with a transaction hash
  we can point you at.
- **The TLS cert that your phone uses for `/attestation` on `-5003s.`
  really is generated inside the enclave.** We pin
  `sha256(cert.DER)` against a value signed by Intel's hardware. The
  public API domain uses normal Let's Encrypt TLS at
  `dstack-ingress`; that is transport protection, not the content
  privacy boundary.
- **The enclave's decryption key is bound to the `compose_hash`.**
  A new compose_hash = a new key = old data re-wrapping needed
  (or the key needs to be the same across `compose_hash` rotations,
  which Phala's dstack-KMS gives us per-app — see
  `deploy/DEPLOYMENTS.md`).

### We're working on

Tracked in `docs/CHANGELOG.md` (most recent entries) and on GitHub
issues:

- **Mainnet migration**: the on-chain contract lives on
  Ethereum Sepolia today. Moving to Ethereum / Base mainnet is
  the last step of the roadmap. Sepolia is a testnet — the
  release log could technically reorg, though it hasn't.

Phases C.2 (ACME-DNS-01 inside the enclave) and C.3 (identity.nudge +
chat-reply encryption) shipped on 2026-04-20 alongside the v0/SINGLE_USER
strip. See `docs/CHANGELOG.md` for the landmark diffs.

---

## 5. Run the verifier yourself

```bash
# Clone the current repo state the CVM is running.
git clone https://github.com/teleport-computer/feedling-mcp
cd feedling-mcp

# Set up an Ethereum Sepolia RPC (any provider — Alchemy,
# Infura, or a public node).
export ETH_SEPOLIA_RPC_URL="https://sepolia.infura.io/v3/<key>"
export FEEDLING_APP_AUTH_CONTRACT="0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F"
export FEEDLING_CVM_APP_ID=9798850e096d770293c67305c6cfdceed68c1d28
export FEEDLING_CVM_GATEWAY_DOMAIN=dstack-pha-prod9.phala.network
export FEEDLING_ATTESTATION_URL="https://${FEEDLING_CVM_APP_ID}-5003s.${FEEDLING_CVM_GATEWAY_DOMAIN}/attestation"

# Fetch the live attestation.
curl -sk "$FEEDLING_ATTESTATION_URL" > /tmp/fl_cvm_attest.json

# Run the auditor. Expected: rows pass; row 8 is a prod9 disclosure.
python3 tools/audit_live_cvm.py
```

The auditor's source is `tools/audit_live_cvm.py`. If you're suspicious
of the auditor itself, the checks it performs are mechanical
(re-implement them in any language that speaks TLS + SHA-256 + a
Sepolia RPC).

---

## 6. The bit that's hard to make crypto-strong

A careful agent will notice: **the iOS app is a compiled binary**.
Even if every byte of this repo is audited, you're still trusting
that the App Store / TestFlight / sideloaded app you installed
actually came from this source.

We don't have a good answer to this today. We build the iOS app
locally on a developer Mac and sign with a team cert. The
reproducible-iOS-build story is a real gap; we mention it here
instead of pretending it's not there.

Mitigation an agent can recommend: if a user is especially
paranoid, they can check out this repo, build the iOS app locally,
and install it via Xcode directly — bypassing any middle party.
`testapp/FeedlingTest.xcodeproj` is the project.

---

## 7. What to do if something here is wrong

File an issue: <https://github.com/teleport-computer/feedling-mcp/issues>.

If you found a real vulnerability, please email
**security@feedling.app** before filing publicly. We will credit
you in the release notes if you want it.

If you disagree with the trust model itself (e.g. "you shouldn't
be using a TEE at all"), we want to hear that too — the goal of
this document is for a user to be able to make an informed
decision, not for them to be convinced.
