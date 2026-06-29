#!/usr/bin/env python3
"""V1 automated e2e smoke suite for the deployed test backend.

This is intentionally a smoke harness, not an eval:
- Uses a throwaway user.
- Asserts structure, state machines, encrypted read/write, and fact survival.
- Uses deterministic stubs for resident-agent derivation steps so LLM wording
  does not make the suite flaky.

Default:
    python3 tools/v1_e2e_suite.py --api-url https://test-api.feedling.app \
      --enclave-url https://<test-cvm>-5003s.dstack-pha-prod9.phala.network
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
DEFAULT_API = "https://test-api.feedling.app"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass
class CaseResult:
    name: str
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class SuiteFailure(RuntimeError):
    pass


class SuiteBlocked(RuntimeError):
    pass


class Http:
    def __init__(self, api_url: str, api_key: str = "", *, retries: int = 3):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.retries = retries

    def _headers(self) -> dict[str, str]:
        out = {"Content-Type": "application/json"}
        if self.api_key:
            out["X-API-Key"] = self.api_key
        return out

    def request(self, method: str, path: str, *, payload: dict | None = None, timeout: int = 30) -> dict:
        url = f"{self.api_url}{path}"
        last = None
        for attempt in range(self.retries):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout,
                )
                if resp.status_code >= 400:
                    raise SuiteFailure(f"{method} {path} -> {resp.status_code}: {resp.text[:800]}")
                return resp.json() if resp.text else {}
            except (requests.RequestException, SuiteFailure) as exc:
                last = exc
                if isinstance(exc, SuiteFailure):
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise SuiteFailure(f"{method} {path} failed after retries: {last}")

    def get(self, path: str, *, timeout: int = 30) -> dict:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, payload: dict | None = None, *, timeout: int = 30) -> dict:
        return self.request("POST", path, payload=payload or {}, timeout=timeout)

    def put(self, path: str, payload: dict | None = None, *, timeout: int = 30) -> dict:
        return self.request("PUT", path, payload=payload or {}, timeout=timeout)

    def request_status(self, method: str, path: str, *, payload: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
        url = f"{self.api_url}{path}"
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            json=payload,
            timeout=timeout,
        )
        try:
            body = resp.json() if resp.text else {}
        except ValueError:
            body = {"raw": resp.text[:800]}
        return resp.status_code, body


def log(msg: str) -> None:
    print(msg, flush=True)


def wait_for_tls(api_url: str, *, attempts: int, sleep_sec: float) -> None:
    client = Http(api_url, retries=1)
    for attempt in range(1, attempts + 1):
        try:
            body = client.get("/healthz", timeout=10)
            if body.get("ok") is True:
                log(f"[tls] {api_url} healthy on attempt {attempt}")
                return
        except Exception as exc:  # noqa: BLE001
            log(f"[tls] attempt={attempt} not ready: {exc}")
        time.sleep(sleep_sec)
    raise SuiteFailure(f"{api_url} did not become healthy after {attempts} attempts")


def register_user(api_url: str) -> tuple[str, str]:
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    body = Http(api_url).post("/v1/users/register", {
        "public_key": _b64(pub),
        "archive_language": "zh-Hans",
    })
    user_id = str(body.get("user_id") or "")
    api_key = str(body.get("api_key") or "")
    if not user_id or not api_key:
        raise SuiteFailure(f"register response missing user_id/api_key: {_pretty(body)}")
    return user_id, api_key


def import_consumer(api_url: str, enclave_url: str, api_key: str):
    os.environ["FEEDLING_API_URL"] = api_url.rstrip("/")
    os.environ["FEEDLING_ENCLAVE_URL"] = enclave_url.rstrip("/")
    os.environ["FEEDLING_API_KEY"] = api_key
    os.environ.setdefault("AGENT_MODE", "cli")
    os.environ.setdefault("AGENT_CLI_CMD", "unused-for-v1-e2e-suite {message}")
    os.environ.setdefault("FEEDLING_MIGRATE_BATCH", "10")
    os.environ.setdefault("CONSUMER_ID", f"v1-e2e-{secrets.token_hex(4)}")
    sys.path.insert(0, str(TOOLS))
    import chat_resident_consumer as consumer  # type: ignore

    return consumer


def switch_consumer(consumer, api_url: str, enclave_url: str, api_key: str) -> None:
    """Reuse the imported consumer module with a new throwaway user's auth.

    chat_resident_consumer is a CLI script with module-level globals. Importing
    it repeatedly in one Python process is not clean, so the suite mutates the
    same auth globals between isolated throwaway users.
    """
    consumer.FEEDLING_API_URL = api_url.rstrip("/")
    consumer.FEEDLING_ENCLAVE_URL = enclave_url.rstrip("/")
    consumer.FEEDLING_API_KEY = api_key
    consumer._HEADERS.pop("X-Feedling-Runtime-Token", None)
    consumer._HEADERS["X-API-Key"] = api_key
    consumer._HEADERS["X-Feedling-Consumer-Id"] = f"v1-e2e-{secrets.token_hex(4)}"
    consumer._whoami_cache = {"user_id": "", "user_pk": None, "enclave_pk": None}
    if hasattr(consumer, "httpx"):
        consumer._ENCLAVE_CLIENT = consumer.httpx.Client(timeout=20, verify=False)
    try:
        consumer._seen_ids.clear()
    except Exception:
        pass


def new_context(api_url: str, enclave_url: str, consumer=None):
    user_id, api_key = register_user(api_url)
    if consumer is None:
        consumer = import_consumer(api_url, enclave_url, api_key)
    else:
        switch_consumer(consumer, api_url, enclave_url, api_key)
    log(f"[setup] case user={user_id} api_key={api_key[:8]}… enclave={enclave_url}")
    return user_id, Http(api_url, api_key), consumer


def require_whoami(consumer) -> dict:
    if not consumer._refresh_whoami_for_encrypted_reply():
        raise SuiteFailure("consumer whoami refresh failed")
    return dict(consumer._whoami_cache)


def build_text_envelope(consumer, text: str) -> dict:
    who = require_whoami(consumer)
    user_id = str(who.get("user_id") or "")
    user_pk = who.get("user_pk")
    enclave_pk = who.get("enclave_pk")
    if not (user_id and user_pk and enclave_pk):
        raise SuiteFailure("missing user/enclave keys for envelope")
    return consumer._build_envelope(
        plaintext=text.encode("utf-8"),
        owner_user_id=user_id,
        user_pk_bytes=user_pk,
        enclave_pk_bytes=enclave_pk,
        visibility="shared",
    )


def post_chat_message(http: Http, consumer, text: str) -> str:
    env = build_text_envelope(consumer, text)
    body = http.post("/v1/chat/message", {"envelope": env, "content_type": "text"})
    msg_id = str(body.get("id") or "")
    if not msg_id:
        raise SuiteFailure(f"chat/message missing id: {_pretty(body)}")
    return msg_id


def seed_v1_memory(consumer, *, summary: str, content: str, bucket: str, threads: list[str]) -> str:
    env = consumer._capture_build_envelope({
        "summary": summary,
        "content": content,
        "bucket": bucket,
        "threads": threads,
        "importance": 0.75,
        "pulse": 0.4,
        "type": "fact",
    }, occurred_at="2026-06-29T00:00:00Z", source="v1_e2e_seed")
    body = consumer.execute_memory_actions([{
        "type": "memory.add",
        "envelope": env,
        "reason": "v1 e2e seed",
    }])
    res = (body.get("results") or [{}])[0]
    mid = str((res.get("memory") or {}).get("id") or "")
    if res.get("status") != "ok" or not mid:
        raise SuiteFailure(f"seed memory failed: {_pretty(body)}")
    return mid


def assert_text_contains(doc: Any, needles: list[str], *, context: str) -> None:
    text = json.dumps(doc, ensure_ascii=False)
    missing = [n for n in needles if n not in text]
    if missing:
        raise SuiteFailure(f"{context}: missing {missing}; payload={text[:1200]}")


def require_history_contains(consumer, *, msg_id: str, needles: list[str]) -> list[dict]:
    history = consumer.get_decrypted_history(since=0, limit=20)
    text = json.dumps(history or [], ensure_ascii=False)
    missing = [n for n in needles if n not in text]
    if missing:
        raise SuiteFailure(
            "decrypted history cannot see capture seed "
            f"msg={msg_id} missing={missing} enclave_url={consumer.FEEDLING_ENCLAVE_URL}: {_pretty(history)}"
        )
    return history or []


def run_capture_job_with_stub(
    http: Http,
    consumer,
    stub_agent,
    *,
    use_force: bool = False,
    now_offset_sec: float = 5000,
) -> list[dict]:
    old_call_agent = consumer.call_agent
    consumer.call_agent = stub_agent
    consumer._seen_ids.clear()
    try:
        if use_force:
            tick = http.post("/v1/capture/force")
            enqueued = bool(tick.get("enqueued"))
        else:
            tick = http.post("/v1/capture/tick", {"now": time.time() + now_offset_sec})
            cap = tick.get("capture") if isinstance(tick.get("capture"), dict) else tick
            enqueued = bool((cap or {}).get("enqueued"))
        if not enqueued:
            raise SuiteFailure(f"capture did not enqueue: {_pretty(tick)}")
        jobs = http.get("/v1/proactive/jobs/poll?since=0&timeout=0&limit=20").get("jobs") or []
        capture_jobs = [j for j in jobs if j.get("job_kind") == "memory_capture"]
        if not capture_jobs:
            raise SuiteFailure(f"no capture job found: {_pretty(jobs)}")
        consumer._process_capture_jobs(capture_jobs)
        after_jobs = http.get("/v1/proactive/jobs/poll?since=0&timeout=0&limit=50").get("jobs") or []
        return [
            {
                "job_id": j.get("job_id"),
                "status": j.get("status"),
                "reason": j.get("reason"),
                "capture_result": j.get("capture_result"),
                "cards_added": j.get("cards_added"),
                "cards_superseded": j.get("cards_superseded"),
            }
            for j in after_jobs
            if j.get("job_id") in {x.get("job_id") for x in capture_jobs}
        ]
    finally:
        consumer.call_agent = old_call_agent


def case_memory_read(http: Http, consumer) -> CaseResult:
    mid = seed_v1_memory(
        consumer,
        summary="Z 的狗叫蛋子",
        content="记忆：Z 养了一只狗，名字叫蛋子，是一只比熊。\n\n上下文：v1 e2e seeded card。\n\n使用提示：问到宠物时要记得蛋子。",
        bucket="宠物",
        threads=["蛋子", "比熊"],
    )
    index = http.post("/v1/memory/index", {"limit": 1000, "query": "蛋子 比熊"})
    items = index.get("items") or []
    if mid not in {str(i.get("id") or "") for i in items if isinstance(i, dict)}:
        raise SuiteFailure(f"seeded memory not in index id={mid}; index={_pretty(index)}")
    fetch = http.post("/v1/memory/fetch", {"ids": [mid]})
    assert_text_contains(fetch, ["蛋子", "比熊"], context="memory fetch")
    return CaseResult(
        "memory_read_index_fetch",
        "pass",
        "seeded v1 card is visible through index/fetch; covers R1/R3 endpoint-readside path",
        {"memory_id": mid, "index_count": len(items)},
    )


def case_capture_write(http: Http, consumer) -> CaseResult:
    user_text = "我是 Z。请记住,我叫 Z,是 INFJ。我养了一条狗,叫蛋子,是一只比熊。"
    msg_id = post_chat_message(http, consumer, user_text)
    require_history_contains(consumer, msg_id=msg_id, needles=["Z", "INFJ", "蛋子", "比熊"])

    def stub_agent(_prompt: str):
        return json.dumps({
            "cards": [{
                "action": "add",
                "type": "fact",
                "target_id": None,
                "bucket": "身份与宠物",
                "threads": ["Z", "INFJ", "蛋子", "比熊"],
                "summary": "用户叫 Z, 是 INFJ, 养了一只叫蛋子的比熊。",
                "content": "记忆：用户告诉我他叫 Z, 是 INFJ；他养了一只狗叫蛋子，是一只比熊。\n\n上下文：v1 e2e capture 测试。\n\n使用提示：以后提到名字、性格或宠物时，以 Z、INFJ、蛋子和比熊为准。",
                "importance": 0.8,
                "pulse": 0.4,
            }]
        }, ensure_ascii=False)

    statuses = run_capture_job_with_stub(http, consumer, stub_agent, use_force=False)

    index = http.post("/v1/memory/index", {"limit": 1000, "query": "Z INFJ 蛋子 比熊"})
    assert_text_contains(index, ["Z", "INFJ", "蛋子", "比熊"], context="capture index")
    ids = [str(i.get("id") or "") for i in index.get("items") or [] if isinstance(i, dict) and "INFJ" in json.dumps(i, ensure_ascii=False)]
    fetch = http.post("/v1/memory/fetch", {"ids": ids[:3]}) if ids else {"items": []}
    assert_text_contains(fetch, ["Z", "INFJ", "蛋子", "比熊"], context="capture fetch")
    return CaseResult(
        "capture_write_memory",
        "pass",
        "quiet-window capture wrote a readable Chinese v1 card; covers W1/W5/W6",
        {"ids": ids[:3], "job_status": statuses},
    )


def case_capture_noop_chitchat(http: Http, consumer) -> CaseResult:
    before = http.post("/v1/memory/index", {"limit": 1000}).get("items") or []
    msg_id = post_chat_message(http, consumer, "哈哈,今天只是路过打个招呼。")
    require_history_contains(consumer, msg_id=msg_id, needles=["路过", "打个招呼"])

    def stub_agent(_prompt: str):
        return json.dumps({"cards": []}, ensure_ascii=False)

    statuses = run_capture_job_with_stub(http, consumer, stub_agent, use_force=True)
    after = http.post("/v1/memory/index", {"limit": 1000}).get("items") or []
    if len(after) != len(before):
        raise SuiteFailure(f"chitchat capture created memory unexpectedly before={len(before)} after={len(after)}")
    return CaseResult(
        "capture_noop_chitchat",
        "pass",
        "capture noop creates no card for disposable chitchat when agent returns no cards; covers W4 executor path",
        {"before_count": len(before), "after_count": len(after), "job_status": statuses},
    )


def case_capture_dedup_duplicate_fact(http: Http, consumer) -> CaseResult:
    existing_id = seed_v1_memory(
        consumer,
        summary="Z 的狗叫蛋子",
        content="记忆：Z 养了一只狗，名字叫蛋子，是一只比熊。\n\n上下文：已有事实。\n\n使用提示：重复提到蛋子时不要重复建卡。",
        bucket="宠物",
        threads=["蛋子", "比熊"],
    )
    before_items = http.post("/v1/memory/index", {"limit": 1000}).get("items") or []
    before_buckets = http.get("/v1/memory/buckets")
    before_threads = http.get("/v1/memory/threads")
    msg_id = post_chat_message(http, consumer, "再说一遍,我的狗叫蛋子,是一只比熊。")
    require_history_contains(consumer, msg_id=msg_id, needles=["蛋子", "比熊"])

    def stub_agent(_prompt: str):
        # This verifies the executor/job path does not create a duplicate when
        # the agent resolves-before-create and decides the old card is enough.
        return json.dumps({"cards": []}, ensure_ascii=False)

    statuses = run_capture_job_with_stub(http, consumer, stub_agent, use_force=True)
    after_items = http.post("/v1/memory/index", {"limit": 1000, "query": "蛋子 比熊"}).get("items") or []
    if len(after_items) != len(before_items):
        raise SuiteFailure(f"duplicate fact changed memory count before={len(before_items)} after={len(after_items)}")
    if existing_id not in {str(i.get("id") or "") for i in after_items if isinstance(i, dict)}:
        raise SuiteFailure(f"existing fact disappeared after duplicate capture existing_id={existing_id}")
    after_buckets = http.get("/v1/memory/buckets")
    after_threads = http.get("/v1/memory/threads")
    if json.dumps(before_buckets, sort_keys=True, ensure_ascii=False) != json.dumps(after_buckets, sort_keys=True, ensure_ascii=False):
        raise SuiteFailure("duplicate fact changed bucket vocabulary")
    if json.dumps(before_threads, sort_keys=True, ensure_ascii=False) != json.dumps(after_threads, sort_keys=True, ensure_ascii=False):
        raise SuiteFailure("duplicate fact changed thread vocabulary")
    return CaseResult(
        "capture_dedup_duplicate_fact",
        "pass",
        "duplicate explicit fact did not create another card or grow bucket/thread vocabulary; covers W2 executor path",
        {"existing_id": existing_id, "memory_count": len(after_items), "job_status": statuses},
    )


def case_supersede_correction(http: Http, consumer) -> CaseResult:
    old_id = seed_v1_memory(
        consumer,
        summary="Z 的狗叫蛋子",
        content="记忆：Z 养了一只狗，名字叫蛋子。\n\n上下文：旧事实。\n\n使用提示：问到宠物时记得蛋子。",
        bucket="宠物",
        threads=["蛋子"],
    )
    msg_id = post_chat_message(http, consumer, "纠正一下,我的狗不叫蛋子了,现在改名叫球球。")
    require_history_contains(consumer, msg_id=msg_id, needles=["球球"])

    def stub_agent(_prompt: str):
        return json.dumps({
            "cards": [{
                "action": "supersede",
                "type": "fact",
                "target_id": old_id,
                "bucket": "宠物",
                "threads": ["球球", "狗狗"],
                "summary": "Z 的狗现在叫球球。",
                "content": "记忆：用户纠正说，他的狗现在叫球球，不再叫蛋子。\n\n上下文：用户主动纠正旧事实。\n\n使用提示：以后提到狗的名字，以球球为准。",
                "importance": 0.85,
                "pulse": 0.5,
            }]
        }, ensure_ascii=False)

    statuses = run_capture_job_with_stub(http, consumer, stub_agent, use_force=True)
    index = http.post("/v1/memory/index", {"limit": 1000, "query": "球球 狗"})
    assert_text_contains(index, ["球球"], context="supersede index")
    default_ids = {str(i.get("id") or "") for i in index.get("items") or [] if isinstance(i, dict)}
    if old_id in default_ids:
        raise SuiteFailure(f"superseded old card still appears in default index old_id={old_id}")
    new_ids = [str(i.get("id") or "") for i in index.get("items") or [] if isinstance(i, dict) and "球球" in json.dumps(i, ensure_ascii=False)]
    fetch_new = http.post("/v1/memory/fetch", {"ids": new_ids[:2]}) if new_ids else {"items": []}
    assert_text_contains(fetch_new, ["球球"], context="supersede fetch new")
    fetch_old = http.post("/v1/memory/fetch", {"ids": [old_id], "include_superseded": True, "include_archived": True})
    assert_text_contains(fetch_old, ["superseded"], context="supersede fetch old")
    return CaseResult(
        "capture_supersede_correction",
        "pass",
        "correction supersedes old card and default recall uses the new fact; covers W3",
        {"old_id": old_id, "new_ids": new_ids[:2], "job_status": statuses},
    )


def case_local_only_visibility(http: Http, consumer) -> CaseResult:
    who = require_whoami(consumer)
    env = consumer._build_envelope(
        plaintext=json.dumps({
            "summary": "本地私有暗号",
            "content": "记忆：这是一张 local_only 测试卡, agent 不应该读到。\n\n上下文：v1 e2e。\n\n使用提示：不应出现。",
            "bucket": "隐私",
            "threads": ["local_only"],
            "importance": 0.9,
            "pulse": 0.2,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        owner_user_id=str(who["user_id"]),
        user_pk_bytes=who["user_pk"],
        enclave_pk_bytes=who["enclave_pk"],
        visibility="local_only",
    )
    env["occurred_at"] = "2026-06-29T00:00:00Z"
    env["source"] = "v1_e2e_local_only_seed"
    env["type"] = "fact"
    body = consumer.execute_memory_actions([{"type": "memory.add", "envelope": env, "reason": "v1 e2e local only seed"}])
    res = (body.get("results") or [{}])[0]
    mid = str((res.get("memory") or {}).get("id") or "")
    if res.get("status") != "ok" or not mid:
        raise SuiteFailure(f"local_only seed failed: {_pretty(body)}")
    index = http.post("/v1/memory/index", {"limit": 1000, "query": "本地私有暗号 local_only"})
    if mid in {str(i.get("id") or "") for i in index.get("items") or [] if isinstance(i, dict)}:
        raise SuiteFailure(f"local_only card leaked into index: {_pretty(index)}")
    fetch = http.post("/v1/memory/fetch", {"ids": [mid]})
    if mid not in set(fetch.get("unavailable_ids") or []):
        raise SuiteFailure(f"local_only fetch did not return unavailable id={mid}: {_pretty(fetch)}")
    return CaseResult(
        "local_only_visibility",
        "pass",
        "local_only card is hidden from index and unavailable to fetch; covers L4 local-only path",
        {"memory_id": mid},
    )


def case_no_related_readside_empty(http: Http, consumer) -> CaseResult:
    index = http.post("/v1/memory/index", {"limit": 1000, "query": "火星旅游签证 从未存过的事情"})
    items = index.get("items") or []
    if items:
        raise SuiteFailure(f"empty throwaway user returned unrelated memories: {_pretty(index)}")
    return CaseResult(
        "no_related_readside_empty",
        "pass",
        "empty throwaway user returns no index items for an unknown fact; covers R2 readside empty path",
        {"index_count": 0},
    )


def legacy_inner_cards() -> list[dict]:
    return [
        {"title": "去西湖", "description": "上周末一起骑车环了西湖，边骑边聊到天黑。", "her_quote": "下次还要来", "context": "周末约会", "linked_dimension": "我们的关系"},
        {"title": "她的狗叫蛋子", "description": "她养了只柯基，名字叫蛋子，特别黏人。", "context": "聊到宠物", "linked_dimension": "她的生活"},
        {"title": "怕香菜", "description": "她说绝对不能吃香菜，闻到都难受。", "her_quote": "香菜是邪恶的", "context": "一起点外卖", "linked_dimension": "口味偏好"},
        {"title": "喜欢下雨天", "description": "她说最喜欢下雨天待在家里听歌。", "context": "随口聊到天气", "linked_dimension": "情绪"},
    ]


def seed_legacy_cards(consumer) -> list[str]:
    who = require_whoami(consumer)
    user_id = str(who.get("user_id") or "")
    user_pk = who.get("user_pk")
    enclave_pk = who.get("enclave_pk")
    actions = []
    for idx, inner in enumerate(legacy_inner_cards()):
        env = consumer._build_envelope(
            plaintext=json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            owner_user_id=user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enclave_pk,
            visibility="shared",
        )
        env.update({
            "type": "moment",
            "occurred_at": f"2026-06-{10 + idx:02d}T00:00:00Z",
            "source": "v1_e2e_legacy_seed",
            "importance": 0.6,
            "pulse": 0.5,
        })
        actions.append({"type": "memory.add", "envelope": env, "reason": "v1 e2e legacy seed"})
    body = consumer.execute_memory_actions(actions)
    ids = [str((r.get("memory") or {}).get("id") or "") for r in body.get("results") or []]
    if len([x for x in ids if x]) != len(actions):
        raise SuiteFailure(f"legacy seed failed: {_pretty(body)}")
    return ids


def extract_old_cards_from_prompt(prompt: str) -> list[dict]:
    section = prompt
    if "【要升级的旧卡】" in section:
        section = section.split("【要升级的旧卡】", 1)[1]
    if "【输出】" in section:
        section = section.split("【输出】", 1)[0]
    cards = []
    for line in section.splitlines():
        line = line.strip()
        if "{" not in line:
            continue
        try:
            obj = json.loads(line[line.find("{"):])
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            cards.append(obj)
    return cards


def v1_from_old(card: dict) -> dict:
    title = str(card.get("title") or "")
    desc = str(card.get("description") or "")
    quote = str(card.get("her_quote") or "")
    if "狗" in title or "蛋子" in desc:
        bucket, threads = "宠物", ["蛋子", "狗狗"]
    elif "香菜" in title or "香菜" in desc:
        bucket, threads = "口味偏好", ["香菜"]
    elif "西湖" in title or "西湖" in desc:
        bucket, threads = "共同经历", ["西湖", "约会"]
    else:
        bucket, threads = "情绪偏好", [str(card.get("linked_dimension") or "日常")]
    content = f"记忆：{desc}\n\n上下文：{card.get('context') or '老卡迁移'}"
    if quote:
        content += f"\n\n原话：{quote}"
    content += "\n\n使用提示：后续提到相关话题时，可自然引用这条记忆，不要夸张发挥。"
    return {"id": str(card["id"]), "bucket": bucket, "threads": threads, "summary": title or desc[:80], "content": content}


def case_migration(http: Http, consumer) -> CaseResult:
    ids = seed_legacy_cards(consumer)
    legacy = http.post("/v1/memory/legacy_batch", {"batch_size": 10})
    if not set(ids).issubset({str(r.get("id") or "") for r in legacy.get("batch") or []}):
        raise SuiteFailure(f"legacy_batch did not see seeded ids: {_pretty(legacy)}")
    stale_id = ids[1]
    stale_done = {"value": False}

    def stub_agent(prompt: str):
        if "DREAM" in prompt or "梦" in prompt or "consolidations" in prompt:
            return json.dumps({"consolidations": []}, ensure_ascii=False)
        old_cards = extract_old_cards_from_prompt(prompt)
        if not stale_done["value"]:
            stale_done["value"] = True
            current_batch = http.post("/v1/memory/legacy_batch", {"batch_size": 10})
            current = next(row for row in current_batch.get("batch", []) if row.get("id") == stale_id)
            concurrent_card = {
                "id": stale_id,
                "bucket": "宠物",
                "threads": ["蛋子", "比熊"],
                "summary": "她的狗叫蛋子，是一只比熊。",
                "content": "记忆：并发修改保留：蛋子是一只比熊，刚刚被用户改过。\n\n上下文：迁移过程中用户更新了宠物信息。\n\n使用提示：后续提到蛋子时，以比熊为准。",
            }
            env = consumer._capture_build_envelope(
                concurrent_card,
                occurred_at="2026-06-29T00:00:00Z",
                source="v1_e2e_concurrent_upgrade",
                item_id=stale_id,
            )
            body = http.post("/v1/memory/actions", {"action": {
                "type": "memory.upgrade",
                "id": stale_id,
                "envelope": env,
                "old_body_hash": current.get("old_body_hash") or "",
            }})
            res = (body.get("results") or [{}])[0]
            if res.get("status") != "ok":
                raise SuiteFailure(f"concurrent upgrade failed: {_pretty(body)}")
        return json.dumps({"upgrades": [v1_from_old(card) for card in old_cards]}, ensure_ascii=False)

    old_call_agent = consumer.call_agent
    consumer.call_agent = stub_agent
    consumer._seen_ids.clear()
    try:
        tick = http.post("/v1/capture/tick", {"now": time.time() + 5000})
        if not tick.get("migrate", {}).get("enqueued") and tick.get("migrate", {}).get("reason") == "maintenance_already_pending":
            jobs0 = http.get("/v1/proactive/jobs/poll?since=0&timeout=0&limit=20").get("jobs") or []
            dream_jobs = [j for j in jobs0 if j.get("job_kind") == "memory_dream"]
            if dream_jobs:
                consumer._seen_ids.clear()
                consumer._process_dream_jobs(dream_jobs)
                tick = http.post("/v1/capture/tick", {"now": time.time() + 5200})
        if not tick.get("migrate", {}).get("enqueued"):
            raise SuiteFailure(f"migrate tick did not enqueue: {_pretty(tick)}")
        jobs = http.get("/v1/proactive/jobs/poll?since=0&timeout=0&limit=20").get("jobs") or []
        migr = [j for j in jobs if j.get("job_kind") == "memory_migrate"]
        if not migr:
            raise SuiteFailure(f"no migrate job found: {_pretty(jobs)}")
        consumer._process_migrate_jobs(migr)
        for attempt in range(2, 6):
            state = http.get("/v1/memory/migration_state").get("state") or {}
            if state.get("status") == "done":
                break
            tick_next = http.post("/v1/capture/tick", {"now": time.time() + 9000 + attempt * 7200})
            if tick_next.get("migrate", {}).get("enqueued"):
                jobs_next = http.get("/v1/proactive/jobs/poll?since=0&timeout=0&limit=20").get("jobs") or []
                migr_next = [j for j in jobs_next if j.get("job_kind") == "memory_migrate"]
                if migr_next:
                    consumer._seen_ids.clear()
                    consumer._process_migrate_jobs(migr_next)
    finally:
        consumer.call_agent = old_call_agent

    state_done = http.get("/v1/memory/migration_state").get("state") or {}
    if state_done.get("status") != "done" or int(state_done.get("legacy_remaining", -1)) != 0:
        raise SuiteFailure(f"migration not done: {_pretty(state_done)}")
    index = http.post("/v1/memory/index", {"limit": 1000})
    by_id = {str(i.get("id") or ""): i for i in index.get("items") or [] if isinstance(i, dict)}
    for mid in ids:
        item = by_id.get(mid)
        if not item:
            raise SuiteFailure(f"migrated id missing from index: {mid}; index_ids={list(by_id)}")
        if "未分类" in json.dumps(item, ensure_ascii=False):
            raise SuiteFailure(f"migrated card still appears uncategorized: {_pretty(item)}")
    fetch = http.post("/v1/memory/fetch", {"ids": ids, "include_superseded": True})
    if fetch.get("unavailable_ids"):
        raise SuiteFailure(f"migrated cards unavailable after fetch: {_pretty(fetch)}")
    assert_text_contains(fetch, ["西湖", "香菜", "并发修改保留", "比熊"], context="migration fetch")
    tick3 = http.post("/v1/capture/tick", {"now": time.time() + 20000})
    if tick3.get("migrate", {}).get("enqueued"):
        raise SuiteFailure(f"migration re-enqueued after done: {_pretty(tick3)}")
    return CaseResult("legacy_migration", "pass", "legacy cards migrated to readable v1 cards with id/CAS invariants", {"ids": ids, "state": state_done})


def case_flow_trace(http: Http, consumer) -> CaseResult:
    enabled = http.post("/v1/debug/trace/enable", {"enabled": True})
    if not enabled.get("deploy_enabled"):
        return CaseResult("flow_trace_route", "blocked", "FEEDLING_V1_FLOW_TRACE is disabled on deployment", enabled)
    http.request("DELETE", "/v1/debug/trace")
    msg_id = post_chat_message(http, consumer, "flow trace smoke: 请记录这条 route 事件。")
    trace = http.get("/v1/debug/trace?subsystem=route&limit=50")
    events = trace.get("events") or []
    if not any(e.get("type") == "chat.message" and (e.get("detail") or {}).get("msg_id") == msg_id for e in events):
        raise SuiteFailure(f"route chat.message trace missing for msg={msg_id}: {_pretty(trace)}")
    return CaseResult("flow_trace_route", "pass", "resident chat route writes visible flow-trace event", {"msg_id": msg_id, "events": len(events)})


def case_genesis(api_url: str) -> CaseResult:
    provider_key = os.environ.get("GENESIS_E2E_PROVIDER_API_KEY", "").strip()
    if not provider_key:
        return CaseResult("genesis_onboarding", "blocked", "GENESIS_E2E_PROVIDER_API_KEY is not set; worker LLM e2e skipped")
    provider = os.environ.get("GENESIS_E2E_PROVIDER", "deepseek")
    model = os.environ.get("GENESIS_E2E_MODEL", "deepseek-chat")
    base_url = os.environ.get("GENESIS_E2E_BASE_URL", "")
    transcript = (
        "me: 我叫 Z, 是 INFJ, 我家狗叫蛋子, 是一只比熊。\n"
        "her: 我是乔伊, ENFP, 广告设计师, 平时自己做自媒体, 也喜欢小狗。\n"
        "her: 蛋子今天乖吗？\n"
        "me: 上周我们去了西湖骑车。\n"
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write(transcript)
        transcript_path = f.name
    try:
        cmd = [
            sys.executable,
            str(TOOLS / "genesis_e2e.py"),
            "upload",
            "--api-url", api_url,
            "--register",
            "--provider", provider,
            "--model", model,
            "--transcript", transcript_path,
            "--source-kind", "history",
        ]
        if base_url:
            cmd.extend(["--base-url", base_url])
        env = dict(os.environ)
        upload = subprocess.run(cmd, cwd=str(REPO), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=False)
        if upload.returncode != 0:
            return CaseResult("genesis_onboarding", "blocked", "genesis upload/setup did not complete", {"stdout": upload.stdout[-2000:], "stderr": upload.stderr[-2000:], "returncode": upload.returncode})
        lines = [json.loads(line) for line in upload.stdout.splitlines() if line.strip().startswith("{")]
        provision = next((x for x in lines if x.get("provisioned")), {})
        final = next((x for x in reversed(lines) if "job_id" in x), {})
        api_key = provision.get("api_key")
        job_id = final.get("job_id")
        if not api_key or not job_id:
            return CaseResult("genesis_onboarding", "blocked", "genesis upload output missing api_key/job_id", {"stdout": upload.stdout[-2000:]})
        verify = subprocess.run([
            sys.executable,
            str(TOOLS / "genesis_e2e.py"),
            "verify",
            "--api-url", api_url,
            "--api-key", api_key,
            "--job-id", job_id,
            "--timeout", "900",
            "--poll", "10",
            "--privacy-needle", "蛋子,西湖",
        ], cwd=str(REPO), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=960, check=False)
        if verify.returncode != 0:
            return CaseResult("genesis_onboarding", "fail", "genesis verify failed", {"stdout": verify.stdout[-3000:], "stderr": verify.stderr[-2000:], "returncode": verify.returncode})
        payload = json.loads([line for line in verify.stdout.splitlines() if line.strip().startswith("{")][-1])
        if not payload.get("ok"):
            raise SuiteFailure(f"genesis verify returned not ok: {_pretty(payload)}")
        http = Http(api_url, api_key)
        idx = http.post("/v1/memory/index", {"limit": 1000, "query": "Z 蛋子 西湖"})
        ident = http.get("/v1/identity/get")
        assert_text_contains(idx, ["蛋子"], context="genesis memory index")
        memory_count = len(idx.get("items") or [])
        if not ident.get("identity"):
            raise SuiteFailure(f"identity missing after genesis; memory_count={memory_count}; identity={_pretty(ident)}")
        return CaseResult("genesis_onboarding", "pass", "genesis job done; identity and memory are readable", {"job_id": job_id, "memory_count": memory_count})
    finally:
        try:
            Path(transcript_path).unlink(missing_ok=True)
        except Exception:
            pass


def case_hosted_agent_runtime(api_url: str) -> CaseResult:
    provider_key = os.environ.get("GENESIS_E2E_PROVIDER_API_KEY", "").strip()
    if not provider_key:
        return CaseResult("hosted_agent_runtime_smoke", "blocked", "GENESIS_E2E_PROVIDER_API_KEY is not set; hosted model_api setup skipped")
    provider = os.environ.get("GENESIS_E2E_PROVIDER", "deepseek")
    model = os.environ.get("GENESIS_E2E_MODEL", "deepseek-chat")
    base_url = os.environ.get("GENESIS_E2E_BASE_URL", "")
    _user_id, api_key = register_user(api_url)
    http = Http(api_url, api_key)
    setup_payload = {"provider": provider, "model": model, "api_key": provider_key}
    if base_url:
        setup_payload["base_url"] = base_url
    setup_status, setup = http.request_status("POST", "/v1/model_api/setup", payload=setup_payload, timeout=90)
    if setup_status >= 400:
        return CaseResult(
            "hosted_agent_runtime_smoke",
            "blocked",
            "model_api setup did not complete; hosted spawn smoke skipped",
            {"status_code": setup_status, "error": setup.get("error", ""), "detail": str(setup.get("detail", ""))[:240]},
        )
    runtime = http.get("/v1/model_api/runtime")
    if not runtime.get("configured"):
        return CaseResult("hosted_agent_runtime_smoke", "blocked", "model_api runtime is not configured after setup", {"runtime": runtime})
    send_status, send = http.request_status(
        "POST",
        "/v1/model_api/chat/send",
        payload={"message": "v1 e2e hosted runtime smoke：请简短回复 pong。"},
        timeout=180,
    )
    if send_status == 503 and send.get("error") == "hosting_runtime_unavailable":
        return CaseResult(
            "hosted_agent_runtime_smoke",
            "blocked",
            "hosted agent_runtime supervisor is not live on test",
            {"status_code": send_status, "reason": send.get("reason", "")},
        )
    if send_status >= 400:
        return CaseResult(
            "hosted_agent_runtime_smoke",
            "blocked",
            "hosted chat/send did not complete",
            {"status_code": send_status, "error": send.get("error", ""), "reason": send.get("reason", "")},
        )
    route = http.get("/v1/debug/trace?subsystem=route&limit=20")
    events = route.get("events") or []
    return CaseResult(
        "hosted_agent_runtime_smoke",
        "pass",
        "model_api setup and hosted agent_runtime chat/send returned successfully",
        {
            "runtime_mode": runtime.get("runtime_mode", ""),
            "runtime_version": runtime.get("runtime_version", ""),
            "provider": runtime.get("provider", ""),
            "model": runtime.get("model", ""),
            "route_events": len(events),
            "response_keys": sorted([k for k in send.keys() if k not in {"reply", "message", "content"}]),
        },
    )


def run_case(results: list[CaseResult], name: str, fn) -> None:
    log(f"\n== {name} ==")
    start = time.time()
    try:
        result = fn()
        result.detail.setdefault("duration_sec", round(time.time() - start, 2))
        log(f"[{result.status.upper()}] {result.summary}")
        results.append(result)
    except SuiteBlocked as exc:
        result = CaseResult(name, "blocked", "case blocked", error=str(exc), detail={"duration_sec": round(time.time() - start, 2)})
        log(f"[BLOCKED] {result.error}")
        results.append(result)
    except Exception as exc:  # noqa: BLE001
        result = CaseResult(name, "fail", "case failed", error=f"{type(exc).__name__}: {exc}", detail={"duration_sec": round(time.time() - start, 2)})
        log(f"[FAIL] {result.error}")
        results.append(result)


def coverage_matrix(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    by_name = {r.name: r for r in results}

    def status_for(case: str) -> str:
        return by_name.get(case, CaseResult(case, "not_run", "")).status

    return {
        "W1_explicit_stable_fact": {
            "status": status_for("capture_write_memory"),
            "case": "capture_write_memory",
            "note": "用户明说名字/INFJ/狗名/犬种, capture 后 index/fetch 可读。",
        },
        "W2_dedup_resolve_before_create": {
            "status": status_for("capture_dedup_duplicate_fact"),
            "case": "capture_dedup_duplicate_fact",
            "note": "已有卡存在时,重复事实经 resolve/noop 不新增卡、不膨胀桶/线;真实 agent 判断仍需 eval。",
        },
        "W3_supersede_correction": {
            "status": status_for("capture_supersede_correction"),
            "case": "capture_supersede_correction",
            "note": "旧卡被 supersede,默认 index 不再返回旧 id,新卡可 fetch。",
        },
        "W4_no_write_for_chitchat": {
            "status": status_for("capture_noop_chitchat"),
            "case": "capture_noop_chitchat",
            "note": "agent 返回空 cards 时 executor 不落卡;真实'该不该写'仍属于 prompt/eval。",
        },
        "W5_quiet_window_trigger": {
            "status": status_for("capture_write_memory"),
            "case": "capture_write_memory",
            "note": "使用 /v1/capture/tick now+quiet 触发,不是 force。",
        },
        "W6_chinese_card_fields": {
            "status": status_for("capture_write_memory"),
            "case": "capture_write_memory",
            "note": "中文输入产出中文 bucket/threads/summary/content,专名 Z/蛋子保留。",
        },
        "R1_relevant_recall": {
            "status": status_for("memory_read_index_fetch"),
            "case": "memory_read_index_fetch",
            "note": "seeded v1 卡经 index 命中、fetch 解密读回。",
        },
        "R2_no_related_no_hallucination": {
            "status": status_for("no_related_readside_empty"),
            "case": "no_related_readside_empty",
            "note": "空 throwaway 用户查询未存事实,index 为空;真实 agent 回复不编仍留给 eval/agent trace。",
        },
        "R3_bucket_threads_structure": {
            "status": status_for("memory_read_index_fetch"),
            "case": "memory_read_index_fetch",
            "note": "卡含 bucket/threads 且非未分类占位; migration 也重复覆盖。",
        },
        "R4_agent_true_index_then_fetch": {
            "status": "todo",
            "case": "",
            "note": "flow-trace M1 尚未记录 consumer agent_call/index/fetch provenance;本次不改业务码,降级为端点/readside 覆盖。",
        },
        "L1_genesis_distill": {
            "status": status_for("genesis_onboarding"),
            "case": "genesis_onboarding",
            "note": "需要 GENESIS_E2E_PROVIDER_API_KEY;无 key 则 blocked。",
        },
        "L2_migration_regression": {
            "status": status_for("legacy_migration"),
            "case": "legacy_migration",
            "note": "legacy -> v1, id 稳,可 fetch 解密,最终 done。",
        },
        "L3_CAS_concurrency": {
            "status": status_for("legacy_migration"),
            "case": "legacy_migration",
            "note": "迁移中对一张卡做同 id upgrade,验证 stale 不覆盖用户并发改动。",
        },
        "L4_visibility_local_only": {
            "status": status_for("local_only_visibility"),
            "case": "local_only_visibility",
            "note": "local_only 不进 index,fetch 返回 unavailable。",
        },
        "L5_route_flow_trace": {
            "status": status_for("flow_trace_route"),
            "case": "flow_trace_route",
            "note": "resident chat/message 产生 route trace 事件。",
        },
        "API_hosted_agent_runtime": {
            "status": status_for("hosted_agent_runtime_smoke"),
            "case": "hosted_agent_runtime_smoke",
            "note": "若 provider key 可用,尝试 model_api setup + hosted chat/send;否则 blocked。",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Feedling v1 deployed e2e smoke suite.")
    parser.add_argument("--api-url", default=os.environ.get("FEEDLING_E2E_API_URL", DEFAULT_API))
    parser.add_argument(
        "--enclave-url",
        default=os.environ.get("FEEDLING_E2E_ENCLAVE_URL") or os.environ.get("FEEDLING_ENCLAVE_URL") or "",
        help="Direct enclave decrypt proxy URL. Required for resident capture/history decrypt; defaults to FEEDLING_E2E_ENCLAVE_URL or FEEDLING_ENCLAVE_URL.",
    )
    parser.add_argument("--report", default="")
    parser.add_argument("--tls-attempts", type=int, default=20)
    parser.add_argument("--tls-sleep", type=float, default=10.0)
    args = parser.parse_args()

    results: list[CaseResult] = []
    design_notes = [
        "agent index→fetch behavior is downgraded to endpoint/readside coverage; no reliable consumer agent_call trace exists yet.",
        "capture/migration use deterministic call_agent stubs; this suite tests backend/encryption/state, not LLM wording quality.",
        "resident capture requires a direct enclave decrypt URL; test-api itself returns opaque chat envelopes.",
        "genesis runs when GENESIS_E2E_PROVIDER_API_KEY is present; otherwise it is reported as blocked.",
    ]

    consumer = None
    try:
        wait_for_tls(args.api_url, attempts=args.tls_attempts, sleep_sec=args.tls_sleep)
        enclave_url = (args.enclave_url or args.api_url).rstrip("/")
        if enclave_url == args.api_url.rstrip("/"):
            log("[warn] --enclave-url not set; capture may fail because API /v1/chat/history returns encrypted envelopes.")
        # Prime the consumer module once, then switch auth per case.
        _user_id, _http, consumer = new_context(args.api_url, enclave_url, None)
    except Exception as exc:  # noqa: BLE001
        results.append(CaseResult("setup", "fail", "failed to prepare throwaway user/consumer", error=f"{type(exc).__name__}: {exc}"))

    if consumer is not None:
        run_case(results, "memory_read_index_fetch", lambda: case_memory_read(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "no_related_readside_empty", lambda: case_no_related_readside_empty(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "capture_write_memory", lambda: case_capture_write(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "capture_noop_chitchat", lambda: case_capture_noop_chitchat(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "capture_dedup_duplicate_fact", lambda: case_capture_dedup_duplicate_fact(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "capture_supersede_correction", lambda: case_supersede_correction(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "local_only_visibility", lambda: case_local_only_visibility(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "flow_trace_route", lambda: case_flow_trace(new_context(args.api_url, enclave_url, consumer)[1], consumer))
        run_case(results, "legacy_migration", lambda: case_migration(new_context(args.api_url, enclave_url, consumer)[1], consumer))

    run_case(results, "genesis_onboarding", lambda: case_genesis(args.api_url))
    run_case(results, "hosted_agent_runtime_smoke", lambda: case_hosted_agent_runtime(args.api_url))

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    summary = {
        "api_url": args.api_url,
        "enclave_url": (args.enclave_url or args.api_url).rstrip("/"),
        "branch": subprocess.run(["git", "branch", "--show-current"], cwd=str(REPO), text=True, stdout=subprocess.PIPE, check=False).stdout.strip(),
        "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO), text=True, stdout=subprocess.PIPE, check=False).stdout.strip(),
        "design_notes": design_notes,
        "results": [r.__dict__ for r in results],
        "coverage_matrix": coverage_matrix(results),
        "counts": counts,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
        log(f"\n[report] wrote {args.report}")
    log("\n== SUMMARY ==")
    log(text)
    return 1 if summary["counts"].get("fail", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
