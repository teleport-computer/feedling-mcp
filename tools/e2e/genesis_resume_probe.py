"""本机全栈探针:plaintext 导入「跑到一半死掉 → 重试续跑」到底发生了什么。

回答两个单测结构上答不了的问题:
  1. 续跑真的跳过了已完成的窗吗?(按 per-window 模型调用数计,fact + voice 两类)
  2. 续跑会不会把上一轮已写的卡再写一遍?(真写卡、真数行)

为什么单测答不了:tests/test_genesis_plaintext_routes.py 的续跑用例把
``apply_reducer_output`` stub 掉了,所以一张卡都不会落地 —— "不重复写卡"这个
性质在那里是测不出来的。usr_3b73f1cb0a9ec975 手上有 138 张来自失败导入的卡,
如果重试会翻倍,那"让用户重传"就是有害建议。

真链路 / 假边界:后端(serve_dev)、enclave(dev seed)、PostgreSQL、信封与
checkpoint 的真实加解密回环全部是真的;**唯一的替身是 provider 的 HTTP 边界**
(``provider_client.reliable_chat_completion``)—— 那是我们不拥有的边界,替身
打在这里才不会把生产走不到的路测成绿的。

跑法(三个终端 / 后台进程):

    createdb feedling_probe

    # enclave —— NO_PROXY 必须给,否则 macOS 系统代理会污染回环(enclave→backend 502)
    cd backend && FEEDLING_DEV_DSTACK_SEED=seed FEEDLING_FLASK_URL=http://127.0.0.1:5001 \
      FEEDLING_RUNTIME_TOKEN_SECRET=secret NO_PROXY='*' python3 enclave_app.py

    # backend
    cd backend && DATABASE_URL=postgresql://$USER@127.0.0.1:5432/feedling_probe \
      FEEDLING_ENCLAVE_URL=http://127.0.0.1:5003 FEEDLING_DEV_DSTACK_SEED=seed \
      FEEDLING_RUNTIME_TOKEN_SECRET=secret NO_PROXY='*' python3 serve_dev.py

    # 探针
    DATABASE_URL=postgresql://$USER@127.0.0.1:5432/feedling_probe \
      FEEDLING_ENCLAVE_URL=http://127.0.0.1:5003 FEEDLING_DEV_DSTACK_SEED=seed \
      FEEDLING_RUNTIME_TOKEN_SECRET=secret NO_PROXY='*' \
      python3 tools/e2e/genesis_resume_probe.py

2026-08-09 实测(第一批合入后):

    run1(reduce 处模拟进程死亡) fact_map 3, voice_map 3, voice_reduce 1, fact_write 1 -> 0 卡, failed
    run2(重试续跑)             fact_map 0, voice_map 0, voice_reduce 2, fact_write 1 -> 1 卡, done

  => 每窗模型调用全部跳过;卡片总数没有翻倍;reduce 尾巴照跑(reduce 不落 checkpoint,已知且便宜)。
  反证:FEEDLING_GENESIS_VOICE_CHECKPOINT_ENABLED=0 时 run2 的 voice_map 回到 3 —— 探针确实会咬人。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Drop this script's own directory from sys.path first: tools/e2e/hosted.py
# would otherwise shadow the backend's `hosted` package.
_HERE = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", ".", _HERE)]
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "backend"))
import db  # noqa: E402
from core import store as core_store  # noqa: E402
from genesis import plaintext  # noqa: E402
from asgi_test_client import make_client  # noqa: E402  (also wires core.envelope)


WINDOWS = ["窗口一:我周末沿江骑行。", "窗口二:我养了一只金毛叫蛋子。", "窗口三:失眠时我听白噪音。"]


class _Calls:
    def __init__(self) -> None:
        self.by_kind: dict[str, int] = {}

    def bump(self, kind: str) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def total(self) -> int:
        return sum(self.by_kind.values())


def _classify(messages: list[dict]) -> str:
    """Classify by prompt text. A *map* call carries a raw window verbatim;
    reduce/write calls carry candidate JSON instead. Splitting on that keeps
    voice-map (per window, checkpointable) apart from voice-reduce (per batch,
    NOT checkpointed) — conflating them hides whether resume really skipped."""
    blob = " ".join(str(m.get("content") or "") for m in messages)
    low = blob.lower()
    carries_window = any(w in blob for w in WINDOWS)
    voiceish = ("behavior_notes" in low) or ("exemplar" in low)
    if carries_window and voiceish:
        return "voice_map"
    if carries_window and "fact_candidates" in low:
        return "fact_map"
    if voiceish and not carries_window:
        return "voice_reduce"
    if "memories" in low:
        return "fact_write"
    if "persona" in low:
        return "persona_build"
    return "other"


def _canned(kind: str) -> str:
    if kind == "fact_map":
        return json.dumps({"fact_candidates": [
            {"summary": "周末沿江骑行", "content": "用户周末沿江骑行。"},
        ]}, ensure_ascii=False)
    if kind == "voice_map":
        return json.dumps({"behavior_notes": ["说话简短"], "exemplars": [
            {"text": "行,那就这样", "founding": True},
        ]}, ensure_ascii=False)
    if kind == "fact_write":
        return json.dumps({"memories": [
            {"summary": "周末沿江骑行", "content": "用户周末沿江骑行。",
             "type": "fact", "occurred_at": "2026-01-01T00:00:00"},
        ], "identity": {"agent_name": "", "dimensions": []}}, ensure_ascii=False)
    if kind == "persona_build":
        return "简短、直接的陪伴风格。"
    return json.dumps({}, ensure_ascii=False)


def _card_count(uid: str) -> int:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_moments WHERE user_id = %s", (uid,)
        ).fetchone()
    return int(row[0]) if row else 0


def run_probe(monkeypatch):
    import base64
    reg = make_client().post("/v1/users/register", json={
        "public_key": base64.b64encode(b"\x11" * 32).decode("ascii"),
        "archive_language": "zh-Hans-CN",
    })
    assert reg.status_code == 201, reg.get_json()
    uid = reg.get_json()["user_id"]
    api_key = reg.get_json()["api_key"]
    job_id = "genesis_gate_resume"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM memory_moments WHERE user_id=%s", (uid,))

    calls = _Calls()

    state = {"die_on_fact_write": True}

    def fake_completion(_runtime, messages, **_kwargs):
        kind = _classify(messages)
        calls.bump(kind)
        if kind == "fact_write" and state["die_on_fact_write"]:
            raise RuntimeError("simulated process death mid-run")
        return {"reply": _canned(kind), "usage": {}, "stop_reason": "stop"}

    monkeypatch.setattr(
        plaintext.worker.provider_client, "reliable_chat_completion", fake_completion
    )
    monkeypatch.setattr(
        plaintext.hosted_config_store, "_load_runtime_provider_config",
        lambda *_a, **_k: plaintext.worker.provider_client.ProviderConfig(
            provider="openai_compatible", model="stub/model",
            base_url="http://127.0.0.1:1/v1", api_key="stub",
        ),
    )

    db.genesis_create_job(uid, {
        "job_id": job_id, "status": "processing", "source_kind": "history_import",
        "total_chunks": len(WINDOWS),
        "metadata": {"ingest": "plaintext", "mode": "onboarding", "window_count": len(WINDOWS)},
    })

    store = core_store.get_store(uid)
    kwargs = {
        "source_groups": [{
            "source_kind": "history_import",
            "source_family": "history",
            "chunk_texts": list(WINDOWS),
        }],
        "analysis_messages": [{"role": "user", "content": w} for w in WINDOWS],
    }

    # --- run 1: let it run for real, then inspect ---------------------------- #
    try:
        plaintext._run_plaintext_genesis_job(store, api_key, job_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 — probe records, does not assert
        print(f"[run1] raised {type(exc).__name__}: {str(exc)[:200]}")
    run1_calls = dict(calls.by_kind)
    run1_cards = _card_count(uid)

    # --- run 2: the retry path re-drives the SAME job id --------------------- #
    state["die_on_fact_write"] = False
    calls.by_kind.clear()
    try:
        plaintext._run_plaintext_genesis_job(store, api_key, job_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[run2] raised {type(exc).__name__}: {str(exc)[:200]}")
    run2_calls = dict(calls.by_kind)
    run2_cards = _card_count(uid)

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, error FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s",
            (uid, job_id),
        ).fetchone()

    print("\n================ GATE PROBE: retry-resume ================")
    print(f"run1 llm calls : {run1_calls}")
    print(f"run1 cards     : {run1_cards}")
    print(f"run2 llm calls : {run2_calls}")
    print(f"run2 cards     : {run2_cards}")
    print(f"job terminal   : {row}")
    print(f"delta cards    : {run2_cards - run1_cards}")
    print("=========================================================\n")

    # cleanup
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM memory_moments WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


class _MP:
    def __init__(self): self._undo = []
    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    def undo(self):
        for obj, name, old in reversed(self._undo): setattr(obj, name, old)


if __name__ == "__main__":
    mp = _MP()
    try:
        run_probe(mp)
    finally:
        mp.undo()
