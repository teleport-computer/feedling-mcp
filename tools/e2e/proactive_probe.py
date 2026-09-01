"""Deep Runtime V2 proactive qualification probes.

The public entry point deliberately returns evidence instead of raising.  A
semantic mismatch is a PRODUCT_FAIL and is never retried; polling is used only
while waiting for asynchronous evidence to become visible.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx


_RESULTS = {
    "PASS",
    "PRODUCT_FAIL",
    "BLOCKED_CREDENTIAL",
    "BLOCKED_DEPLOYMENT",
    "BLOCKED_EVIDENCE",
    "AGENT_ERROR",
    "SECURITY_FAIL",
}
_AGENT_ROLES = {"agent", "openclaw", "assistant"}
_MODEL_TIMEOUT = float(os.environ.get("FEEDLING_DEEP_MODEL_TIMEOUT", "240"))
_COLLISION_CLOCK_SKEW_SEC = 5.0
_COLLISION_SETTLE_SEC = 1.0


class _ProbeIssue(RuntimeError):
    def __init__(self, result: str, detail: str):
        if result not in _RESULTS:
            raise ValueError(f"unknown probe result: {result}")
        self.result = result
        self.detail = detail
        super().__init__(detail)


def _case(name: str, fn: Callable[[], str]) -> dict[str, str]:
    try:
        detail = fn()
    except _ProbeIssue as exc:
        return {"name": name, "result": exc.result, "detail": exc.detail[:1000]}
    except httpx.TransportError as exc:
        return {
            "name": name,
            "result": "BLOCKED_DEPLOYMENT",
            "detail": f"transport failure: {type(exc).__name__}",
        }
    except Exception as exc:  # noqa: BLE001 - one case must not abort the matrix
        return {
            "name": name,
            "result": "AGENT_ERROR",
            "detail": f"unexpected {type(exc).__name__}: {exc}"[:1000],
        }
    return {"name": name, "result": "PASS", "detail": str(detail)[:1000]}


def _body(response: httpx.Response, *, expected: tuple[int, ...], action: str) -> dict:
    if response.status_code in (401, 403):
        raise _ProbeIssue("BLOCKED_CREDENTIAL", f"{action}: HTTP {response.status_code}")
    if response.status_code in (404, 405, 501, 502, 503, 504):
        raise _ProbeIssue(
            "BLOCKED_DEPLOYMENT",
            f"{action}: HTTP {response.status_code} {response.text[:160]}",
        )
    if response.status_code not in expected:
        raise _ProbeIssue(
            "PRODUCT_FAIL",
            f"{action}: HTTP {response.status_code} {response.text[:160]}",
        )
    try:
        body = response.json()
    except Exception:
        raise _ProbeIssue("PRODUCT_FAIL", f"{action}: response is not JSON") from None
    if not isinstance(body, dict):
        raise _ProbeIssue("PRODUCT_FAIL", f"{action}: response is not an object")
    return body


def _history(c, since: float) -> list[dict]:
    response = c.get("/v1/chat/history", params={"since": max(0, since - 1), "limit": 200})
    body = _body(response, expected=(200,), action="chat history")
    rows = body.get("messages")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _ProbeIssue("PRODUCT_FAIL", "chat history messages is not a list of objects")
    return rows


def _message_ts(message: dict) -> float:
    try:
        return float(message.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _wake_job_id(job: dict) -> str:
    return str(job.get("id") or job.get("job_id") or "").strip()


def _wake_terminal_state(user: dict, job_id: str) -> tuple[str, str]:
    """Classify one exact V2 wake without treating silence as disappearance."""
    recent = user.get("v2_recent_jobs")
    activity = user.get("v2_wake_activity")
    if not isinstance(recent, dict) or recent.get("error"):
        return "unavailable", "v2_recent_jobs is unavailable"
    if not isinstance(activity, dict) or activity.get("error"):
        return "unavailable", "v2_wake_activity is unavailable"

    wanted = str(job_id)
    silences = [
        row for row in (activity.get("recent_silences") or [])
        if isinstance(row, dict) and str(row.get("job_id") or "") == wanted
    ]
    if silences:
        return "silent", str(silences[0].get("reason") or "explicit sleep")

    failures = [
        row for row in (activity.get("recent_failures") or [])
        if isinstance(row, dict) and str(row.get("job_id") or "") == wanted
    ]
    if failures:
        row = failures[0]
        return "failed", str(row.get("reason") or row.get("status") or "failed")

    jobs = [
        row for row in (recent.get("jobs") or [])
        if isinstance(row, dict) and str(row.get("job_id") or "") == wanted
    ]
    if not jobs:
        return "pending", "job not visible yet"
    status = str(jobs[0].get("status") or "").strip().lower()
    if status in {"failed", "expired"}:
        return "failed", status
    if status == "completed":
        return "completed_without_output", "completed without visible reply or explicit sleep"
    return "pending", status or "unknown"


def _wait_for_wake_delivery(
    c,
    since: float,
    job_id: str,
    *,
    action: str,
    timeout: float | None = None,
) -> dict:
    """Wait for a bubble while preserving terminal failure and legal sleep."""
    budget = _MODEL_TIMEOUT if timeout is None else max(0.0, float(timeout))
    deadline = time.time() + budget
    terminal_state = "pending"
    terminal_detail = "job not visible yet"
    while time.time() < deadline:
        rows = _history(c, since)
        candidates = [
            row for row in rows
            if str(row.get("role") or "") in _AGENT_ROLES and _message_ts(row) > since
        ]
        if candidates:
            return min(candidates, key=_message_ts)

        terminal_state, terminal_detail = _wake_terminal_state(_admin_user(c), job_id)
        if terminal_state == "silent":
            raise _ProbeIssue(
                "BLOCKED_EVIDENCE",
                f"{action}: wake job={job_id} legally slept ({terminal_detail})",
            )
        if terminal_state == "failed":
            raise _ProbeIssue(
                "PRODUCT_FAIL",
                f"{action}: wake job={job_id} failed ({terminal_detail})",
            )
        if terminal_state == "unavailable":
            raise _ProbeIssue("BLOCKED_EVIDENCE", f"{action}: {terminal_detail}")
        time.sleep(2)

    if terminal_state == "completed_without_output":
        raise _ProbeIssue(
            "PRODUCT_FAIL",
            f"{action}: wake job={job_id} {terminal_detail}",
        )
    raise _ProbeIssue(
        "AGENT_ERROR",
        f"{action}: wake job={job_id} produced no auditable outcome within {budget:.0f}s",
    )


def _collision_window_sec() -> float:
    raw = os.environ.get("PROACTIVE_CHAT_COLLISION_WINDOW_SEC", "90")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise _ProbeIssue("AGENT_ERROR", "invalid proactive chat collision window") from exc
    if not math.isfinite(value):
        raise _ProbeIssue("AGENT_ERROR", "invalid proactive chat collision window")
    return max(0.0, value)


def _wait_out_chat_collision(c, *, window: float | None = None) -> float:
    """Leave no recent user turn that can suppress a weak-wake reply."""
    guard = _collision_window_sec() if window is None else max(0.0, float(window))
    now = time.time()
    rows = _history(c, now - guard - _COLLISION_CLOCK_SKEW_SEC)
    latest_user = max(
        (_message_ts(row) for row in rows if str(row.get("role") or "") == "user"),
        default=0.0,
    )
    wait = max(
        0.0,
        latest_user + guard + _COLLISION_CLOCK_SKEW_SEC + _COLLISION_SETTLE_SEC - now,
    )
    if wait:
        time.sleep(wait)
    return wait


def _wait_for_proactive_reply(
    c,
    proactive_job_id: str,
    since: float,
    *,
    timeout: float | None = None,
) -> dict:
    """Wait for the exact legacy/resident proactive job's published reply."""
    budget = _MODEL_TIMEOUT if timeout is None else max(0.0, float(timeout))
    deadline = time.time() + budget
    while time.time() < deadline:
        rows = _history(c, since)
        replies = [
            row for row in rows
            if str(row.get("role") or "") in _AGENT_ROLES
            and str(row.get("proactive_job_id") or "") == proactive_job_id
        ]
        if replies:
            return min(replies, key=_message_ts)
        time.sleep(2)
    raise _ProbeIssue(
        "PRODUCT_FAIL",
        f"scheduled job={proactive_job_id} produced no correlated must-deliver reply "
        f"within {budget:.0f}s",
    )


def _wait_for_correlated_reply(c, user_message_id: str, since: float) -> tuple[dict, list[dict]]:
    deadline = time.time() + _MODEL_TIMEOUT
    while time.time() < deadline:
        rows = _history(c, since)
        user = next((row for row in rows if str(row.get("id") or "") == user_message_id), None)
        reply_id = str((user or {}).get("reply_message_id") or "")
        replies = [
            row
            for row in rows
            if str(row.get("role") or "") in _AGENT_ROLES
            and (
                (reply_id and str(row.get("id") or "") == reply_id)
                or str(row.get("reply_to_message_id") or "") == user_message_id
            )
        ]
        if replies:
            return min(replies, key=_message_ts), rows
        time.sleep(2)
    raise _ProbeIssue("AGENT_ERROR", f"no correlated reply within {_MODEL_TIMEOUT:.0f}s")


def _send_hosted(c, text: str) -> tuple[float, str]:
    response = c.post(
        "/v1/model_api/chat/send",
        json={"message": text, "client_msg_id": str(uuid.uuid4())},
    )
    body = _body(response, expected=(202,), action="hosted chat send")
    user = body.get("user_message")
    if not isinstance(user, dict) or not str(user.get("id") or ""):
        raise _ProbeIssue("PRODUCT_FAIL", "hosted chat send omitted user_message.id")
    try:
        sent_at = float(user.get("ts") or time.time())
    except (TypeError, ValueError):
        raise _ProbeIssue("PRODUCT_FAIL", "hosted chat send returned invalid timestamp") from None
    return sent_at, str(user["id"])


def _decrypt(c, message: dict, *, action: str) -> str:
    try:
        text = c.read_reply_strict(message)
    except Exception as exc:
        raise _ProbeIssue(
            "SECURITY_FAIL",
            f"{action}: reply envelope is not user-decryptable ({type(exc).__name__})",
        ) from None
    if not text.strip():
        raise _ProbeIssue("PRODUCT_FAIL", f"{action}: decrypted reply is empty")
    return text.strip()


def _save_settings(c, patch: dict) -> dict:
    response = c.post("/v1/proactive/settings", json=patch)
    return _body(response, expected=(200,), action="save proactive settings")


def _install_identity(c, identity: dict, *, action: str) -> None:
    payload = {
        "identity": identity,
        "days_with_user": 0,
        "relationship_anchor_evidence": "deep-e2e fresh synthetic account",
    }
    response = c.post(
        "/v1/identity/init",
        json=payload,
    )
    if response.status_code == 400:
        try:
            mismatch = response.json()
        except Exception:
            mismatch = {}
        computed_days = mismatch.get("computed_from_earliest_memory")
        if (
            mismatch.get("error") == "days_with_user_mismatch"
            and isinstance(computed_days, int)
            and computed_days >= 0
        ):
            payload["days_with_user"] = computed_days
            payload["relationship_anchor_evidence"] = (
                "server-confirmed earliest memory date "
                + str(mismatch.get("earliest_memory_date") or "unknown")
            )
            response = c.post("/v1/identity/init", json=payload)
    if response.status_code == 409:
        envelope = c._seal(json.dumps(identity, ensure_ascii=False, separators=(",", ":")))
        response = c.post(
            "/v1/identity/replace",
            json={"envelope": envelope, "audit": {"reason": action}},
        )
        _body(response, expected=(200,), action=f"replace {action} identity")
        return
    _body(response, expected=(201,), action=f"install {action} identity")


def _install_quality_identity(c) -> None:
    _install_identity(c, {
        "agent_name": "小栖",
        "self_introduction": "我是小栖，会留意真实处境，用简短而温和的中文陪伴七七。",
        "user_preferred_name": "七七",
        "agent_role": "长期陪伴者",
        "tone_style": "简体中文，克制、具体，不写模板式客服话术。",
        "custom_persona_prompt": (
            "主动开口时称呼用户七七，并自然包含短语‘此刻陪你’；"
            "用一到两句简体中文，提到当前采用上海时区，不提模型、供应商或系统。"
        ),
        "signature": ["此刻陪你"],
        "dimensions": [{"name": "温和", "value": 82, "description": "具体而不打扰"}],
    }, action="proactive quality")


def _admin_user(c) -> dict:
    token = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not token:
        raise _ProbeIssue("BLOCKED_CREDENTIAL", "FEEDLING_ADMIN_TOKEN is unavailable")
    response = httpx.get(
        f"{c.api_url}/v1/admin/data-track/users/{c.user_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        verify=False,
    )
    body = _body(response, expected=(200,), action="admin data-track user")
    user = body.get("user")
    if not isinstance(user, dict):
        raise _ProbeIssue("BLOCKED_EVIDENCE", "admin data-track omitted user object")
    return user


def _case_user_turn_priority(c) -> str:
    _install_quality_identity(c)
    _save_settings(c, {"timezone": "Asia/Shanghai", "ambient": True})
    collision_wait = _wait_out_chat_collision(c)

    control_started = time.time()
    control_wake = _body(
        c.post("/v1/proactive/tick", json={"force": True}),
        expected=(200,),
        action="enqueue priority control wake",
    )
    control_job = control_wake.get("job")
    control_job_id = _wake_job_id(control_job) if isinstance(control_job, dict) else ""
    if not control_job_id or control_job.get("lane") != "manual_wake":
        raise _ProbeIssue("PRODUCT_FAIL", f"priority control wake not admitted: {control_wake}")
    control_reply = _wait_for_wake_delivery(
        c,
        control_started,
        control_job_id,
        action="priority no-competition control",
    )
    _decrypt(c, control_reply, action="priority control reply")

    # History timestamps are server-owned; carry the completed control reply
    # forward so modest client/server clock skew cannot re-count it as the
    # competing wake in the treatment half.
    started = max(time.time(), _message_ts(control_reply))
    wake = _body(
        c.post("/v1/proactive/tick", json={"force": True}),
        expected=(200,),
        action="enqueue priority wake",
    )
    job = wake.get("job")
    job_id = _wake_job_id(job) if isinstance(job, dict) else ""
    if not job_id or job.get("lane") != "manual_wake":
        raise _ProbeIssue("PRODUCT_FAIL", f"manual wake not admitted to V2: {wake}")
    sent_at, user_id = _send_hosted(
        c,
        "我刚看到窗外天色变了，想和你聊两句。你现在会怎么回应我？",
    )
    reply, rows = _wait_for_correlated_reply(c, user_id, min(started, sent_at))
    _decrypt(c, reply, action="priority reply")
    reply_ts = _message_ts(reply)
    correlated_reply_ids = {
        str(row.get("reply_message_id") or "")
        for row in rows
        if str(row.get("role") or "") == "user" and str(row.get("reply_message_id") or "")
    }
    earlier_agent = [
        row
        for row in rows
        if str(row.get("role") or "") in _AGENT_ROLES
        and _message_ts(row) > started
        and _message_ts(row) < reply_ts
        and str(row.get("id") or "") != str(reply.get("id") or "")
        # A slow reply to an earlier user turn is not the competing wake. Exclude
        # every agent row that history explicitly correlates to a user message.
        and not str(row.get("reply_to_message_id") or "")
        and str(row.get("id") or "") not in correlated_reply_ids
    ]
    if earlier_agent:
        raise _ProbeIssue(
            "PRODUCT_FAIL",
            f"wake produced {len(earlier_agent)} agent message(s) before the user reply",
        )
    return (
        "no-competition wake delivered, then correlated user reply arrived before "
        f"any competing wake output; control_job={control_job_id}; wake_job={job_id}; "
        f"collision_wait={collision_wait:.1f}s"
    )


def _case_proactive_message_quality(c) -> str:
    _install_quality_identity(c)
    _save_settings(c, {"timezone": "Asia/Shanghai", "ambient": True})
    collision_wait = _wait_out_chat_collision(c)
    started = time.time()
    wake = _body(
        c.post("/v1/proactive/tick", json={"force": True}),
        expected=(200,),
        action="enqueue quality wake",
    )
    job = wake.get("job")
    job_id = _wake_job_id(job) if isinstance(job, dict) else ""
    if not job_id or job.get("lane") != "manual_wake":
        raise _ProbeIssue("PRODUCT_FAIL", f"quality wake was not enqueued: {wake}")
    reply = _wait_for_wake_delivery(
        c,
        started,
        job_id,
        action="proactive quality",
    )
    text = _decrypt(c, reply, action="proactive quality")
    required = [value for value in ("七七", "此刻陪你") if value not in text]
    if required:
        raise _ProbeIssue("PRODUCT_FAIL", f"persona/language markers missing: {required}; head={text[:160]!r}")
    if not any(marker in text for marker in ("上海", "北京时间", "东八区")):
        raise _ProbeIssue("PRODUCT_FAIL", f"timezone grounding missing; head={text[:160]!r}")
    forbidden = [
        marker for marker in ("Anthropic", "OpenAI", "OpenRouter", "Gemini", "DeepSeek", "系统提示")
        if marker.lower() in text.lower()
    ]
    if forbidden:
        raise _ProbeIssue("PRODUCT_FAIL", f"runtime/provider identity leaked: {forbidden}")
    time.sleep(6)
    rows = _history(c, started)
    agents = [row for row in rows if str(row.get("role") or "") in _AGENT_ROLES]
    if len(agents) != 1:
        raise _ProbeIssue("PRODUCT_FAIL", f"one wake produced {len(agents)} agent messages")
    return (
        f"decryptable zh-Hans persona/timezone message; job={job_id}; "
        f"chars={len(text)}; no spam; collision_wait={collision_wait:.1f}s"
    )


def _case_scheduled_must_deliver(c) -> str:
    _install_quality_identity(c)
    _save_settings(c, {"timezone": "UTC", "scheduled": True})
    due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    scheduled = _body(
        c.post("/v1/proactive/scheduled/actions", json={
            "actions": [{
                "type": "schedule_wake",
                "at": due,
                "tz": "UTC",
                "note": "这是定时必达探针：到时间后请发一条简短提醒。",
            }],
        }),
        expected=(200,),
        action="schedule must-deliver wake",
    )
    results = scheduled.get("results")
    row = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    timer_id = str(row.get("timer_id") or "")
    if row.get("status") != "scheduled" or not timer_id:
        raise _ProbeIssue("PRODUCT_FAIL", f"must-deliver wake was not scheduled: {scheduled}")

    started = time.time()
    fired = _body(
        c.post("/v1/proactive/scheduled/fire", json={}),
        expected=(200,),
        action="fire must-deliver wake",
    )
    fire_rows = fired.get("results") or []
    jobs = fired.get("jobs") or []
    fire_row = fire_rows[0] if fire_rows and isinstance(fire_rows[0], dict) else {}
    job = jobs[0] if len(jobs) == 1 and isinstance(jobs[0], dict) else {}
    proactive_job_id = str(job.get("job_id") or "")
    if (
        int(fired.get("queued") or 0) != 1
        or len(fire_rows) != 1
        or fire_row.get("status") != "fired"
        or str(fire_row.get("timer_id") or "") != timer_id
        or not proactive_job_id
    ):
        raise _ProbeIssue("PRODUCT_FAIL", f"must-deliver wake did not fire exactly once: {fired}")

    reply = _wait_for_proactive_reply(c, proactive_job_id, started)
    text = _decrypt(c, reply, action="scheduled must-deliver")
    return (
        f"scheduled must-deliver produced one correlated decryptable reply; "
        f"timer={timer_id}; job={proactive_job_id}; chars={len(text)}"
    )


def _case_wake_coalescing(c) -> str:
    _install_quality_identity(c)
    # Encourage a quick terminal sleep after the queue-level coalescing evidence.
    _save_settings(c, {
        "timezone": "Asia/Shanghai",
        "wake_directive": "For this synthetic queue probe, silently sleep on a manual wake.",
    })

    def post_one() -> dict:
        response = httpx.post(
            f"{c.api_url}/v1/proactive/tick",
            headers={"X-API-Key": c.api_key},
            json={"force": True},
            timeout=30,
            verify=False,
        )
        return _body(response, expected=(200,), action="coalescing wake")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _n: post_one(), range(2)))
    jobs = [item.get("job") for item in (first, second)]
    if any(not isinstance(job, dict) or job.get("lane") != "manual_wake" for job in jobs):
        raise _ProbeIssue("PRODUCT_FAIL", f"wake responses omitted manual_wake jobs: {[first, second]}")
    ids = {str(job.get("id") or "") for job in jobs}
    enqueued = [bool(item.get("enqueued")) for item in (first, second)]
    if len(ids) != 1 or enqueued.count(True) != 1 or enqueued.count(False) != 1:
        raise _ProbeIssue(
            "PRODUCT_FAIL",
            f"single-flight mismatch: ids={sorted(ids)} enqueued={enqueued}",
        )
    time.sleep(12)
    return f"two concurrent wakes coalesced to job={next(iter(ids))}"


def _case_stale_wake_expiry(c) -> str:
    _save_settings(c, {
        "permission_states": {"location": "authorized"},
        "arrival_wake_enabled": True,
    })
    before = _admin_user(c)
    before_pro = before.get("proactive") if isinstance(before.get("proactive"), dict) else {}
    before_expired = int((before_pro.get("jobs_by_status") or {}).get("expired") or 0)

    started = time.time()
    old_ts = started - 1200
    payload = json.dumps(
        {"values": {"place_label": "qa-old-anchor", "wifi_anchor_id": uuid.uuid4().hex}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response = c.post(
        "/v1/perception/report",
        json={
            "client_ts": old_ts,
            "context_snapshot": [{
                "key": "location_signal",
                "envelope": c._seal(payload),
                "changed": True,
            }],
        },
    )
    report = _body(response, expected=(200,), action="seed stale perception wake")
    if (report.get("results") or {}).get("location_signal") != "accepted":
        raise _ProbeIssue("PRODUCT_FAIL", f"stale perception seed was not accepted: {report}")
    poll = _body(
        c.get("/v1/proactive/jobs/poll", params={"since": 0, "timeout": 0, "limit": 100}),
        expected=(200,),
        action="expire stale wake through resident poll",
    )
    stale_returned = [
        job for job in (poll.get("jobs") or [])
        if isinstance(job, dict) and float(job.get("ts") or 0) <= old_ts + 5
    ]
    if stale_returned:
        raise _ProbeIssue("PRODUCT_FAIL", "a >900s stale wake remained pollable")
    current_wakes = [
        job for job in (poll.get("jobs") or [])
        if isinstance(job, dict)
        and float(job.get("ts") or 0) >= started - 5
        and "ios_report:location_signal" in (job.get("origin_refs") or [])
    ]
    for job in current_wakes:
        job_id = str(job.get("job_id") or "")
        if job_id:
            c.post(
                f"/v1/proactive/jobs/{job_id}/status",
                json={"status": "skipped", "reason": "deep_probe_cleanup"},
            )
    after = _admin_user(c)
    pro = after.get("proactive") if isinstance(after.get("proactive"), dict) else {}
    expired = int((pro.get("jobs_by_status") or {}).get("expired") or 0)
    if expired <= before_expired:
        if current_wakes:
            raise _ProbeIssue(
                "BLOCKED_EVIDENCE",
                "public perception ingress stamped the wake with server time despite an old client_ts; "
                "no public path can create a >900s pending wake",
            )
        raise _ProbeIssue(
            "BLOCKED_EVIDENCE",
            "old perception report was accepted but data-track exposed no new stale wake terminal evidence",
        )
    return f"stale wake hidden from poll; expired status count {before_expired}->{expired}"


def _case_dream_latest_only(c) -> str:
    envelope = c._seal(json.dumps({
        "summary": "Deep probe dream seed",
        "content": "Synthetic memory used only to exercise dream single-flight.",
        "bucket": "QA",
        "threads": ["release qualification"],
    }, separators=(",", ":")))
    envelope.update({
        "type": "fact",
        "source": "deep_probe",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "importance": 0.5,
        "pulse": 0.0,
    })
    _body(c.post("/v1/memory/add", json={"envelope": envelope}), expected=(201,), action="seed dream memory")
    first = _body(c.post("/v1/dream/tick", json={"force": True}), expected=(200,), action="first dream tick")
    second = _body(c.post("/v1/dream/tick", json={"force": True}), expected=(200,), action="second dream tick")
    if first.get("enqueued") is not True or (first.get("job") or {}).get("job_kind") != "memory_dream":
        raise _ProbeIssue("PRODUCT_FAIL", f"first forced dream did not enqueue: {first}")
    if second.get("enqueued") is not False or second.get("reason") != "dream_already_pending":
        raise _ProbeIssue("PRODUCT_FAIL", f"duplicate dream was not suppressed: {second}")
    poll = _body(
        c.get("/v1/proactive/jobs/poll", params={"since": 0, "timeout": 0, "limit": 100}),
        expected=(200,),
        action="poll dream jobs",
    )
    dreams = [
        job for job in (poll.get("jobs") or [])
        if isinstance(job, dict) and job.get("job_kind") == "memory_dream"
    ]
    if len(dreams) != 1:
        raise _ProbeIssue("PRODUCT_FAIL", f"expected one latest dream job, observed {len(dreams)}")
    return f"duplicate forced dream suppressed; one pollable job={dreams[0].get('job_id')}"


def _case_self_wake_min_lead(c) -> str:
    _save_settings(c, {"scheduled": True, "timezone": "UTC"})
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    scheduled = _body(
        c.post("/v1/proactive/scheduled/actions", json={
            "self_wake": True,
            "actions": [{"type": "schedule_wake", "at": past, "tz": "UTC", "note": "lead clamp probe"}],
        }),
        expected=(200,),
        action="schedule self wake",
    )
    results = scheduled.get("results")
    row = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    timer_id = str(row.get("timer_id") or "")
    if row.get("status") != "scheduled" or row.get("reason") != "self_wake_min_lead_clamped" or not timer_id:
        raise _ProbeIssue("PRODUCT_FAIL", f"self-wake was not clamped: {scheduled}")
    fired = _body(
        c.post("/v1/proactive/scheduled/fire", json={}),
        expected=(200,),
        action="fire clamped self wake",
    )
    if int(fired.get("queued") or 0) != 0 or fired.get("results") not in ([], None):
        raise _ProbeIssue("PRODUCT_FAIL", f"clamped self-wake fired immediately: {fired}")
    canceled = _body(
        c.post("/v1/proactive/scheduled/actions", json={
            "actions": [{"type": "cancel_wake", "wake_id": timer_id, "reason": "probe_cleanup"}],
        }),
        expected=(200,),
        action="cancel clamped self wake",
    )
    cancel_rows = canceled.get("results") or []
    if not cancel_rows or cancel_rows[0].get("status") != "canceled":
        raise _ProbeIssue("PRODUCT_FAIL", f"self-wake cleanup failed: {canceled}")
    return f"past self-wake clamped and remained not-due; timer={timer_id}"


def _blocked_case(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "result": "BLOCKED_EVIDENCE", "detail": detail}


def run_proactive_probe(c, cfg) -> dict:
    """Return the deep.py proactive result contract for one configured account."""
    cfg = cfg if isinstance(cfg, dict) else {}
    cases = [
        _case("proactive_message_quality", lambda: _case_proactive_message_quality(c)),
        _case("user_turn_priority", lambda: _case_user_turn_priority(c)),
        _case("scheduled_must_deliver", lambda: _case_scheduled_must_deliver(c)),
    ]
    if bool(cfg.get("run_invariants")):
        cases.extend([
            _case("wake_coalescing_window", lambda: _case_wake_coalescing(c)),
            _case("stale_wake_900s_expiry", lambda: _case_stale_wake_expiry(c)),
            _case("dream_migrate_latest_only", lambda: _case_dream_latest_only(c)),
            _blocked_case(
                "maintenance_soft_gap_backoff",
                "the user/admin surfaces expose legacy job aggregates but no controllable resident-consumer soft-gap clock",
            ),
            _case("self_wake_min_lead_clamp", lambda: _case_self_wake_min_lead(c)),
            _blocked_case(
                "introduction_fallback_grace",
                "model_api provisioning does not create a resident introduction fallback job and data-track has no injection seam",
            ),
        ])
    return {"area": "proactive", "cases": cases}
