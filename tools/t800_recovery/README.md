# T800 incident recovery — root operated, independently reviewed

This is a one-off recovery of production source
`08b2629670ab753413bfe804b99b90697bc1c579`, deployment pin
`1962fba419350d5bb33738cda94342df81ccd931`. It does not repair the normal
publisher permanently. Do not merge/push main or rerun the failed deployment.

Author codex3, reviewer codex4. Root must approve the exact code SHA and each
operational stage after reviewing preceding evidence. No execution is authorized
by this document. Secrets remain in Actions; do not export CI credentials.
A's independently reviewed prerequisites are recorded in fleet T800 recovery
prerequisites (manifest c47b6fb740c364e7455a0031bd69860fd75399cc7e991ee1d6cbe185279cd9c4).
Original absent optional runner variables retain empty values; specifically do not
substitute PROD_AGENT_MAX_CHILDREN for the original AGENT_MAX_CHILDREN mapping.

After bus approval/commit and a normal test-target PR, root can dispatch the
existing `ci.yml` on the exact reviewed recovery branch. No new default-branch
workflow registration or production product rebuild is necessary. Inputs are
passed as environment data, never interpolated into shell code. Replace the
code SHA with the actual reviewed full commit; branch movement fails closed.

```sh
gh workflow run ci.yml --repo teleport-computer/feedling-mcp \
  --ref fix/t800-ci-recovery -f operation=t800-recover \
  -f recovery_stage=plan -f recovery_code_sha="$REVIEWED_CODE_SHA"
```

Plan performs identity checks only; no production secret mapping, RPC or CVM calls.
It installs tooling but skips the ordinary CI tests/builds/deployments.
For each subsequent dispatch root separately sets `stage` to `authorize-main`,
`attestation`, `canary`, then `runner`. `previous_run` is blank only for main
transaction; later stages require the preceding successful recovery job/artifact
from this exact code SHA. Root reads the evidence before moving to the next step.

```sh
gh workflow run ci.yml --repo teleport-computer/feedling-mcp \
  --ref fix/t800-ci-recovery -f operation=t800-recover \
  -f recovery_stage="$stage" -f recovery_code_sha="$REVIEWED_CODE_SHA" \
  -f recovery_confirm="ROOT-T800-$stage" \
  -f recovery_previous_run="$previous_run"
```

Root serializes all deployments and owner-wallet transactions. `deploy-cvm`
concurrency prevents overlap with the main deployment job, but root must also
exclude separate runner jobs and external senders. Cross-service compares are
not an atomic CAS. Do not delete workflow history or recovery artifacts.

The main transaction is fixed to nonce638, Sepolia11155111, contract/owner/hash
in `recover.py`. If the live nonce or tuple differs, stop for reviewed reconciliation.
Do not simply raise the nonce or broaden targets. The failed transaction calldata
is in `failed-transactions.json`; all three attempts exhausted1365297 gas.
RPC estimation1829803 plus ceil20% margin produces2195764; cap3000000 and fee
cap0.1gwei remain hard limits. Prepare signs in memory, saves only intent metadata,
and performs no send. The upload-artifact step must succeed before the send step.
Send rechecks live state and the persisted signed parameters; no price increase,
replacement, automatic resend or signed-rawtx artifact. Lower estimates/prices use
the originally frozen limits; higher needs stop. The intent artifact survives a
runner crash before/after broadcast, and is available for transaction-hash reconciliation.

Every rerun (`run_attempt>1`) is rejected. Every earlier same-stage incident dispatch blocks a new dispatch across code branches,
with exactly one reviewed exception below for the failed pre-send run38077149919.
Fixed nonce and current pending/latest equality also prevent replacement sends.
History reads are bounded and incomplete history fails closed. A failed attempt
requires root read-only reconciliation and a newly reviewed recovery decision;
this tool provides no reset/retry escape hatch. Do not use a fresh output directory
or workflow run as permission to resend. The local r1/r2 latch is not the CI guard.

Attestation retains the exact existing enclave trust mechanism and fixed public-key
baseline. Missing/inert/failed gate is not PASS. Canary retains the original owned
register/seal/decrypt/finally-reset, no model calls; failure blocks progression even
though old CI ignored it. Canary is unbuffered and labels its fixture with product source plus recovery run ID;
recovery code SHA remains separate. `canary.json` is preserved/uploaded on success,
nonzero exit, launch failure and timeout. Its allowlist records constrained observed
fixture IDs, key/plaintext/roundtrip markers, reset status and inferred reset distinction,
CANARY OK, exit/timeout, source/pin/code/run identity. Missing output is UNKNOWN,
never evidence that no fixture exists. A normal zero exit plus all required observed
markers and reset200 is needed for stage success. Additional unobserved registration
fixtures remain UNKNOWN (the original script can abandon transport-uncertain attempts).
Raw stdout/stderr, payloads, keys and error text are never persisted or uploaded.
All exception exits sanitize secret-bearing argv/body. Failed canary cleanup needs
root reconciliation using these safe facts, never a blind rerun.

Runner stage uses original secret mappings and explicit tee/1 checks, fixed CVM ID,
exact release checkout, and rechecks A's frozen digest
`sha256:302a3a02be35f461db2c720ff7f39ede07454cf5777099d3aaf274cc0d578842`.
It runs only the existing runner deployment and bounded readiness step, no main
redeployment, no pinwriter, no original three-retry publisher. Root must confirm
current runner is the recorded old release/config before authorizing this stage;
if already healthy at target, do not redeploy merely to create evidence.

After runner deployment, `runner-next.json` records its actual distinct compose hash.
STOP there for independent freeze/review of runner hash authorization if needed.
The main transaction cannot authorize the runner hash. This adapter intentionally
has no generic chain target/calldata input or automatic next transaction.

Artifacts `t800-intent-RUNID` and `t800-stage-RUNID` contain safe identity/receipt/
attestation/failure/stage evidence. STAGE_COMPLETE always has deployment_acceptance=false.
A/root must still verify actual full API/V2/Hosted source, health, trust and cleanup,
plus runner authorization. Green job, notification, or main receipt alone is not
release success. Preserve the original failed CI result.

Local verification (no secrets/network):
`python -m pytest -q tools/t800_recovery` and `actionlint -shellcheck='' .github/workflows/ci.yml`.
No API, architecture, production configuration semantics or public documentation change.
Permanent generic publisher gas/retry remediation stays a separate T800 test PR follow-up.

## Explicit zero-send reconciliation after run38077149919

The first authorize-main attempt stopped in prepare with HTTPError; artifact
11678942435 contains only failure.json, and intent-upload/send were skipped.
Read-only nonce latest/pending remained638 and main hash remained unauthorized.
The code now requires this exact failed run to remain in history and verifies its
old codeSHA f0d84a4634480fac03a9a6c60049f8e5cad4d694, attempt1, failed prepare,
skipped archive/send/postdeploy, exact single artifact ID/digest, and sole
failure.json content. Any drift, extra intent, deleted history, or another earlier
authorize-main run blocks execution. This is not a user-selectable retry bypass.
The safe reconciled.json receipt is archived; all nonce/owner/hash guards remain.

Verified-TLS local control reproduced RPC403 for urllib's default User-Agent and
200 for the explicit `feedling-t800-recovery/1` application User-Agent; the same
health endpoint returned200 for both. Original CI lacked per-request diagnostics,
so this is a reproduced transport cause, not conclusive attribution of its exact
HTTPError. RPC now sends this honest application identifier without changing URL,
credentials, TLS verification, targets or signing semantics. Any future RPC/health
HTTP error records only fixed operation and numeric HTTP status; transport errors
record fixed operation/type, never URLs/queries/headers/body.

Root must approve the NEW reviewed codeSHA and a new one-time stage decision
before dispatch. Do not rerun the failed workflow or use its old code ref. This
revision alone grants no chain-send or deployment permission.

### Runner authorization (separately reviewed after actual deployment)

`authorize-runner` authorizes only runner CVM
`130fdfc6-5736-4cdc-9d0f-a35af8957cf2`, live hash
`eaa4091cbf5a295abda701cbadc020e0145fbc926bd133437a8d5b71de55d6b2`.
It requires exact prior runner run `38078351673`, reviewed code
`933bae8f8107cb360c80f5b70c1e6be62dcec4b1`, and its matching runner-next artifact.
The new dispatch itself must use its newly reviewed code SHA. Product source/pin,
contract and owner remain fixed; metadata URL names `docker-compose.phala.prod.runner.yaml`.
Main authorization is a prerequisite, never a substitute runner target.

Nonce `640` is fixed, not derived automatically. Nonce639 was independently found
in transaction `0xe57880da245e178a225fef9c565ea6796e224ced2979e483ae7a3be91463d2cc`
and matched Rokku CI run38077762030, targeting a different contract/project.
The shared owner can transact concurrently outside this workflow's lock. Latest
and pending must both match the frozen nonce, including after final estimation;
any drift stops without signing/sending or incrementing. A remaining race after
that check requires root coordination; unknown outcomes are reconciled by hash,
never retried. The prepare/archive/single-send controls and gas caps are unchanged.
Live running CVM/hash/image tag and registry digest are checked again before each
signing phase. Successful hash authorization does not prove Hosted instances or
runtime health. Root and the independent reviewer own subsequent acceptance.

The first runner authorization run `38079057709` prepared and archived intent but
failed before entering the transaction function: both phases downloaded prior
stage evidence into one directory, and gh's second extraction rejected existing
files. Each phase now downloads and validates into a new temporary directory.
A subprocess exclusive-extraction two-phase test guards this regression; the
same actual gh download and validation were also verified twice locally.

Only this exact failed run at code `ba28299977a3d38894a0870b791217e937ce876f`
is reconciled, by fixed artifact IDs/digests, exact file sets/failure/intent, and
live chain checks (intent transaction and receipt absent, nonce640 latest=pending,
runner hash unallowed). The original public intent is frozen in
`runner-failed-intent.json`; recovery must produce the identical signed transaction
hash and all intent fields, without repricing. Any evidence/chain drift stops.
This is a new root-reviewed one-attempt dispatch, never an automatic rerun;
future same-stage attempts block. The independent pre-send failure reproduction
is necessary context: absence from one RPC alone does not prove zero sends.

The original-intent recovery run `38079458231` then stopped before preparing a
new intent with `frozen_limits_insufficient`; archive/send were skipped. Read-only
measurement found unchanged estimate1,943,685 but doubled gas price2,000,032 wei,
2 wei above the old2,000,030 bound. The exact CI instantaneous quote was not
archived, so this measurement supports the fee-drift diagnosis without inventing
an original quote.

A separately reviewed replacement intent uses fixed gas2,400,000 and
price3,000,000 wei (0.003gwei), maximum0.0000072 ETH, within the original3Mgas/
0.1gwei caps. This explicitly supersedes the earlier same-hash requirement; the
old intent remains immutable evidence and must still be absent on-chain. No
automatic fee changes occur. The new prepared hash is archived before send and
all prepared fields must stay equal in send. Exact run38079458231, its sole
artifact ID/digest/file set, reason and relation to the old intent must be
revalidated, in addition to the earlier download-failure evidence. Any other
attempt or missing evidence blocks. Root alone authorizes this new intent.
