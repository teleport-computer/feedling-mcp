# NOTES — swap `backend/perception_kernel/` for the `sensegate` package

Branch: `feat/sensegate-integration`, based on `origin/test`. Mirrors how
`memgarden` (commit `4d25dbfb`) was swapped from a vendored copy to an
external dependency.

## What changed

1. **Imports rewritten.** `perception_kernel` → `sensegate` in every importer
   (module names underneath are unchanged: `catalog`, `fields`, `glance`,
   `history`, `prompts`, `wake`).

   Found with:

       grep -rn perception_kernel backend tools --include="*.py"

   This returned **11 files with an actual `from perception_kernel import …`
   statement** (not 12 as the task brief said — I double-checked with a
   full-repo grep and only found 11; the 12th may have been a miscount or a
   file that no longer exists on `test`). The 11:

       backend/capabilities/tool_schema.py
       backend/model_api_runtime/v2/worker.py
       backend/model_api_runtime/v2/context.py
       backend/perception/differ_v2.py
       backend/perception/catalog.py
       backend/perception/signal_state_v2.py
       backend/perception/glance.py
       backend/perception/agent_fields.py
       backend/perception/permissions.py
       backend/perception/history.py
       tools/chat_resident_consumer.py

   All rewritten with `perl -pi -e 's/\bperception_kernel\b/sensegate/g'`
   (macOS BSD `sed -E` silently does not support `\b`, so a first attempt with
   `sed` was a no-op — worth remembering for the next swap).

2. **`backend/perception_kernel/` deleted** (`git rm -r`), 7 files: `__init__.py`,
   `catalog.py`, `fields.py`, `glance.py`, `history.py`, `prompts.py`, `wake.py`.

3. **Dependency declared**, mirroring memgarden's style exactly:

   `backend/requirements.txt` (appended after the memgarden block):

       # 感知内核 —— 同样从本仓库提取出去的独立库（sensegate），走法与上面的
       # memgarden 一致：GitHub Release 的 wheel URL，理由同上（--require-hashes +
       # compose 哈希上链要求构建可复现）。
       #
       # 2026-08-26 挂起：sensegate 仓库当前是私有的，且还没发过 Release，下面这个
       # URL 现在还 404。CI 装不了这一步，要等到：① 仓库转 public，② 打出
       # v0.1.0 这个 Release（带 wheel 附件）。挂起期间本地验证走
       # `pip install -e /Users/hx/Projects/sensegate`（见 docs/NOTES-sensegate-swap.md）。
       sensegate @ https://github.com/teleport-computer/sensegate/releases/download/v0.1.0/sensegate-0.1.0-py3-none-any.whl

   `backend/requirements.lock` (inserted alphabetically between `s3transfer`
   and `six`, **commented out** — see below):

       # sensegate @ https://github.com/teleport-computer/sensegate/releases/download/v0.1.0/sensegate-0.1.0-py3-none-any.whl
           # PENDING (2026-08-26)：sensegate 仓库还是私有的、还没打过 Release，这个
           # URL 现在 404，`uv pip compile --generate-hashes` 也就算不出真哈希。
           # 先注释掉，别在 --require-hashes 的锁文件里放一个没有哈希的条目
           # （pip 会直接拒绝整个安装）。等仓库转 public + 打出 v0.1.0 Release 后，
           # 取消注释这行、删掉这段注释，重新跑一次
           # `uv pip compile backend/requirements.txt --generate-hashes --python-version 3.12 --python-platform linux -o backend/requirements.lock`
           # 让 uv 补齐真实哈希。
           # via -r backend/requirements.txt
       six==1.17.0 \

   **Why the lock line is commented out instead of a live entry**: the lock
   file is consumed with `pip install --require-hashes` (see
   `deploy/Dockerfile:50`). In that mode, if *any* package line is missing a
   `--hash=`, pip refuses the entire install — not just that package. Since
   there is no way to compute a real hash for a wheel that doesn't exist yet
   (private repo, no release), a live un-hashed line would break every
   hash-verified build, not just fail to resolve sensegate. Leaving it
   commented documents intent without breaking the file's contract.

   **Blocks CI clearly: today, `pip install --require-hashes -r
   requirements.lock` will not install `sensegate` at all** (the line is
   commented out) — the Docker image build step
   (`docker compose build (hash-verified)` in `.github/workflows/ci.yml`) will
   fail with `ModuleNotFoundError: No module named 'sensegate'` at container
   startup, or fail earlier if any code path imports it during build-time
   checks. **This cannot be fixed until the `sensegate` repo goes public and
   cuts a `v0.1.0` GitHub Release with a wheel asset** — at that point,
   uncomment the lock line and run `uv pip compile
   backend/requirements.txt --generate-hashes --python-version 3.12
   --python-platform linux -o backend/requirements.lock` to fill in the real
   hash.

4. **Local verification install** (not committed, this repo's global
   `python3` at `/opt/homebrew/lib/python3.14`, already has `fastapi` etc.
   installed via `--break-system-packages` from prior work):

       python3 -m pip install --break-system-packages -e /Users/hx/Projects/sensegate
       python3 -m pip install --break-system-packages \
           -e /Users/hx/Projects/io/memory-garden/packages/agent-protocol-core \
           -e /Users/hx/Projects/io/memory-garden

   (memgarden + agent-protocol-core also had to be installed editable — they
   weren't present in this environment either, and `backend/memory/actions.py`
   imports `memgarden.text.card_guard` transitively, which the golden test
   pulls in.)

5. **Tests removed** (`tests/test_perception_kernel_{catalog,projection,purity,wake}.py`),
   matching the memgarden precedent exactly (commit `4d25dbfb` deleted
   `test_memgarden_purity.py` only, keeping backend-owned tests like
   `test_memgarden_policies.py`/`*_golden.py`):

   - `tests/conftest.py` `_PURE_UNIT`: removed the 4 kernel-only entries,
     kept `test_perception_prompt_golden.py` (per instructions — it tests
     how *this backend* assembles prompts), added a note dated 2026-08-26
     explaining the deletion, matching the exact wording style memgarden used
     ("已删（内核成了外部包...守卫搬进了包自己的仓库）").
   - `.github/workflows/ci.yml`: the "Perception kernel unit tests" step had
     4 kernel-only tests + the golden test. Memgarden's equivalent step
     ("Memory Garden kernel unit tests") was **kept and its list trimmed**
     (purity test out, everything else — which was backend-owned — stayed).
     Perception's step has *only* the golden test left after removing the 4
     kernel tests, so I renamed it to **"Perception prompt golden test"** and
     reduced the `pytest` invocation to just that one file, rather than
     deleting the step outright — same "repurpose, don't delete" call
     memgarden made, just with a smaller surviving list.
   - Did **not** create a `test_sensegate_is_a_real_dependency.py` guard
     (memgarden's parallel to the deleted purity test). The task didn't ask
     for it and I wanted to stay in scope, but it's a good follow-up: it would
     assert `backend/perception_kernel` never reappears and that the lock pins
     an immutable release wheel with a hash — exactly the drift guard this
     swap is meant to prevent.

6. **Doc note added** to `docs/PERCEPTION_PROMPT_ASSETS.zh.md` pointing
   `perception_kernel.*` references at the new `sensegate.*` package name,
   mirroring the pointer note memgarden's swap added to `docs/MEMORY.md`.

## Important finding: sensegate is missing a fix that shipped in io days ago

While running the broader test suite, 12 non-baseline failures appeared in
`tests/test_perception_history.py` and `tests/test_agent_perception_route.py`:

    TypeError: cross_domain_recent() got an unexpected keyword argument 'last_report_ts_by_signal'

**Root cause**: io's `test` branch landed commit `c7cdae93` ("fix(perception):
mark stale digest trends last known") *after* the sensegate extraction/fork
point. That fix changed `history.notable_changes()` and
`history.cross_domain_recent()` to accept `last_report_ts_by_signal` / `now` /
`timezone_name` and mark stale digest fields as `last_known`/`as_of` instead
of silently claiming they're current. `backend/agent/perception_core.py`
(a real caller, not just a test) already calls the new signature. The
standalone `/Users/hx/Projects/sensegate` library never received this fix —
it's still on the old 2-arg signature.

This is exactly the drift problem the memgarden migration was designed to
prevent, caught in the act: the vendored copy kept receiving fixes after the
library fork, and nothing flagged the divergence.

**What I did**: ported the fix into
`/Users/hx/Projects/sensegate/src/sensegate/history.py` (added
`_field_report_freshness`, `_format_report_as_of`, updated both function
signatures) so local verification passes with real parity, not by working
around the mismatch. **This edit is uncommitted in the sensegate repo** — I
did not commit or push it there, since that's a separate product repo outside
this task's scope. I diffed every other shared module
(`catalog`/`fields`/`glance`/`wake`/`prompts`) against the vendored copies at
HEAD and found **no other functional divergence** — only doc-wording
rewrites (host-neutral language), confirmed by the golden test's 23/23 pass
(byte-for-byte prompt text).

**This must be committed and released in the sensegate repo before its
`v0.1.0` release is cut** — if the release is tagged from the current
`sensegate` main without this fix, the backend would silently regress: stale
health digest fields would be reported as `current` again, the exact bug
`c7cdae93` fixed.

## Verification

    $ python3 -m pytest tests/test_perception_prompt_golden.py -v
    ...
    23 passed, 2 warnings in 2.11s

    $ python3 -m pytest tests/ -k "perception or wake or glance" -q -p no:randomly \
        --ignore=tests/test_api.py --ignore=tests/test_redis_pool.py --ignore=tests/test_image_generation_model.py
    ...
    788 passed, 3 skipped, 10751 deselected, 7 warnings in 16.23s

    $ python3 -m pytest tests/ -q -p no:randomly \
        --ignore=tests/test_api.py --ignore=tests/test_redis_pool.py --ignore=tests/test_image_generation_model.py
    ...
    11 failed, 11514 passed, 8 skipped, 9 xfailed, 68 warnings, 3 subtests passed in 422.19s

    FAILED tests/test_e2b_template_contract.py::test_tracked_template_tag_matches_extractor_and_pinned_contract
    FAILED tests/test_file_text.py::test_pdf_extracts_text_via_pypdf
    FAILED tests/test_plaintext_shadow_config.py::test_live_identity_rejects_hostname_aliases_to_same_database
    FAILED tests/test_plaintext_shadow_config.py::test_live_identity_accepts_different_databases_on_same_server
    FAILED tests/test_provider_client.py::test_dedicated_url_answer_is_fetched_and_must_decode
    FAILED tests/test_provider_client.py::test_a_link_inside_a_chat_reply_is_never_fetched
    FAILED tests/test_provider_client.py::test_links_are_capped_and_share_one_byte_budget
    FAILED tests/test_provider_client.py::test_official_providers_also_fetch_a_url_answer
    FAILED tests/test_provider_client.py::test_links_share_one_wall_clock_budget
    FAILED tests/test_v2_downloadable_files.py::test_workspace_file_result_renders_real_word_and_pdf_documents
    FAILED tests/test_v2_downloadable_files.py::test_process_job_commits_single_generated_image_without_empty_followups

11 failures, matching the pre-existing baseline count exactly, and none of
them touch perception/wake/glance/history — all e2b/PDF/plaintext-shadow-config/
provider_client(network fetch)/downloadable-files, unrelated areas.

## What still blocks CI

1. **`sensegate` is not installable from `requirements.lock` yet.** The
   dependency line is commented out (see above) because the release wheel
   doesn't exist (repo private, no `v0.1.0` tag). CI's hash-verified Docker
   build will fail to find `sensegate` until: (a) the `sensegate` repo goes
   public, (b) a `v0.1.0` GitHub Release with a wheel asset is cut, (c) the
   lock line is uncommented and `uv pip compile --generate-hashes` is rerun
   to fill in the real hash.
2. **The stale-digest fix (`c7cdae93`) is not yet in the sensegate repo.**
   I patched the working copy at `/Users/hx/Projects/sensegate` locally
   (uncommitted) to unblock verification here. Someone needs to commit and
   land that fix in the sensegate repo before cutting `v0.1.0`, or the
   release will regress behavior that's already shipped in io.
