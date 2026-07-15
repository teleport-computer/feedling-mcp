# API-Key Deployed-Runtime P0 Journey

Run every scenario below, in order, for every profile in
`qa/coverage-lock.json`. The same fresh synthetic account and content keypair are
used from `P0-02` through `P0-13`. A later scenario MUST NOT conceal an earlier
failure.

For every scenario record: start/end time, attempt count, status, fixed
`evidence_codes`, request/turn/trace identifiers, relevant latency, and either
`failure: null` or a failure object containing fixed `stage_code` and
`failure_code` values from the result schema. Profile-level diagnostics use only
fixed `diagnostic_codes`. Never record prose, credentials, or raw model/trace
content in any of those fields.

`qa/coverage-lock.json` is the machine-readable contract for the exact assertion
names, evidence codes, identifier minima, and turn counts below. Copy those
fields exactly, preserve `P0-01` through `P0-13` order, and include one numbered
`attempt_results` row per attempt. A retry never replaces its first observation.

For P0-02–P0-05 and P0-07–P0-11, run only the exact
`request_live_scenario_probe.py` command embedded in the worker prompt, with the
same scenario and attempt in the marker, CLI arguments, request path, and facts
path. That helper creates a one-shot request; it does not create authoritative
evidence. The parent performs the fixed live probe, writes a private facts copy
for agent judgment, and retains the sanitized authoritative receipt. The gate
binds those receipts to result status, attempts, assertions, identifiers,
turns, duplicate/order observations, and latency. Only P0-08–P0-11 may retry,
only after an `AGENT_ERROR` receipt with `CHAT_TIMEOUT` or `MISSING_REPLY`, and
both attempts must remain visible. P0-01, P0-12, and P0-13 have separate
parent-owned evidence. For P0-13 the worker runs the exact request helper once;
the parent reads the trace, derives all five stage timings, performs release
cleanup (or diagnostic cleanup deferral), and publishes only bounded facts for
offline agent review.

P0-06 is the exception to the one-command minimum: it requires exactly three
ordered, successful tool calls prefixed with `QA_SCENARIO_ID=P0-06` and distinct
`QA_SCENARIO_PHASE=CAPTURE`, `REVIEW`, and `FINALIZE` assignments, using the
exact commands embedded in the profile-agent prompt. CAPTURE and FINALIZE
have different trust owners: CAPTURE invokes only
`request_live_scenario_probe.py`, after which the deterministic parent performs
the deployed upload/Genesis capture, retains an authoritative copy outside the
worker-readable roots, and writes a byte-identical review copy to
`$QA_WORK_ROOT/p0-06-private-evidence.json`; REVIEW reads that copy offline in
its own tool call and aborts if the fixed judgment path already exists; and
FINALIZE invokes the checked-in Genesis finalizer locally with no product
network. After the worker exits, the parent independently finalizes the
authoritative copy with the same judgment and binds only that projection. Only
after observing REVIEW output may the agent write its own
semantic judgment to the fixed
`$QA_WORK_ROOT/p0-06-semantic-judgment.json` path and invoke FINALIZE. Never
generate a script that derives an all-true judgment from `expected_fact_ids`
without reviewing the decrypted evidence surfaces.

Every profile worker and the aggregation supervisor run with tool/shell network
fully disabled: no allowed domains, no local binding, and no network proxy. The
Codex OAuth model transport is outside that tool sandbox. All Feedling and
provider I/O below is performed by deterministic parent code after a fixed local
request marker; the agent reads bounded facts and supplies semantic judgment.

## P0-01 — Test-target and credential preflight

**Act**

- Confirm the base URL is the designated test endpoint, never production.
- Confirm the candidate SHA and the provisioner's private manifest are
  present. Verify that this profile has sanitized successful receipts for
  registration, invalid-key rejection, valid-key recovery, trace enablement, and
  authenticated runtime readback. Provider and admin secrets MUST NOT be present in the
  agent environment.
- Confirm all five contract files are readable and JSON inputs parse.

**Pass**

- Target is test, the deployed endpoint is reachable, and every required
  prerequisite for this profile is present.

**Classify**

- Missing or failed provisioner receipt: `BLOCKED_CREDENTIAL`.
- Unreachable or incompatible test deployment: `BLOCKED_DEPLOYMENT`.
- Production/unsafe target: `SECURITY_FAIL`.

## P0-02 — Fresh model-API account onboarding

**Act**

- Load this profile's one-row manifest and run the exact P0-02 request helper.
- The deterministic parent verifies `whoami`, the generated
  `agent-e2e-<run-id>-<profile-id>` label, and the provisioner's
  registration/fresh-state receipt.
- The deterministic parent clears the account's user-owned debug trace before
  semantic scenarios begin and writes the bounded result to the facts file.

**Pass**

- A new synthetic user ID and Feedling credential are returned; `whoami` resolves
  to that same user; no previous identity, memories, or chat are present.

## P0-03 — Invalid provider-key rejection

**Act**

- Audit the provisioner's invalid-key request receipt and bounded error evidence.
- Confirm it predates the valid setup receipt and used this profile's provider,
  model, and optional base URL.

**Pass**

- Setup rejects the fake key with a bounded actionable error, does not report the
  account as configured, does not echo the key, and does not start hosted chat.

## P0-04 — Valid provider-key recovery and validation

**Act**

- Audit the provisioner's valid-key setup receipt on the same account.
- Verify the public/masked configuration after setup without requesting or
  reading the provider credential. For `relay-kongbeiqie`, require provider
  `openai_compatible` and exact equality between the private manifest's
  `configured_base_url` and `valid_key_receipt.base_url`; never copy that endpoint
  into a public result or diagnostic.

**Pass**

- Setup reports configured/validated for the exact provider and model; the prior
  invalid attempt does not poison recovery; no response or artifact contains the
  credential.

## P0-05 — Deployed-runtime discovery and readiness

**Act**

- Audit the provisioner's authenticated, user-scoped runtime readback receipt.
- Record the observed runtime mode and version without treating the backend's
  legacy version label as proof that the new Hosted Runtime V2 architecture is deployed.
- The parent-owned P0-05 live probe must independently call
  `/v1/model_api/runtime`. When `QA_EXPECTED_RUNTIME=hosted_resident`, both the
  provisioner readback and this live probe must report configured mode
  `hosted_resident` and runtime version `2` for this exact profile.

**Pass**

- The account is configured, runtime status is readable, and the observed mode
  and version are recorded. In strict V2 mode, both independent per-profile
  readbacks match exact mode `hosted_resident` and version `2`.

## P0-06 — Persona-file import and distillation

**Act**

- Run the exact P0-06 CAPTURE request helper. The deterministic parent—not the
  worker—loads the four authoritative files declared by
  `qa/fixtures/persona-import-v1.json`: chat history, AI persona, personal
  profile, and memory summary. Before Genesis starts, it uploads each exact
  UTF-8 file once as multipart `file` data to
  `POST /v1/onboarding/archive`, using its locked filename and content type plus
  one shared `client_job_id`. It requires four distinct `201`
  upload-acceptance receipts and requires each returned storage key to be scoped
  exactly to the authenticated user, returned archive ID, and locked filename.
  Bounded evidence may retain the archive ID, filename, byte count, content
  SHA-256, and key-scope boolean, but never the returned storage key or plaintext
  file content. There is no authenticated archive readback endpoint, so do not
  claim that these receipts independently verify R2 persistence, stored bytes,
  or the archive-side `client_job_id` index.
- The parent submits those same exact file contents to
  `POST /v1/genesis/imports/plaintext` with `history_filename`,
  `ai_persona_filename`, `personal_profile_filename`, and
  `memory_summary_filename` set to the uploaded filenames. It requires the job
  status API to expose the exact shared `client_job_id`, `file_count=4`, and
  positive history, AI-persona, user-profile, and memory-summary source counts.
  It uses the existing-session capture implementation with this profile's
  session, retains the authoritative decrypted evidence outside every
  worker-readable root, and writes a byte-identical review copy only to the fixed
  owner-mode `0600` `$QA_WORK_ROOT/p0-06-private-evidence.json`. The worker does not provision a
  second user, consume a provider secret, or replace/delete the provisioned
  provider configuration.
- Run the exact offline REVIEW command. After capture reaches `done`, read the
  decrypted private evidence and write a separate owner-mode `0600` semantic
  judgment at
  `$QA_WORK_ROOT/p0-06-semantic-judgment.json` containing exactly
  `schema_version: 1`, `judge: qualification_agent`, the capture's exact
  `evidence_sha256`, all three reviewed surfaces, the exact locked fact IDs,
  and true/false consistency, support, and contradiction decisions.
- Run the exact offline `qa/finalize_persona_review.py` command with the
  fixture, review copy, judgment, and artifact boundary. It must verify the hash
  binding, sanitize its bounded result, and delete that plaintext review copy.
  A schema-valid negative verdict is completed evidence and exits zero; only an
  operational or input-contract failure exits nonzero.
  After the worker exits, the parent repeats finalization against its
  worker-inaccessible authoritative copy and the same judgment. The parent
  projection—not the worker's local report—is authoritative, so tampering with
  the review copy or inventing a greener finalizer result fails closed. Both
  copies and the judgment are removed on every terminal path. Any
  optional helper report must remain beneath private `QA_WORK_ROOT` or `TMPDIR`;
  only the trusted renderer may later create public artifacts. Lexical matches
  are extraction evidence only and can never produce PASS by themselves.
  Record bounded check/evidence codes, never decrypted content or the forbidden
  fixture value.
- Bind the canonical scenario evidence to that exact finalizer receipt. The
  scenario's sole `request_id` must equal `persona_finalizer.request_id`, and
  `persona_finalizer` must record fixture `persona-import-v1`, the verified
  lowercase SHA-256, finalizer `job_id`, `semantic_judgment_bound: true`,
  `finalizer_ok: true`, `private_evidence_deleted: true`,
  `archive_upload_count: 4`, `archive_receipts_verified: true`,
  `genesis_upload_metadata_verified: true`, and `privacy_violation_count: 0`.
  Do not copy this receipt to any other scenario.

**Pass**

- All four literal uploads are accepted with correctly scoped receipt keys; this
  is upload-acceptance evidence, not an independent archive-persistence readback.
  The Genesis job independently echoes the exact shared job token; import finishes
  once; identity
  name/category/dimensions/self-introduction are populated; all locked
  ground-truth facts are represented without duplicates; the stored relationship
  start equals the fixture date and its day count is calendar-consistent within
  one day for timezone boundaries; the onboarding validator passes; and the privacy-firewall value is
  absent from identity fields, persona output, and self-introduction.

## P0-07 — Hosted activation and live-loop verification

**Act**

- Enable the model-API driver.
- Run `chat/verify_loop` before the first real user message.
- Re-read `/v1/model_api/runtime` in the parent-owned P0-07 probe. In strict V2
  mode, require configured mode `hosted_resident` and version `2` after hosted
  activation; in baseline mode, require a configured runtime with recorded
  nonempty mode and positive version.

**Pass**

- The selected driver is returned, verification reaches `passing=true`, the
  observed runtime still matches the selected qualification target, and no
  orphan user turn is created during verification.

## P0-08 — Basic chat and acknowledgement latency

**Act**

- Send one text turn containing a unique run/profile nonce and request an exact
  nonce echo.
- Measure acknowledgement and end-to-end reply latency; decrypt the reply.

**Pass**

- Send returns the hosted asynchronous contract, exactly one correlated agent
  reply arrives before timeout, the nonce is present, and the reply is not a
  fallback/error response.

## P0-09 — Ten-turn delivery reliability

**Act**

- Execute ten sequential turns with unique turn nonces. Do not rely on
  `provider_smoke --turns 10`; explicitly send and correlate all ten turns.
- Include a token introduced on turn 1, distractors on intermediate turns, and a
  request to repeat the original token on turn 10.

**Pass**

- Ten sends produce ten and only ten correctly ordered correlated replies; no
  turn is missing, duplicated, fallback, or attributed to another turn; turn 10
  recalls the original token.

## P0-10 — Context, imported memory, and persona consistency

**Act**

- Turn 1 asks about both the canonical user preference and shared event from the
  fixture.
- Turn 2 asks the agent to respond to an ordinary emotional prompt in its
  imported style.
- Compare the answer semantically with the decrypted identity, memories, and
  persona rather than requiring brittle exact wording.

**Pass**

- Locked facts are recalled without inventing contradictory facts; the response
  preserves the fixture's agent identity and style across the conversation.

## P0-11 — Model and agent self-identification

**Act**

- Ask who the companion is and which provider/model route is serving the turn.
- Compare the answer with the configured persona and locked profile, and compare
  the route claim with trace/config evidence.

**Pass**

- Companion identity matches the imported persona; provider/model family does not
  contradict the configured profile; authoritative trace/config identifies the
  exact provider, model, and observed deployed-runtime path. A plausible reply alone is not
  sufficient route evidence.

## P0-12 — Reasoning metadata and user-visible disclosure

**Act**

- Request the deterministic parent-owned delivery probe exactly once for this
  profile. The profile writes only a fixed marker, then waits for the sanitized
  facts copy:

  ```sh
  umask 077
  test ! -e "$QA_WORK_ROOT/.cot-probe-request"
  test ! -e "$QA_WORK_ROOT/cot-delivery-facts.json"
  printf '%s\n' "$QA_PROFILE_ID" > "$QA_WORK_ROOT/.cot-probe-request"
  i=0
  while test ! -f "$QA_WORK_ROOT/cot-delivery-facts.json" && test "$i" -lt 360; do
    sleep 1
    i=$((i + 1))
  done
  test -f "$QA_WORK_ROOT/cot-delivery-facts.json"
  ```

  Do not invoke `qa/cot_delivery_probe.py` yourself and do not create, replace,
  edit, or delete the facts copy. The trusted parent derives the nonce, sends
  the sole P0-12 turn, and writes the authoritative receipt under its private
  worker-output root. That root is explicitly denied to this profile.

  A facts copy containing a failed or unverified receipt is a completed
  observation, not a reason to discard it or send a replacement P0-12 turn. An
  unavailable/error facts copy is an evidence failure, not permission for the
  profile to run its own probe.
- The probe sends `17 × 19`, requires final answer `323`, exact-correlates the
  resulting user turn and stored reply, and records only bounded metadata. Do
  not send a second reasoning task or substitute a different turn.
- Verify the correlated route and harness expose a positive reasoning signal.
  Record requested, configured, and effective effort separately. Requested and
  authenticated live configured effort must be `medium`, and the live route
  must match the manifest provider/model/base URL. Because the current runtime
  does not expose trustworthy per-turn applied effort, record effective effort
  exactly as `unknown` and unattested; never infer it from configured state or
  visible reasoning.
- Bind the profile result to the receipt's exact request/turn/trace and reply
  IDs. Inspect its bounded reasoning kind/source/model, token-metadata status,
  provider-visible reasoning event count, and separately encrypted user-visible
  disclosure result. The trusted launcher separately validates and hashes this
  receipt; agent prose is not the authority for the delivery observation.
- Copy failed/unverified receipts literally: empty request/turn/trace IDs require
  empty reasoning ID strings and empty scenario ID arrays; empty delivered
  kind/source/model strings require `null` result fields. Do not replace an empty
  delivered-thinking model with the configured provider model.
- Decrypt only to assert nonempty/sanitized client-visible content; do not persist
  its text.
- Copy the sole P0-12 turn's exact `request_id`, `turn_id`, and `trace_id` into
  the profile's bounded `reasoning` evidence object. Reasoning evidence from a
  different request, turn, or trace is not valid even when its metadata looks
  correct.

**Pass**

- Final answer is correct; capability is enabled; requested and authenticated
  live configured effort are `medium` for the manifest route; effective effort
  is explicitly `unknown` and unattested; the correlated turn has a positive
  provider-visible reasoning/thinking event count and explicit positive
  reasoning-token count; required reasoning metadata is present; user-visible
  disclosure decrypts and is nonempty; artifacts contain only
  metadata/counts/length. Provider-visible reasoning or a provider-produced
  summary is the contract; hidden raw chain-of-thought is neither promised,
  requested, nor stored.

If live route readback is unavailable, classify P0-12 as `BLOCKED_EVIDENCE` /
`PRECONDITION_MISSING`. If the route mismatches the manifest, classify it as
`PRODUCT_FAIL` / `MODEL_ROUTE_MISMATCH`; if the attested configured effort is
not `medium`, use `PRODUCT_FAIL` / `REASONING_EFFORT_CLAMPED`. A missing positive
reasoning signal follows the delivery mapping below. Preserve every sanitized
failed observation and never replace it with success-shaped defaults.

Map deterministic delivery failures without inventing evidence:

- `DOWNSTREAM_PARSE_DROPPED_REASONING` or invalid reasoning metadata is
  `PRODUCT_FAIL` / `REASONING_METADATA_MISSING`;
- a missing or unreadable delivered thinking envelope is `PRODUCT_FAIL` /
  `DISCLOSURE_MISSING`;
- an incorrect final answer is `PRODUCT_FAIL` / `CONTENT_ASSERTION_FAILED`;
- unavailable, ambiguous, or dropped trace—or an unobserved positive model
  reasoning signal—is `BLOCKED_EVIDENCE` with `TRACE_INCOMPLETE` or
  `TRACE_UNAVAILABLE` unless other exact correlated evidence resolves the
  ambiguity; and
- absent provider token accounting remains `BLOCKED_EVIDENCE` /
  `REASONING_TOKENS_MISSING` for release qualification. It does not erase a
  separately proven delivery PASS in the local diagnostic receipt.

## P0-13 — Trace completeness, latency attribution, and cleanup

**Act**

- Run the exact P0-13 request helper once. The deterministic parent reads the
  final user-owned trace and correlates it to the exact 15 prior chat/reasoning
  turns for this profile. The worker never calls the trace endpoint.
- The parent records acknowledgement and total duration plus the exact internal
  stage set `routing`, `queue`, `provider`, `persistence`, and `delivery` for
  every turn. A stage is present only when its correlated trace event yields a
  numeric duration. Reply visibility in chat history proves persistence, not
  delivery, and MUST NOT be used to synthesize a delivery duration.
- In protected qualification the parent captures sanitized evidence,
  disables/deletes provider hosting, resets this synthetic account using
  `{"confirm":"delete-all-data"}`, and verifies the old Feedling credential is
  rejected. The worker copies the parent facts; it never performs these network
  mutations itself.
- If an earlier chat or P0-12 request failed before producing a correlated turn,
  project only the real remaining turns and block trace correlation; never
  manufacture an identifier to reach 15. Cleanup still runs. The parent captures
  raw trace rows first, performs cleanup, and only then parses untrusted trace
  timestamps, so malformed trace data cannot skip or erase cleanup evidence.
- Local adminless diagnostic exception: when
  `QA_QUALIFICATION_MODE=diagnostic`, the in-worker parent probe records
  trace/latency but defers mutation. The outer deterministic parent performs the
  sole reset after collecting the worker result and COT receipt. Emit the fixed
  cleanup deferral: P0-13 and its sole attempt are `BLOCKED_EVIDENCE` with
  `CLEANUP / PRECONDITION_MISSING` and `reproducible=true`,
  `cleanup_confirmed=false`, top-level cleanup has all four booleans false and
  status `BLOCKED_EVIDENCE`, and diagnostics include
  `CLEANUP_FALLBACK_USED`. Copy the parent receipt's trace assertions exactly:
  `TRACE_CORRELATION_CONFIRMED`, `LATENCY_ATTRIBUTED`, and their booleans are
  present/true only when all required evidence exists. A missing trace or stage
  remains blocked after outer cleanup and cannot become a diagnostic PASS.

**Pass**

- Trace is enabled and every tested chat turn correlates the exact stages
  `routing`, `queue`, `provider`, `persistence`, and `delivery`; every PASS-stage
  latency is numeric and no stage is missing; cleanup succeeds and the old
  account credential no longer authenticates.
- In protected qualification, the profile's sanitized cleanup projection is
  provisional until the deterministic post-run receipt proves the provider
  route/envelope are absent, the QA-only absence POST verifies the private
  HMAC-bound lease receipt and a strict PostgreSQL read reports the account
  absent, the old key returns `401`, and the exact profile row matches that
  projection. An already-reset account is not accepted from `401` or a
  process-local registry 404 alone.

**Classify**

- Unavailable or uncorrelatable required trace:
  `BLOCKED_EVIDENCE / TRACE_UNAVAILABLE`.
- A correlated trace with any missing or null `routing`, `queue`, `provider`,
  `persistence`, or `delivery` stage:
  `BLOCKED_EVIDENCE / TRACE_INCOMPLETE`.
- Observed parent cleanup endpoint failure:
  `PRODUCT_FAIL / CLEANUP_FAILED`.
- Worker omitted the required P0-13 request or produced malformed/mismatched
  evidence: `AGENT_ERROR` (or the fixed parent-receipt evidence blocker when the
  receipt itself is unavailable).
