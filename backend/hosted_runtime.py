from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from memory import timestamps as memory_timestamps
from memory.prompts_v1 import MEMORY_WRITE_GUIDANCE_V1


IDENTITY_STRING_FIELDS = (
    "agent_name",
    "self_introduction",
    "category",
    "user_preferred_name",
    "agent_role",
    "tone_style",
    "language_preference",
    "relationship_anchor",
)
IDENTITY_LIST_FIELDS = (
    "signature",
    "boundaries",
    "do_not_say",
    "stable_definitions",
)


def clean_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    return text[:max_chars].strip()


def clean_list(value: Any, max_items: int = 12, max_chars: int = 240) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("；", ";").replace("\n", ";").split(";")
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    return [item for item in (clean_text(part, max_chars) for part in raw[:max_items]) if item]


def _memory_occurred_at(raw: dict) -> str:
    """Normalize a supplied event time or freeze a full UTC time when absent."""
    if "occurred_at" in raw:
        return memory_timestamps.normalize(clean_text(raw.get("occurred_at"), 80))
    return memory_timestamps.now_iso()


def compact_pending_items(pending_items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in pending_items[:5]:
        if not isinstance(item, dict):
            continue
        runtime_action = item.get("runtime_action") if isinstance(item.get("runtime_action"), dict) else {}
        out.append({
            "id": str(item.get("id") or ""),
            "type": str(runtime_action.get("runtime_type") or ""),
            "confidence": runtime_action.get("confidence", 0),
            "reason": str(runtime_action.get("reason") or "")[:500],
            "executor_action": runtime_action.get("executor_action") if isinstance(runtime_action.get("executor_action"), dict) else {},
        })
    return [item for item in out if item["id"]]


def compact_memory_terms(memory_terms: dict | None) -> dict:
    if not isinstance(memory_terms, dict):
        return {"buckets": [], "threads": []}

    def clean_list(value: Any) -> list[str]:
        raw = value if isinstance(value, list) else []
        out: list[str] = []
        for item in raw:
            text = str(item or "").strip()[:120]
            if text and text not in out:
                out.append(text)
        return out[:80]

    return {
        "buckets": clean_list(memory_terms.get("buckets")),
        "threads": clean_list(memory_terms.get("threads")),
    }


def build_background_execution_messages(
    *,
    user_message: str,
    identity: dict,
    memory_candidates: list[dict],
    context_refs: list[dict],
    pending_items: list[dict],
    memory_terms: dict | None = None,
) -> list[dict]:
    payload = {
        "today": date.today().isoformat(),
        "latest_user_message": user_message[:4000],
        "identity": identity,
        "memory_candidates": memory_candidates[:12],
        "existing_memory_terms": compact_memory_terms(memory_terms),
        "user_selected_context_refs": context_refs[:8],
        "pending_actions_waiting_for_user_confirmation": compact_pending_items(pending_items),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are Feedling hosted runtime's background execution controller. "
                "You are inside the backend runtime, not the user-visible assistant. "
                "Return one strict JSON object only; never answer the user here. "
                "Your job is to decide whether the latest user message should produce durable Feedling state actions. "
                "Durable state means Identity or Memory Garden state that should remain true after this turn. "
                "If the user only chats normally, asks a question, roleplays, jokes, or references a memory without asking to change it, return no actions. "
                "If the user asks you to remember, forget, correct, rename, correct relationship day count, change address preferences, update persona/voice/boundaries, or fix a selected Memory Garden card, produce actions. "
                "For an explicit first-person durable preference or correction with no clear existing card target, prefer memory.add with high confidence instead of memory.patch. "
                "Use confidence >= 0.9 for explicit, non-destructive state writes. Use lower confidence mainly for destructive actions or ambiguous patch/delete targets. "
                "Use memory.supersede when the user corrects or replaces an existing memory; target.memory_id must be the old card being replaced and payload.memory must contain the new card. "
                "Use memory_candidates or user_selected_context_refs for memory.patch/delete/supersede targets. If the target is ambiguous, use low confidence. "
                "If pending_actions_waiting_for_user_confirmation is non-empty and the latest message confirms or rejects one of them, set pending_decision instead of inventing a new action. "
                "Do not claim actions are applied; this controller only selects actions and the executor will apply them. "
                "Supported action types: identity.patch, identity.dimension_nudge, identity.relationship_days_set, memory.add, memory.supersede, memory.delete. "
                "Legacy memory.create and memory.patch are accepted aliases, but do not prefer them. "
                "For memory actions, follow the v1 write guidance below and reuse existing_memory_terms.buckets / existing_memory_terms.threads when they fit before creating a new bucket or thread. "
                "Use identity.dimension_nudge only when the user asks to raise or lower an existing identity dimension; payload must include dimension and delta. "
                "Use identity.relationship_days_set when the user says the displayed days together / relationship day count is wrong; payload must include days_with_user as an integer. "
                "JSON shape: {"
                "\"pending_decision\":{\"decision\":\"none|confirm|reject\",\"pending_ids\":[\"...\"],\"reason\":\"optional\"},"
                "\"actions\":[{\"type\":\"identity.patch|identity.dimension_nudge|identity.relationship_days_set|memory.add|memory.supersede|memory.delete\","
                "\"confidence\":0.0,\"target\":{\"memory_id\":\"optional\",\"candidate_ids\":[\"...\"]},"
                "\"payload\":{},\"reason\":\"short reason\"}],"
                "\"why_empty\":\"optional\"}."
                "\n\n"
                + MEMORY_WRITE_GUIDANCE_V1
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:16000]},
    ]


def _candidate_ids(target: dict) -> list[str]:
    ids = target.get("candidate_ids") if isinstance(target.get("candidate_ids"), list) else []
    return [str(cid) for cid in ids[:3] if str(cid or "").strip()]


def _coerce_days_with_user(*sources: dict) -> int | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in (
            "days_with_user",
            "relationship_days",
            "days_together",
            "together_days",
            "day_count",
            "days",
        ):
            if key not in source:
                continue
            try:
                days = int(source.get(key))
            except Exception:
                continue
            if days >= 0:
                return days
    return None


def coerce_runtime_action(
    action: dict,
    memory_candidates: list[dict],
    *,
    direct_confidence: float,
) -> dict | None:
    if not isinstance(action, dict):
        return None
    action_type = str(action.get("type") or action.get("action") or "").strip().lower()
    try:
        confidence = float(action.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    reason = clean_text(action.get("reason") or "Planned by Feedling hosted runtime.", 500)
    runtime_action = {
        "action_id": str(action.get("action_id") or f"rt_{uuid.uuid4().hex[:12]}"),
        "runtime_type": action_type,
        "confidence": confidence,
        "reason": reason,
        "requires_confirmation": confidence < direct_confidence,
    }

    if action_type in {
        "identity.relationship_days_set",
        "identity.relationship_days",
        "identity.days_with_user_set",
        "identity.relationship_anchor",
    }:
        days = _coerce_days_with_user(payload, action, target)
        if days is None:
            return None
        runtime_action["domain"] = "identity"
        runtime_action["executor_action"] = {
            "type": "identity.relationship_days_set",
            "days_with_user": days,
            "reason": reason,
            "relationship_anchor_evidence": reason,
            "source": "hosted_runtime_action",
        }
        return runtime_action

    if action_type in {"identity.patch", "identity.profile_patch"}:
        raw_patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
        days = _coerce_days_with_user(raw_patch)
        if days is not None and not any(key in raw_patch for key in (*IDENTITY_STRING_FIELDS, *IDENTITY_LIST_FIELDS)):
            runtime_action["runtime_type"] = "identity.relationship_days_set"
            runtime_action["domain"] = "identity"
            runtime_action["executor_action"] = {
                "type": "identity.relationship_days_set",
                "days_with_user": days,
                "reason": reason,
                "relationship_anchor_evidence": reason,
                "source": "hosted_runtime_action",
            }
            return runtime_action
        patch: dict[str, Any] = {}
        for key in IDENTITY_STRING_FIELDS:
            if key in raw_patch:
                patch[key] = clean_text(
                    raw_patch.get(key),
                    1200 if key in {"self_introduction", "relationship_anchor", "tone_style"} else 240,
                )
        for key in IDENTITY_LIST_FIELDS:
            if key in raw_patch:
                values = clean_list(raw_patch.get(key))
                if values:
                    patch[key] = values
        if not patch:
            return None
        runtime_action["domain"] = "identity"
        runtime_action["executor_action"] = {
            "type": "identity.profile_patch",
            "patch": patch,
            "reason": reason,
            "source": "hosted_runtime_action",
        }
        return runtime_action

    if action_type in {"identity.dimension_nudge", "identity.dimension"}:
        dimension = clean_text(
            payload.get("dimension")
            or payload.get("dimension_name")
            or target.get("dimension")
            or target.get("dimension_name"),
            80,
        )
        try:
            delta = int(payload.get("delta") if "delta" in payload else action.get("delta"))
        except Exception:
            delta = 0
        if not dimension or delta == 0:
            return None
        delta = max(-10, min(10, delta))
        runtime_action["domain"] = "identity"
        runtime_action["executor_action"] = {
            "type": "identity.dimension_nudge",
            "dimension": dimension,
            "delta": delta,
            "reason": reason,
            "source": "hosted_runtime_action",
        }
        return runtime_action

    if action_type in {"memory.create", "memory.add", "memory.add_correction"}:
        raw = payload.get("memory") if isinstance(payload.get("memory"), dict) else payload
        summary = str(raw.get("summary") or raw.get("description") or raw.get("content") or raw.get("title") or "").strip()[:2000]
        content = str(raw.get("content") or raw.get("description") or summary).strip()[:5000]
        if not summary or not content:
            return None
        source = "model_api_correction" if action_type == "memory.add_correction" else "hosted_runtime_state"
        runtime_action["domain"] = "memory"
        runtime_action["executor_action"] = {
            "type": "memory.add",
            "memory": {
                "summary": summary,
                "content": content,
                "bucket": clean_text(raw.get("bucket") or "未分类", 80),
                "threads": [str(item).strip()[:80] for item in raw.get("threads", []) if str(item or "").strip()][:8]
                if isinstance(raw.get("threads"), list) else [],
                "importance": raw.get("importance", 0.5),
                "pulse": raw.get("pulse", 0.3),
                "occurred_at": _memory_occurred_at(raw),
                "source": clean_text(raw.get("source") or source, 80),
            },
            "reason": reason,
            "capture_mode": "state",
        }
        return runtime_action

    if action_type in {"memory.supersede", "memory.replace", "memory.correct"}:
        memory_id = str(target.get("memory_id") or target.get("id") or payload.get("memory_id") or payload.get("id") or "").strip()
        ids = _candidate_ids(target)
        if not memory_id and ids:
            memory_id = ids[0]
            runtime_action["requires_confirmation"] = True
            runtime_action["candidate_ids"] = ids
        if not memory_id:
            return None
        raw = payload.get("memory") if isinstance(payload.get("memory"), dict) else payload
        summary = str(raw.get("summary") or raw.get("description") or raw.get("content") or raw.get("title") or "").strip()[:2000]
        if not summary:
            return None
        content = str(raw.get("content") or raw.get("description") or summary).strip()[:5000]
        memory_payload = {
            "summary": summary,
            "content": content,
            "bucket": clean_text(raw.get("bucket") or "", 80),
            "threads": [str(item).strip()[:80] for item in raw.get("threads", []) if str(item or "").strip()][:8]
            if isinstance(raw.get("threads"), list) else [],
            "importance": raw.get("importance", 0.5),
            "pulse": raw.get("pulse", 0.3),
            "occurred_at": _memory_occurred_at(raw),
            "source": clean_text(raw.get("source") or "hosted_runtime_state", 80),
        }
        runtime_action["domain"] = "memory"
        runtime_action["target"] = {"memory_id": memory_id}
        runtime_action["executor_action"] = {
            "type": "memory.supersede",
            "supersedes": memory_id,
            "memory": memory_payload,
            "reason": reason,
            "capture_mode": "state",
        }
        return runtime_action

    if action_type in {"memory.patch", "memory.content_patch", "memory.delete"}:
        memory_id = str(target.get("memory_id") or target.get("id") or payload.get("memory_id") or payload.get("id") or "").strip()
        ids = _candidate_ids(target)
        if not memory_id and ids:
            memory_id = ids[0]
            runtime_action["requires_confirmation"] = True
            runtime_action["candidate_ids"] = ids
        if not memory_id:
            return None
        preview = next((item for item in memory_candidates if str(item.get("id") or "") == memory_id), {})
        if isinstance(preview, dict) and preview:
            runtime_action["target_preview"] = {
                "id": str(preview.get("id") or ""),
                "title": clean_text(preview.get("title"), 180),
                "description": clean_text(preview.get("description"), 600),
                "type": clean_text(preview.get("type"), 80),
                "occurred_at": clean_text(preview.get("occurred_at"), 80),
            }
        runtime_action["target"] = {"memory_id": memory_id}
        runtime_action["domain"] = "memory"
        if action_type == "memory.delete":
            runtime_action["executor_action"] = {
                "type": "memory.delete",
                "memory_id": memory_id,
                "reason": reason,
            }
            return runtime_action

        raw_patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
        summary = str(raw_patch.get("summary") or raw_patch.get("description") or raw_patch.get("content") or "").strip()[:2000]
        content = str(raw_patch.get("content") or raw_patch.get("description") or summary).strip()[:5000]
        if not summary or not content:
            return None
        runtime_action["executor_action"] = {
            "type": "memory.supersede",
            "supersedes": memory_id,
            "memory": {
                "summary": summary,
                "content": content,
                "bucket": clean_text(raw_patch.get("bucket") or "", 80),
                "threads": [str(item).strip()[:80] for item in raw_patch.get("threads", []) if str(item or "").strip()][:8]
                if isinstance(raw_patch.get("threads"), list) else [],
                "importance": raw_patch.get("importance", 0.5),
                "pulse": raw_patch.get("pulse", 0.3),
                "occurred_at": _memory_occurred_at(raw_patch),
                "source": clean_text(raw_patch.get("source") or "hosted_runtime_state", 80),
            },
            "reason": reason,
            "capture_mode": "state",
        }
        return runtime_action

    return None
