#!/usr/bin/env python3
"""
Seed / sync server-managed UI copy from the iOS string catalog
==============================================================
Extracts en + zh-Hans for a chosen set of keys from the app's
Localizable.xcstrings and pushes them to the backend's admin copy endpoint
(POST /v1/copytext). This is the zero-deploy way to put copy under server
management: pick keys, run this, done — no app release, no backend deploy.

Scope is intentionally a curated key list (see DEFAULT_KEYS), not all ~980
keys. Add keys here (or pass --keys / --keys-file) as more copy gets adopted.

Usage:
  FEEDLING_API_URL=http://localhost:5001 \
  FEEDLING_ADMIN_TOKEN=<token> \
  python tools/seed_copytext.py \
      --xcstrings ../feedling-mcp-ios/App/FeedlingTest/Localizable.xcstrings

  # override the key set
  python tools/seed_copytext.py --keys chat.empty.title api_setup.ai_persona.description
  python tools/seed_copytext.py --keys-file my_keys.txt        # one key per line

  # inspect what would be sent without writing
  python tools/seed_copytext.py --dry-run

Exit codes: 0 OK · 1 usage/IO error · 2 backend rejected the write.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

LANGS = ("en", "zh-Hans")

# Curated starter set — enough to exercise the override path end to end.
# Grow this list as copy is moved under server management.
DEFAULT_KEYS = [
    "chat.empty.title",
    "api_setup.ai_persona.description",
]

DEFAULT_XCSTRINGS = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "feedling-mcp-ios", "App", "FeedlingTest", "Localizable.xcstrings",
)


def extract(path: str, keys: list[str]) -> dict[str, dict[str, str]]:
    """{key: {lang: value}} for the requested keys from a Localizable.xcstrings.

    Source-language ('en') value can be implicit; fall back to the key itself
    (matches the app's `.localized` key-as-fallback behavior).
    """
    with open(path, "r", encoding="utf-8") as fh:
        catalog = json.load(fh)
    catalog_strings = catalog.get("strings") or {}
    out: dict[str, dict[str, str]] = {}
    for key in keys:
        locs = (catalog_strings.get(key) or {}).get("localizations") or {}
        by_lang: dict[str, str] = {}
        for lang in LANGS:
            value = locs.get(lang, {}).get("stringUnit", {}).get("value")
            if value is None and lang == "en":
                value = key
            if value is not None:
                by_lang[lang] = value
        if by_lang:
            out[key] = by_lang
        else:
            print(f"  ! key not found in catalog, skipping: {key}", file=sys.stderr)
    return out


def post(api_url: str, token: str, strings: dict) -> dict:
    body = json.dumps({"strings": strings}).encode("utf-8")
    req = urllib.request.Request(
        api_url.rstrip("/") + "/v1/copytext",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Token": token},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xcstrings", default=DEFAULT_XCSTRINGS, help="path to Localizable.xcstrings")
    ap.add_argument("--keys", nargs="*", help="explicit key list (overrides DEFAULT_KEYS)")
    ap.add_argument("--keys-file", help="file with one key per line (overrides DEFAULT_KEYS)")
    ap.add_argument("--dry-run", action="store_true", help="print payload, do not POST")
    args = ap.parse_args()

    keys = args.keys
    if args.keys_file:
        with open(args.keys_file, "r", encoding="utf-8") as fh:
            keys = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if not keys:
        keys = DEFAULT_KEYS

    path = os.path.abspath(args.xcstrings)
    if not os.path.exists(path):
        print(f"xcstrings not found: {path}", file=sys.stderr)
        return 1

    strings = extract(path, keys)
    if not strings:
        print("no keys extracted; nothing to do", file=sys.stderr)
        return 1

    print(f"extracted {len(strings)} key(s) from {path}:")
    print(json.dumps(strings, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n--dry-run: not posting")
        return 0

    api_url = os.environ.get("FEEDLING_API_URL")
    token = os.environ.get("FEEDLING_ADMIN_TOKEN")
    if not api_url or not token:
        print("set FEEDLING_API_URL and FEEDLING_ADMIN_TOKEN to write", file=sys.stderr)
        return 1
    try:
        result = post(api_url, token, strings)
    except urllib.error.HTTPError as e:
        print(f"backend rejected write: {e.code} {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"cannot reach backend: {e}", file=sys.stderr)
        return 2
    print(f"\nposted OK → revision {result.get('revision')}, "
          f"upserted {result.get('upserted')}, deleted {result.get('deleted')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
