"""Deep Runtime V2 perception qualification probes."""
from __future__ import annotations

import json
import time
import uuid

from .proactive_probe import (
    _ProbeIssue,
    _body,
    _case,
    _decrypt,
    _install_identity,
    _save_settings,
    _send_hosted,
    _wait_for_correlated_reply,
)


def _sealed_item(c, key: str, values: dict, *, changed: bool = False, message: str = "") -> dict:
    plaintext = json.dumps(
        {"values": values, "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "key": key,
        "envelope": c._seal(plaintext),
        "changed": changed,
        "message": message,
    }


def _report(c, items: list[dict], *, client_ts: float | None = None) -> dict:
    payload: dict = {"context_snapshot": items}
    if client_ts is not None:
        payload["client_ts"] = client_ts
    return _body(
        c.post("/v1/perception/report", json=payload),
        expected=(200,),
        action="perception report",
    )


def _signals(c, names: str) -> dict:
    body = _body(
        c.get("/v1/agent/perception", params={"signals": names}),
        expected=(200,),
        action=f"agent perception ({names})",
    )
    if body.get("ok") is not True or not isinstance(body.get("signals"), dict):
        raise _ProbeIssue("PRODUCT_FAIL", f"agent perception malformed: {body}")
    return body["signals"]


def _case_permission_honesty(c) -> str:
    settings = _save_settings(c, {
        "permission_states": {
            "motion": "off",
            "weather": "denied",
            "location": "not_permitted",
        }
    })
    saved = settings.get("permission_states") if isinstance(settings.get("permission_states"), dict) else {}
    if saved.get("motion") != "off" or saved.get("location") != "not_permitted":
        raise _ProbeIssue("PRODUCT_FAIL", f"permission states were not persisted: {saved}")
    signals = _signals(c, "motion,weather,location")
    expected = {
        "motion": "switch_off",
        "weather": "not_permitted",
        "location": "not_permitted",
    }
    for name, reason in expected.items():
        doc = signals.get(name)
        if not isinstance(doc, dict) or doc.get("disabled") is not True:
            raise _ProbeIssue("SECURITY_FAIL", f"disabled {name} leaked readable data: {doc}")
        if doc.get("reason") != reason:
            raise _ProbeIssue("PRODUCT_FAIL", f"disabled {name} reason={doc.get('reason')!r}, expected {reason!r}")
    return "HTTP 200 returned explicit disabled+reason for off/denied signals; no values leaked"


def _case_fast_slow_snapshot(c) -> str:
    _save_settings(c, {"permission_states": {
        "motion": "authorized",
        "weather": "authorized",
        "health": "authorized",
        "sleep": "authorized",
    }})
    now = time.time()
    report = _report(c, [
        {
            "key": "time",
            "data": json.dumps({
                "local_time": "2026-07-21T23:58:00+08:00",
                "timezone": "Asia/Shanghai",
                "locale": "zh-Hans",
            }),
        },
        {"key": "battery", "data": json.dumps({"level": "0.73", "charging": True})},
        _sealed_item(c, "motion_state", {"motion_state": "walking"}),
        _sealed_item(c, "weather", {"condition": "clear", "temperature": 27.5, "is_daylight": False}),
        _sealed_item(c, "health_vitals", {"step_count": 4321, "resting_heart_rate": 61}),
        _sealed_item(c, "health_sleep", {"asleep_minutes": 421, "deep_minutes": 88}),
    ], client_ts=now)
    results = report.get("results") if isinstance(report.get("results"), dict) else {}
    expected_keys = {"time", "battery", "motion_state", "weather", "health_vitals", "health_sleep"}
    bad = {key: results.get(key) for key in expected_keys if results.get(key) != "accepted"}
    if bad:
        raise _ProbeIssue("PRODUCT_FAIL", f"perception ingest rejected fields: {bad}")
    signals = _signals(c, "now,motion,weather,steps,sleep")
    checks = {
        "now.timezone": ((signals.get("now") or {}).get("timezone"), "Asia/Shanghai"),
        "motion.motion_state": ((signals.get("motion") or {}).get("motion_state"), "walking"),
        "weather.temperature": ((signals.get("weather") or {}).get("temperature"), 27.5),
        "steps.step_count": ((signals.get("steps") or {}).get("step_count"), 4321),
        "sleep.asleep_minutes": ((signals.get("sleep") or {}).get("asleep_minutes"), 421),
    }
    mismatches = {name: pair for name, pair in checks.items() if pair[0] != pair[1]}
    if mismatches:
        raise _ProbeIssue("PRODUCT_FAIL", f"fast/slow signal mismatch: {mismatches}")
    snapshot = _body(c.get("/v1/perception/snapshot"), expected=(200,), action="perception snapshot")
    if snapshot.get("timezone") != "Asia/Shanghai" or snapshot.get("motion_state") != "walking":
        raise _ProbeIssue("PRODUCT_FAIL", "fast flattened snapshot disagrees with agent perception projection")
    return "fast snapshot plus inline slow steps/sleep signals round-tripped through encrypted ingest"


def _case_timezone_boundary(c) -> str:
    timezone_name = "Pacific/Kiritimati"
    settings = _save_settings(c, {"timezone": timezone_name})
    if settings.get("timezone") != timezone_name:
        raise _ProbeIssue("PRODUCT_FAIL", f"proactive timezone not persisted: {settings.get('timezone')!r}")
    event = _body(
        c.post("/v1/device/events", json={
            "type": "app_presence",
            "payload": {
                "phase": "foreground",
                "timezone": timezone_name,
                "locale": "zh-Hans",
            },
        }),
        expected=(200,),
        action="device timezone event",
    )
    if event.get("type") != "app_presence":
        raise _ProbeIssue("PRODUCT_FAIL", f"device timezone event malformed: {event}")
    report = _report(c, [{
        "key": "time",
        "data": json.dumps({
            "local_time": "2026-07-22T00:01:00+14:00",
            "timezone": timezone_name,
            "locale": "zh-Hans",
        }),
    }])
    if (report.get("results") or {}).get("time") != "accepted":
        raise _ProbeIssue("PRODUCT_FAIL", f"boundary time report rejected: {report}")
    now = _signals(c, "now").get("now") or {}
    if now.get("timezone") != timezone_name or now.get("local_time") != "2026-07-22T00:01:00+14:00":
        raise _ProbeIssue("PRODUCT_FAIL", f"timezone boundary changed in readback: {now}")
    return "UTC+14 local-day boundary preserved in settings, device context, and agent perception"


def _install_grounding_identity(c) -> None:
    _install_identity(c, {
        "agent_name": "小栖",
        "self_introduction": "我是小栖，会先确认感知证据再开口。",
        "user_preferred_name": "七七",
        "tone_style": "简体中文，简短具体。",
        "custom_persona_prompt": (
            "每次主动醒来先调用 perception.location；若 place_label 可用，"
            "必须在发给用户的消息里逐字写出该 place_label，不能猜测或改写。"
        ),
        "language_preference": "zh-Hans",
        "dimensions": [{"name": "证据诚实", "value": 95}],
    }, action="perception grounding")


def _case_grounding(c) -> str:
    _install_grounding_identity(c)
    _save_settings(c, {
        "timezone": "Asia/Shanghai",
        "ambient": True,
        "permission_states": {"location": "authorized"},
    })
    label = "qa-place-" + uuid.uuid4().hex[:10]
    report = _report(c, [
        _sealed_item(c, "location_signal", {
            "place_label": label,
            "wifi_anchor_id": "anchor-" + uuid.uuid4().hex,
        }, changed=False),
    ])
    if (report.get("results") or {}).get("location_signal") != "accepted":
        raise _ProbeIssue("PRODUCT_FAIL", f"grounding signal rejected: {report}")
    location = _signals(c, "location").get("location") or {}
    if location.get("place_label") != label:
        raise _ProbeIssue("PRODUCT_FAIL", f"location readback lost injected label: {location}")
    sent_at, user_id = _send_hosted(
        c,
        "请调用 perception.location 读取当前地点，然后只回复 place_label 的原值，"
        "不要猜测、翻译或改写。",
    )
    reply, rows = _wait_for_correlated_reply(c, user_id, sent_at)
    text = _decrypt(c, reply, action="perception grounding")
    if label not in text:
        raise _ProbeIssue("PRODUCT_FAIL", f"model turn omitted exact injected place label; head={text[:180]!r}")
    correlated = [
        row for row in rows
        if str(row.get("reply_to_message_id") or "") == user_id
        or str(row.get("id") or "") == str(reply.get("id") or "")
    ]
    if len({str(row.get("id") or "") for row in correlated}) != 1:
        raise _ProbeIssue("PRODUCT_FAIL", "grounding turn had ambiguous correlated replies")
    return f"encrypted location -> explicit perception tool read -> decryptable text contained {label}"


def run_perception_probe(c, cfg) -> dict:
    """Return the deep.py perception result contract for one configured account."""
    cfg = cfg if isinstance(cfg, dict) else {}
    cases = []
    if bool(cfg.get("run_invariants")):
        cases.extend([
            _case("permission_honesty", lambda: _case_permission_honesty(c)),
            _case("fast_slow_signal_snapshot", lambda: _case_fast_slow_snapshot(c)),
            _case("timezone_boundary", lambda: _case_timezone_boundary(c)),
        ])
    cases.append(_case("perception_grounding", lambda: _case_grounding(c)))
    return {"area": "perception", "cases": cases}
