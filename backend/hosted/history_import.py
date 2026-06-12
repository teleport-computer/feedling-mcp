"""History import pipeline: parse → extract candidates → memory cards → identity → greeting. /v1/history_import/*."""

import base64
import copy
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from flask import Blueprint, Response, jsonify, request, g

import db
from core import envelope as core_envelope
from core.store import UserStore

from hosted_runtime import (
    ACTION_RESPONSE_FORMAT as HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
    ACTION_METHOD as HOSTED_RUNTIME_ACTION_METHOD,
    BACKGROUND_METHOD as HOSTED_RUNTIME_BACKGROUND_METHOD,
    BACKGROUND_NOT_STARTED_METHOD as HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
    NOOP_METHOD as HOSTED_RUNTIME_NOOP_METHOD,
    PENDING_CONFIRM_METHOD as HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
    PENDING_REJECT_METHOD as HOSTED_RUNTIME_PENDING_REJECT_METHOD,
    RUNTIME_ENGINE_NATIVE as HOSTED_RUNTIME_ENGINE_NATIVE,
    build_background_execution_messages as build_hosted_runtime_background_execution_messages,
    background_execution_trace as hosted_runtime_background_trace,
    companion_turn_contract_message as hosted_runtime_companion_turn_contract_message,
    coerce_pending_decision as coerce_hosted_runtime_pending_decision,
    coerce_runtime_action as coerce_hosted_runtime_action,
)
from model_api_runtime.prompts import (
    build_foreground_chat_messages as build_model_api_foreground_chat_messages,
    build_memory_capture_messages as build_model_api_memory_capture_messages,
    build_pending_confirmation_messages as build_model_api_pending_confirmation_messages,
    build_web_search_results_message as build_model_api_web_search_results_message,
    web_search_followup_message as model_api_web_search_followup_message,
)
from model_api_runtime.tools import (
    extract_web_search_requests as extract_model_api_web_search_requests,
    run_web_searches as run_model_api_web_searches,
    web_search_trace as model_api_web_search_trace,
)
from model_api_runtime.wake import (
    wake_turn_contract_message as model_api_wake_turn_contract_message,
    build_wake_event_message as build_model_api_wake_event_message,
    hosted_tick_trigger as model_api_hosted_tick_trigger,
    parse_wake_actions as parse_model_api_wake_actions,
)
from context_memory_selection import memory_relevance_details
from content_encryption import build_envelope

from accounts import auth
from accounts import registry
from bootstrap import gates as boot_gates
from core import util as core_util
from identity import service as identity_service
from memory import service as memory_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import onboarding_validation as hosted_onboarding_validation


bp = Blueprint("hosted_history_import", __name__)

def _history_job_kind(job_id: str) -> str:
    """user_blobs kind for a single history-import job. One blob per job_id."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", job_id or "")
    return f"history_import_job:{safe}"


HISTORY_IMPORT_STALE_SEC = int(os.environ.get("FEEDLING_HISTORY_IMPORT_STALE_SEC", str(30 * 60)))
_HISTORY_IMPORT_PHASES = {
    "upload_received": (5, "Upload received"),
    "parsing_materials": (15, "Reading materials"),
    "chat_history_importing": (24, "Reading chat history"),
    "candidate_extracting": (38, "Distilling memory candidates"),
    "candidate_merging": (52, "Merging memory candidates"),
    "memory_writing": (64, "Writing core Memory Garden"),
    "identity_deriving": (76, "Deriving Identity Card"),
    "relationship_anchor_writing": (86, "Writing relationship anchor"),
    "hosted_chat_preparing": (92, "Preparing hosted chat"),
    "background_importing": (96, "Continuing history distillation"),
    "completed": (100, "Completed"),
    "failed": (100, "Failed"),
}
_history_import_active_jobs: set[str] = set()
_history_import_active_lock = threading.Lock()


def _save_history_job(store: UserStore, job: dict) -> dict:
    job["updated_at"] = core_util._now_iso()
    db.set_blob(store.user_id, _history_job_kind(job["job_id"]), job)
    return job


def _history_import_phase_fields(phase: str) -> dict:
    progress, label = _HISTORY_IMPORT_PHASES.get(phase, (0, phase or ""))
    return {
        "phase": phase,
        "phase_label": label,
        "progress": progress,
    }


def _update_history_job_phase(
    store: UserStore,
    job: dict,
    phase: str,
    *,
    status: str = "processing",
    **fields,
) -> dict:
    job.update(_history_import_phase_fields(phase))
    job["status"] = status
    job.update(fields)
    return _save_history_job(store, job)


def _history_import_payload_hash(payload: dict) -> str:
    relevant = {
        key: payload.get(key)
        for key in (
            "format",
            "content",
            "fresh_start",
            "relationship_started_at",
            "ai_persona_content",
            "ai_persona_filename",
            "character_content",
            "character_card",
            "character_filename",
            "character_card_filename",
            "persona_content",
            "persona",
            "personal_profile_content",
            "personal_profile_filename",
            "profile_content",
            "persona_filename",
            "memory_summary_content",
            "memory_summary",
            "memory_sample_content",
            "memory_summary_filename",
            "memory_sample_filename",
            "history_filename",
        )
    }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _history_import_client_job_id(payload: dict) -> str:
    raw = str(payload.get("client_job_id") or "").strip()
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:96]


def _load_history_import_jobs(store: UserStore) -> list[dict]:
    jobs = db.list_blobs(store.user_id, "history_import_job:")
    jobs.sort(key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""))
    return jobs


def _history_import_age_sec(job: dict) -> float:
    raw = str(job.get("updated_at") or job.get("created_at") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, time.time() - datetime.fromisoformat(raw).timestamp())
    except Exception:
        return 0.0


def _history_import_find_reusable_job(
    store: UserStore,
    *,
    client_job_id: str,
    input_hash: str,
) -> dict | None:
    for job in reversed(_load_history_import_jobs(store)):
        status = str(job.get("status") or "")
        if status == "failed":
            continue
        matches_client = client_job_id and str(job.get("client_job_id") or "") == client_job_id
        matches_hash = input_hash and str(job.get("input_hash") or "") == input_hash
        if not (matches_client or matches_hash):
            continue
        if status in {"queued", "processing"} and _history_import_age_sec(job) > HISTORY_IMPORT_STALE_SEC:
            job.update({
                "status": "failed",
                "failed_at": core_util._now_iso(),
                "error": "RuntimeError:stale_history_import_job",
            })
            _update_history_job_phase(store, job, "failed", status="failed")
            continue
        return job
    return None


_HISTORY_LINE_RE = re.compile(
    r"^\s*(?:\[(?P<bracket_ts>[^\]]{6,80})\]\s*)?"
    r"(?:(?P<iso_ts>\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)\s+)?"
    r"(?:(?P<role>[A-Za-z\u4e00-\u9fff _-]{1,32})\s*[:：]\s*)?"
    r"(?P<text>.+?)\s*$"
)


def _parse_history_ts(raw: str) -> float | None:
    val = (raw or "").strip()
    if not val:
        return None
    norm = val.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", norm):
        norm += "T00:00:00"
    if " " in norm and "T" not in norm:
        norm = norm.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return dt.timestamp()
    except Exception:
        return None


def _normalize_history_role(raw: str) -> str:
    role = (raw or "").strip().lower()
    if role in {"user", "human", "me", "you", "我", "用户"}:
        return "user"
    if role in {"assistant", "ai", "agent", "claude", "chatgpt", "gemini", "gpt", "io", "ta", "他", "助手"}:
        return "assistant"
    return "user"


def _normalize_json_history_role(raw: str) -> str:
    role = (raw or "").strip().lower()
    if not role:
        return "user"
    if role in {"user", "human", "me", "you", "我", "用户"}:
        return "user"
    if role in {"assistant", "ai", "agent", "model", "claude", "chatgpt", "gemini", "gpt", "io", "ta", "他", "助手"}:
        return "assistant"
    if role in {"system", "developer", "tool", "function", "browser", "插件"}:
        return ""
    return ""


def _parse_plaintext_history(content: str) -> list[dict]:
    lines = (content or "").splitlines()
    messages: list[dict] = []
    cur: dict | None = None

    def flush() -> None:
        nonlocal cur
        if cur and str(cur.get("content") or "").strip():
            cur["content"] = str(cur["content"]).strip()
            messages.append(cur)
        cur = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            continue
        m = _HISTORY_LINE_RE.match(line)
        if not m:
            if cur:
                cur["content"] += "\n" + line
            continue
        role_raw = m.group("role") or ""
        ts_raw = m.group("iso_ts") or m.group("bracket_ts") or ""
        text = (m.group("text") or "").strip()
        has_role = bool(role_raw.strip())
        has_ts = bool(ts_raw.strip() and _parse_history_ts(ts_raw) is not None)
        if has_role or has_ts or cur is None:
            flush()
            cur = {
                "role": _normalize_history_role(role_raw),
                "content": text,
                "ts": _parse_history_ts(ts_raw),
                "source": "history_import",
            }
        else:
            cur["content"] += "\n" + line
    flush()

    if not messages and content.strip():
        messages.append({
            "role": "user",
            "content": content.strip(),
            "ts": None,
            "source": "history_import",
        })
    return messages


def _extract_json_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_extract_json_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "body", "value"):
            if key in value:
                text = _extract_json_text(value.get(key))
                if text:
                    return text
        parts = value.get("parts")
        if isinstance(parts, list):
            return _extract_json_text(parts)
    return ""


def _extract_json_ts(item: dict) -> float | None:
    for key in ("create_time", "created_at", "timestamp", "time", "date"):
        raw = item.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        parsed = _parse_history_ts(str(raw))
        if parsed is not None:
            return parsed
    return None


def _role_from_json_item(item: dict) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        role = author.get("role") or author.get("name")
        if role:
            return _normalize_json_history_role(str(role))
    role = item.get("role") or item.get("sender") or item.get("from") or item.get("speaker")
    if role:
        return _normalize_json_history_role(str(role))
    return "user"


def _dedupe_history_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, float | None]] = set()
    for msg in messages:
        text = str(msg.get("content") or "").strip()
        role = str(msg.get("role") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        ts = msg.get("ts")
        try:
            ts_key = float(ts) if ts is not None else None
        except Exception:
            ts_key = None
        key = (role, re.sub(r"\s+", " ", text)[:800], ts_key)
        if key in seen:
            continue
        seen.add(key)
        clean = dict(msg)
        clean["content"] = text
        clean["role"] = role
        out.append(clean)
    return out


def _parse_json_history(content: str) -> list[dict]:
    raw = json.loads(content)
    messages: list[dict] = []
    seen: set[tuple[str, str, float | None]] = set()

    def maybe_add(item: dict) -> bool:
        candidate = item.get("message") if isinstance(item.get("message"), dict) else item
        if not isinstance(candidate, dict):
            return False
        text = _extract_json_text(candidate.get("content"))
        if not text:
            text = _extract_json_text(candidate.get("text"))
        if not text:
            return False
        role = _role_from_json_item(candidate)
        if role not in {"user", "assistant"}:
            return False
        ts = _extract_json_ts(candidate) or _extract_json_ts(item)
        key = (role, text[:500], ts)
        if key in seen:
            return True
        seen.add(key)
        messages.append({
            "role": role,
            "content": text,
            "ts": ts,
            "source": "history_import",
        })
        return True

    def walk(value) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if maybe_add(value):
            return
        mapping = value.get("mapping")
        if isinstance(mapping, dict):
            for node in mapping.values():
                walk(node)
        for key in ("messages", "chat_messages", "conversation", "conversations", "items"):
            if key in value:
                walk(value.get(key))

    walk(raw)
    messages.sort(key=lambda m: (m.get("ts") is None, float(m.get("ts") or 0)))
    return _dedupe_history_messages(messages)


_WRAPPED_CHAT_HISTORY_RE = re.compile(
    r"(?ms)^\s*={3,}\s*BEGIN\s+CHAT\s+HISTORY\s+FILE:\s*(?P<filename>.*?)\s*={3,}\s*"
    r"(?P<body>.*?)(?:^\s*={3,}\s*END\s+CHAT\s+HISTORY\s+FILE:.*?={3,}\s*|\Z)"
)


def _parse_wrapped_history_content(content: str, warnings: list[str]) -> tuple[bool, list[dict]]:
    blocks = list(_WRAPPED_CHAT_HISTORY_RE.finditer(content or ""))
    if not blocks:
        return False, []

    messages: list[dict] = []
    for block in blocks:
        filename = str(block.group("filename") or "").strip()
        body = str(block.group("body") or "").strip()
        if not body:
            warnings.append(f"empty_chat_history_file:{filename[:120]}")
            continue
        lower_name = filename.lower()
        looks_json = lower_name.endswith(".json") or body.lstrip().startswith(("{", "["))
        if looks_json:
            try:
                parsed = _parse_json_history(body)
            except Exception as e:
                warnings.append(f"wrapped_json_parse_failed:{filename[:120]}:{type(e).__name__}")
                continue
        else:
            parsed = _parse_plaintext_history(body)
        for msg in parsed:
            if filename:
                msg["source_filename"] = filename[:240]
        messages.extend(parsed)
    return True, _dedupe_history_messages(messages)


def _parse_import_history_content(content: str, fmt: str, warnings: list[str]) -> list[dict]:
    if not content.strip():
        return []
    normalized = (fmt or "plaintext").strip().lower()
    has_wrapped, wrapped_messages = _parse_wrapped_history_content(content, warnings)
    if has_wrapped:
        return wrapped_messages
    if normalized in {"json", "chatgpt_json", "claude_json"}:
        return _parse_json_history(content)
    if normalized in {"auto", "file"}:
        stripped = content.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _parse_json_history(content)
            except Exception as e:
                warnings.append(f"json_parse_failed_falling_back_to_plaintext:{type(e).__name__}")
        return _parse_plaintext_history(content)
    if normalized in {"plaintext", "text"}:
        return _parse_plaintext_history(content)
    raise ValueError("format must be plaintext, text, json, or auto")


_AI_PERSONA_SOURCE = "ai_persona_import"
_USER_PROFILE_SOURCE = "user_profile_import"
_MEMORY_SUMMARY_SOURCE = "memory_summary_import"
_HISTORY_SOURCE = "history_import"
_FRESH_START_SOURCE = "fresh_start"

_IMPORT_SOURCE_FAMILY = {
    _AI_PERSONA_SOURCE: _AI_PERSONA_SOURCE,
    "agent_prompt_import": _AI_PERSONA_SOURCE,
    "character_import": _AI_PERSONA_SOURCE,
    _USER_PROFILE_SOURCE: _USER_PROFILE_SOURCE,
    "persona_import": _USER_PROFILE_SOURCE,
    _MEMORY_SUMMARY_SOURCE: _MEMORY_SUMMARY_SOURCE,
    _HISTORY_SOURCE: _HISTORY_SOURCE,
    _FRESH_START_SOURCE: _FRESH_START_SOURCE,
}


def _import_source_family(source: str | None) -> str:
    raw = str(source or "").strip()
    return _IMPORT_SOURCE_FAMILY.get(raw, raw or _HISTORY_SOURCE)


_SUPPORT_BLOCK_RE = re.compile(
    r"(?ms)^\s*={3,}\s*BEGIN\s+"
    r"(?P<label>AGENT\s+PROMPT|SYSTEM\s+PROMPT|ORIGINAL\s+SYSTEM\s+PROMPT|"
    r"AI\s+PERSONA(?:\s+MATERIALS?)?|CHARACTER\s+CARD|"
    r"PERSONAL\s+PROFILE(?:\s+CARD)?|PERSONA\s+PROFILE|USER\s+PROFILE|PERSONA|"
    r"MEMORY\s+SUMMARY|MEMORY\s+SAMPLE|MEMORY\s+SAMURAI)"
    r"(?:\s*:(?P<filename>[^=]*))?\s*={3,}\s*"
    r"(?P<body>.*?)(?=^\s*={3,}\s*BEGIN\s+"
    r"(?:AGENT\s+PROMPT|SYSTEM\s+PROMPT|ORIGINAL\s+SYSTEM\s+PROMPT|"
    r"AI\s+PERSONA(?:\s+MATERIALS?)?|CHARACTER\s+CARD|"
    r"PERSONAL\s+PROFILE(?:\s+CARD)?|PERSONA\s+PROFILE|USER\s+PROFILE|PERSONA|"
    r"MEMORY\s+SUMMARY|MEMORY\s+SAMPLE|MEMORY\s+SAMURAI)"
    r"(?:\s*:[^=]*)?\s*={3,}\s*|\Z)"
)
_SUPPORT_MARKER_RE = re.compile(r"^\s*={3,}\s*(?:BEGIN|END)\s+[^=]+={3,}\s*$", re.IGNORECASE)


def _clean_support_material_text(text: str) -> str:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _SUPPORT_MARKER_RE.match(line):
            continue
        if re.match(r"^\s*={3,}\s*(?:BEGIN|END)\s+CHAT\s+HISTORY\s+FILE:", line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _support_message(
    label: str,
    source: str,
    content: str,
    filename: str = "",
    *,
    source_detail: str = "",
) -> dict | None:
    clean = _normalize_support_material_text(content, filename)
    if not clean:
        return None
    title = label
    if filename.strip():
        title += f" ({filename.strip()[:120]})"
    family = _import_source_family(source)
    cap = 120000 if family == _MEMORY_SUMMARY_SOURCE else 60000 if family in {_AI_PERSONA_SOURCE, _USER_PROFILE_SOURCE} else 30000
    return {
        "role": "user",
        "content": f"{title}:\n{clean[:cap]}",
        "ts": None,
        "source": family,
        "source_family": family,
        "source_detail": source_detail or source,
        "source_filename": filename.strip()[:240],
    }


_SUPPORT_JSON_TEXT_KEYS = (
    "conversations_memory",
    "conversation_memory",
    "personal_profile",
    "persona_profile",
    "user_profile",
    "character_card",
    "memory_summary",
    "memory_sample",
    "memory_samurai",
    "memory_summaries",
    "persona",
    "profile",
    "memory",
    "memories",
    "summary",
    "description",
    "notes",
    "content",
    "text",
    "body",
    "value",
)
_SUPPORT_JSON_PRIVATE_KEYS = {
    "uuid",
    "account_uuid",
    "user_id",
    "email",
    "email_address",
    "phone",
    "phone_number",
    "verified_phone_number",
    "id",
}


def _support_json_is_account_metadata(value: dict) -> bool:
    keys = {str(k).lower() for k in value.keys()}
    has_private = bool(keys & _SUPPORT_JSON_PRIVATE_KEYS) or any(
        "email" in key or "phone" in key or "uuid" in key for key in keys
    )
    has_content = any(key in keys for key in _SUPPORT_JSON_TEXT_KEYS)
    return has_private and not has_content


def _support_json_scalar_text(value) -> str:
    if not isinstance(value, str):
        return ""
    text = _clean_support_material_text(value)
    if len(text) < 2:
        return ""
    if re.fullmatch(r"[0-9a-fA-F-]{16,}", text):
        return ""
    if "@" in text and re.fullmatch(r"\S+@\S+\.\S+", text):
        return ""
    return text


def _extract_support_json_text(value, *, depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    if isinstance(value, str):
        text = _support_json_scalar_text(value)
        return [text] if text else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:200]:
            parts.extend(_extract_support_json_text(item, depth=depth + 1))
        return parts
    if not isinstance(value, dict):
        return []
    if _support_json_is_account_metadata(value):
        return []

    parts: list[str] = []
    lower_map = {str(k).lower(): k for k in value.keys()}
    for key in _SUPPORT_JSON_TEXT_KEYS:
        original_key = lower_map.get(key)
        if original_key is not None:
            parts.extend(_extract_support_json_text(value.get(original_key), depth=depth + 1))

    if parts:
        return parts

    for key, nested in value.items():
        lower_key = str(key).lower()
        if lower_key in _SUPPORT_JSON_PRIVATE_KEYS:
            continue
        if any(token in lower_key for token in ("email", "phone", "uuid", "avatar", "url")):
            continue
        parts.extend(_extract_support_json_text(nested, depth=depth + 1))
    return parts


def _normalize_support_material_text(content: str, filename: str = "") -> str:
    clean = _clean_support_material_text(content)
    if not clean:
        return ""

    stripped = clean.strip()
    if not stripped.startswith(("{", "[")):
        return clean

    try:
        parsed = json.loads(stripped)
    except Exception:
        return clean

    parts = _extract_support_json_text(parsed)
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = re.sub(r"\s+", " ", part).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(part.strip())
    if not deduped:
        return ""
    return "\n\n".join(deduped)[:30000].strip()


def _split_support_sections(content: str) -> list[tuple[str, str, str, str]]:
    sections: list[tuple[str, str, str, str]] = []
    for m in _SUPPORT_BLOCK_RE.finditer(content or ""):
        raw_label = re.sub(r"\s+", " ", str(m.group("label") or "").strip().lower())
        filename = str(m.group("filename") or "").strip()
        body = str(m.group("body") or "").strip()
        if (
            "system prompt" in raw_label
            or "agent prompt" in raw_label
            or "character" in raw_label
            or "ai persona" in raw_label
        ):
            sections.append(("AI Persona material", _AI_PERSONA_SOURCE, body, filename))
        elif "memory summary" in raw_label or "memory sample" in raw_label or "memory samurai" in raw_label:
            sections.append(("Memory summary", _MEMORY_SUMMARY_SOURCE, body, filename))
        else:
            sections.append(("User profile", _USER_PROFILE_SOURCE, body, filename))
    return sections


def _persona_support_messages(payload: dict) -> list[dict]:
    messages: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, source: str, content: str, filename: str = "") -> None:
        msg = _support_message(label, source, content, filename, source_detail=source)
        if not msg:
            return
        key = (_import_source_family(source), re.sub(r"\s+", " ", str(msg.get("content") or ""))[:1000])
        if key in seen:
            return
        seen.add(key)
        messages.append(msg)

    ai_persona = str(payload.get("ai_persona_content") or payload.get("ai_persona") or "").strip()
    if ai_persona:
        add(
            "AI Persona material",
            _AI_PERSONA_SOURCE,
            ai_persona,
            str(payload.get("ai_persona_filename") or "").strip(),
        )

    agent_prompt = str(
        payload.get("agent_prompt_content")
        or payload.get("original_system_prompt_content")
        or payload.get("system_prompt_content")
        or payload.get("agent_prompt")
        or payload.get("system_prompt")
        or payload.get("original_system_prompt")
        or ""
    ).strip()
    if agent_prompt:
        filename = str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        ).strip()
        msg = _support_message(
            "AI Persona material",
            _AI_PERSONA_SOURCE,
            agent_prompt,
            filename,
            source_detail="agent_prompt_import",
        )
        if msg:
            key = (_AI_PERSONA_SOURCE, re.sub(r"\s+", " ", str(msg.get("content") or ""))[:1000])
            if key not in seen:
                seen.add(key)
                messages.append(msg)

    character = str(payload.get("character_content") or payload.get("character_card") or "").strip()
    if character:
        msg = _support_message(
            "AI Persona material",
            _AI_PERSONA_SOURCE,
            character,
            str(payload.get("character_filename") or payload.get("character_card_filename") or "").strip(),
            source_detail="character_import",
        )
        if msg:
            key = (_AI_PERSONA_SOURCE, re.sub(r"\s+", " ", str(msg.get("content") or ""))[:1000])
            if key not in seen:
                seen.add(key)
                messages.append(msg)

    profile = str(
        payload.get("personal_profile_content")
        or payload.get("profile_content")
        or ""
    ).strip()
    if profile:
        add(
            "User profile",
            _USER_PROFILE_SOURCE,
            profile,
            str(payload.get("personal_profile_filename") or payload.get("persona_filename") or "").strip(),
        )

    memory_summary = str(
        payload.get("memory_summary_content")
        or payload.get("memory_summary")
        or payload.get("memory_sample_content")
        or payload.get("memory_sample")
        or ""
    ).strip()
    if memory_summary:
        add(
            "Memory summary",
            _MEMORY_SUMMARY_SOURCE,
            memory_summary,
            str(payload.get("memory_summary_filename") or payload.get("memory_sample_filename") or "").strip(),
        )

    persona = str(payload.get("persona_content") or payload.get("persona") or "").strip()
    if persona:
        split_sections = _split_support_sections(persona)
        if split_sections:
            outer_filename = str(payload.get("persona_filename") or "").strip()
            for label, source, body, section_filename in split_sections:
                add(label, source, body, section_filename or outer_filename)
        else:
            add(
                "User profile",
                _USER_PROFILE_SOURCE,
                persona,
                str(payload.get("persona_filename") or "").strip(),
            )
    return messages


def _message_iso_date(msg: dict, fallback: date) -> str:
    try:
        ts = msg.get("ts")
        if ts:
            return datetime.fromtimestamp(float(ts)).date().isoformat()
    except Exception:
        pass
    return fallback.isoformat()


_IMPORT_ARTIFACT_KEYS = (
    "async_status",
    "atlas_mode_enabled",
    "blocked_urls",
    "context_scopes",
    "conversation_id",
    "conversation_origin",
    "conversation_template_id",
    "current_node",
    "default_model_slug",
    "disabled_tool_ids",
    "gizmo_id",
    "is_archived",
    "is_do_not_remember",
    "is_read_only",
    "mapping",
)


def _looks_like_import_artifact(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    upper = raw.upper()
    if "BEGIN CHAT HISTORY FILE" in upper or "END CHAT HISTORY FILE" in upper:
        return True
    key_hits = sum(1 for key in _IMPORT_ARTIFACT_KEYS if f'"{key}"' in raw or f"{key}:" in raw)
    if key_hits >= 2:
        return True
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    jsonish = sum(1 for line in lines if re.match(r'^"[^"]+"\s*:', line) or line in {"{", "}", "[", "]", "},"})
    return len(lines) >= 4 and jsonish / max(len(lines), 1) > 0.5


def _clean_import_memory_text(text: str, max_chars: int = 900) -> str:
    kept: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if _SUPPORT_MARKER_RE.match(line):
            continue
        if re.match(r"^\s*={3,}\s*(?:BEGIN|END)\s+CHAT\s+HISTORY\s+FILE:", line, re.IGNORECASE):
            continue
        if re.match(r'^"[^"]+"\s*:', line):
            continue
        if line in {"{", "}", "[", "]", "},", "],"}:
            continue
        if any(key in line for key in _IMPORT_ARTIFACT_KEYS):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if _looks_like_import_artifact(cleaned):
        return ""
    return cleaned[:max_chars].strip()


def _relationship_start_from_import(payload: dict, messages: list[dict]) -> tuple[date | None, str]:
    raw = str(payload.get("relationship_started_at") or "").strip()
    if raw:
        parsed = identity_service._parse_iso_calendar_date(raw)
        if parsed:
            return parsed, ""

    dated: list[date] = []
    for msg in messages:
        try:
            ts = msg.get("ts")
            if ts:
                dated.append(datetime.fromtimestamp(float(ts)).date())
        except Exception:
            pass
    if dated:
        return min(dated), ""
    if bool(payload.get("fresh_start")) or raw:
        return date.today(), ""
    return None, "relationship_started_at required when transcript has no timestamps; or pass fresh_start=true"


def _detect_import_language(messages: list[dict]) -> str:
    sample = "\n".join(str(m.get("content") or "") for m in messages)[:24000]
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin_count = len(re.findall(r"[A-Za-z]", sample))
    if zh_count >= 8 and zh_count >= max(3, int(latin_count * 0.08)):
        return "zh-Hans"
    return "en"


def _import_language_for_store(store: UserStore, messages: list[dict]) -> str:
    detected = _detect_import_language(messages)
    archive_language = str(registry._get_user_archive_language(store.user_id) or "").strip()
    if archive_language.lower().startswith("zh"):
        return archive_language
    if detected.startswith("zh"):
        return detected
    if archive_language.lower().startswith("en"):
        return archive_language
    return detected


def _language_instruction(language: str) -> str:
    if str(language).startswith("zh"):
        return (
            "Write every user-visible field in Simplified Chinese. Keep proper names, model IDs, "
            "and exact quoted phrases in their original language when needed."
        )
    return "Write every user-visible field in natural English."


def _english_only_for_zh(text: str) -> bool:
    raw = str(text or "")
    return bool(re.search(r"[A-Za-z]{4,}", raw)) and not re.search(r"[\u4e00-\u9fff]", raw)


_IMPORT_SUPPORT_SOURCES = {
    _AI_PERSONA_SOURCE,
    "agent_prompt_import",
    "character_import",
    _USER_PROFILE_SOURCE,
    "persona_import",
    _MEMORY_SUMMARY_SOURCE,
    _FRESH_START_SOURCE,
}


def _format_import_message_line(msg: dict) -> str:
    source = _import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
    if source == _AI_PERSONA_SOURCE:
        role = "AI Persona material"
    elif source == _USER_PROFILE_SOURCE:
        role = "User profile"
    elif source == _MEMORY_SUMMARY_SOURCE:
        role = "Memory summary"
    elif source == _FRESH_START_SOURCE:
        role = "Fresh start"
    else:
        role = "User" if msg.get("role") == "user" else "Assistant"
    at = ""
    try:
        if msg.get("ts"):
            at = datetime.fromtimestamp(float(msg["ts"])).isoformat(timespec="seconds") + " "
    except Exception:
        at = ""
    text = str(msg.get("content") or "").strip()
    if not text:
        return ""
    return f"{at}{role}: {text}"


def _model_api_agent_profile_context(store: UserStore, identity: dict) -> dict:
    latest_job = None
    try:
        latest_job = hosted_onboarding_validation._latest_history_import_job(store)
    except Exception:
        latest_job = None
    latest_job = latest_job if isinstance(latest_job, dict) else {}
    return {
        "runtime_boundary": (
            "Feedling provides the container, iOS context, tools, Identity, and durable memory cards. "
            "The imported agent materials and chat history own the companion persona."
        ),
        "agent_name": str(identity.get("agent_name") or ""),
        "self_introduction": str(identity.get("self_introduction") or "")[:1200],
        "category": str(identity.get("category") or "")[:240],
        "signature": identity.get("signature", []) if isinstance(identity.get("signature"), list) else [],
        "dimensions": identity.get("dimensions", []) if isinstance(identity.get("dimensions"), list) else [],
        "import_sources": {
            "ai_persona": bool(latest_job.get("ai_persona_chars") or latest_job.get("agent_prompt_chars") or latest_job.get("character_chars")),
            "user_profile": bool(latest_job.get("user_profile_chars") or latest_job.get("persona_chars")),
            "memory_summary": bool(latest_job.get("memory_summary_chars")),
            "chat_history": bool(latest_job.get("messages_parsed")),
        },
        "source_priority": [
            "explicit user corrections",
            "AI persona materials",
            "Feedling Identity",
            "relevant memory cards",
            "recent chat",
        ],
    }


def _append_import_lines(lines: list[str], out: list[str], budget: int, *, reverse: bool = False) -> int:
    total = sum(len(line) + 1 for line in out)
    iterable = reversed(lines) if reverse else lines
    staged: list[str] = []
    for line in iterable:
        if not line:
            continue
        if total + len(line) + 1 > budget:
            break
        staged.append(line)
        total += len(line) + 1
    if reverse:
        staged.reverse()
    out.extend(staged)
    return total


def _sequential_transcript_sample(messages: list[dict], max_chars: int) -> str:
    lines = [_format_import_message_line(msg) for msg in messages]
    out: list[str] = []
    _append_import_lines(lines, out, max_chars)
    return "\n".join(out)


def _stratified_history_sample(messages: list[dict], max_chars: int) -> str:
    lines = [_format_import_message_line(msg) for msg in messages]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full

    sections: list[tuple[str, list[str], bool]] = [
        ("[Earliest history]", lines, False),
        ("[Middle history]", lines[len(lines) // 2:], False),
        ("[Latest history]", lines, True),
    ]
    per_section = max(1200, max_chars // 3)
    out_sections: list[str] = []
    seen: set[str] = set()
    for title, section_lines, reverse in sections:
        picked: list[str] = []
        _append_import_lines(section_lines, picked, per_section, reverse=reverse)
        picked = [line for line in picked if line not in seen]
        seen.update(picked)
        if picked:
            out_sections.append(title + "\n" + "\n".join(picked))
    text = "\n\n".join(out_sections)
    return text[:max_chars].strip()


def _is_import_support_message(msg: dict) -> bool:
    return _import_source_family(str(msg.get("source") or msg.get("source_family") or "")) in _IMPORT_SUPPORT_SOURCES


def _import_source_stats(messages: list[dict]) -> dict:
    stats = {
        _AI_PERSONA_SOURCE: {"count": 0, "chars": 0},
        _USER_PROFILE_SOURCE: {"count": 0, "chars": 0},
        _MEMORY_SUMMARY_SOURCE: {"count": 0, "chars": 0},
        _HISTORY_SOURCE: {"count": 0, "chars": 0},
        _FRESH_START_SOURCE: {"count": 0, "chars": 0},
    }
    for msg in messages:
        family = _import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
        if family not in stats:
            stats[family] = {"count": 0, "chars": 0}
        stats[family]["count"] += 1
        stats[family]["chars"] += len(str(msg.get("content") or ""))
    return stats


def _messages_for_source_family(messages: list[dict], family: str) -> list[dict]:
    return [
        m for m in messages
        if _import_source_family(str(m.get("source") or m.get("source_family") or "")) == family
    ]


def _source_briefing_text(support_messages: list[dict], max_chars: int = 7000) -> str:
    groups = [
        ("AI Persona materials", _AI_PERSONA_SOURCE, 1800),
        ("User Profile", _USER_PROFILE_SOURCE, 1600),
        ("Memory Summary", _MEMORY_SUMMARY_SOURCE, 2600),
        ("Fresh Start", _FRESH_START_SOURCE, 600),
    ]
    parts: list[str] = []
    budget_used = 0
    for title, family, family_budget in groups:
        group = _messages_for_source_family(support_messages, family)
        if not group:
            continue
        text = _sequential_transcript_sample(group, min(family_budget, max_chars - budget_used))
        if not text:
            continue
        part = f"[{title}]\n{text}"
        if budget_used + len(part) + 2 > max_chars:
            break
        parts.append(part)
        budget_used += len(part) + 2
    return "\n\n".join(parts).strip()


def _split_text_windows(text: str, max_chars: int) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if len(raw) <= max_chars:
        return [raw]
    paras = re.split(r"\n{2,}", raw)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if current and current_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0
        if len(para) > max_chars:
            for start in range(0, len(para), max_chars):
                chunk = para[start:start + max_chars].strip()
                if chunk:
                    chunks.append(chunk)
            continue
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks


def _support_source_windows(support_messages: list[dict], *, max_chars: int) -> list[dict]:
    windows: list[dict] = []
    groups = [
        (_AI_PERSONA_SOURCE, "AI Persona materials", max(4000, min(12000, max_chars - 1200))),
        (_USER_PROFILE_SOURCE, "User Profile", max(4000, min(10000, max_chars - 1200))),
        (_MEMORY_SUMMARY_SOURCE, "Memory Summary", max(5000, min(14000, max_chars - 800))),
        (_FRESH_START_SOURCE, "Fresh Start", max(1000, min(3000, max_chars))),
    ]
    for family, label, family_max in groups:
        group = _messages_for_source_family(support_messages, family)
        if not group:
            continue
        text = _sequential_transcript_sample(group, family_max * 8)
        for idx, chunk in enumerate(_split_text_windows(text, family_max), start=1):
            windows.append({
                "id": f"{family.replace('_import', '').replace('_', '-')}-{idx:03d}",
                "index": len(windows) + 1,
                "total": 0,
                "text": f"[{label} source window {idx}]\n{chunk}",
                "line_start": 0,
                "line_end": 0,
                "first_ts": None,
                "last_ts": None,
                "support_only": True,
                "source_families": [family],
            })
    return windows


def _transcript_sample(messages: list[dict], max_chars: int = 18000) -> str:
    support = [m for m in messages if _is_import_support_message(m)]
    history = [m for m in messages if not _is_import_support_message(m)]
    full = "\n".join(_format_import_message_line(m) for m in messages if _format_import_message_line(m))
    if len(full) <= max_chars:
        return full

    parts: list[str] = []
    support_budget = min(7000, max(2000, max_chars // 3))
    support_text = _source_briefing_text(support, support_budget)
    if support_text:
        parts.append("[Onboarding support material]\n" + support_text)
    history_budget = max(2000, max_chars - sum(len(p) + 2 for p in parts))
    history_text = _stratified_history_sample(history, history_budget)
    if history_text:
        parts.append(history_text)
    return "\n\n".join(parts)[:max_chars].strip()


def _transcript_extraction_windows(
    messages: list[dict],
    *,
    max_chars: int = 18000,
    max_windows: int = 8,
) -> list[str]:
    return [w["text"] for w in _build_transcript_windows(messages, max_chars=max_chars, max_windows=max_windows)]


def _select_evenly(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    idxs = {
        round(i * (len(items) - 1) / max(limit - 1, 1))
        for i in range(limit)
    }
    return [items[i] for i in sorted(idxs)]


def _build_transcript_windows(
    messages: list[dict],
    *,
    max_chars: int = 18000,
    max_windows: int | None = None,
    overlap_lines: int = 8,
) -> list[dict]:
    support = [m for m in messages if _is_import_support_message(m)]
    history = [m for m in messages if not _is_import_support_message(m)]
    support_windows = _support_source_windows(support, max_chars=max_chars)
    support_text = _source_briefing_text(support, min(7000, max_chars // 3))
    history_lines = [
        {
            "line": _format_import_message_line(m),
            "ts": m.get("ts"),
            "source": _import_source_family(str(m.get("source") or "history_import")),
        }
        for m in history
    ]
    history_lines = [item for item in history_lines if item["line"]]

    if not history_lines:
        windows = support_windows
        if not windows:
            only = _transcript_sample(support, max_chars=max_chars)
            windows = [{
                "id": "support-1",
                "index": 1,
                "total": 1,
                "text": only,
                "line_start": 0,
                "line_end": 0,
                "first_ts": None,
                "last_ts": None,
                "support_only": True,
                "source_families": sorted({_import_source_family(str(m.get("source") or "")) for m in support}),
            }] if only else []
        total_support = len(windows)
        for idx, window in enumerate(windows, start=1):
            window["index"] = idx
            window["total"] = total_support
        return windows

    prefix = ("[Global onboarding source briefing]\n" + support_text + "\n\n") if support_text else ""
    line_budget = max(4000, max_chars - len(prefix) - 80)
    chunks: list[dict] = []
    current: list[dict] = []
    current_start = 0
    total = 0
    for idx, item in enumerate(history_lines):
        line = item["line"]
        if current and total + len(line) + 1 > line_budget:
            chunks.append({
                "line_start": current_start,
                "line_end": current_start + len(current) - 1,
                "items": current,
            })
            overlap = current[-overlap_lines:] if overlap_lines > 0 else []
            current = list(overlap)
            current_start = max(0, idx - len(current))
            total = sum(len(x["line"]) + 1 for x in current)
        current.append(item)
        total += len(line) + 1
    if current:
        chunks.append({
            "line_start": current_start,
            "line_end": current_start + len(current) - 1,
            "items": current,
        })

    history_window_budget = None
    if max_windows is not None:
        history_window_budget = max(1, max_windows - len(support_windows))
        if len(chunks) > history_window_budget:
            chunks = _select_evenly(chunks, history_window_budget)

    windows: list[dict] = []
    total_windows = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        first_ts = next((x.get("ts") for x in chunk["items"] if x.get("ts")), None)
        last_ts = next((x.get("ts") for x in reversed(chunk["items"]) if x.get("ts")), None)
        body = "\n".join(x["line"] for x in chunk["items"])
        windows.append({
            "id": f"window-{idx:03d}",
            "index": idx,
            "total": total_windows,
            "text": f"{prefix}[Chat history window {idx}/{total_windows}]\n{body}".strip(),
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "first_ts": first_ts,
            "last_ts": last_ts,
            "support_only": False,
            "source_families": [_HISTORY_SOURCE],
        })
    combined = support_windows + windows
    total_combined = len(combined)
    for idx, window in enumerate(combined, start=1):
        window["index"] = idx
        window["total"] = total_combined
    return combined


def _history_span_days(messages: list[dict]) -> int:
    dates: list[date] = []
    for msg in messages:
        try:
            ts = msg.get("ts")
            if ts:
                dates.append(datetime.fromtimestamp(float(ts)).date())
        except Exception:
            pass
    if len(dates) < 2:
        return 0
    return max(0, (max(dates) - min(dates)).days)


_HISTORY_IMPORT_TIER_CONFIG = {
    "small": {
        "label": "small",
        "initial_windows": 8,
        "total_windows": 8,
        "story": 4,
        "about_me": 8,
        "ta_thinking": 0,
        "total": 12,
        "chat_ready_cards": 2,
        "background": False,
    },
    "medium": {
        "label": "medium",
        "initial_windows": 24,
        "total_windows": 24,
        "story": 12,
        "about_me": 28,
        "ta_thinking": 2,
        "total": 42,
        "chat_ready_cards": 8,
        "background": False,
    },
    "large": {
        "label": "large",
        "initial_windows": 36,
        "total_windows": 64,
        "story": 24,
        "about_me": 56,
        "ta_thinking": 6,
        "total": 86,
        "chat_ready_cards": 20,
        "background": True,
    },
    "ultra": {
        "label": "ultra",
        "initial_windows": 36,
        "total_windows": 96,
        "story": 32,
        "about_me": 78,
        "ta_thinking": 10,
        "total": 120,
        "chat_ready_cards": 20,
        "background": True,
    },
}


def _history_import_profile(
    history_messages: list[dict],
    support_messages: list[dict],
    *,
    content_chars: int | None = None,
) -> dict:
    message_count = len(history_messages)
    support_chars = sum(len(str(m.get("content") or "")) for m in support_messages)
    history_chars = content_chars if content_chars is not None else sum(len(str(m.get("content") or "")) for m in history_messages)
    span_days = _history_span_days(history_messages)
    if message_count >= 250_000 or history_chars >= 10_000_000 or span_days >= 1095:
        tier = "ultra"
    elif message_count >= 50_000 or history_chars >= 2_000_000 or span_days >= 365:
        tier = "large"
    elif message_count >= 5_000 or history_chars >= 200_000 or span_days >= 90:
        tier = "medium"
    else:
        tier = "small"
    return {
        "tier": tier,
        "message_count": message_count,
        "support_count": len(support_messages),
        "history_chars": int(history_chars),
        "support_chars": support_chars,
        "span_days": span_days,
        **_HISTORY_IMPORT_TIER_CONFIG[tier],
    }


_GENERIC_IMPORT_TITLE_RE = re.compile(
    r"^(?:导入(?:片段|原话|的个人细节|的事件)|imported\s+(?:exchange|quote|user detail|event|fragment|segment))\s*\d*$",
    re.IGNORECASE,
)
_LOW_VALUE_IMPORT_PATTERNS = [
    re.compile(r"^\s*(?:请|帮我|麻烦|can you|could you|please)\s*.{0,80}(?:介绍|解释|列出|生成|写|改写|优化|summarize|explain|write|list)", re.IGNORECASE),
    re.compile(r"^\s*(?:什么是|有哪些|如何|怎么|what is|what are|how to|how do i)\b", re.IGNORECASE),
    re.compile(r"^(?:继续|还有吗|再举例一些|more|continue|[0-9]+)$", re.IGNORECASE),
    re.compile(r"i'?m sorry,?\s+i don'?t understand", re.IGNORECASE),
]


def _normalize_card_similarity_text(text: str) -> str:
    raw = re.sub(r"\s+", "", str(text or "").lower())
    raw = re.sub(r"[，。！？、,.!?;:：；\"'“”‘’（）()\[\]{}<>《》]", "", raw)
    return raw[:260]


def _memory_similarity_tokens(text: str) -> set[str]:
    raw = str(text or "").lower()
    latin = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", raw))
    cjk = re.findall(r"[\u4e00-\u9fff]", raw)
    grams = {''.join(cjk[idx:idx + 2]) for idx in range(max(0, len(cjk) - 1))}
    if len(cjk) <= 3:
        grams.update(cjk)
    return {tok for tok in latin.union(grams) if tok}


def _token_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


_SENSITIVE_IDENTITY_CLAIM_PATTERNS = [
    re.compile(r"(?:真实姓名|真实名字|本名|法定姓名|身份证|证件号|住址|家庭住址)"),
    re.compile(r"\b(?:real|legal)\s+name\b", re.IGNORECASE),
    re.compile(r"\b(?:id|passport|social security)\s+(?:number|no\.?)\b", re.IGNORECASE),
    re.compile(r"\b(?:home|residential)\s+address\b", re.IGNORECASE),
]


def _looks_like_sensitive_identity_claim(text: str) -> bool:
    raw = str(text or "")
    return any(pattern.search(raw) for pattern in _SENSITIVE_IDENTITY_CLAIM_PATTERNS)


def _looks_like_low_value_import_card(title: str, desc: str, mem_type: str) -> bool:
    clean_title = str(title or "").strip()
    clean_desc = str(desc or "").strip()
    joined = clean_title + "\n" + clean_desc
    if _GENERIC_IMPORT_TITLE_RE.match(clean_title):
        return True
    if len(clean_desc) < 8:
        return True
    if any(p.search(joined) or p.search(clean_desc) for p in _LOW_VALUE_IMPORT_PATTERNS):
        return True
    if mem_type in {"fact", "event"}:
        normalized = _normalize_card_similarity_text(clean_desc)
        if len(normalized) < 12:
            return True
    return False


def _sort_memory_cards_newest_first(cards: list[dict]) -> list[dict]:
    def sort_key(card: dict) -> tuple[int, str]:
        raw_date = str(card.get("occurred_at") or "")[:10]
        parsed = identity_service._parse_iso_calendar_date(raw_date)
        ordinal = parsed.toordinal() if parsed else 0
        return ordinal, str(card.get("created_at") or "")

    return sorted(cards, key=sort_key, reverse=True)


_IMPORT_CANDIDATE_TYPES = {
    "user_fact",
    "preference",
    "boundary",
    "relationship_event",
    "emotional_pattern",
    "communication_style",
    "conflict_repair",
    "ai_character",
    "external_memory",
}
_IMPORT_CANDIDATE_SUBJECTS = {"user", "ai", "relationship"}


def _candidate_type_from_memory_type(mem_type: str) -> str:
    if mem_type in {"moment", "quote"}:
        return "relationship_event"
    if mem_type == "insight":
        return "emotional_pattern"
    if mem_type == "reflection":
        return "ai_character"
    return "user_fact"


def _coerce_import_candidates(
    raw,
    relationship_start: date,
    *,
    window_id: str = "",
    source_families: list[str] | None = None,
) -> list[dict]:
    if isinstance(raw, dict):
        raw_items = raw.get("candidates")
        if raw_items is None:
            raw_items = raw.get("memories") or raw.get("cards") or raw.get("items") or []
    else:
        raw_items = raw
    if not isinstance(raw_items, list):
        return []

    out: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        legacy_type = str(item.get("type") or "").strip().lower()
        cand_type = str(item.get("candidate_type") or item.get("kind") or "").strip().lower()
        if not cand_type and legacy_type:
            cand_type = _candidate_type_from_memory_type(legacy_type)
        if cand_type not in _IMPORT_CANDIDATE_TYPES:
            cand_type = "user_fact"
        subject = str(item.get("subject") or "").strip().lower()
        if subject not in _IMPORT_CANDIDATE_SUBJECTS:
            subject = "ai" if cand_type == "ai_character" else ("relationship" if cand_type in {"relationship_event", "conflict_repair", "emotional_pattern"} else "user")
        title = str(item.get("title") or "").strip()[:120]
        summary = str(item.get("summary") or item.get("description") or item.get("content") or "").strip()
        summary = _clean_import_memory_text(summary, max_chars=1400)
        if not summary or _looks_like_low_value_import_card(title, summary, legacy_type or "fact"):
            continue
        if _looks_like_import_artifact(summary) or _looks_like_import_artifact(title):
            continue
        quotes_raw = item.get("evidence_quotes") or item.get("quotes") or item.get("quote") or item.get("her_quote") or []
        if isinstance(quotes_raw, str):
            quotes = [quotes_raw]
        elif isinstance(quotes_raw, list):
            quotes = [str(q).strip() for q in quotes_raw if str(q).strip()]
        else:
            quotes = []
        quotes = [
            _clean_import_memory_text(q, max_chars=360)
            for q in quotes
        ]
        quotes = [q for q in quotes if q and not _looks_like_import_artifact(q)]
        signals_raw = item.get("importance_signals") or item.get("signals") or []
        if isinstance(signals_raw, str):
            signals = [signals_raw]
        elif isinstance(signals_raw, list):
            signals = [str(s).strip().lower() for s in signals_raw if str(s).strip()]
        else:
            signals = []
        try:
            confidence = float(item.get("confidence", 0.55))
        except Exception:
            confidence = 0.55
        first_seen = str(item.get("first_seen_at") or item.get("occurred_at") or item.get("date") or "").strip()
        last_seen = str(item.get("last_seen_at") or first_seen).strip()
        if not identity_service._parse_iso_calendar_date(first_seen):
            first_seen = relationship_start.isoformat()
        if not identity_service._parse_iso_calendar_date(last_seen):
            last_seen = first_seen
        families = sorted({
            _import_source_family(str(source))
            for source in (source_families or [])
            if str(source or "").strip()
        })
        if not families:
            families = [_HISTORY_SOURCE]
        out.append({
            "id": f"cand_{uuid.uuid4().hex[:12]}",
            "candidate_type": cand_type,
            "subject": subject,
            "title": title,
            "summary": summary[:1400],
            "evidence_quotes": quotes[:3],
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "importance_signals": sorted(set(signals))[:8],
            "confidence": max(0.0, min(confidence, 1.0)),
            "source_ids": [str(s)[:160] for s in item.get("source_ids", [])] if isinstance(item.get("source_ids"), list) else [],
            "chunk_ids": sorted(set([window_id] + ([str(c) for c in item.get("chunk_ids", [])] if isinstance(item.get("chunk_ids"), list) else []))),
            "source_families": families,
        })
    return out


def _candidate_score(candidate: dict) -> float:
    ctype = str(candidate.get("candidate_type") or "user_fact")
    subject = str(candidate.get("subject") or "user")
    title = str(candidate.get("title") or "")
    summary = str(candidate.get("summary") or "")
    signals = set(str(s).lower() for s in candidate.get("importance_signals") or [])
    score = float(candidate.get("confidence") or 0.55) * 30.0
    score += {
        "boundary": 34,
        "relationship_event": 32,
        "emotional_pattern": 30,
        "conflict_repair": 30,
        "preference": 25,
        "user_fact": 23,
        "communication_style": 22,
        "external_memory": 21,
        "ai_character": 18,
    }.get(ctype, 15)
    if subject == "relationship":
        score += 10
    elif subject == "user":
        score += 6
    if "explicit_memory" in signals or "remembered" in signals:
        score += 18
    if "repeated" in signals or "recurring" in signals:
        score += 16
    if "emotional_peak" in signals:
        score += 15
    if "relationship_boundary" in signals or "boundary" in signals:
        score += 14
    if candidate.get("evidence_quotes"):
        score += min(10, 4 * len(candidate.get("evidence_quotes") or []))
    score += min(8, max(0, len(candidate.get("chunk_ids") or []) - 1) * 3)
    families = set(str(s) for s in candidate.get("source_families") or [])
    if _MEMORY_SUMMARY_SOURCE in families:
        score += 20
    if _USER_PROFILE_SOURCE in families and subject == "user":
        score += 12
    if _AI_PERSONA_SOURCE in families:
        if subject == "ai" or ctype == "ai_character":
            score += 18
        elif subject == "user":
            score -= 18
    if any(p.search(summary) for p in _LOW_VALUE_IMPORT_PATTERNS):
        score -= 45
    if _looks_like_import_artifact(summary):
        score -= 80
    if (
        subject == "user"
        and ctype in {"user_fact", "external_memory"}
        and _looks_like_sensitive_identity_claim(title + "\n" + summary)
        and not candidate.get("evidence_quotes")
        and not ({"explicit_memory", "repeated", "remembered"} & signals)
    ):
        score -= 55
    return score


def _candidate_has_strong_evidence(candidate: dict) -> bool:
    signals = set(str(s).lower() for s in candidate.get("importance_signals") or [])
    return bool(candidate.get("evidence_quotes")) or bool({"explicit_memory", "repeated", "remembered"} & signals)


def _candidate_should_skip(candidate: dict) -> bool:
    title = str(candidate.get("title") or "")
    summary = str(candidate.get("summary") or "")
    ctype = str(candidate.get("candidate_type") or "")
    subject = str(candidate.get("subject") or "")
    mem_type = _candidate_memory_type(candidate)
    if _looks_like_low_value_import_card(title, summary, mem_type):
        return True
    if _looks_like_import_artifact(title) or _looks_like_import_artifact(summary):
        return True
    if (
        subject == "user"
        and ctype in {"user_fact", "external_memory"}
        and _looks_like_sensitive_identity_claim(title + "\n" + summary)
        and not _candidate_has_strong_evidence(candidate)
    ):
        return True
    return False


def _candidate_mergeable(existing: dict, candidate: dict) -> bool:
    if existing.get("subject") != candidate.get("subject"):
        return False
    existing_type = _candidate_memory_type(existing)
    candidate_type = _candidate_memory_type(candidate)
    if memory_service.TAB_FOR_TYPE.get(existing_type, "about_me") != memory_service.TAB_FOR_TYPE.get(candidate_type, "about_me"):
        return False
    norm = existing.get("_norm", "")
    cand_norm = candidate.get("_norm", "")
    if norm and cand_norm and (
        norm == cand_norm
        or norm[:90] == cand_norm[:90]
        or norm in cand_norm
        or cand_norm in norm
    ):
        return True
    title_sim = _token_jaccard(
        _memory_similarity_tokens(existing.get("title") or ""),
        _memory_similarity_tokens(candidate.get("title") or ""),
    )
    body_sim = _token_jaccard(
        existing.get("_tokens") or set(),
        candidate.get("_tokens") or set(),
    )
    return body_sim >= 0.50 or (body_sim >= 0.40 and title_sim >= 0.25)


def _merge_import_candidates(candidates: list[dict]) -> list[dict]:
    clusters: list[dict] = []
    for cand in sorted(candidates, key=_candidate_score, reverse=True):
        if _candidate_should_skip(cand):
            continue
        norm = _normalize_card_similarity_text(cand.get("summary", ""))
        if not norm:
            continue
        cand = dict(cand)
        cand["_norm"] = norm
        cand["_tokens"] = _memory_similarity_tokens(
            " ".join([
                str(cand.get("title") or ""),
                str(cand.get("summary") or ""),
                " ".join(str(q) for q in cand.get("evidence_quotes") or []),
            ])
        )
        merged = False
        for cluster in clusters:
            if _candidate_mergeable(cluster, cand):
                cluster["evidence_quotes"] = list(dict.fromkeys((cluster.get("evidence_quotes") or []) + (cand.get("evidence_quotes") or [])))[:4]
                cluster["source_ids"] = sorted(set((cluster.get("source_ids") or []) + (cand.get("source_ids") or [])))
                cluster["chunk_ids"] = sorted(set((cluster.get("chunk_ids") or []) + (cand.get("chunk_ids") or [])))
                cluster["source_families"] = sorted(set((cluster.get("source_families") or []) + (cand.get("source_families") or [])))
                cluster["importance_signals"] = sorted(set((cluster.get("importance_signals") or []) + (cand.get("importance_signals") or [])))[:10]
                cluster["confidence"] = max(float(cluster.get("confidence") or 0), float(cand.get("confidence") or 0))
                cluster["_tokens"] = (cluster.get("_tokens") or set()).union(cand.get("_tokens") or set())
                cluster["score"] = _candidate_score(cluster)
                merged = True
                break
        if not merged:
            cluster = dict(cand)
            cluster["score"] = _candidate_score(cluster)
            clusters.append(cluster)
    clusters.sort(key=lambda c: float(c.get("score") or 0), reverse=True)
    for cluster in clusters:
        cluster.pop("_norm", None)
        cluster.pop("_tokens", None)
    return clusters


def _candidate_memory_type(candidate: dict) -> str:
    ctype = str(candidate.get("candidate_type") or "")
    if ctype in {"relationship_event", "conflict_repair"}:
        return "moment"
    if ctype == "communication_style" and candidate.get("evidence_quotes"):
        return "quote"
    if ctype in {"emotional_pattern", "ai_character"}:
        return "insight"
    if ctype in {"boundary", "preference", "user_fact", "external_memory"}:
        return "fact"
    return "event"


def _candidate_title(candidate: dict, mem_type: str, language: str) -> str:
    title = str(candidate.get("title") or "").strip()
    if title and not _GENERIC_IMPORT_TITLE_RE.match(title):
        return title[:120]
    summary = str(candidate.get("summary") or "")
    if str(language).startswith("zh"):
        prefix = {
            "moment": "关系片段",
            "quote": "原话",
            "fact": "关于用户",
            "event": "用户事件",
            "insight": "TA 的理解",
            "reflection": "TA 在想",
        }.get(mem_type, "记忆")
        natural = _natural_import_title(summary, mem_type, language)
        if natural and natural != "导入的真实片段":
            return natural[:120]
        return prefix
    natural = _natural_import_title(summary, mem_type, language)
    return natural[:120] or "Memory"


def _render_candidates_to_memory_cards(
    candidates: list[dict],
    relationship_start: date,
    targets: dict,
    *,
    language: str = "en",
    max_cards: int | None = None,
) -> list[dict]:
    merged = _merge_import_candidates(candidates)
    quotas = {
        "story": max(0, int(targets.get("story", 1))),
        "about_me": max(0, int(targets.get("about_me", 1))),
        "ta_thinking": max(0, int(targets.get("ta_thinking", 0))),
    }
    target_total = int(targets.get("total") or sum(quotas.values()))
    configured_cap = int(max_cards) if max_cards is not None else target_total
    extra_allowance = max(6, min(30, max(target_total, configured_cap) // 4))
    emergency_total = max(target_total, configured_cap) + extra_allowance
    cards: list[dict] = []
    used_candidates: set[str] = set()

    def tab_for_candidate(c: dict) -> tuple[str, str]:
        mem_type = _candidate_memory_type(c)
        return mem_type, memory_service.TAB_FOR_TYPE.get(mem_type, "about_me")

    def append_card(c: dict, mem_type: str) -> None:
        if _candidate_should_skip(c):
            return
        cid = str(c.get("id") or "")
        used_candidates.add(cid)
        occurred = str(c.get("first_seen_at") or "").strip()
        if not identity_service._parse_iso_calendar_date(occurred):
            occurred = relationship_start.isoformat()
        body = {
            "type": mem_type,
            "title": _candidate_title(c, mem_type, language),
            "description": str(c.get("summary") or "")[:1200],
            "occurred_at": occurred,
            "context": (
                f"distilled from {len(c.get('chunk_ids') or [])} source window(s); "
                f"sources={','.join(str(s) for s in (c.get('source_families') or []))}; "
                f"score={float(c.get('score') or _candidate_score(c)):.1f}"
            ),
        }
        quotes = c.get("evidence_quotes") or []
        if quotes:
            body["her_quote"] = str(quotes[0])[:500]
        cards.append(body)

    for tab in ("story", "about_me", "ta_thinking"):
        for cand in merged:
            if len(cards) >= target_total:
                break
            cid = str(cand.get("id") or "")
            if cid in used_candidates:
                continue
            mem_type, cand_tab = tab_for_candidate(cand)
            if cand_tab != tab:
                continue
            if quotas.get(tab, 0) <= 0:
                continue
            append_card(cand, mem_type)
            quotas[tab] -= 1

    for cand in merged:
        if len(cards) >= emergency_total:
            break
        if len(cards) >= target_total and _candidate_score(cand) < 58:
            continue
        cid = str(cand.get("id") or "")
        if cid in used_candidates:
            continue
        mem_type, _ = tab_for_candidate(cand)
        append_card(cand, mem_type)
    return _sort_memory_cards_newest_first(_dedupe_memory_cards(cards))


def _memory_candidate_extraction_prompt(
    window: dict,
    *,
    idx: int,
    total: int,
    per_window_target: int,
    relationship_start: date,
    language: str,
) -> str:
    sample = str(window.get("text") or "")
    source_families = ", ".join(str(s) for s in (window.get("source_families") or [])) or "history_import"
    return (
        "Distill durable Feedling onboarding memory candidates from this material window. "
        "This is pass 1 of a two-pass pipeline: output candidates only, not final Memory Garden cards. "
        "Return JSON only in this exact shape: "
        "{\"candidates\":[{\"candidate_type\":\"user_fact|preference|boundary|relationship_event|emotional_pattern|communication_style|conflict_repair|ai_character|external_memory\","
        "\"subject\":\"user|ai|relationship\",\"title\":\"optional natural short title\",\"summary\":\"durable memory candidate\","
        "\"evidence_quotes\":[\"short exact quote if available\"],\"first_seen_at\":\"YYYY-MM-DD\",\"last_seen_at\":\"YYYY-MM-DD\","
        "\"importance_signals\":[\"explicit_memory|repeated|emotional_peak|relationship_boundary|future_utility\"],\"confidence\":0.0}],\"why_empty\":\"optional\"}. "
        f"Return up to {per_window_target} candidates, fewer or zero if this window is generic task Q&A, assistant filler, raw JSON metadata, or has no durable relationship/user/AI-character signal. "
        "High-value candidates include stable user facts, preferences, boundaries, relationship milestones, emotional patterns, conflict/repair patterns, repeated themes, and AI character/voice definitions. "
        "Do not preserve ordinary knowledge questions, one-off task instructions, product copy, code/debug chatter, upload wrappers, file delimiters, or raw JSON keys. "
        "Source contract: AI Persona materials describe the AI companion and should mainly produce subject=ai / ai_character candidates; User Profile describes the user and should mainly produce subject=user candidates; "
        "Memory Summary is a high-recall migration source, so split every meaningful durable detail into candidates instead of returning empty just because the material is already summarized; "
        "Chat History is evidence for lived exchanges and relationship patterns. "
        "Never treat User Profile facts as the AI companion's identity, name, or self-description. "
        "Do not make one candidate per message; merge repeated details inside this window. "
        f"{_language_instruction(language)} "
        f"If dates are unclear, use {relationship_start.isoformat()}."
        f"\n\nWindow id: {window.get('id', idx)} ({idx}/{total})\nSource families: {source_families}\nMaterial:\n{sample}"
    )


def _split_candidate_retry_windows(window: dict, max_chars: int = 8500) -> list[dict]:
    text = str(window.get("text") or "")
    if len(text) <= max_chars:
        return []
    parts: list[dict] = []
    for part_idx in range(0, len(text), max_chars):
        chunk = text[part_idx:part_idx + max_chars].strip()
        if not chunk:
            continue
        copy = dict(window)
        copy["id"] = f"{window.get('id') or 'window'}:retry-{len(parts) + 1}"
        copy["text"] = chunk
        parts.append(copy)
        if len(parts) >= 4:
            break
    return parts


def _repair_candidate_json_with_provider(
    provider: provider_client.ProviderConfig,
    raw_reply: str,
    *,
    relationship_start: date,
    window_id: str,
    language: str,
    source_families: list[str] | None = None,
) -> list[dict]:
    prompt = (
        "The previous model response was not valid JSON for Feedling memory candidate extraction. "
        "Convert only the durable memory candidates in that response into this exact JSON schema: "
        "{\"candidates\":[{\"candidate_type\":\"user_fact|preference|boundary|relationship_event|emotional_pattern|communication_style|conflict_repair|ai_character|external_memory\","
        "\"subject\":\"user|ai|relationship\",\"title\":\"optional natural short title\",\"summary\":\"durable memory candidate\","
        "\"evidence_quotes\":[\"short exact quote if available\"],\"first_seen_at\":\"YYYY-MM-DD\",\"last_seen_at\":\"YYYY-MM-DD\","
        "\"importance_signals\":[\"explicit_memory|repeated|emotional_peak|relationship_boundary|future_utility\"],\"confidence\":0.0}]}. "
        "Return JSON only. Drop raw JSON metadata, generic tasks, and filler. "
        f"{_language_instruction(language)} "
        f"If dates are unclear, use {relationship_start.isoformat()}.\n\nPrevious response:\n{str(raw_reply or '')[:12000]}"
    )
    result = provider_client.chat_completion(
        provider,
        [
            {"role": "system", "content": "You repair malformed JSON into strict Feedling memory candidate JSON."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1800,
        temperature=0.0,
        timeout=35.0,
    )
    return _coerce_import_candidates(
        core_util._json_from_model_text(result["reply"]),
        relationship_start,
        window_id=window_id,
        source_families=source_families,
    )


def _extract_memory_candidates_with_provider(
    provider: provider_client.ProviderConfig,
    windows: list[dict],
    relationship_start: date,
    *,
    per_window_target: int,
    language: str = "en",
    on_progress=None,
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    all_candidates: list[dict] = []
    for idx, window in enumerate(windows, start=1):
        source_families = [str(s) for s in (window.get("source_families") or [])]
        prompt = _memory_candidate_extraction_prompt(
            window,
            idx=idx,
            total=len(windows),
            per_window_target=per_window_target,
            relationship_start=relationship_start,
            language=language,
        )
        reply = ""
        try:
            result = provider_client.chat_completion(
                provider,
                [
                    {"role": "system", "content": "You are a strict JSON candidate extraction engine for long-memory distillation."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2200,
                temperature=0.1,
                timeout=60.0,
            )
            reply = str(result.get("reply") or "")
            parsed = core_util._json_from_model_text(reply)
            all_candidates.extend(_coerce_import_candidates(
                parsed,
                relationship_start,
                window_id=str(window.get("id") or idx),
                source_families=source_families,
            ))
        except Exception as e:
            repaired = False
            if reply:
                try:
                    repaired_candidates = _repair_candidate_json_with_provider(
                        provider,
                        reply,
                        relationship_start=relationship_start,
                        window_id=str(window.get("id") or idx),
                        language=language,
                        source_families=source_families,
                    )
                    all_candidates.extend(repaired_candidates)
                    warnings.append(f"provider_candidate_json_repaired_window_{idx}:{len(repaired_candidates)}")
                    repaired = True
                except Exception as repair_e:
                    warnings.append(f"provider_candidate_json_repair_failed_window_{idx}:{type(repair_e).__name__}:{str(repair_e)[:120]}")
            if not repaired:
                retry_candidates: list[dict] = []
                for part_idx, retry_window in enumerate(_split_candidate_retry_windows(window), start=1):
                    retry_prompt = _memory_candidate_extraction_prompt(
                        retry_window,
                        idx=part_idx,
                        total=1,
                        per_window_target=max(2, per_window_target // 2),
                        relationship_start=relationship_start,
                        language=language,
                    )
                    try:
                        retry_result = provider_client.chat_completion(
                            provider,
                            [
                                {"role": "system", "content": "You are a strict JSON candidate extraction engine for long-memory distillation."},
                                {"role": "user", "content": retry_prompt},
                            ],
                            max_tokens=1800,
                            temperature=0.1,
                            timeout=45.0,
                        )
                        retry_parsed = core_util._json_from_model_text(retry_result["reply"])
                        retry_candidates.extend(_coerce_import_candidates(
                            retry_parsed,
                            relationship_start,
                            window_id=str(retry_window.get("id") or idx),
                            source_families=[str(s) for s in (retry_window.get("source_families") or source_families)],
                        ))
                    except Exception as retry_e:
                        warnings.append(f"provider_candidate_retry_failed_window_{idx}_part_{part_idx}:{type(retry_e).__name__}:{str(retry_e)[:100]}")
                if retry_candidates:
                    all_candidates.extend(retry_candidates)
                    warnings.append(f"provider_candidate_retry_split_window_{idx}:{len(retry_candidates)}")
                else:
                    warnings.append(f"provider_candidate_extraction_failed_window_{idx}:{type(e).__name__}:{str(e)[:160]}")
        if on_progress:
            on_progress(idx, len(windows), len(all_candidates))
    return all_candidates, warnings


def _coerce_memory_cards(raw, relationship_start: date) -> list[dict]:
    if isinstance(raw, dict):
        raw_items = raw.get("memories") or raw.get("cards") or raw.get("items") or []
    else:
        raw_items = raw
    if not isinstance(raw_items, list):
        return []

    cards: list[dict] = []
    allowed = {"moment", "quote", "fact", "event"}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        mem_type = str(item.get("type") or "fact").strip().lower()
        if mem_type not in allowed:
            mem_type = "fact"
        title = str(item.get("title") or "").strip()[:120]
        desc = str(item.get("description") or item.get("content") or "").strip()[:1200]
        if not desc:
            continue
        if not title:
            title = desc[:72]
        if _looks_like_low_value_import_card(title, desc, mem_type):
            continue
        quote = str(item.get("her_quote") or item.get("quote") or "").strip()
        context = str(item.get("context") or "").strip()
        if any(_looks_like_import_artifact(value) for value in (title, desc, quote, context) if value):
            continue
        occurred = str(item.get("occurred_at") or item.get("date") or "").strip()
        if not identity_service._parse_iso_calendar_date(occurred):
            occurred = relationship_start.isoformat()
        card = {
            "type": mem_type,
            "title": title,
            "description": desc,
            "occurred_at": occurred,
        }
        if quote:
            card["her_quote"] = quote[:500]
        if context:
            card["context"] = context[:600]
        cards.append(card)
    return cards


def _dedupe_memory_cards(cards: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    seen_text: set[str] = set()
    for card in cards:
        title = str(card.get("title") or "")
        desc = str(card.get("description") or "")
        mem_type = str(card.get("type") or "")
        if _looks_like_low_value_import_card(title, desc, mem_type):
            continue
        key = re.sub(
            r"\s+",
            " ",
            (mem_type + "|" + title + "|" + desc[:160]).lower(),
        )
        if key in seen:
            continue
        norm = _normalize_card_similarity_text(desc)
        if norm:
            if any(norm == prev or norm[:80] == prev[:80] or norm in prev or prev in norm for prev in seen_text):
                continue
            seen_text.add(norm)
        seen.add(key)
        out.append(card)
    return out


def _natural_import_title(content: str, mem_type: str, language: str) -> str:
    clean = re.sub(r"\s+", " ", str(content or "")).strip()
    clean = re.sub(r"^(User|Assistant|用户|助手|AI|TA)[:：]\s*", "", clean)
    if not clean:
        return "新的记忆" if str(language).startswith("zh") else "New memory"
    if str(language).startswith("zh"):
        compact = re.sub(r"[。！？].*$", "", clean)
        return compact[:24] or "导入的真实片段"
    words = clean.split()
    return " ".join(words[:8])[:72] or "Imported memory"


def _fallback_chunks_from_message(msg: dict, max_chunks: int = 18) -> list[str]:
    content = _clean_import_memory_text(str(msg.get("content") or ""), max_chars=30000)
    if not content:
        return []
    if ":" in content[:240]:
        _, body = content.split(":", 1)
        content = body.strip() or content
    parts = [
        part.strip(" \t\r\n-•*0123456789.、)）")
        for part in re.split(r"(?:\n{2,}|\n\s*(?:[-*•]|\d+[.)、）])\s+)", content)
    ]
    chunks: list[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = _clean_import_memory_text(part, max_chars=900)
        if len(clean) < 8:
            continue
        norm = _normalize_card_similarity_text(clean)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        chunks.append(clean)
        if len(chunks) >= max_chunks:
            break
    if not chunks and content:
        chunks = [content[:900]]
    return chunks


def _fallback_memory_cards(
    messages: list[dict],
    relationship_start: date,
    *,
    story_needed: int,
    about_needed: int,
    language: str = "en",
) -> list[dict]:
    cards: list[dict] = []
    story_pool: list[dict] = []
    about_pool: list[dict] = []
    for msg in messages:
        content = _clean_import_memory_text(str(msg.get("content") or ""))
        if not content:
            continue
        clean_msg = dict(msg)
        clean_msg["content"] = content
        family = _import_source_family(str(clean_msg.get("source") or clean_msg.get("source_family") or ""))
        if family in {_HISTORY_SOURCE, _MEMORY_SUMMARY_SOURCE, _FRESH_START_SOURCE}:
            story_pool.append(clean_msg)
        if family in {_HISTORY_SOURCE, _MEMORY_SUMMARY_SOURCE, _USER_PROFILE_SOURCE, _FRESH_START_SOURCE}:
            about_pool.append(clean_msg)
    if not story_pool and not about_pool and any(
        _import_source_family(str(m.get("source") or m.get("source_family") or "")) == _FRESH_START_SOURCE
        for m in messages
    ):
        fallback_text = "从空白状态开始。" if str(language).startswith("zh") else "Fresh start with IO."
        fresh = {"role": "user", "content": fallback_text, "ts": None, "source": _FRESH_START_SOURCE}
        story_pool = [fresh]
        about_pool = [fresh]

    def expand(pool: list[dict], limit: int) -> list[tuple[dict, str]]:
        expanded: list[tuple[dict, str]] = []
        for msg in pool:
            family = _import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
            chunk_limit = max(1, limit - len(expanded))
            chunks = _fallback_chunks_from_message(
                msg,
                max_chunks=max(chunk_limit, 4 if family == _MEMORY_SUMMARY_SOURCE else 1),
            )
            for chunk in chunks:
                expanded.append((msg, chunk))
                if len(expanded) >= limit:
                    return expanded
        return expanded

    idx = 0
    story_types = ["moment", "quote"]
    story_items = expand(story_pool, story_needed)
    while story_needed > 0 and idx < len(story_items):
        msg, content = story_items[idx]
        mem_type = story_types[idx % len(story_types)]
        title = _natural_import_title(content, mem_type, language)
        cards.append({
            "type": mem_type,
            "title": title,
            "description": content[:900],
            "her_quote": content[:360] if mem_type == "quote" and msg.get("role") == "user" else "",
            "occurred_at": _message_iso_date(msg, relationship_start),
            "context": f"fallback source={_import_source_family(str(msg.get('source') or msg.get('source_family') or ''))}",
        })
        idx += 1
        story_needed -= 1

    idx = 0
    about_types = ["fact", "event"]
    about_items = expand(about_pool, about_needed)
    while about_needed > 0 and idx < len(about_items):
        msg, content = about_items[idx]
        mem_type = about_types[idx % len(about_types)]
        title = _natural_import_title(content, mem_type, language)
        cards.append({
            "type": mem_type,
            "title": title,
            "description": content[:900],
            "occurred_at": _message_iso_date(msg, relationship_start),
            "context": f"fallback source={_import_source_family(str(msg.get('source') or msg.get('source_family') or ''))}",
        })
        idx += 1
        about_needed -= 1
    return cards


def _import_memory_targets(
    floors: dict,
    history_messages: list[dict],
    support_messages: list[dict],
    profile: dict | None = None,
) -> dict:
    profile = profile or _history_import_profile(history_messages, support_messages)
    source_stats = _import_source_stats(support_messages + history_messages)
    ai_persona_chars = int(source_stats.get(_AI_PERSONA_SOURCE, {}).get("chars") or 0)
    user_profile_chars = int(source_stats.get(_USER_PROFILE_SOURCE, {}).get("chars") or 0)
    memory_summary_chars = int(source_stats.get(_MEMORY_SUMMARY_SOURCE, {}).get("chars") or 0)
    cfg = _HISTORY_IMPORT_TIER_CONFIG.get(str(profile.get("tier") or "small"), _HISTORY_IMPORT_TIER_CONFIG["small"])
    story = int(cfg["story"])
    about = int(cfg["about_me"])
    thinking = int(cfg["ta_thinking"])
    if len(history_messages) <= 0:
        story = 2 if memory_summary_chars else (1 if any(_import_source_family(str(m.get("source") or "")) == _FRESH_START_SOURCE for m in support_messages) else 0)
        about = 3 if (memory_summary_chars or user_profile_chars) else (1 if support_messages else 0)
        thinking = 2 if ai_persona_chars else 0
    elif str(profile.get("tier")) == "small":
        # Keep short histories compact; do not pad to the old bootstrap floors.
        story = min(story, max(2, len(history_messages) // 8 + 2))
        about = min(about, max(3, len(history_messages) // 5 + 3))
    if user_profile_chars:
        about = max(about, 3)
        about += min(5, max(1, user_profile_chars // 2500))
    if memory_summary_chars:
        story = max(story, 2)
        about = max(about, 3)
        extra = min(36, max(4, memory_summary_chars // 900))
        story += max(1, extra // 3)
        about += max(2, extra - (extra // 3))
    if ai_persona_chars:
        thinking = max(thinking, 2)
        thinking += min(6, max(0, ai_persona_chars // 3500))
    total = max(2, story + about + thinking)
    return {
        "story": max(0, story),
        "about_me": max(0, about),
        "ta_thinking": max(0, thinking),
        "total": total,
        "tier": str(profile.get("tier") or "small"),
        "initial_windows": int(cfg["initial_windows"]),
        "total_windows": int(cfg["total_windows"]),
        "chat_ready_cards": int(cfg["chat_ready_cards"]),
        "background": bool(cfg["background"]),
        "floor_reference": floors,
        "source_stats": source_stats,
    }


def _extract_memory_cards_with_provider(
    provider: provider_client.ProviderConfig,
    messages: list[dict],
    relationship_start: date,
    targets: dict,
    language: str = "en",
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    target_total = int(targets.get("story", 1)) + int(targets.get("about_me", 1))
    windows = _transcript_extraction_windows(messages, max_chars=18000, max_windows=8)
    if not windows:
        return [], ["empty_transcript_sample"]
    if len(windows) > 1:
        warnings.append(f"history_import_windows:{len(windows)}")

    all_cards: list[dict] = []
    per_window_target = max(3, min(8, (target_total + len(windows) - 1) // len(windows) + 2))
    for idx, sample in enumerate(windows, start=1):
        prompt = (
            "Extract high-signal Feedling Memory Garden cards from this onboarding material window. "
            "Large imports are processed across multiple timeline windows, so use this window's durable details "
            "without assuming it is the whole relationship. Return JSON only in this shape: "
            "{\"memories\":[{\"type\":\"moment|quote|fact|event\",\"title\":\"...\","
            "\"description\":\"...\",\"her_quote\":\"optional exact user quote\","
            "\"occurred_at\":\"YYYY-MM-DD\"}]}. "
            f"Window {idx}/{len(windows)}. Return up to {per_window_target} cards from this window, fewer or zero if the material is repetitive, generic, or not personal. "
            "moment/quote cards belong to Story and must be specific lived exchanges or exact user wording. "
            "fact/event cards belong to About me and must be durable user preferences, relationships, habits, projects, dates, or boundaries. "
            "Do not save generic encyclopedia Q&A, product-copy drafts, assistant filler, empty commands, raw JSON, file delimiters, upload wrappers, or internal field names. "
            "Do not write one card per message; merge repeated content into one stronger card. "
            "Use natural, specific titles. Never use titles like Imported exchange, Imported quote, 导入片段, 导入原话, 导入的个人细节, or 导入的事件. "
            "Character card material describes the AI companion; personal profile material describes the user. Do not confuse the two. "
            f"{_language_instruction(language)} "
            f"If dates are unclear, use {relationship_start.isoformat()}."
            "\n\nMaterial window:\n" + sample
        )
        try:
            result = provider_client.chat_completion(
                provider,
                [
                    {"role": "system", "content": "You are a strict JSON extraction engine for Feedling Memory Garden."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1800,
                temperature=0.1,
                timeout=45.0,
            )
            parsed = core_util._json_from_model_text(result["reply"])
            all_cards.extend(_coerce_memory_cards(parsed, relationship_start))
        except Exception as e:
            warnings.append(f"provider_memory_extraction_failed_window_{idx}:{type(e).__name__}:{str(e)[:160]}")
    return _dedupe_memory_cards(all_cards), warnings


def _memory_counts_for_cards(cards: list[dict]) -> dict:
    counts = {"story": 0, "about_me": 0, "ta_thinking": 0, "total": 0}
    for card in cards:
        t = str(card.get("type") or "")
        tab = memory_service.TAB_FOR_TYPE.get(t)
        counts["total"] += 1
        if tab:
            counts[tab] += 1
    return counts


def _ensure_import_memory_floors(
    cards: list[dict],
    messages: list[dict],
    relationship_start: date,
    floors: dict,
    language: str = "en",
) -> list[dict]:
    counts = _memory_counts_for_cards(cards)
    story_needed = max(0, int(floors.get("story", 1)) - counts["story"])
    about_needed = max(0, int(floors.get("about_me", 1)) - counts["about_me"])
    if story_needed or about_needed:
        cards = cards + _fallback_memory_cards(
            messages,
            relationship_start,
            story_needed=story_needed,
            about_needed=about_needed,
            language=language,
        )
    # Force the first persisted memory to anchor the relationship start date.
    if cards:
        cards[0]["occurred_at"] = relationship_start.isoformat()
    return _sort_memory_cards_newest_first(_dedupe_memory_cards(cards))


def _ensure_import_minimum_cards(
    cards: list[dict],
    messages: list[dict],
    relationship_start: date,
    *,
    min_story: int = 1,
    min_about: int = 1,
    language: str = "en",
) -> list[dict]:
    counts = _memory_counts_for_cards(cards)
    story_needed = max(0, min_story - counts["story"])
    about_needed = max(0, min_about - counts["about_me"])
    if story_needed or about_needed:
        cards = cards + _fallback_memory_cards(
            messages,
            relationship_start,
            story_needed=story_needed,
            about_needed=about_needed,
            language=language,
        )
    if cards:
        cards[0]["occurred_at"] = relationship_start.isoformat()
    return _dedupe_memory_cards(cards)


def _card_dedupe_key(card: dict) -> str:
    return "|".join([
        str(card.get("type") or ""),
        _normalize_card_similarity_text(card.get("title") or ""),
        _normalize_card_similarity_text(card.get("description") or ""),
    ])


def _new_cards_only(existing_cards: list[dict], candidate_cards: list[dict]) -> list[dict]:
    existing = {_card_dedupe_key(card) for card in existing_cards}
    out: list[dict] = []
    for card in candidate_cards:
        key = _card_dedupe_key(card)
        if not key or key in existing:
            continue
        existing.add(key)
        out.append(card)
    return out


def _moment_from_memory_card(store: UserStore, card: dict, envelope: dict) -> dict:
    now = core_util._now_iso()
    moment = {
        "v": 1,
        "id": envelope.get("id") or f"mom_{uuid.uuid4().hex[:12]}",
        "type": str(card.get("type") or "fact"),
        "occurred_at": str(card.get("occurred_at") or now),
        "created_at": now,
        "source": "history_import",
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    return moment


def _append_import_memory_cards(store: UserStore, cards: list[dict]) -> list[dict]:
    moments = memory_service._load_moments(store)
    created: list[dict] = []
    for card in _sort_memory_cards_newest_first(cards):
        mem_type = str(card.get("type") or "")
        if mem_type not in memory_service.MEMORY_TYPES:
            continue
        body = {
            "title": str(card.get("title") or "")[:120],
            "description": str(card.get("description") or "")[:1200],
            "type": mem_type,
        }
        for key in ("her_quote", "context", "linked_dimension"):
            value = str(card.get(key) or "").strip()
            if value:
                body[key] = value[:800]
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        if envelope is None:
            raise RuntimeError(f"memory_envelope_failed:{err}")
        envelope["type"] = mem_type
        envelope["occurred_at"] = str(card.get("occurred_at") or date.today().isoformat())
        envelope["source"] = "history_import"
        moments.append(_moment_from_memory_card(store, card, envelope))
        created.append(moments[-1])
    memory_service._save_moments(store, moments)
    if created:
        boot_gates._log_bootstrap_event(store, "history_import_memory_written", success=True)
    return created


def _fallback_identity_payload(memories: list[dict], days: int, language: str = "en") -> dict:
    sample_desc = ""
    if memories:
        sample_desc = str(memories[0].get("description") or memories[0].get("title") or "")
    is_zh = str(language).startswith("zh")
    desc = sample_desc[:220] or ("我还在从导入材料里学习。" if is_zh else "I am still learning from the imported history.")
    names = (
        ["细心", "稳定", "有趣", "守护感", "好奇", "直接", "温柔"]
        if is_zh else
        ["Attentive", "Steady", "Playful", "Protective", "Curious", "Direct", "Tender"]
    )
    values = [74, 68, 56, 63, 71, 59, 66]
    dimensions = [
        {
            "name": name,
            "value": values[idx],
            "description": (
                (f"根据导入材料初步估计。依据：{desc}" if is_zh else f"Estimated from imported history. Anchor: {desc}")
                if idx == 0 else
                ("根据导入的对话模式初步估计；后续会在真实对话中继续校准。" if is_zh else "Estimated from imported chat patterns; refine after live conversation.")
            ),
        }
        for idx, name in enumerate(names)
    ]
    return {
        "agent_name": "",
        "self_introduction": (
            "我已经读过你导入的材料，并先搭好了一版记忆和身份。现在我还没有名字，你可以告诉我以后该怎么称呼我。"
            if is_zh else
            "I imported the previous history and built a first version of my memory "
            "from it. I do not have a confirmed name yet, so you can tell me what "
            "you would like to call me."
        ),
        "dimensions": dimensions,
        "days_with_user": days,
        "category": "细心 · 稳定" if is_zh else "Attentive · Grounded",
        "signature": ["从材料里醒来", "继续记住你"] if is_zh else ["Built from receipts", "Ready to keep noticing"],
    }


def _sanitize_import_agent_name(name: str) -> str:
    clean = re.sub(r"\s+", " ", str(name or "")).strip()
    clean = clean.strip(" `\"'“”‘’。，,.;；:：!！?？")
    if not clean or len(clean) > 80:
        return ""
    if any(ch in clean for ch in "\n\r{}[]"):
        return ""
    labels = set(identity_service._IDENTITY_RUNTIME_LABELS)
    normalized = re.sub(r"\s+", " ", clean.lower())
    if normalized in labels:
        return ""
    if normalized.startswith(("openai/", "anthropic/", "google/", "deepseek/")):
        return ""
    if re.search(r"\b(?:api|model|runtime|provider|endpoint|assistant|agent)\b", normalized):
        return ""
    return clean[:80]


def _normalize_identity_payload(raw, memories: list[dict], days: int, language: str = "en") -> dict:
    fallback = _fallback_identity_payload(memories, days, language)
    if not isinstance(raw, dict):
        return fallback
    payload = dict(raw.get("identity") if isinstance(raw.get("identity"), dict) else raw)
    dims = payload.get("dimensions")
    if not isinstance(dims, list):
        return fallback
    clean_dims: list[dict] = []
    for idx, dim in enumerate(dims[:7]):
        if not isinstance(dim, dict):
            continue
        name = str(dim.get("name") or f"Dimension {idx + 1}")[:60]
        if str(language).startswith("zh") and _english_only_for_zh(name):
            name = str(fallback["dimensions"][idx].get("name") or f"维度 {idx + 1}")[:60]
        try:
            value = int(dim.get("value", 50))
        except Exception:
            value = 50
        desc = str(dim.get("description") or dim.get("evidence") or "")[:500]
        if str(language).startswith("zh") and _english_only_for_zh(desc):
            desc = ""
        clean_dims.append({
            "name": name,
            "value": max(0, min(value, 100)),
            "description": desc or ("根据导入材料得出。" if str(language).startswith("zh") else "Derived from imported history."),
        })
    if len(clean_dims) != 7:
        return fallback
    payload["agent_name"] = _sanitize_import_agent_name(str(payload.get("agent_name") or ""))
    payload["self_introduction"] = str(payload.get("self_introduction") or "")[:1200]
    if str(language).startswith("zh") and _english_only_for_zh(payload["self_introduction"]):
        payload["self_introduction"] = ""
    if not payload["self_introduction"]:
        payload["self_introduction"] = fallback["self_introduction"]
    payload["dimensions"] = clean_dims
    payload["days_with_user"] = days
    signature = payload.get("signature")
    if not isinstance(signature, list):
        signature = fallback.get("signature", [])
    clean_signature = [str(item).strip()[:80] for item in signature if str(item).strip()][:2]
    if str(language).startswith("zh") and any(_english_only_for_zh(item) for item in clean_signature):
        clean_signature = fallback.get("signature", [])
    payload["signature"] = clean_signature if len(clean_signature) == 2 else fallback.get("signature", [])
    category = str(payload.get("category") or "").strip()[:120]
    if not category or (str(language).startswith("zh") and _english_only_for_zh(category)):
        category = str(fallback.get("category") or "")
    payload["category"] = category
    return payload


def _derive_identity_with_provider(
    provider: provider_client.ProviderConfig,
    messages: list[dict],
    memory_cards: list[dict],
    days: int,
    language: str = "en",
) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    memory_sample = json.dumps(memory_cards[:40], ensure_ascii=False)
    source_stats = _import_source_stats(messages)
    has_ai_persona = bool(source_stats.get(_AI_PERSONA_SOURCE, {}).get("chars"))
    has_assistant_history = any(
        str(m.get("role") or "") in {"assistant", "agent", "openclaw"}
        for m in messages
        if _import_source_family(str(m.get("source") or m.get("source_family") or "")) == _HISTORY_SOURCE
    )
    has_ai_memory = any(
        str(card.get("type") or "") in {"insight", "reflection"}
        or "ai" in str(card.get("context") or "").lower()
        for card in memory_cards
    )
    transcript = _transcript_sample(messages, max_chars=12000)
    prompt = (
        "Derive a Feedling Identity Card for the AI companion from typed onboarding sources and Memory Garden cards. "
        "Return JSON only with fields: agent_name, self_introduction, category, "
        "signature (array of two short strings), dimensions (exactly 7 objects with "
        "name, value 0-100, description). Do not invent facts not grounded in input. "
        "Source priority: AI Persona materials are the primary source for the AI companion's identity, voice, role, name, and boundaries. "
        "Memory Garden cards are secondary evidence and may refine the identity. Chat History can show how the AI behaved in relationship. "
        "User Profile describes the user only; use it as relationship context, never as the AI companion's self-description. "
        "If there are no AI Persona materials, infer the companion only from assistant-side chat evidence, relationship patterns, and AI-related Memory Garden cards; otherwise keep the identity generic and ask the user to name/define the companion later. "
        "agent_name is the AI companion's own chosen or user-given name, not the user's name, account name, provider, model, runtime, platform, or product name. "
        "Only set agent_name when the imported Character Card or conversation explicitly names the AI companion; otherwise return an empty string for agent_name. "
        "self_introduction must be written in the AI companion's own voice; never describe the user as 'I'. "
        "High-risk personal claims such as legal/real name, address, or IDs require explicit user-authored evidence; otherwise omit them. "
        f"{_language_instruction(language)} "
        f"days_with_user is {days}.\n\nSource stats:\n{json.dumps(source_stats, ensure_ascii=False)}"
        f"\n\nMemory cards:\n{memory_sample}\n\nTranscript sample:\n{transcript}"
    )
    try:
        result = provider_client.chat_completion(
            provider,
            [
                {"role": "system", "content": "You write concise, grounded Feedling identity JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1800,
            temperature=0.3,
            timeout=45.0,
        )
        identity = _normalize_identity_payload(core_util._json_from_model_text(result["reply"]), memory_cards, days, language)
        if not has_ai_persona and not has_assistant_history and not has_ai_memory:
            fallback = _fallback_identity_payload(memory_cards, days, language)
            identity["agent_name"] = ""
            identity["self_introduction"] = fallback["self_introduction"]
            identity["category"] = fallback["category"]
            identity["signature"] = fallback["signature"]
            warnings.append("identity_guard_no_ai_source_used_generic_identity")
        return identity, warnings
    except Exception as e:
        warnings.append(f"provider_identity_failed:{type(e).__name__}:{str(e)[:160]}")
        return _fallback_identity_payload(memory_cards, days, language), warnings


def _store_identity_payload(
    store: UserStore,
    identity_payload: dict,
    *,
    days_with_user: int,
    evidence: str,
    language: str = "en",
) -> dict:
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(identity_payload, ensure_ascii=False).encode("utf-8"),
    )
    if envelope is None:
        raise RuntimeError(f"identity_envelope_failed:{err}")

    existing = identity_service._load_identity(store)
    now = core_util._now_iso()
    identity = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else uuid.uuid4().hex),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
        "relationship_started_at": identity_service._anchor_from_days(days_with_user, store=store, prefer_memory=True),
        "relationship_anchor_source": "history_import",
        "relationship_anchor_evidence": evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity)
    boot_gates._log_bootstrap_event(store, "history_import_identity_written", success=True)
    identity_service._append_identity_change(store, {
        "action": "replace" if existing else "init",
        "reason": "根据导入材料写入身份卡。" if str(language).startswith("zh") else "Identity card written from Model API history import.",
    })
    return identity


def _generate_model_api_onboarding_greeting(
    provider: provider_client.ProviderConfig,
    messages: list[dict],
    memory_cards: list[dict],
    identity_payload: dict,
    days: int,
    language: str = "en",
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    agent_name = _sanitize_import_agent_name(str(identity_payload.get("agent_name") or ""))
    is_zh = str(language).startswith("zh")
    identity_summary = {
        "agent_name": agent_name,
        "self_introduction": identity_payload.get("self_introduction", ""),
        "category": identity_payload.get("category", ""),
        "signature": identity_payload.get("signature", []),
        "days_with_user": days,
    }
    if not agent_name and is_zh:
        name_instruction = (
            "这个身份还没有确认的 AI 伴侣名字。第一句话里要自然说明“现在我还没有名字”，"
            "并问用户以后想怎么叫你。不要自己起名。 "
        )
    elif not agent_name:
        name_instruction = (
            "This identity has no confirmed AI companion name. In this first message, "
            "naturally ask the user what they would like to call you. Do not assign yourself a name. "
        )
    else:
        name_instruction = "Use the confirmed AI companion name only if it feels natural. "
    prompt = (
        "Write the first visible chat message from the user's IO companion after onboarding. "
        "The imported files have already been analyzed into memory and identity; do not paste, "
        "summarize, or mention the source files, onboarding, import, API keys, encryption, or internal tools. "
        "Speak in the companion's own voice, grounded in the context below. "
        f"{name_instruction}"
        f"{_language_instruction(language)} "
        "Return only the message text, no JSON, no bullets, 1-3 short sentences.\n\n"
        "Identity JSON:\n"
        + json.dumps(identity_summary, ensure_ascii=False)[:4000]
        + "\n\nMemory cards:\n"
        + json.dumps(memory_cards[:12], ensure_ascii=False)[:8000]
        + "\n\nTranscript sample:\n"
        + _transcript_sample(messages, max_chars=8000)
    )
    try:
        result = provider_client.chat_completion(
            provider,
            [
                {"role": "system", "content": "You are the user's IO companion writing one natural first message."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=320,
            temperature=0.7,
            timeout=45.0,
        )
    except Exception as e:
        warnings.append(f"provider_onboarding_greeting_failed:{type(e).__name__}:{str(e)[:160]}")
        fallback = (
            "我已经先把能读懂的部分整理成记忆了。现在我还没有名字，你想以后怎么叫我？"
            if is_zh and not agent_name else
            "I have turned the readable parts into memory first. What would you like to call me?"
            if not agent_name else
            ("我已经整理好一版记忆了，接下来可以从这里继续。" if is_zh else "I have a first version of the memory ready, and we can continue from here.")
        )
        return fallback, warnings

    text = str(result.get("reply") or "").strip()
    text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text).strip()
    if not text:
        warnings.append("provider_onboarding_greeting_empty")
        fallback = (
            "我已经先把能读懂的部分整理成记忆了。现在我还没有名字，你想以后怎么叫我？"
            if is_zh and not agent_name else
            "I have turned the readable parts into memory first. What would you like to call me?"
            if not agent_name else
            ("我已经整理好一版记忆了，接下来可以从这里继续。" if is_zh else "I have a first version of the memory ready, and we can continue from here.")
        )
        return fallback, warnings
    return text[:1200], warnings


def _append_model_api_onboarding_greeting(store: UserStore, text: str) -> dict:
    envelope, err = core_envelope._build_shared_envelope_for_store(store, text.encode("utf-8"))
    if envelope is None:
        raise RuntimeError(f"onboarding_greeting_envelope_failed:{err}")
    row = store.append_chat(
        "openclaw",
        "model_api",
        envelope,
        extra={"model_api_kind": "onboarding_greeting"},
    )
    store.notify_chat_waiters()
    boot_gates._log_bootstrap_event(store, "model_api_onboarding_greeting_written", success=True)
    return row


def _process_history_import_sync(
    store: UserStore,
    api_key: str | None,
    job: dict,
    payload: dict,
) -> dict:
    _update_history_job_phase(store, job, "parsing_materials")
    content = str(payload.get("content") or "")
    fmt = str(payload.get("format") or "plaintext").strip().lower()
    warnings: list[str] = []
    history_messages = _parse_import_history_content(content, fmt, warnings)
    support_messages = _persona_support_messages(payload)
    if not history_messages and not support_messages:
        if not bool(payload.get("fresh_start")):
            raise ValueError(
                "content, ai_persona_content, character_content, personal_profile_content, memory_summary_content, persona_content, "
                "or fresh_start=true required"
            )
        support_messages = [{
            "role": "user",
            "content": "Fresh start. No persona profile or previous chat history was provided.",
            "ts": None,
            "source": "fresh_start",
        }]
        warnings.append("fresh_start_without_support_material")

    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        raise RuntimeError(json.dumps(err, ensure_ascii=False))

    analysis_messages = support_messages + history_messages
    fallback_messages = history_messages if history_messages else support_messages
    source_stats = _import_source_stats(analysis_messages)
    relationship_start, rel_err = _relationship_start_from_import(payload, fallback_messages)
    if relationship_start is None:
        raise ValueError(rel_err)
    days = max(0, (date.today() - relationship_start).days)
    floors = memory_service._per_tab_floors_for_days(days)
    profile = _history_import_profile(
        history_messages,
        support_messages,
        content_chars=len(content),
    )
    import_targets = _import_memory_targets(floors, history_messages, support_messages, profile)
    language = _import_language_for_store(store, analysis_messages)
    windows = _build_transcript_windows(
        analysis_messages,
        max_chars=18000,
        max_windows=int(import_targets.get("total_windows") or 8),
    )
    initial_windows = _select_evenly(windows, int(import_targets.get("initial_windows") or len(windows)))
    initial_window_ids = {str(w.get("id") or "") for w in initial_windows}
    background_windows = [
        w for w in windows
        if bool(import_targets.get("background")) and str(w.get("id") or "") not in initial_window_ids
    ]

    _update_history_job_phase(store, job, "chat_history_importing", **{
        "format": fmt or "plaintext",
        "history_filename": str(payload.get("history_filename") or "")[:240],
        "ai_persona_filename": str(payload.get("ai_persona_filename") or "")[:240],
        "character_filename": str(
            payload.get("character_filename")
            or payload.get("character_card_filename")
            or ""
        )[:240],
        "agent_prompt_filename": str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        )[:240],
        "persona_filename": str(
            payload.get("personal_profile_filename")
            or payload.get("persona_filename")
            or ""
        )[:240],
        "memory_summary_filename": str(
            payload.get("memory_summary_filename")
            or payload.get("memory_sample_filename")
            or ""
        )[:240],
        "messages_parsed": len(history_messages),
        "support_materials": len(support_messages),
        "source_stats": source_stats,
        "ai_persona_chars": int(source_stats.get(_AI_PERSONA_SOURCE, {}).get("chars") or 0),
        "agent_prompt_chars": sum(len(str(m.get("content") or "")) for m in support_messages if m.get("source_detail") == "agent_prompt_import"),
        "character_chars": sum(len(str(m.get("content") or "")) for m in support_messages if m.get("source_detail") == "character_import"),
        "user_profile_chars": int(source_stats.get(_USER_PROFILE_SOURCE, {}).get("chars") or 0),
        "persona_chars": int(source_stats.get(_USER_PROFILE_SOURCE, {}).get("chars") or 0),
        "memory_summary_chars": int(source_stats.get(_MEMORY_SUMMARY_SOURCE, {}).get("chars") or 0),
        "import_language": language,
        "relationship_started_at": relationship_start.isoformat(),
        "relationship_days": days,
        "floors": floors,
        "import_targets": import_targets,
        "history_profile": profile,
        "history_tier": profile["tier"],
        "timeline_span_days": profile["span_days"],
        "candidate_windows_total": len(windows),
        "candidate_windows_initial": len(initial_windows),
        "background_windows_total": len(background_windows),
    })

    def initial_progress(done: int, total: int, candidate_count: int) -> None:
        progress = 24 + int(24 * done / max(total, 1))
        _update_history_job_phase(
            store,
            job,
            "candidate_extracting",
            progress=progress,
            candidate_windows_done=done,
            candidate_windows_total=total,
            candidates_extracted=candidate_count,
        )

    _update_history_job_phase(
        store,
        job,
        "candidate_extracting",
        candidate_windows_done=0,
        candidate_windows_total=len(initial_windows),
        candidates_extracted=0,
    )
    per_window_target = max(4, min(10, (int(import_targets.get("total", 12)) + max(len(initial_windows), 1) - 1) // max(len(initial_windows), 1) + 2))
    initial_candidates, provider_warnings = _extract_memory_candidates_with_provider(
        runtime,
        initial_windows,
        relationship_start,
        per_window_target=per_window_target,
        language=language,
        on_progress=initial_progress,
    )
    warnings.extend(provider_warnings)
    merged_candidates = _merge_import_candidates(initial_candidates)
    cards = _render_candidates_to_memory_cards(
        merged_candidates,
        relationship_start,
        import_targets,
        language=language,
        max_cards=int(import_targets.get("total") or 12),
    )
    cards = _ensure_import_minimum_cards(
        cards,
        fallback_messages,
        relationship_start,
        min_story=min(1, int(import_targets.get("story") or 0)),
        min_about=min(1, int(import_targets.get("about_me") or 0)),
        language=language,
    )
    cards = _sort_memory_cards_newest_first(cards)

    _update_history_job_phase(
        store,
        job,
        "candidate_merging",
        candidates_extracted=len(initial_candidates),
        candidates_merged=len(merged_candidates),
        memories_planned=len(cards),
    )
    _update_history_job_phase(
        store,
        job,
        "memory_writing",
        memories_planned=len(cards),
    )
    memory_rows = _append_import_memory_cards(store, cards)
    _update_history_job_phase(
        store,
        job,
        "identity_deriving",
        memories_created=len(memory_rows),
    )

    identity_payload, id_warnings = _derive_identity_with_provider(runtime, analysis_messages, cards, days, language)
    warnings.extend(id_warnings)
    _update_history_job_phase(
        store,
        job,
        "relationship_anchor_writing",
        memories_created=len(memory_rows),
    )
    identity = _store_identity_payload(
        store,
        identity_payload,
        days_with_user=days,
        evidence=f"history_import:{job['job_id']} relationship_started_at={relationship_start.isoformat()}",
        language=language,
    )
    _update_history_job_phase(
        store,
        job,
        "hosted_chat_preparing",
        memories_created=len(memory_rows),
        identity_written=bool(identity),
    )

    greeting_text, greeting_warnings = _generate_model_api_onboarding_greeting(
        runtime,
        analysis_messages,
        cards,
        identity_payload,
        days,
        language,
    )
    warnings.extend(greeting_warnings)
    greeting_row = _append_model_api_onboarding_greeting(store, greeting_text) if greeting_text else None
    chat_ready_cards = int(import_targets.get("chat_ready_cards") or 2)
    chat_ready = bool(identity) and bool(greeting_row) and len(memory_rows) >= min(chat_ready_cards, max(2, len(cards)))

    job.update({
        "chat_ready": chat_ready,
        "chat_ready_at": core_util._now_iso() if chat_ready else "",
        "chat_ready_cards_required": chat_ready_cards,
        "initial_memories_created": len(memory_rows),
        "candidate_count": len(initial_candidates),
        "candidate_cluster_count": len(merged_candidates),
        "background_status": "pending" if background_windows else "not_needed",
        "warnings": warnings,
    })
    if chat_ready and background_windows:
        _update_history_job_phase(
            store,
            job,
            "background_importing",
            status="processing",
            memories_created=len(memory_rows),
            identity_written=bool(identity),
            onboarding_greeting_written=bool(greeting_row),
            background_windows_done=0,
            background_windows_total=len(background_windows),
        )

        def background_progress(done: int, total: int, candidate_count: int) -> None:
            progress = 96 + int(3 * done / max(total, 1))
            _update_history_job_phase(
                store,
                job,
                "background_importing",
                status="processing",
                progress=progress,
                background_windows_done=done,
                background_windows_total=total,
                background_candidates_extracted=candidate_count,
                memories_created=len(memory_rows),
            )

        try:
            background_candidates, bg_warnings = _extract_memory_candidates_with_provider(
                runtime,
                background_windows,
                relationship_start,
                per_window_target=max(3, min(7, per_window_target - 1)),
                language=language,
                on_progress=background_progress,
            )
            warnings.extend(bg_warnings)
            all_candidates = initial_candidates + background_candidates
            all_cards = _render_candidates_to_memory_cards(
                all_candidates,
                relationship_start,
                import_targets,
                language=language,
                max_cards=int(import_targets.get("total") or 120),
            )
            additional_cards = _new_cards_only(cards, all_cards)
            additional_cards = _sort_memory_cards_newest_first(additional_cards)
            additional_rows = _append_import_memory_cards(store, additional_cards)
            memory_rows.extend(additional_rows)
            cards = _sort_memory_cards_newest_first(_dedupe_memory_cards(cards + additional_cards))
            merged_all = _merge_import_candidates(all_candidates)
            job.update({
                "background_status": "completed",
                "background_candidates_extracted": len(background_candidates),
                "background_memories_created": len(additional_rows),
                "candidate_count": len(all_candidates),
                "candidate_cluster_count": len(merged_all),
            })
        except Exception as e:
            warnings.append(f"background_import_failed:{type(e).__name__}:{str(e)[:180]}")
            job.update({
                "background_status": "failed",
                "background_error": f"{type(e).__name__}:{str(e)[:240]}",
            })

    job.update({
        "status": "completed",
        "completed_at": core_util._now_iso(),
        "chat_messages_imported": 0,
        "memories_created": len(memory_rows),
        "identity_written": bool(identity),
        "onboarding_greeting_written": bool(greeting_row),
        "onboarding_greeting_message_id": (greeting_row or {}).get("id", ""),
        "warnings": warnings,
    })
    return _update_history_job_phase(store, job, "completed", status="completed")


def _run_history_import_job(
    store: UserStore,
    api_key: str | None,
    job_id: str,
    payload: dict,
) -> None:
    try:
        job = db.get_blob(store.user_id, _history_job_kind(job_id)) or {
            "job_id": job_id,
            "status": "queued",
            "created_at": core_util._now_iso(),
        }
        job["started_at"] = job.get("started_at") or core_util._now_iso()
        _process_history_import_sync(store, api_key, job, payload)
        print(
            f"[history_import:{store.user_id}] job={job_id} messages={job.get('messages_parsed')} "
            f"memories={job.get('memories_created')} chat={job.get('chat_messages_imported')} async=1"
        )
    except Exception as e:
        job = db.get_blob(store.user_id, _history_job_kind(job_id)) or {
            "job_id": job_id,
            "created_at": core_util._now_iso(),
        }
        job.update({
            "failed_at": core_util._now_iso(),
            "error": f"{type(e).__name__}:{str(e)[:500]}",
        })
        _update_history_job_phase(store, job, "failed", status="failed")
        print(f"[history_import:{store.user_id}] job={job_id} failed={type(e).__name__}:{str(e)[:220]}")
    finally:
        with _history_import_active_lock:
            _history_import_active_jobs.discard(job_id)


def _start_history_import_job(
    store: UserStore,
    api_key: str | None,
    job: dict,
    payload: dict,
) -> bool:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return False
    with _history_import_active_lock:
        if job_id in _history_import_active_jobs:
            return False
        _history_import_active_jobs.add(job_id)
    thread = threading.Thread(
        target=_run_history_import_job,
        args=(store, api_key, job_id, dict(payload)),
        daemon=True,
        name=f"history-import-{job_id[:18]}",
    )
    thread.start()
    return True


@bp.route("/v1/history_import/upload", methods=["POST"])
def history_import_upload():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    input_hash = _history_import_payload_hash(payload)
    client_job_id = _history_import_client_job_id(payload)
    existing = _history_import_find_reusable_job(
        store,
        client_job_id=client_job_id,
        input_hash=input_hash,
    )
    if existing:
        if str(existing.get("status") or "") in {"queued", "processing"}:
            _start_history_import_job(store, api_key, existing, payload)
            return jsonify({"job": existing}), 202
        return jsonify({"job": existing}), 200

    job_id = f"hi_{uuid.uuid4().hex[:16]}"
    job = {
        "job_id": job_id,
        "status": "queued",
        "client_job_id": client_job_id,
        "input_hash": input_hash,
        "created_at": core_util._now_iso(),
        "content_chars": len(str(payload.get("content") or "")),
        "ai_persona_chars": len(str(
            payload.get("ai_persona_content")
            or payload.get("ai_persona")
            or ""
        )),
        "character_chars": len(str(
            payload.get("character_content")
            or payload.get("character_card")
            or ""
        )),
        "agent_prompt_chars": len(str(
            payload.get("agent_prompt_content")
            or payload.get("original_system_prompt_content")
            or payload.get("system_prompt_content")
            or payload.get("agent_prompt")
            or payload.get("system_prompt")
            or payload.get("original_system_prompt")
            or ""
        )),
        "persona_chars": len(str(
            payload.get("personal_profile_content")
            or payload.get("persona_content")
            or payload.get("persona")
            or payload.get("profile_content")
            or ""
        )),
        "memory_summary_chars": len(str(
            payload.get("memory_summary_content")
            or payload.get("memory_summary")
            or payload.get("memory_sample_content")
            or payload.get("memory_sample")
            or ""
        )),
        "ai_persona_filename": str(payload.get("ai_persona_filename") or "")[:240],
        "character_filename": str(
            payload.get("character_filename")
            or payload.get("character_card_filename")
            or ""
        )[:240],
        "agent_prompt_filename": str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        )[:240],
        "persona_filename": str(
            payload.get("personal_profile_filename")
            or payload.get("persona_filename")
            or ""
        )[:240],
        "memory_summary_filename": str(
            payload.get("memory_summary_filename")
            or payload.get("memory_sample_filename")
            or ""
        )[:240],
        "chat_ready": False,
        "background_status": "not_started",
        **_history_import_phase_fields("upload_received"),
    }
    _save_history_job(store, job)
    _start_history_import_job(store, api_key, job, payload)
    print(f"[history_import:{store.user_id}] job={job_id} queued async=1 client_job_id={client_job_id[:24]}")
    return jsonify({"job": job}), 202


@bp.route("/v1/history_import/status/<job_id>", methods=["GET"])
def history_import_status(job_id):
    store = auth.require_user()
    data = db.get_blob(store.user_id, _history_job_kind(job_id))
    if not data:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify({"job": data})
