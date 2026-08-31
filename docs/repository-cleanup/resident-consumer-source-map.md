---
document_lifecycle: current
canonical_owner: self
---
# Resident consumer source map

`tools/chat_resident_consumer.py` is a protected, single-file distribution
boundary. It must continue to be executed as:

```sh
python tools/chat_resident_consumer.py
```

Do not split it into additional Python modules. The same file is directly
distributed to user VPS machines, launched by the fixed systemd command,
imported through test seams, and baked into the hosted agent-runner image. File
length is not evidence for decomposition. This map is for navigation and
cleanup review; it does not change the executable path or runtime behavior.

Current topology and runtime ownership remain in
[`docs/CURRENT_STATE.md`](../CURRENT_STATE.md). Operator setup and environment
examples remain in [`tools/README.md`](../../tools/README.md).

## Protected distribution and process contracts

| Contract | Current evidence | Must remain unchanged in documentation-only cleanup batches |
|---|---|---|
| Direct script | [`tools/chat_resident_consumer.py`](../../tools/chat_resident_consumer.py) starts `run()` under `if __name__ == "__main__"`. | The `python tools/chat_resident_consumer.py` entry path and a single executable consumer file. |
| VPS service | [`deploy/feedling-chat-resident.service`](../../deploy/feedling-chat-resident.service) sets the checkout as `WorkingDirectory`, loads an `EnvironmentFile`, and uses `ExecStart=/home/ubuntu/feedling-venv/bin/python tools/chat_resident_consumer.py`; `Restart=always` owns recovery. | The command shape, one-process model, environment-file handoff, and systemd restart semantics. |
| VPS P0 | [`tools/e2e/vps.py`](../../tools/e2e/vps.py)'s `run_vps_cell()` launches that exact script as a subprocess, supplies the resident environment, waits for the official poll heartbeat and `verify_loop`, then proves a chat round trip and decryptable reply. | The direct subprocess seam, supplied checkpoint/session paths, and end-to-end consumer lifecycle. |
| Hosted resident | [`deploy/Dockerfile.agent-runner`](../../deploy/Dockerfile.agent-runner) copies both `backend/` and `tools/`, then starts `backend/agent_runtime/supervisor.py`; the supervisor hosts one resident process per user. [`tests/test_agent_runtime_resident_contract.py`](../../tests/test_agent_runtime_resident_contract.py) pins the `spawners.consumer_env` → consumer import seam. | The hosted image's copy/import graph, supervisor-to-consumer environment names, and per-user process isolation. |
| Test imports | [`tests/test_chat_resident_self_update.py`](../../tests/test_chat_resident_self_update.py) imports `tools.chat_resident_consumer`; the hosted contract test loads the script with `importlib.util.spec_from_file_location`. | These direct import seams; moving behavior to companion modules changes their distribution and update obligations. |

The consumer is one foreground process per self-hosted user VPS installation.
Hosted Resident reuses the same file, with the supervisor creating one process
per user. Neither form authorizes a multi-module packaging or process-model
change as a cleanup side effect.

## Configuration, persistence, and exit contracts

| Area | Source navigation | Contract |
|---|---|---|
| Import-time configuration | `Config` section and module-level `FEEDLING_API_URL`, `FEEDLING_API_KEY`, `AGENT_MODE`, `CHECKPOINT_FILE`, `AGENT_SESSION_FILE_TEMPLATE`, and `FEEDLING_RUNTIME_TOKEN_FILE`. | The environment names are part of the VPS env-file, E2E, and hosted `consumer_env` seam. The module reads them at import time; preserve that import behavior and the runtime-token/API-key authentication fallback. |
| Checkpoints | `Checkpoint (persist last processed message timestamp)` section: `_load_checkpoint()`, `_save_checkpoint()`, `_load_proactive_checkpoint()`, and `_save_proactive_checkpoint()`. | Preserve the checkpoint file location behavior and JSON state shape, including `last_ts`, `last_job_ts`, `api_key_fingerprint`, and scoped `user_id`. It is the restart/redelivery cursor, not disposable cache. |
| Native-agent sessions | `Agent backends` section: `_load_agent_session_id()`, `_save_agent_session_id()`, `_clear_agent_session_id()`, and `_prepare_cli_command()`. | Preserve session-file placement and metadata/resume semantics. CLI session rotation or `--resume` behavior is a user-facing continuity contract. |
| Poll, decrypt, and reply wire | `Decrypt sources — plaintext content for v1 encrypted messages`, `Feedling API helpers`, and `Main loop` sections, especially `poll_chat()`, `_process_messages()`, and `run()`. | Preserve poll/response endpoint handling, encrypted-message processing, checkpoint advancement rules, and the single foreground loop. |
| Exit and supervision | `Main loop`'s `run()` plus `_apply_self_update()`. Fatal authentication handling exits the process; a failed re-exec exits cleanly so its supervisor/systemd restarts it. | Do not alter exit ownership, signal/restart expectations, or turn the daemon into a child task of an agent gateway. |

## Self-update and hosted-image boundary

The `Self-update — keep a self-hosted resident on the commit the backend
deploys` section owns release convergence:

1. `_maybe_self_update()` reads
   `client_release.expected_consumer_commit` from an idle poll response.
2. `_run_self_update()` compares it to `RUNNING_COMMIT`, protects dirty trees,
   and treats unknown/diff-failure state as not proven compatible.
3. `_runtime_repo_files()` and `_relevant_changed()` decide whether a changed
   release touches the consumer's actual runtime dependency set, including
   `tools/io_cli.py`, requirements files, and lazily imported backend paths.
4. `_apply_self_update()` checks out the advertised commit, installs changed
   requirements through `_pip_install()`, and `os.execv`s the same script.

The order is contractual: checkout precedes requirements installation, which
precedes re-exec. A release with no relevant changed files may advertise a
compatibility commit instead of forcing a restart. Do not replace this with a
generic updater that discovers modules differently.

`_HOSTED` is true for supervisor-managed runtime-token-file runs. Hosted
consumers do not self-update: their immutable image is refreshed by deployment.
The Dockerfile intentionally ships the consumer together with its backend
imports and agent CLIs; splitting the consumer would require revisiting this
image, the supervisor, and all direct-VPS update discovery together.

## Responsibility index

Use these existing section headers and stable symbols to navigate the large
file; do not reformat or split it merely to make this index shorter.

| Responsibility | Existing section header | Stable symbols to start from |
|---|---|---|
| Environment, paths, runtime mode, and process-wide policy | `Config` | `FEEDLING_API_URL`, `FEEDLING_API_KEY`, `AGENT_MODE`, `CHECKPOINT_FILE`, `AGENT_SESSION_FILE_TEMPLATE`, `RUNNING_COMMIT` |
| Backend commit convergence | `Self-update — keep a self-hosted resident on the commit the backend deploys` | `_runtime_repo_files()`, `_should_self_update()`, `_git_changed_files()`, `_apply_self_update()`, `_run_self_update()`, `_maybe_self_update()` |
| Chat/proactive cursors and replay protection | `Checkpoint (persist last processed message timestamp)` and `Message dedup` | `_load_checkpoint()`, `_save_checkpoint()`, `_load_proactive_checkpoint()`, `_save_proactive_checkpoint()`, `_msg_key()` |
| Decrypt sources and health | `Decrypt sources — plaintext content for v1 encrypted messages` and `Resident decrypt-source health — reported to the backend on every poll` | `get_decrypted_history()`, `_poll_decrypt_since()`, `_apply_infra_health()` |
| Attachments and screen context | `Image message handling` and `Screen-sharing context` | `_hydrate_omitted_bodies()`, `_should_attach_screen_context()` |
| Agent invocation and session continuity | `Agent backends` | `_prepare_cli_command()`, `_load_agent_session_id()`, `_save_agent_session_id()`, `call_agent()` |
| Local capability handoff | `io_cli capability catalog injection — VPS/self-hosted CLI resident only` | `_IO_CLI_PATH`, `_agent_can_use_local_io_cli()`, `_prepend_io_cli_capability_catalog()` |
| Backend wire helpers | `Feedling API helpers` | `_HEADERS`, `_load_whoami()`, `poll_chat()`, `post_reply()` |
| Foreground and maintenance work scheduling | `Main loop` and `Resident genesis-distill lane` | `_process_messages()`, `_process_resident_jobs()`, `_process_resident_distill_once()`, `run()` |

## Cleanup rule

Internal deletion is permitted only when exact call-site, configuration, wire,
persistence, and VPS/hosted-distribution evidence proves the responsibility
obsolete. Keep the fixed executable path, process model, import behavior,
update-relevance logic, checkpoint format, and session format unchanged in any
documentation-only cleanup batch.

After an accepted internal deletion, run:

```sh
python3 -m pytest -q tests/test_chat_resident_self_update.py \
  tests/test_agent_runtime_resident_contract.py \
  tests/test_chat_resident_consumer*.py
```

For a behavior-affecting deletion, additionally run the applicable VPS P0 path
on `test` and prove checkout/re-exec, checkpoint preservation, and the next
chat turn. The normal VPS entry is:

```sh
python3 tools/e2e/p0.py --only vps-claude-code
```

Choose the installed CLI cell when Claude Code is not the affected path.

## Future simplification artifacts

This baseline has no Task 9 Feedling simplification skill or standalone
candidate template, and this task creates neither. Any future simplification
skill or candidate template must consume this protection rule: classify
`tools/chat_resident_consumer.py` as **excluded from decomposition** and require
the evidence and verification gates above before proposing an internal deletion.

Task 6 candidate records under [`candidates/`](candidates/) remain separate
review artifacts. This source map neither accepts nor deletes a candidate.
