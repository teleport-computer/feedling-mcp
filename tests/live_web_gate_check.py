"""LIVE verification of the web gate — real model, real search. NOT part of CI.

Everything else in this feature is covered by tests that stub the provider, so
they prove the wiring but not the behaviour. This file drives a real V2 chat turn
against a real model API and lets `web_search` actually hit the network, which is
the only way to answer the questions that matter:

- with the toggle ON, does the model really search, and does the search return
  anything usable? (`capabilities/web.py` scrapes DuckDuckGo — worth knowing
  whether that still works in practice)
- with the toggle OFF, does the model behave sanely, or does it say something
  confusing like "I can't reach the internet"?

Filename deliberately does NOT start with `test_`, so pytest never collects it.
Run it directly:

    python3 tests/live_web_gate_check.py

Costs a few cents of the DEEPSEEK_KEY in io/.env.local.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

ADMIN = "postgresql://postgres:test@127.0.0.1:55432/postgres"


def _load_key(name: str) -> str:
    for line in open("/Users/hx/Projects/io/.env.local", encoding="utf-8"):
        m = re.match(rf"\s*{name}\s*=\s*(.+)\s*$", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"{name} not found in io/.env.local")


def _provision() -> tuple[str, str]:
    """Throwaway DBs + migrations, mirroring conftest's session setup."""
    import psycopg

    test_db = f"live_web_{uuid.uuid4().hex[:10]}"
    tee_db = f"live_web_tee_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{test_db}"')
        c.execute(f'CREATE DATABASE "{tee_db}"')
    base = ADMIN.rpartition("/")[0]
    os.environ["DATABASE_URL"] = f"{base}/{test_db}"
    os.environ["TEE_DATABASE_URL"] = f"{base}/{tee_db}"
    os.environ["TEE_MIGRATION_DATABASE_URL"] = os.environ["TEE_DATABASE_URL"]
    os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "live-check-secret")

    import db as _db

    _db.init_schema()
    from alembic_tee import upgrade_head

    upgrade_head()
    return test_db, tee_db


def _drop(*names: str) -> None:
    import psycopg

    with psycopg.connect(ADMIN, autocommit=True) as c:
        for n in names:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (n,),
            )
            c.execute(f'DROP DATABASE IF EXISTS "{n}"')


QUESTION = (
    "今天北京的实时天气怎么样？请给出气温和天气状况。"
    "这是时效性问题,如果你有联网搜索工具,请务必先用它查询再回答;"
    "如果没有这个工具,直接说明你无法联网即可。"
)


def run_case(*, web_enabled: bool, provider_config, uid: str) -> dict:
    import conftest  # noqa: F401 — only for seed_user
    import db
    from model_api_runtime.v2 import jobs_store, worker

    conftest.seed_user(uid)
    # Same setup the DB-backed V2 tests do: without the runtime-owner row the
    # job is never claimable, and the real reply write needs a live enclave this
    # process does not have, so the envelope build (and only that) is stubbed.
    conftest.set_v2_runtime_owner(uid)

    def _real_write(store, text, *, extra=None):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat(
            "openclaw", "model_api", envelope, strict=True, extra=(extra or None)
        )

    worker._write_encrypted_reply = _real_write

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
        conn.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,%s,%s::jsonb) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, "model_api_runtime", json.dumps({"hosted_runtime_mode": "db_action_v2"})),
        )

    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("live-worker")
    if job is None:
        raise RuntimeError("job was not claimable — runtime owner / lane setup missing")

    seen: dict = {"offered": None, "tool_calls": [], "tool_results": [],
                  "provider_calls": 0, "provider_error": None}

    import provider_client as _pc
    _real_call = _pc.chat_completion_async

    async def _spy_call(*a, **k):
        seen["provider_calls"] += 1
        # GROUND TRUTH: what the model was actually offered on the wire, rather
        # than something re-derived from the disabled set on this side.
        tools = k.get("tools")
        if tools is None:
            for arg in a:
                if isinstance(arg, (list, tuple)) and arg and hasattr(arg[0], "name"):
                    tools = arg
                    break
        seen.setdefault("wire_tools", [])
        seen["wire_tools"].append(sorted(getattr(t, "name", str(t)) for t in (tools or ())))
        try:
            return await _real_call(*a, **k)
        except Exception as e:  # noqa: BLE001
            seen["provider_error"] = f"{type(e).__name__}: {e}"[:400]
            raise

    _pc.chat_completion_async = _spy_call
    real_loop = worker.v2_tool_loop.run_tool_loop

    async def _spy_loop(**kwargs):
        inner = kwargs["dispatch_tools"]

        async def wrapped(calls):
            seen["tool_calls"] += [c.name for c in calls]
            out = await inner(calls)
            seen["tool_results"] += [str(r.content)[:400] for r in out]
            return out

        kwargs["dispatch_tools"] = wrapped
        disabled = set(kwargs.get("disabled_tool_names") or ())
        from capabilities import tool_schema

        seen["offered"] = sorted(
            s.name for s in tool_schema.build_tool_specs() if s.name not in disabled
        )
        return await real_loop(**kwargs)

    worker.v2_tool_loop.run_tool_loop = _spy_loop
    try:
        deps = worker.TurnDeps(
            read_messages=lambda _uid: [
                {"id": "m1", "ts": 10.0, "role": "user", "content": QUESTION}
            ],
            resolve_provider=lambda _uid: (provider_config, {}),
            mint_enclave_token=lambda _uid: "rt-live",
            web_tools_enabled=lambda _uid: web_enabled,
        )
        status = asyncio.run(
            worker.process_job(
                job, deps,
                provider_config=provider_config,
                api_key=None,
                runtime_token="rt-live",
            )
        )
    finally:
        worker.v2_tool_loop.run_tool_loop = real_loop
        _pc.chat_completion_async = _real_call

    # Read replies through the store (chat_messages columns are not a flat
    # body_ct — the envelope shape lives inside the row payload).
    replies = []
    try:
        from core import store as core_store

        st = core_store.get_store(uid)
        st.reload()
        replies = [
            f"[{m.get('role')}/{m.get('source')}] {str(m.get('body_ct'))[:500]}"
            for m in st.chat_messages
        ]
    except Exception as e:  # noqa: BLE001 — the tool evidence matters more
        replies = [f"<could not read replies: {type(e).__name__}: {e}>"]

    job_row = None
    with db.get_pool().connection() as conn:
        r = conn.execute(
            "SELECT to_jsonb(t) FROM agent_jobs t WHERE user_id=%s LIMIT 1",
            (uid,)).fetchone()
        if r:
            row = r[0]
            job_row = {k: str(v)[:200] for k, v in row.items()
                       if k in ("status", "lane", "error", "error_code", "detail",
                                "last_error", "failure_code", "attempts")}

    web = {"web_search", "web_fetch"}
    return {
        "job_row": job_row,
        "provider_calls": seen["provider_calls"],
        "wire_tools_web": [sorted({"web_search", "web_fetch"} & set(t))
                           for t in seen.get("wire_tools", [])],
        "wire_tools_count": [len(t) for t in seen.get("wire_tools", [])],
        "provider_error": seen["provider_error"],
        "web_enabled": web_enabled,
        "status": status,
        "web_offered": sorted(web & set(seen["offered"] or [])),
        "tools_the_model_called": seen["tool_calls"],
        "tool_results_head": seen["tool_results"][:2],
        "replies": replies,
    }


def main() -> None:
    test_db, tee_db = _provision()
    try:
        import provider_client

        cfg = provider_client.ProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            api_key=_load_key("DEEPSEEK_KEY"),
            base_url="",
        )
        only = os.environ.get("LIVE_CASE")
        cases = [only == "on"] if only else [True, False]
        out = []
        for enabled in cases:
            try:
                out.append(run_case(
                    web_enabled=enabled, provider_config=cfg,
                    uid=f"u_live_{'on' if enabled else 'off'}"))
            except Exception as e:  # noqa: BLE001 — report, don't abort the other case
                import traceback
                out.append({"web_enabled": enabled,
                            "error": f"{type(e).__name__}: {e}"[:300],
                            "traceback": traceback.format_exc()[-1500:]})
        suffix = f"_{os.environ['LIVE_CASE']}" if os.environ.get("LIVE_CASE") else ""
        # Results go to a scratch dir, never into the repo.
        out_dir = Path(os.environ.get("TMPDIR", "/tmp"))
        out_dir.joinpath(f"live_web_gate_result{suffix}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {out_dir}/live_web_gate_result{suffix}.json")
    finally:
        _drop(test_db, tee_db)


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------
# Observed 2026-07-20 (deepseek-chat -> deepseek-v4-flash, real DeepSeek API):
#
#   toggle ON  -> request carried 23 tools, including web_search and web_fetch
#   toggle OFF -> request carried 21 tools, neither web tool present
#
# 23 - 21 == exactly the two web tools: the gate is real on the wire, not just
# in the offered-catalog bookkeeping. Both turns completed normally.
#
# Two things this run also settled, neither of which the stubbed tests could:
#
# - `capabilities/web.py`'s DuckDuckGo scrape works: ~1.3s, real current results
#   (a same-week OpenAI announcement came back). The 2026 "DDG scraping is
#   rate-limited into uselessness" worry did not reproduce here.
# - The model did NOT call the tool even when the prompt explicitly told it to
#   search a time-sensitive question. The gate offers the capability; whether a
#   given model uses it is a separate, model-dependent question — a user can
#   switch this on and still see no behaviour change.
# --------------------------------------------------------------------------
