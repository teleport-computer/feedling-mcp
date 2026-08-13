"""World book readside core: match decrypted entries against recent messages."""

from __future__ import annotations

import os
from typing import Any

import httpx

import worldbook_match

WORLD_BOOK_CONTENT_CAP = 20000


def _entry_name(entry: dict) -> str:
    name = str(entry.get("name") or "").strip()
    if name:
        return name
    return str(entry.get("id") or "").strip()


def build_block(
    decrypted_entries: list[dict],
    recent_messages: list[dict],
    *,
    content_cap: int = WORLD_BOOK_CONTENT_CAP,
) -> dict:
    eligible: list[dict] = []
    rejected_over_cap: list[str] = []
    for entry in decrypted_entries or []:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "")
        if content_cap > 0 and len(content) > content_cap:
            entry_id = str(entry.get("id") or "").strip()
            if entry_id:
                rejected_over_cap.append(entry_id)
            continue
        eligible.append(entry)
    matched = worldbook_match.matched_entries(eligible, recent_messages)
    block = worldbook_match.build_world_book_block(matched, recent_messages)
    return {
        "block": block,
        "matched_names": [_entry_name(entry) for entry in matched if _entry_name(entry)],
        "rejected_over_cap": rejected_over_cap,
    }


def merge_match_results(parts: list[dict]) -> dict:
    lines: list[str] = []
    matched_names: list[str] = []
    rejected_over_cap: list[str] = []
    unavailable_ids: list[str] = []
    for part in parts:
        block = str(part.get("block") or "").strip()
        if block.startswith("<world_book>\n") and block.endswith("\n</world_book>"):
            block = block[len("<world_book>\n"):-len("\n</world_book>")]
        if block:
            lines.append(block)
        matched_names.extend(str(item) for item in part.get("matched_names") or [])
        rejected_over_cap.extend(str(item) for item in part.get("rejected_over_cap") or [])
        unavailable_ids.extend(str(item) for item in part.get("unavailable_ids") or [])
    return {
        "block": "<world_book>\n" + "\n".join(lines) + "\n</world_book>" if lines else "",
        "matched_names": matched_names,
        "rejected_over_cap": rejected_over_cap,
        "unavailable_ids": unavailable_ids,
    }


def post_enclave_worldbook_match(
    api_key: str | None,
    world_books: list[dict],
    messages: list[dict],
    *,
    runtime_token: str | None = None,
) -> dict:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if runtime_token:
        auth_headers = {"X-Feedling-Runtime-Token": runtime_token}
    elif api_key:
        auth_headers = {"X-API-Key": api_key}
    else:
        raise RuntimeError("api_key_unavailable")
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}/v1/worldbook/match",
                headers=auth_headers,
                json={"world_books": world_books, "messages": messages},
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    response: Any = resp.json()
    if not isinstance(response, dict):
        raise RuntimeError("enclave_invalid_worldbook_response")
    return response
