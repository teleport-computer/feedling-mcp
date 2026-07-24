# Runtime V2 isolation and sandbox boundary

> **Current status — 2026-07-19.** The former hosted per-user
> `process | container` resident-supervisor design has been retired. This path
> remains only as a stable redirect for older links; it is not an implementation
> plan and none of its old controls can select a hosted runtime.

Managed hosted execution uses a bounded pooled `serve-worker` on dedicated
runner CVMs. A turn watchdog/child process is a resource and wedge boundary, not
a strong sandbox for hostile code. There is no per-user resident process, home,
checkpoint, lease, Docker socket, or container spawner in the hosted image.

Runtime V2 instead exposes two explicit seams:

- a backend-pluggable encrypted virtual filesystem (`/artifacts`, `/workspace`,
  `/memory/WORKING.md`, and `/skills`); and
- a lazily acquired `SandboxProvider` for physical artifact materialization,
  untrusted binary parsing, shell, or code execution.

Ordinary text chat, Memory Garden reads, virtual text-file reads, and virtual
text edits do not acquire a sandbox. An artifact cache miss fails closed when no
provider is configured or available; it never falls back to parsing inside the
API or shared worker. The E2B adapter is source-wired but requires the deployment
to select it and supply an API key plus template. It runs the fixed extractor in
a secure microVM with internet disabled by default and records a content-free
usage event for billing. Sending decrypted bytes to E2B changes the plaintext
trust boundary and requires explicit deployment policy.

Independent reads and bounded `task` subagents can run concurrently. Disjoint
workspace mutations may commit in conflict-free parallel waves, while
same/ancestor/descendant paths serialize and external effectful mutations remain
provider-ordered. Subagents have isolated transcripts and read-only tool access;
they cannot reply, recurse, or perform platform/MCP/workspace mutations. The
current model catalog does not expose generic shell or arbitrary code execution.

See:

- [`docs/RUNTIME_V2_WORKSPACE.md`](RUNTIME_V2_WORKSPACE.md) for namespaces,
  encryption, conflict control, and sandbox triggers;
- [`docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`](HOSTED_RUNTIME_V2_PARITY_MATRIX.md)
  for current capability status; and
- [`deploy/HOSTED_RUNTIME_V2_ROLLOUT.md`](../deploy/HOSTED_RUNTIME_V2_ROLLOUT.md)
  for the V2-only deployment and recovery procedure.

The historical resident/container proposal remains available in Git history.
