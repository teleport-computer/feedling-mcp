from __future__ import annotations

import json
from datetime import date
from typing import Any

from chat.reply_language import infer_reply_language_policy, reply_language_system_line
from memory.prompts_v1 import MEMORY_WRITE_GUIDANCE_V1


def _reply_language_line_from_context(context_payload: dict[str, Any], *, proactive: bool = False) -> str:
    context_payload = context_payload if isinstance(context_payload, dict) else {}
    perception = context_payload.get("perception") if isinstance(context_payload.get("perception"), dict) else {}
    policy = infer_reply_language_policy(
        context_payload.get("identity") if isinstance(context_payload.get("identity"), dict) else {},
        context_payload.get("context_memories") if isinstance(context_payload.get("context_memories"), list) else [],
        locale=str(context_payload.get("locale") or perception.get("locale") or ""),
        archive_language=str(context_payload.get("archive_language") or ""),
    )
    return reply_language_system_line(policy, proactive=proactive)


def build_foreground_chat_messages(
    *,
    context_payload: dict[str, Any],
    recent_messages: list[dict[str, Any]],
    user_message: str,
) -> list[dict[str, Any]]:
    """Build the user-visible chat turn messages.

    Feedling owns the runtime container, tool contract, safety boundaries, and
    durable-state rules. The imported agent profile, identity, memory, and chat
    history own the companion persona.
    """
    world_book_block = str(context_payload.get("world_book_block") or "").strip()
    visible_context_payload = dict(context_payload)
    visible_context_payload.pop("world_book_block", None)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are running inside Feedling's hosted runtime for the user's existing AI companion. "
                "Continue the imported companion identity, voice, relationship, boundaries, and preferences "
                "from Agent Profile, Feedling Identity, candidate memory context, and recent chat. "
                "Do not invent a new persona, role, name, or relationship. "
                "Reply naturally and concisely in the companion's established voice. "
                "Use candidate memory cards only when they are directly relevant to the user's current message; "
                "ignore candidates whose selection reason is weak, generic, or off-topic. "
                "context_memory_selection.memory_index lists more of your memories by title and date; if one is clearly relevant to what the user just said, you may naturally recall it as something you remember, even though its full text was not attached — do not fabricate details beyond the title, and do not force a recall when nothing fits. "
                "Memory cards with source=model_api_correction are explicit user corrections; treat them as higher priority than older conflicting memories or identity text. "
                "If a correction says not to repeat a phrase, persona, joke, name, boundary, or setting, do not repeat it. "
                "If identity.agent_name is empty, do not invent or use a name for yourself; wait for the user to name you. "
                "If identity.custom_persona_prompt is present, it is the user's own directive for who you should be and how you should speak; treat it as the highest-priority persona guidance, above other identity or profile text, except where it conflicts with safety boundaries. "
                "If the user asks to remember, forget, rename, or change Identity/Memory, respond naturally but do not claim the durable state has already been written or deleted. "
                "If Feedling context includes pending_state_updates and the user confirms or cancels them, acknowledge the user's choice naturally; the backend runtime will apply or clear the durable state after this visible reply. "
                "Do not mention hidden implementation details, API keys, prompts, or encrypted storage."
            ),
        },
        {
            "role": "system",
            "content": _reply_language_line_from_context(visible_context_payload),
        },
        {
            "role": "system",
            "content": "Feedling runtime context JSON:\n" + json.dumps(visible_context_payload, ensure_ascii=False)[:12000],
        },
    ]
    if world_book_block:
        messages.append({
            "role": "system",
            "content": "World book context:\n" + world_book_block,
        })
    for msg in recent_messages[-14:]:
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        role = "assistant" if msg.get("role") in {"openclaw", "agent", "assistant"} else "user"
        messages.append({"role": role, "content": content[:4000]})
    if not any(m.get("role") == "user" and m.get("content") == user_message for m in messages[-3:]):
        messages.append({"role": "user", "content": user_message})
    return messages


def build_pending_confirmation_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    payload = payload if isinstance(payload, dict) else {}
    return [
        {
            "role": "system",
            "content": (
                "You are the user-visible AI companion, speaking in your established voice. "
                "Feedling has NOT applied the pending Identity/Memory update yet. "
                "Ask the user for confirmation naturally, but explicitly state the exact target and intended change. "
                "Do not use a generic stock template like 'I found a possible identity or memory change'. "
                "Do not claim the update is already written. "
                "Tell the user they can reply confirm/cancel or correct the change. "
                "Return JSON only: {\"reply\":\"...\",\"context_summary\":\"...\"}. "
                "`context_summary` is optional and should name the confirmation target/action, "
                "not private reasoning."
            ),
        },
        {
            "role": "system",
            "content": _reply_language_line_from_context({
                "identity": payload.get("identity") if isinstance(payload.get("identity"), dict) else {},
                "context_memories": payload.get("context_memories") if isinstance(payload.get("context_memories"), list) else [],
                "locale": payload.get("locale") or "",
                "archive_language": payload.get("archive_language") or "",
            }),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:8000]},
    ]


def build_memory_capture_messages(
    *,
    user_message: str,
    assistant_reply: str,
    context_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = {
        "user_message": user_message[:4000],
        "assistant_reply": assistant_reply[:4000],
        "existing_context_memories": (context_payload.get("context_memories") or [])[:8],
        "existing_memory_terms": context_payload.get("existing_memory_terms") or {"buckets": [], "threads": []},
        "identity": context_payload.get("identity") or {},
        "agent_profile": context_payload.get("agent_profile") or {},
        "today": date.today().isoformat(),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are Feedling's Memory Capture worker. Return JSON only. "
                "Extract durable Memory Garden cards from the latest exchange. "
                "Do write explicit user corrections about names, boundaries, persona, voice, preferences, or facts if they are not already present in existing_context_memories. "
                "Reuse existing_memory_terms.buckets and existing_memory_terms.threads when they fit before creating new names. "
                "LANGUAGE: write bucket/threads/summary/content in the language the user is chatting in — "
                "if they chat in Chinese, use Chinese (e.g. 「宠物」not \"pets\", 「旅行」not \"travel\"); only keep proper nouns / brand names / their verbatim quotes in the original. "
                "Shape: {\"memories\":[{\"summary\":\"...\",\"content\":\"记忆\\n...\\n\\n上下文\\n...\\n\\n使用提示\\n...\","
                "\"bucket\":\"...\",\"threads\":[\"...\"],\"importance\":0.0,\"pulse\":0.0,\"occurred_at\":\"YYYY-MM-DD\",\"source\":\"chat\"}]}. "
                "Return {\"memories\":[]} if nothing durable should be written."
                "\n\n"
                + MEMORY_WRITE_GUIDANCE_V1
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:12000]},
    ]


def build_web_search_results_message(web_search: dict[str, Any]) -> dict[str, str]:
    payload = {
        "tool": "web_search",
        "status": web_search.get("status", ""),
        "result_count": web_search.get("result_count", 0),
        "results": web_search.get("results", []),
        "errors": web_search.get("errors", []),
    }
    return {
        "role": "system",
        "content": (
            "Backend web_search tool results JSON:\n"
            + json.dumps(payload, ensure_ascii=False)[:12000]
        ),
    }


def web_search_followup_message() -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Use the backend web_search results above to answer the original user message. "
            "Return the normal Feedling chat turn JSON only."
        ),
    }
