# Agent runtime isolation (P5 — optional strong isolation)

Date: 2026-06-25

This is the **opt-in** strong-isolation design for the hosted agent runtime
(`backend/agent_runtime/`). It is **not** the v1 default — per
`docs/AGENT_RUNTIME_CC_CODEX_PLAN.zh.md` §P5, per-user containers/microVMs are a
separate security design layered on only for high-value/high-risk users, not the
first-version default path.

## The seam

The supervisor spawns consumers through an injected `(spawn_fn, alive_fn)` pair,
selected by `AGENT_RUNTIME_ISOLATION` (`process` | `container`). Strategies live
in `backend/agent_runtime/spawners.py`:

- **`process` (default, implemented):** one child process per user inside the
  shared agent-runner container. Isolation is per-user runtime home + per-user
  runtime token + non-root user. Cheap; the v1 path.
- **`container` (opt-in, partial):** `build_container_argv` produces the
  `docker run -d` command for one container + one volume per user, with secrets
  passed by env-var *reference* (never as plaintext argv). `get_spawner` still
  falls back to the process strategy for live spawn until the container lifecycle
  is finished (see below).

## What `container` buys

| Boundary | process (default) | container (opt-in) |
|---|---|---|
| Filesystem | shared FS, per-user home dir | per-user volume, no shared FS |
| Kernel/PID | shared | shared kernel, isolated PID/mounts/net ns |
| Blast radius of a compromised turn | other users' homes on same FS | contained to that user's container |
| Cost | ~1 process | ~1 container/user (image, memory, startup) |

A microVM strategy (Firecracker/Kata) would extend the same seam for kernel-level
isolation where the threat model demands it.

## To finish the container strategy

1. **Handle/alive mapping.** `process_alive` uses `os.kill(pid, 0)`; a container
   has a container id, not a local pid. Either store the container id in the
   lease (`pid` is `INTEGER` today → add a `handle TEXT` column, or map id→int)
   and implement `container_alive` via `docker inspect -f '{{.State.Running}}'`.
2. **Volume lifecycle.** Create/reuse `feedling-agent-vol-<uid>`; decide GC on
   long-idle users.
3. **Docker socket exposure.** The supervisor needs the Docker socket to spawn
   containers. Do **not** give it to the Flask backend (plan non-goal). Scope it
   to the agent-runner service only, and review the TDX/Phala implications of a
   docker-in-CVM or sibling-container model before enabling in prod.
4. **Image + secret delivery.** Per-user containers run
   `consumer_main.py` (single user), not a nested supervisor. Provider keys and
   the runtime token flow via env reference from the supervisor's environment.

## Non-goals (unchanged from the plan)

- Not the default: most users run the `process` strategy.
- The Flask backend never gets the Docker socket.
- No long-term provider keys on disk in either strategy.
