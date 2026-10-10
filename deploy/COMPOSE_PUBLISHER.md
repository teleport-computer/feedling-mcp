# Compose authorization publisher

`publish-compose-hash.sh <chain>` and `make -C contracts add-hash` use
`compose_publisher.py`. Install `deploy/publisher-requirements.txt` first.
The Solidity script remains a low-level development tool; ordinary CI does not
use Forge broadcast or wrap authorization in a retry loop.

The contract, owner and chain are checked before signing. The publisher simulates
exact calldata, estimates gas and selects ceil(estimate × 1.20), capped at
3,000,000 gas. Legacy transaction gas price is 3 × the current RPC quote, capped
at 100,000,000 wei. Before signing it repeats estimation, nonce and fee checks;
the original price must still cover twice the fresh quote. This creates bounded
fee headroom without replacing or repricing a transaction. Gas and fees may
exceed these bounds on another network: that is a stop, not permission to raise
the caps during a deployment.

CI uses three ordered steps:

1. `FEEDLING_PUBLISH_PHASE=prepare` records the deterministic transaction hash,
   nonce, calldata digest and fixed limits, without broadcasting.
2. Upload the evidence to `compose-intent-<chain>-<contract>-<compose_hash>`.
3. `FEEDLING_PUBLISH_PHASE=send` verifies the artifact identity, current run and
   exact archived intent bytes, then rechecks the live target and nonce and
   broadcasts those same signed bytes once. It records the receipt and requires
   `isAppAllowed` readback before reporting success.

Keys and raw signed transactions are never written to evidence. Secret keys are
read from the existing environment, not passed to Forge on the command line.
TLS verification is enabled and RPC redirects are refused. An RPC timeout,
HTTP/RPC rejection, unexpected hash, missing receipt or failed readback leaves
an explicit failure; it never authorizes automatic resending. A failed receipt
whose gasUsed equals its limit is classified as gas exhausted, while other
status-zero receipts are reported as reverted without guessing a revert reason.
Success means a successful observed receipt plus current authorization, not a
finality guarantee or a complete attestation verification.

Evidence defaults to `git rev-parse --git-path compose-publish-evidence`, or the
explicit `FEEDLING_PUBLISH_EVIDENCE_DIR`. Local exclusive claims survive process
restarts. CI requires a pre-send archive and refuses run-attempt > 1 for an
unauthorized hash. Fresh CI runs also check retained artifacts for that target
and stop if an older intent exists. Authorized hashes take the read-only skip
path. This is not a distributed nonce lock: another project sharing the signer
can still race after the final nonce check; ambiguous results require manual
reconciliation.

## Reconciliation and retention

Do not rerun a failed authorization or delete its evidence to unblock it. Retain
the intent and receipt, query that exact transaction hash and receipt, both
latest/pending signer nonces, and authorization for the exact chain/contract/hash.
A coordinator must review those observations before authorizing any recovery.
This ordinary publisher has no reset, replacement, cap override or recovery
switch. Workflow artifacts retain evidence for 90 days; artifact deletion or
expiry limits historical duplicate detection. Operators must preserve unresolved
intents outside expiring CI artifacts before that deadline. Artifact lookup
errors, truncated history and observed prior intents all fail closed.

Production runner CVMs authorize through a serialized CI matrix (`max-parallel:
1`, `fail-fast: true`). Each unit checks out the exact pin emitted by the deploy
job and completes preparation, its own intent archive, send and receipt/readback
before another unit starts. Distinct hashes therefore use fresh sequential
nonces; same-hash CVMs skip after the first confirmed authorization. A failed or
unknown result fails its unit and cancels queued authorizations. Final deployment
notification includes this authorization matrix. A live hash changing between
preparation and send stops; it is never signed under another target's archive.

## Offline verification

Run `python -m pytest -q deploy/tests`. Tests use synthetic keys and external
service doubles, including a real shell/CLI prepare → archive → send sequence.
Historical failed transaction calldata is public regression data; tests do not
replay it on a network. No chain writes or paid model calls are part of this
suite. Live test deployment and on-chain integration require coordinator-owned
execution after review and cannot be inferred from offline green results.
