"""Proactive debug dashboard: snapshot + HTML renderer + zh translation cache."""

import html
import json
import os
import re
import threading
import time
from datetime import datetime

import httpx
from flask import request
from urllib.parse import quote, urlencode

from core import util

from core import store as core_store
from core.store import UserStore
from proactive import service

PROACTIVE_DEBUG_DECISION_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_DECISION_READ_MAX", 1000))
PROACTIVE_DEBUG_JOB_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_JOB_READ_MAX", core_store.PROACTIVE_JOB_MAX))
PROACTIVE_DEBUG_EVENT_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_EVENT_READ_MAX", 500))
PROACTIVE_DEBUG_REVIEW_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_REVIEW_READ_MAX", 500))
PROACTIVE_DEBUG_MESSAGE_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_MESSAGE_READ_MAX", 500))
PROACTIVE_DEBUG_FRAME_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_FRAME_READ_MAX", core_store.MAX_FRAMES))
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
PROACTIVE_DEBUG_TRANSLATION_MODEL = os.environ.get(
    "FEEDLING_PROACTIVE_DEBUG_TRANSLATION_MODEL",
    "google/gemini-3.1-flash-lite",
).strip()
PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC = float(
    os.environ.get("FEEDLING_PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC", "10")
)
_debug_translation_cache: dict[str, str] = {}
_debug_translation_lock = threading.Lock()

def _proactive_debug_snapshot(store: UserStore) -> dict:
    # The debug dashboard is used as an investigation surface, not a tiny
    # status widget. Read enough rows to cover a normal day of proactive
    # activity; the renderer still lets callers cap visible sections via
    # query params, but the backing snapshot should not hide history first.
    decisions = store.list_gate_decisions(limit=PROACTIVE_DEBUG_DECISION_READ_MAX)
    jobs = store.list_proactive_jobs(limit=PROACTIVE_DEBUG_JOB_READ_MAX)
    events = store.list_device_events(limit=PROACTIVE_DEBUG_EVENT_READ_MAX)
    reviews = store.list_gate_reviews(limit=PROACTIVE_DEBUG_REVIEW_READ_MAX)
    latest_review_by_decision: dict[str, dict] = {}
    for review in reviews:
        did = str(review.get("decision_id") or "")
        if did:
            latest_review_by_decision[did] = review
    with store.chat_lock:
        proactive_messages = [
            {
                "id": m.get("id"),
                "ts": m.get("ts"),
                "source": m.get("source"),
                "gate_decision_id": m.get("gate_decision_id", ""),
                "proactive_job_id": m.get("proactive_job_id", ""),
                "content_type": m.get("content_type", "text"),
                "alert_preview": m.get("alert_preview", ""),
                "push_body_preview": m.get("push_body_preview", ""),
                "push_live_activity_requested": bool(m.get("push_live_activity_requested")),
                "live_activity_status": m.get("live_activity_status", ""),
                "live_activity_reason": m.get("live_activity_reason", ""),
                "live_activity_activity_id": m.get("live_activity_activity_id", ""),
                "live_activity_mode": m.get("live_activity_mode", ""),
                "alert_status": m.get("alert_status", ""),
                "alert_reason": m.get("alert_reason", ""),
                "push_decision": m.get("push_decision", ""),
                "push_reason": m.get("push_reason", ""),
                "app_presence_phase": m.get("app_presence_phase", ""),
                "app_presence_age_sec": m.get("app_presence_age_sec", ""),
            }
            for m in store.chat_messages
            if m.get("source") == service.PROACTIVE_JOB_SOURCE or str(m.get("proactive_job_id") or "")
        ][-PROACTIVE_DEBUG_MESSAGE_READ_MAX:]
    messages_by_job = {
        str(m.get("proactive_job_id") or ""): m
        for m in proactive_messages
        if m.get("proactive_job_id")
    }
    enriched_jobs: list[dict] = []
    for job in jobs:
        row = dict(job)
        msg = messages_by_job.get(str(job.get("job_id") or ""))
        if msg:
            live_status = str(msg.get("live_activity_status") or "")
            alert_status = str(msg.get("alert_status") or "")
            if live_status == "delivered" and alert_status in {"", "delivered", "logged_only"}:
                row["derived_status"] = "delivered"
            elif live_status:
                row["derived_status"] = f"chat_written_live_activity_{live_status}"
            else:
                row["derived_status"] = "chat_written"
            row["chat_message_id"] = msg.get("id", "")
            row["chat_ts"] = msg.get("ts")
            row["alert_status"] = alert_status
            row["alert_reason"] = msg.get("alert_reason", "")
            row["live_activity_status"] = live_status
            row["live_activity_reason"] = msg.get("live_activity_reason", "")
            row["live_activity_mode"] = msg.get("live_activity_mode", "")
            row["push_decision"] = msg.get("push_decision", "")
            row["push_reason"] = msg.get("push_reason", "")
            row["preview"] = msg.get("alert_preview") or msg.get("push_body_preview") or ""
        else:
            row["derived_status"] = row.get("status", "pending")
            row.setdefault("preview", "")
        enriched_jobs.append(row)
    with store.frames_lock:
        frames = [
            {
                "id": f.get("id"),
                "ts": f.get("ts"),
                "app": f.get("app") or "unknown",
                "ocr_len": len((f.get("ocr_text") or "").strip()),
                "encrypted": bool(f.get("encrypted")),
            }
            for f in store.frames_meta[-PROACTIVE_DEBUG_FRAME_READ_MAX:]
        ]
    return {
        "user_id": store.user_id,
        "generated_at": datetime.now().isoformat(),
        "settings": store.load_proactive_settings(),
        "decisions": decisions,
        "reviews": reviews,
        "latest_review_by_decision": latest_review_by_decision,
        "jobs": enriched_jobs,
        "device_events": events,
        "proactive_messages": proactive_messages,
        "recent_frames": frames,
        "counts": {
            "decisions": len(decisions),
            "reviews": len(reviews),
            "jobs": len(jobs),
            "device_events": len(events),
            "proactive_messages": len(proactive_messages),
            "recent_frames": len(frames),
        },
    }


def _gate_input_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _gate_decision_has_frame_context(decision: dict) -> bool:
    try:
        schema_version = int(decision.get("schema_version") or 1)
    except (TypeError, ValueError):
        schema_version = 1
    if schema_version >= 2 or decision.get("decision_type") == "wake_event":
        return True
    frame_ids = decision.get("frame_ids")
    if isinstance(frame_ids, list) and any(str(fid).strip() for fid in frame_ids):
        return True
    gate_input = _gate_input_dict(decision.get("gate_input"))
    for key in ("sampled_frame_count", "image_count", "ocr_chars"):
        try:
            if int(gate_input.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return bool(gate_input.get("decrypt_ok"))


def _debug_translation_candidate(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 8:
        return False
    # Machine enum values are handled by the fixed label dictionary. The
    # model translator is only for prose fields such as reason/context_hint.
    if re.fullmatch(r"[a-zA-Z0-9_:\-./ ]+", raw) and len(raw.split()) <= 4:
        return False
    return bool(re.search(r"[A-Za-z]", raw))


def _translate_debug_texts_to_zh(texts: list[str]) -> dict[str, str]:
    """Best-effort display-only translation for the debug dashboard.

    This never mutates wake/job records. Raw English remains in JSON logs and
    folded payloads; translated strings are only used for HTML rendering when
    `lang=zh`.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for text in texts:
        raw = str(text or "").strip()
        if not raw or raw in seen or not _debug_translation_candidate(raw):
            continue
        seen.add(raw)
        unique.append(raw[:1800])

    if not unique:
        return {}

    with _debug_translation_lock:
        cached = {text: _debug_translation_cache[text] for text in unique if text in _debug_translation_cache}
    missing = [text for text in unique if text not in cached][:24]

    if missing and OPENROUTER_API_KEY and PROACTIVE_DEBUG_TRANSLATION_MODEL:
        try:
            payload = {
                "model": PROACTIVE_DEBUG_TRANSLATION_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Translate debug-dashboard prose from English to natural, concise Simplified Chinese "
                            "for a product debugging UI. "
                            "Preserve IDs, model names, JSON keys, product names, and technical terms like Wake, "
                            "context_hint, Live Activity, APNs, OCR. Do not preserve generic words like companion, "
                            "user, screen, response, or reason; translate them naturally. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "texts": missing,
                                "schema": {"translations": ["same length as texts"]},
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC) as client:
                resp = client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(util._strip_json_code_fence(content))
            translations = parsed.get("translations")
            if isinstance(translations, list):
                with _debug_translation_lock:
                    for raw, translated in zip(missing, translations):
                        out = str(translated or "").strip()
                        if out:
                            _debug_translation_cache[raw] = out[:1800]
                            cached[raw] = _debug_translation_cache[raw]
        except Exception as e:
            print(f"[proactive-debug] translation failed: {e}")

    return cached


def _render_proactive_dashboard(snapshot: dict) -> str:
    def esc(value) -> str:
        return html.escape(str(value if value is not None else ""))

    lang_param = str(request.args.get("lang") or "").strip().lower()
    if lang_param not in {"zh", "en"}:
        accept_lang = request.headers.get("Accept-Language", "").lower()
        lang_param = "zh" if "zh" in accept_lang else "en"
    is_zh = lang_param == "zh"

    def ui(en: str, zh: str) -> str:
        return zh if is_zh else en

    def lang_url(target: str) -> str:
        args = request.args.to_dict(flat=True)
        args["lang"] = target
        return f"/debug/proactive?{urlencode(args)}"

    def dashboard_url(**updates) -> str:
        args = request.args.to_dict(flat=True)
        for key, value in updates.items():
            if value is None:
                args.pop(key, None)
            else:
                args[key] = str(value)
        return f"/debug/proactive?{urlencode(args)}"

    def int_arg(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(str(request.args.get(name) or default).strip())
        except (TypeError, ValueError):
            value = default
        return max(lower, min(upper, value))

    decision_cap = int_arg("decision_limit", 80, 1, PROACTIVE_DEBUG_DECISION_READ_MAX)
    no_frame_cap = int_arg("no_frame_limit", 50, 1, PROACTIVE_DEBUG_DECISION_READ_MAX)
    job_cap = int_arg("job_limit", 100, 1, PROACTIVE_DEBUG_JOB_READ_MAX)
    table_cap = int_arg(
        "table_limit",
        100,
        1,
        max(
            PROACTIVE_DEBUG_EVENT_READ_MAX,
            PROACTIVE_DEBUG_MESSAGE_READ_MAX,
            PROACTIVE_DEBUG_FRAME_READ_MAX,
        ),
    )
    show_no_frame = str(request.args.get("show_no_frame") or "").strip().lower() in {"1", "true", "yes"}
    show_payloads = str(request.args.get("detail") or "1").strip().lower() not in {"0", "false", "no", "off"}

    debug_labels_zh = {
        "time": "时间",
        "ts": "时间戳",
        "model": "模型",
        "id": "判定 ID",
        "trigger": "触发来源",
        "wake_kind": "Wake 类型",
        "screen_context_available": "屏幕上下文",
        "user_state": "用户状态",
        "ai_state": "AI 状态",
        "broadcast_state": "屏幕共享",
        "agent_action": "Agent 动作",
        "wake_result": "Wake 结果",
        "intent": "意图",
        "abstention": "不触发原因",
        "reason": "判定理由",
        "context_hint": "上下文提示",
        "frames": "屏幕帧",
        "frames sent": "已发送帧",
        "connection": "关联依据",
        "gate_input": "Wake 输入",
        "payload": "事件数据",
        "consumer": "消费服务",
        "decision": "Wake",
        "job": "任务",
        "preview": "消息预览",
    }

    debug_value_labels_zh = {
        "TRUE": "触发",
        "FALSE": "不触发",
        "true": "触发",
        "false": "不触发",
        "pending": "等待处理",
        "claimed": "处理中",
        "completed": "已完成",
        "delivered": "已送达",
        "chat_written": "已写入聊天",
        "logged_only": "仅记录",
        "skipped": "已跳过",
        "failed": "失败",
        "error": "错误",
        "unreviewed": "未标注",
        "correct_true": "正确触发",
        "correct_false": "正确不触发",
        "missed_opportunity": "漏掉机会",
        "spam": "打扰/垃圾",
        "weak_connection": "关联太弱",
        "repeated": "重复触发",
        "privacy_bad": "隐私不合适",
        "great_companion_moment": "很好的陪伴时机",
        "blocked_before_model": "模型前拦截",
        "reviewable_false": "可复查的不触发",
        "manual_proactive_test": "手动主动触发测试",
        "research_pause": "研究停顿",
        "proactive_screen_context": "屏幕上下文",
        "manual_hint": "手动提示",
        "already_responded": "已经回应过",
        "shared_build_reflection": "共享构建反思",
        "no_recent_frames": "最近没有屏幕帧",
        "no_recent_frames_unit_test": "最近没有屏幕帧（测试）",
        "recent_proactive_fire": "10 分钟内已经主动触发过",
        "proactive_disabled": "主动触发已关闭",
        "dnd_enabled": "勿扰模式开启",
        "frame_decrypt_unavailable": "屏幕帧无法解密",
        "memory_context_unavailable": "记忆/身份上下文不可用",
        "model_not_configured": "Gate 模型未配置",
        "model_false": "模型判断不触发",
        "llm_false": "模型判断不触发",
        "llm_true": "模型判断触发",
        "screen": "屏幕",
        "presence": "Presence",
        "available": "可用",
        "none": "无",
        "llm_non_object": "模型返回不是 JSON 对象",
        "llm_missing_context_hint": "模型缺少上下文提示",
        "llm_missing_concrete_connection": "模型缺少具体关联",
        "llm_unrecognized_connection": "模型给出的关联无法验证",
        "invalid_gate_response": "Gate 返回无效",
        "has_connection": "存在具体关联",
        "model_detected_helpful_moment": "模型发现可帮助时机",
        "model_detected_memory_connection": "模型发现记忆关联",
        "agent_call_failed": "调用用户 Agent 失败",
    }

    def tr_label(value) -> str:
        raw = str(value or "")
        return debug_labels_zh.get(raw, raw) if is_zh else raw

    def tr_value(value) -> str:
        raw = str(value if value is not None else "")
        return debug_value_labels_zh.get(raw, raw) if is_zh else raw

    def value_html(value) -> str:
        raw = str(value if value is not None else "")
        translated = tr_value(raw)
        if translated != raw:
            return f"<span title='{esc(raw)}'>{esc(translated)}</span>"
        return esc(raw)

    def status_detail_html(status, reason) -> str:
        html = value_html(status)
        reason_text = str(reason or "").strip()
        if reason_text:
            html += f"<div class='mono mini'>{esc(reason_text[:180])}</div>"
        return html

    api_key = (request.args.get("key") or "").strip()
    key_qs = f"?key={quote(api_key)}" if api_key else ""
    settings = snapshot.get("settings") or {}
    dashboard_tz_name = str(
        request.args.get("tz")
        or settings.get("timezone")
        or core_store.PROACTIVE_DEFAULT_TIMEZONE
    ).strip() or "UTC"
    dashboard_tz = util._safe_zoneinfo(dashboard_tz_name)

    def fmt_time(ts_value) -> str:
        try:
            ts = float(ts_value or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts, dashboard_tz).strftime("%Y-%m-%d %H:%M:%S")

    def fmt_epoch(ts_value) -> str:
        try:
            return str(round(float(ts_value or 0), 3))
        except (TypeError, ValueError):
            return ""

    def frame_links(frame_ids) -> str:
        ids = [str(fid).strip() for fid in (frame_ids or []) if str(fid).strip()]
        if not ids:
            return ""
        links = []
        for fid in ids:
            safe = quote(fid)
            label = esc(fid[:10])
            links.append(
                f"<a class='mono' href='/v1/screen/frames/{safe}/image{key_qs}' target='_blank'>{label}</a>"
                f"<a class='mini' href='/v1/screen/frames/{safe}/decrypt{key_qs}{'&' if key_qs else '?'}include_image=false' target='_blank'>{esc(ui('json', '解密 JSON'))}</a>"
            )
        return " ".join(links)

    def status_class(value) -> str:
        text = str(value or "").lower()
        if text in {"true", "delivered", "chat_written", "logged_only"} or text.startswith("chat_written"):
            return "ok"
        if text in {"pending", "false", "skipped"}:
            return "muted"
        if "error" in text or "failed" in text:
            return "bad"
        return ""

    decisions = list(reversed(snapshot.get("decisions") or []))
    frame_decisions = [d for d in decisions if _gate_decision_has_frame_context(d)]
    no_frame_decisions = [d for d in decisions if not _gate_decision_has_frame_context(d)]
    latest_reviews = snapshot.get("latest_review_by_decision") or {}
    jobs = list(reversed(snapshot.get("jobs") or []))
    messages = list(reversed(snapshot.get("proactive_messages") or []))
    frames = list(reversed(snapshot.get("recent_frames") or []))
    events = list(reversed(snapshot.get("device_events") or []))
    translation_map: dict[str, str] = {}
    if is_zh:
        translation_candidates: list[str] = []
        translated_decisions = frame_decisions[:decision_cap]
        if show_no_frame:
            translated_decisions += no_frame_decisions[:no_frame_cap]
        for d in translated_decisions:
            translation_candidates.extend([
                str(d.get("reason") or ""),
                str(d.get("abstention_reason") or ""),
                str(d.get("context_hint") or ""),
            ])
        for j in jobs[:job_cap]:
            translation_candidates.extend([
                str(j.get("context_hint") or ""),
                str(j.get("status_reason") or ""),
                str(j.get("preview") or ""),
            ])
        for m in messages[:table_cap]:
            translation_candidates.append(
                str(m.get("alert_preview") or m.get("push_body_preview") or "")
            )
        translation_map = _translate_debug_texts_to_zh(translation_candidates)

    def prose_html(value) -> str:
        raw = str(value if value is not None else "").strip()
        if is_zh and raw in translation_map:
            return f"<span title='{esc(raw)}'>{esc(translation_map[raw])}</span>"
        return esc(raw)

    def prose_or_value_html(value) -> str:
        raw = str(value if value is not None else "")
        return prose_html(raw) if tr_value(raw) == raw else value_html(raw)

    def short_id(value, head: int = 8) -> str:
        """Truncate long IDs for display; full value shown on hover via title attr."""
        s = str(value or "").strip()
        if len(s) <= head + 2:
            return esc(s)
        return f"<span class='mono trunc' title='{esc(s)}'>{esc(s[:head])}…</span>"

    def fold_json(label: str, payload) -> str:
        """Collapse JSON payloads behind a <details> summary.

        Production debug pages can accumulate large wake inputs quickly. Keep
        the dashboard response bounded so browsers do not receive a truncated
        HTML document from the edge path.
        """
        try:
            pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(payload or "")
        if not pretty.strip() or pretty.strip() in ("{}", "null", "[]"):
            return f"<span class='muted mini'>{esc(tr_label(label))}: ∅</span>"
        max_chars = 500 if show_payloads else 180
        if len(pretty) > max_chars:
            pretty = pretty[:max_chars].rstrip() + "\n… truncated"
        return (
            f"<details class='inline-json'><summary>{esc(tr_label(label))}</summary>"
            f"<pre class='mono'>{esc(pretty)}</pre></details>"
        )

    def section_limit_link(
        param: str,
        current: int,
        total: int,
        step: int,
        label_en: str,
        label_zh: str,
        extra_updates: dict | None = None,
    ) -> str:
        if total <= current:
            return ""
        updates = dict(extra_updates or {})
        updates[param] = min(total, current + step)
        return f"<a class='control-link' href='{esc(dashboard_url(**updates))}'>{esc(ui(label_en, label_zh))}</a>"

    # Wake decisions are the heaviest rendering on this page (13 columns of
    # mixed text + JSON + form). Converted from a wide table to a stack of
    # cards: each decision is one block, fields laid out in a 2-column grid
    # that wraps to single-column on narrow viewports. JSON payloads
    # (connection, gate_input) collapse behind <details>. Same data, same
    # density, no horizontal scroll.
    def decision_card(d) -> str:
        verdict = ui("TRUE", "触发") if d.get("should_reach_out") else ui("FALSE", "不触发")
        verdict_cls = "ok" if d.get("should_reach_out") else "muted"
        gate_input = _gate_input_dict(d.get("gate_input"))
        connection = d.get("connection") or {}
        review = latest_reviews.get(str(d.get("decision_id") or "")) or {}
        decision_id = str(d.get("decision_id") or "")
        review_action = f"/v1/proactive/decisions/{quote(decision_id)}/review{key_qs}"
        frame_links_html = frame_links(d.get("frame_ids"))
        intent = d.get("intent_label") or ""
        abstention = d.get("abstention_reason") or ""
        context_hint = d.get("context_hint") or ""
        reason = d.get("reason") or ""

        meta_bits = [
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('time'))}</span> {esc(fmt_time(d.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('ts'))}</span> {esc(fmt_epoch(d.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('model'))}</span> {esc(d.get('gate_model'))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('id'))}</span> {short_id(decision_id)}</span>",
        ]
        if intent:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('intent'))}</span> {value_html(intent)}</span>")
        if d.get("trigger"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('trigger'))}</span> {value_html(d.get('trigger'))}</span>")
        if d.get("wake_kind"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('wake_kind'))}</span> {value_html(d.get('wake_kind'))}</span>")
        if "screen_context_available" in d:
            screen_context = "available" if d.get("screen_context_available") else "none"
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('screen_context_available'))}</span> {value_html(screen_context)}</span>")
        if d.get("user_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('user_state'))}</span> {value_html(d.get('user_state'))}</span>")
        if d.get("ai_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('ai_state'))}</span> {value_html(d.get('ai_state'))}</span>")
        if d.get("broadcast_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('broadcast_state'))}</span> {value_html(d.get('broadcast_state'))}</span>")
        if abstention:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('abstention'))}</span> {prose_or_value_html(abstention)}</span>")

        body_blocks = []
        if reason:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('reason'))}</span><div class='block-text'>{prose_or_value_html(reason)}</div></div>")
        if context_hint:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('context_hint'))}</span><div class='block-text'>{prose_html(context_hint)}</div></div>")
        if frame_links_html:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('frames'))}</span><div class='block-text'>{frame_links_html}</div></div>")
        if show_payloads:
            body_blocks.append(f"<div class='block'>{fold_json('connection', connection)}</div>")
            body_blocks.append(f"<div class='block'>{fold_json('gate_input', gate_input)}</div>")

        review_html = (
            f"<div class='review'>"
            f"<div class='mini'>{esc(ui('last review', '最近标注'))}: <span class='{ 'ok' if review.get('label') == 'correct_true' else '' }'>{value_html(review.get('label') or 'unreviewed')}</span></div>"
            f"<form method='post' action='{review_action}'>"
            "<select name='label'>"
            f"<option value='correct_true'>{esc(ui('correct true', '正确触发'))}</option>"
            f"<option value='correct_false'>{esc(ui('correct false', '正确不触发'))}</option>"
            f"<option value='missed_opportunity'>{esc(ui('missed opportunity', '漏掉机会'))}</option>"
            f"<option value='spam'>{esc(ui('spam', '打扰/垃圾'))}</option>"
            f"<option value='weak_connection'>{esc(ui('weak connection', '关联太弱'))}</option>"
            f"<option value='repeated'>{esc(ui('repeated', '重复触发'))}</option>"
            f"<option value='privacy_bad'>{esc(ui('privacy bad', '隐私不合适'))}</option>"
            f"<option value='great_companion_moment'>{esc(ui('great companion moment', '很好的陪伴时机'))}</option>"
            "</select>"
            f"<input name='notes' placeholder='{esc(ui('notes', '标注备注'))}' maxlength='300'>"
            f"<button type='submit'>{esc(ui('save', '保存'))}</button>"
            "</form>"
            "</div>"
        )

        return (
            f"<article class='card decision-card'>"
            f"  <header class='card-head'>"
            f"    <span class='verdict {verdict_cls}'>{verdict}</span>"
            f"    <div class='meta-bits'>{''.join(meta_bits)}</div>"
            f"  </header>"
            f"  <div class='card-body'>{''.join(body_blocks)}</div>"
            f"  {review_html}"
            f"</article>"
        )

    def decision_section(rows_source, empty_text: str, limit: int = decision_cap) -> str:
        if not rows_source:
            return f"<div class='empty'>{esc(empty_text)}</div>"
        return "<div class='card-list'>" + "".join(decision_card(d) for d in rows_source[:limit]) + "</div>"

    hidden_gate_details = ""
    if no_frame_decisions:
        if show_no_frame:
            hidden_body = decision_section(
                no_frame_decisions,
                ui("No hidden legacy no-frame ticks.", "没有隐藏的旧版无屏幕帧空 tick。"),
                limit=no_frame_cap,
            )
            more_no_frame = section_limit_link(
                "no_frame_limit",
                no_frame_cap,
                len(no_frame_decisions),
                50,
                "show more no-frame ticks",
                "显示更多空 tick",
                {"show_no_frame": 1},
            )
            hidden_hint = (
                f"<div class='hint'>{esc(ui('Showing no-frame ticks with a high cap; increase it if you need older scheduler history.', '正在显示无屏幕帧空 tick；需要更早的定时历史可以继续增加上限。'))} "
                f"{more_no_frame} "
                f"<a href='{esc(dashboard_url(show_no_frame=None))}'>{esc(ui('hide no-frame ticks', '隐藏空 tick 明细'))}</a></div>"
            )
        else:
            hidden_body = (
                f"<div class='empty'>{esc(ui('Legacy no-frame ticks are folded to keep this page lightweight.', '旧版无屏幕帧空 tick 已折叠，以保持页面轻量。'))} "
                f"<a href='{esc(dashboard_url(show_no_frame=1))}'>{esc(ui('show sample', '显示样本'))}</a></div>"
            )
            hidden_hint = ""
        hidden_gate_details = (
            "<details class='debug-details'>"
            f"<summary>{esc(ui(f'Show hidden legacy no-frame ticks ({len(no_frame_decisions)})', f'显示隐藏的旧版无屏幕帧空 tick（{len(no_frame_decisions)}）'))}</summary>"
            + hidden_hint
            + hidden_body
            + "</details>"
        )

    # Hidden Jobs — same card pattern as wake decisions. Fewer JSON blobs
    # so cards render lighter, but the wide horizontal table is the
    # bigger problem; cards solve it the same way.
    def job_card(j) -> str:
        status = j.get("derived_status") or j.get("status", "pending")
        intent = j.get("intent_label") or ""
        meta_bits = [
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('time'))}</span> {esc(fmt_time(j.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('ts'))}</span> {esc(fmt_epoch(j.get('ts')))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('job'))}</span> {short_id(j.get('job_id'))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('decision'))}</span> {short_id(j.get('gate_decision_id'))}</span>",
        ]
        if j.get("consumer_id"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('consumer'))}</span> {short_id(j.get('consumer_id'))}</span>")
        if intent:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('intent'))}</span> {value_html(intent)}</span>")
        if j.get("trigger"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('trigger'))}</span> {value_html(j.get('trigger'))}</span>")
        if j.get("wake_kind"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('wake_kind'))}</span> {value_html(j.get('wake_kind'))}</span>")
        if "screen_context_available" in j:
            screen_context = "available" if j.get("screen_context_available") else "none"
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('screen_context_available'))}</span> {value_html(screen_context)}</span>")
        if j.get("user_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('user_state'))}</span> {value_html(j.get('user_state'))}</span>")
        if j.get("ai_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('ai_state'))}</span> {value_html(j.get('ai_state'))}</span>")
        if j.get("broadcast_state"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('broadcast_state'))}</span> {value_html(j.get('broadcast_state'))}</span>")
        if j.get("agent_action"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('agent_action'))}</span> {value_html(j.get('agent_action'))}</span>")
        if j.get("wake_result"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('wake_result'))}</span> {value_html(j.get('wake_result'))}</span>")

        body_blocks = []
        if j.get("context_hint"):
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('context_hint'))}</span><div class='block-text'>{prose_html(j.get('context_hint'))}</div></div>")
        if j.get("status_reason"):
            status_reason = j.get("status_reason")
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('reason'))}</span><div class='block-text'>{prose_or_value_html(status_reason)}</div></div>")
        if j.get("preview"):
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('preview'))}</span><div class='block-text'>{prose_html(j.get('preview'))}</div></div>")
        frames_sent = frame_links(j.get("frame_ids"))
        if frames_sent:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('frames sent'))}</span><div class='block-text'>{frames_sent}</div></div>")
        if show_payloads and j.get("agent_actions"):
            body_blocks.append(f"<div class='block'>{fold_json('agent_actions', j.get('agent_actions'))}</div>")
        if show_payloads and j.get("request_broadcast"):
            body_blocks.append(f"<div class='block'>{fold_json('request_broadcast', j.get('request_broadcast'))}</div>")

        status_pill = f"<span class='verdict {status_class(status)}'>{value_html(status)}</span>"
        chips = []
        if j.get("alert_status"):
            chips.append(
                f"<span class='chip {status_class(j.get('alert_status'))}'>{esc(ui('alert', '通知'))}: "
                f"{status_detail_html(j.get('alert_status'), j.get('alert_reason'))}</span>"
            )
        if j.get("live_activity_status"):
            live_detail = status_detail_html(j.get("live_activity_status"), j.get("live_activity_reason"))
            if j.get("live_activity_mode"):
                live_detail += f" <span class='mono mini'>{esc(j.get('live_activity_mode'))}</span>"
            chips.append(
                f"<span class='chip {status_class(j.get('live_activity_status'))}'>Live Activity: "
                f"{live_detail}</span>"
            )

        chips_html = f"<div class='chip-row'>{''.join(chips)}</div>" if chips else ""

        return (
            f"<article class='card'>"
            f"  <header class='card-head'>{status_pill}<div class='meta-bits'>{''.join(meta_bits)}</div></header>"
            f"  {chips_html}"
            f"  <div class='card-body'>{''.join(body_blocks)}</div>"
            f"</article>"
        )

    def job_section() -> str:
        if not jobs:
            return f"<div class='empty'>{esc(ui('No hidden proactive jobs yet.', '还没有隐藏主动任务。'))}</div>"
        return "<div class='card-list'>" + "".join(job_card(j) for j in jobs[:job_cap]) + "</div>"

    # The remaining three sections (chat writes / frames / events) have
    # fewer columns; tables still fit a reasonable max-width with
    # `table-layout: fixed` + the new `.table-scroll` wrapper for the
    # rare overflow case. No card conversion needed.
    def message_rows() -> str:
        if not messages:
            return f"<tr><td colspan='8'>{esc(ui('No proactive chat writes yet.', '还没有主动消息写入。'))}</td></tr>"
        rows = []
        for m in messages[:table_cap]:
            preview = m.get("alert_preview") or m.get("push_body_preview") or ui("(encrypted envelope; no plaintext preview recorded)", "（加密 envelope；没有记录明文预览）")
            live_detail = status_detail_html(m.get("live_activity_status"), m.get("live_activity_reason"))
            if m.get("live_activity_mode"):
                live_detail += f"<div class='mono mini'>{esc(m.get('live_activity_mode'))}</div>"
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(m.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(m.get('ts')))}</div></td>"
                f"<td>{esc(m.get('content_type'))}</td>"
                f"<td class='wrap'>{prose_html(preview)}</td>"
                f"<td class='{status_class(m.get('alert_status'))}'>{status_detail_html(m.get('alert_status'), m.get('alert_reason'))}</td>"
                f"<td class='{status_class(m.get('live_activity_status'))}'>{live_detail}</td>"
                f"<td>{short_id(m.get('gate_decision_id'))}</td>"
                f"<td>{short_id(m.get('proactive_job_id'))}</td>"
                f"<td>{short_id(m.get('id'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    def frame_rows() -> str:
        if not frames:
            return f"<tr><td colspan='5'>{esc(ui('No frames indexed.', '还没有索引到屏幕帧。'))}</td></tr>"
        rows = []
        for f in frames[:table_cap]:
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(f.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(f.get('ts')))}</div></td>"
                f"<td>{esc(f.get('app'))}</td>"
                f"<td>{esc(f.get('ocr_len'))}</td>"
                f"<td>{esc(f.get('encrypted'))}</td>"
                f"<td>{frame_links([f.get('id')])}</td>"
                "</tr>"
            )
        return "".join(rows)

    def event_rows() -> str:
        if not events:
            return f"<tr><td colspan='4'>{esc(ui('No device events yet.', '还没有设备事件。'))}</td></tr>"
        rows = []
        for e in events[:table_cap]:
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(e.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(e.get('ts')))}</div></td>"
                f"<td>{esc(e.get('source'))}</td>"
                f"<td>{esc(e.get('type'))}</td>"
                f"<td>{fold_json('payload', e.get('payload') or {}) if show_payloads else short_id((e.get('payload') or {}).get('id') or e.get('id') or e.get('type'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    counts = snapshot.get("counts") or {}
    visible_wake_count = len(frame_decisions)
    hidden_no_frame_count = len(no_frame_decisions)
    page_title = "IO Proactive Harness"
    visible_empty_text = ui(
        f"No visible wake decisions yet. Hidden legacy no-frame ticks: {hidden_no_frame_count}.",
        f"还没有可见的 Wake 判定。隐藏旧版空 tick：{hidden_no_frame_count}。",
    )
    detail_toggle = (
        f"<a class='control-link' href='{esc(dashboard_url(detail=0))}'>{esc(ui('hide JSON detail', '隐藏 JSON 详情'))}</a>"
        if show_payloads
        else f"<a class='control-link' href='{esc(dashboard_url(detail=1))}'>{esc(ui('show JSON detail', '显示 JSON 详情'))}</a>"
    )
    control_links = " ".join(
        link for link in [
            detail_toggle,
            section_limit_link(
                "decision_limit",
                decision_cap,
                len(frame_decisions),
                80,
                "show more wake decisions",
                "显示更多 Wake",
            ),
            section_limit_link(
                "job_limit",
                job_cap,
                len(jobs),
                100,
                "show more completed jobs",
                "显示更多已完成任务",
            ),
            section_limit_link(
                "table_limit",
                table_cap,
                max(len(messages), len(frames), len(events)),
                100,
                "show more tables",
                "显示更多表格记录",
            ),
            (
                f"<a class='control-link' href='{esc(dashboard_url(show_no_frame=1))}'>{esc(ui('show no-frame ticks', '显示空 tick'))}</a>"
                if no_frame_decisions and not show_no_frame
                else ""
            ),
        ]
        if link
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(page_title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0 auto;
      padding: 24px;
      max-width: 1240px;
      color: #1f1d1a;
      background: #f6f0e6;
      line-height: 1.4;
    }}
    h1 {{ margin: 0 0 4px; }}
    .topbar {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .lang-switch {{
      display: inline-flex;
      gap: 4px;
      border: 1px solid #c9bfb2;
      background: #fffaf1;
      padding: 3px;
      border-radius: 2px;
      flex: 0 0 auto;
    }}
    .lang-switch a {{
      display: inline-block;
      padding: 4px 8px;
      border: 0;
      color: #6f6961;
      font-size: 12px;
    }}
    .lang-switch a.active {{
      background: #8e301f;
      color: #fffaf1;
    }}
    @media (max-width: 640px) {{
      .topbar {{ flex-direction: column; }}
    }}
    h2 {{
      margin-top: 32px;
      border-top: 1px solid #d8d0c4;
      padding-top: 18px;
      font-size: 18px;
    }}
    .meta {{ color: #6f6961; margin-bottom: 16px; font-size: 13px; }}
    .pill {{
      display: inline-block;
      border: 1px solid #c9bfb2;
      padding: 4px 8px;
      margin: 2px;
      background: #fffaf1;
      font-size: 12px;
      border-radius: 2px;
    }}
    .hint {{ margin: 8px 0 16px; color: #6f6961; font-size: 13px; }}
    .control-link {{ display: inline-block; margin-left: 8px; white-space: nowrap; }}
    .empty {{
      padding: 16px;
      background: #fffaf1;
      border: 1px solid #ddd2c5;
      color: #6f6961;
      font-style: italic;
    }}

    /* ---- Card layout (Wake Decisions + Hidden Jobs) ---- */
    .card-list {{ display: flex; flex-direction: column; gap: 12px; }}
    .card {{
      background: #fffaf1;
      border: 1px solid #ddd2c5;
      padding: 14px 16px;
      border-radius: 2px;
    }}
    .card-head {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
      padding-bottom: 10px;
      border-bottom: 1px solid #eee5d5;
    }}
    .verdict {{
      display: inline-block;
      padding: 4px 10px;
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.5px;
      background: #efe5d7;
      border-radius: 2px;
      white-space: nowrap;
    }}
    .verdict.ok    {{ background: #d4ead8; color: #0b7d42; }}
    .verdict.muted {{ background: #ebe4d4; color: #8b8176; }}
    .verdict.bad   {{ background: #f5d8d4; color: #b42318; }}
    .meta-bits {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 18px;
      flex: 1;
      align-items: baseline;
      font-size: 12px;
    }}
    .meta-bit {{ color: #1f1d1a; }}
    .meta-bit .label {{
      color: #8b8176;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      margin-right: 4px;
    }}
    .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
    .chip {{
      display: inline-block;
      padding: 2px 8px;
      background: #efe5d7;
      font-size: 11px;
      border-radius: 2px;
    }}
    .chip.ok    {{ background: #d4ead8; color: #0b7d42; }}
    .chip.muted {{ background: #ebe4d4; color: #8b8176; }}
    .chip.bad   {{ background: #f5d8d4; color: #b42318; }}
    .card-body {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 24px;
    }}
    @media (max-width: 720px) {{
      .card-body {{ grid-template-columns: 1fr; }}
    }}
    .block {{ min-width: 0; }}
    .block-label {{
      display: block;
      color: #8b8176;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      margin-bottom: 3px;
    }}
    .block-text {{
      font-size: 13px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}
    .review {{
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px solid #eee5d5;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      font-size: 12px;
    }}
    .review .mini {{ margin-right: 6px; color: #6f6961; }}
    .review form {{ display: inline-flex; gap: 6px; flex-wrap: wrap; }}
    .review select, .review input, .review button {{ font: inherit; font-size: 12px; }}
    .review input {{ width: 160px; }}

    /* ---- Inline JSON disclosure (used inside cards + small tables) ---- */
    details.inline-json {{ display: block; }}
    details.inline-json > summary {{
      cursor: pointer;
      color: #8e301f;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      list-style: revert;
    }}
    details.inline-json[open] > summary {{ margin-bottom: 4px; }}
    details.inline-json pre {{
      margin: 0;
      padding: 8px;
      background: #f0e8d8;
      overflow-x: auto;
      max-height: 280px;
      font-size: 11px;
      border-radius: 2px;
    }}

    /* ---- Tables (Chat Writes / Frames / Events) ---- */
    .table-scroll {{
      max-width: 100%;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      border: 1px solid #ddd2c5;
      background: #fffaf1;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #eee5d5;
      padding: 8px 10px;
      vertical-align: top;
      text-align: left;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #efe5d7;
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #5a544b;
    }}
    tr:last-child td {{ border-bottom: none; }}
    .wrap {{ white-space: pre-wrap; }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11.5px;
    }}
    .mini {{ font-size: 10.5px; color: #8b8176; }}
    .trunc {{ cursor: help; border-bottom: 1px dotted #b8a895; }}

    /* Per-table column widths */
    .t-messages col.c-time   {{ width: 13%; }}
    .t-messages col.c-type   {{ width: 8%; }}
    .t-messages col.c-prev   {{ width: 33%; }}
    .t-messages col.c-status {{ width: 9%; }}
    .t-messages col.c-id     {{ width: 9%; }}
    .t-frames   col.c-time   {{ width: 18%; }}
    .t-frames   col.c-app    {{ width: 22%; }}
    .t-frames   col.c-num    {{ width: 12%; }}
    .t-frames   col.c-link   {{ width: 32%; }}
    .t-events   col.c-time   {{ width: 16%; }}
    .t-events   col.c-source {{ width: 14%; }}
    .t-events   col.c-type   {{ width: 18%; }}
    .t-events   col.c-payload{{ width: 52%; }}

    a {{ color: #8e301f; text-decoration: none; border-bottom: 1px solid #d0a094; }}
    a.mini {{ margin-left: 6px; font-size: 11px; color: #6f6961; }}
    .ok    {{ color: #0b7d42; font-weight: 600; }}
    .muted {{ color: #8b8176; }}
    .bad   {{ color: #b42318; font-weight: 600; }}
    details.debug-details {{ margin-top: 14px; }}
    details.debug-details > summary {{ cursor: pointer; color: #8e301f; margin-bottom: 8px; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>{esc(page_title)}</h1>
      <div class="meta">{esc(ui('user', '用户'))} <span class="mono">{esc(snapshot.get('user_id'))}</span> · {esc(ui('generated', '生成时间'))} {esc(snapshot.get('generated_at'))} · {esc(ui('times shown in', '页面时间按'))} {esc(dashboard_tz_name)} {esc(ui('', '显示'))} · {esc(ui('auto-refresh 5s', '每 5 秒自动刷新'))}</div>
    </div>
    <nav class="lang-switch" aria-label="language">
      <a class="{'active' if not is_zh else ''}" href="{esc(lang_url('en'))}">English</a>
      <a class="{'active' if is_zh else ''}" href="{esc(lang_url('zh'))}">中文</a>
    </nav>
  </div>
  <div>
    <span class="pill">{esc(ui('all decisions', '全部判定'))} {esc(counts.get('decisions', 0))}</span>
    <span class="pill">{esc(ui('visible decisions', '主表判定'))} {esc(visible_wake_count)}</span>
    <span class="pill">{esc(ui('hidden no-frame ticks', '隐藏空 tick'))} {esc(hidden_no_frame_count)}</span>
    <span class="pill">{esc(ui('human reviews', '人工标注'))} {esc(counts.get('reviews', 0))}</span>
    <span class="pill">{esc(ui('hidden jobs', '隐藏任务'))} {esc(counts.get('jobs', 0))}</span>
    <span class="pill">{esc(ui('proactive writes', '主动写入'))} {esc(counts.get('proactive_messages', 0))}</span>
    <span class="pill">{esc(ui('screen frames', '屏幕帧'))} {esc(counts.get('recent_frames', 0))}</span>
    <span class="pill">{esc(ui('device events', '设备事件'))} {esc(counts.get('device_events', 0))}</span>
  </div>
  <div class="hint">
    {esc(ui('Full debug mode is on by default. The page reads deeper history and caps only the rendered sections; use the links to expand further or hide JSON payloads.', '默认已恢复完整调试模式。页面会读取更深的历史，只限制渲染条数；可以用下面链接继续展开或隐藏 JSON。'))}
    {control_links}
  </div>

  <h2>{esc(ui('Wake Decisions', 'Wake 判定'))}</h2>
  <div class="hint">{esc(ui('V2 wake events are always shown. Legacy no-frame ticks stay folded below.', 'V2 wake 事件会直接显示；旧版无屏幕帧空 tick 会折叠在下方。'))}</div>
  {decision_section(frame_decisions, visible_empty_text, limit=decision_cap)}
  {hidden_gate_details}

  <h2>{esc(ui('Hidden Jobs', '隐藏任务'))}</h2>
  {job_section()}

  <h2>{esc(ui('Proactive Chat Writes', '主动消息写入'))}</h2>
  <div class="table-scroll">
    <table class="t-messages">
      <colgroup>
        <col class="c-time"><col class="c-type"><col class="c-prev">
        <col class="c-status"><col class="c-status">
        <col class="c-id"><col class="c-id"><col class="c-id">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>{esc(ui('type', '类型'))}</th><th>{esc(ui('preview', '预览'))}</th><th>{esc(ui('alert', '系统通知'))}</th><th>Live Activity</th><th>{esc(ui('decision', '判定'))}</th><th>{esc(ui('job', '任务'))}</th><th>{esc(ui('message', '消息'))}</th></tr></thead>
      <tbody>{message_rows()}</tbody>
    </table>
  </div>

  <h2>{esc(ui('Recent Screen Frames', '最近屏幕帧'))}</h2>
  <div class="table-scroll">
    <table class="t-frames">
      <colgroup>
        <col class="c-time"><col class="c-app"><col class="c-num"><col class="c-num"><col class="c-link">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>App</th><th>{esc(ui('OCR length', 'OCR 长度'))}</th><th>{esc(ui('encrypted', '已加密'))}</th><th>{esc(ui('frame', '屏幕帧'))}</th></tr></thead>
      <tbody>{frame_rows()}</tbody>
    </table>
  </div>

  <h2>{esc(ui('Device Events', '设备事件'))}</h2>
  <div class="table-scroll">
    <table class="t-events">
      <colgroup>
        <col class="c-time"><col class="c-source"><col class="c-type"><col class="c-payload">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>{esc(ui('source', '来源'))}</th><th>{esc(ui('type', '类型'))}</th><th>{esc(ui('payload', '事件数据'))}</th></tr></thead>
      <tbody>{event_rows()}</tbody>
    </table>
  </div>
</body>
</html>"""
