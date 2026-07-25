#!/usr/bin/env python3
"""Product-facing smoke test for IO Memory readside.

Use this against a real backend + real account API key to see what changed:
the agent can first inspect a safe memory index, then fetch full cards by id.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
import urllib.error
import urllib.request


BLOCKED_INDEX_FIELDS = {"verbatim", "her_quote", "follow_up", "sensitive_scope"}


def _post_json(base_url: str, path: str, api_key: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} from {path}: {detail}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach backend {base_url}: {e}") from e


def _clip(value: object, width: int = 96) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


def _print_index(items: list[dict]) -> None:
    print("\n=== 1. index: agent 先看到的安全摘要目录 ===")
    if not items:
        print("没有返回 index item。人话：这个账号可能还没有可被 enclave 读取的 shared memory。")
        return
    for idx, item in enumerate(items, start=1):
        blocked = sorted(BLOCKED_INDEX_FIELDS.intersection(item.keys()))
        print(
            f"{idx:02d}. {item.get('id', '')} | "
            f"salience={item.get('salience', 'medium')} | "
            f"sensitive={item.get('is_sensitive', False)} | "
            f"score={item.get('score', 0)}"
        )
        print(f"    summary: {_clip(item.get('summary'))}")
        if item.get("bucket_refs"):
            print(f"    buckets: {', '.join(map(str, item.get('bucket_refs') or []))}")
        if blocked:
            print(f"    FAIL: index leaked blocked fields: {blocked}")


def _print_fetch(items: list[dict], missing_ids: list[str], unavailable_ids: list[str]) -> None:
    print("\n=== 2. fetch: agent 命中后拿到的完整正文 ===")
    if missing_ids:
        print(f"missing_ids: {missing_ids}")
    if unavailable_ids:
        print(f"unavailable_ids: {unavailable_ids}")
    if not items:
        print("没有 fetch 到正文。人话：可能 index 为空，或这些 id 解不开/不可读。")
        return
    for idx, item in enumerate(items, start=1):
        print(f"{idx:02d}. {item.get('id', '')}")
        print(f"    summary : {_clip(item.get('summary'))}")
        print(f"    verbatim: {_clip(item.get('verbatim'))}")
        if item.get("follow_up"):
            print(f"    follow  : {_clip(item.get('follow_up'))}")
        if item.get("context"):
            print(f"    context : {_clip(item.get('context'))}")
        if "sensitive_scope" in item:
            print("    FAIL: fetch leaked concrete sensitive_scope")


def _print_acceptance(index_items: list[dict], fetch_body: dict) -> None:
    leaked = [
        item.get("id", "")
        for item in index_items
        if BLOCKED_INDEX_FIELDS.intersection(item.keys())
    ]
    print("\n=== 3. 产品验收结论 ===")
    print(f"index_count={len(index_items)}")
    print(f"fetch_count={len(fetch_body.get('items') or [])}")
    print(f"index_no_raw_quote={'PASS' if not leaked else 'FAIL'}")
    print(f"missing_ids={fetch_body.get('missing_ids') or []}")
    print(f"unavailable_ids={fetch_body.get('unavailable_ids') or []}")
    print(
        textwrap.dedent(
            """
            人话：
            - index_count > 0：说明 agent 能先看到记忆目录。
            - index_no_raw_quote=PASS：说明目录没有暴露原话/敏感 scope。
            - fetch_count > 0：说明 agent 可以按 id 拿到正文。
            - unavailable_ids 有值：通常是 local_only、无 K_enclave、被归档、或解密失败。
            """
        ).strip()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test /v1/memory/index + /v1/memory/fetch with a real account.")
    parser.add_argument("--backend-url", default=os.environ.get("FEEDLING_BACKEND_URL", "http://127.0.0.1:5001"))
    parser.add_argument("--api-key", default=os.environ.get("FEEDLING_API_KEY", ""))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--fetch", type=int, default=3, help="How many index ids to fetch.")
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Set FEEDLING_API_KEY or pass --api-key.")

    index_body = _post_json(args.backend_url, "/v1/memory/index", args.api_key, {"limit": args.limit})
    index_items = index_body.get("items") if isinstance(index_body.get("items"), list) else []
    _print_index(index_items)

    ids = [str(item.get("id") or "") for item in index_items[: max(0, args.fetch)] if item.get("id")]
    fetch_body = {"items": [], "missing_ids": [], "unavailable_ids": []}
    if ids:
        fetch_body = _post_json(args.backend_url, "/v1/memory/fetch", args.api_key, {"ids": ids})
    _print_fetch(
        fetch_body.get("items") if isinstance(fetch_body.get("items"), list) else [],
        fetch_body.get("missing_ids") if isinstance(fetch_body.get("missing_ids"), list) else [],
        fetch_body.get("unavailable_ids") if isinstance(fetch_body.get("unavailable_ids"), list) else [],
    )
    _print_acceptance(index_items, fetch_body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
