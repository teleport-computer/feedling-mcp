"""Shared detector for torn / leaked agent-protocol JSON.

A reasoning model behind a stream-cutting relay can split one protocol envelope
``{"messages":[],"actions":[{"type":"proactive.sleep","reason":"..."}]}`` across
the provider's ``reasoning_content`` / ``content`` channel boundary: the head
lands in the reasoning channel, the tail in the visible message. Every historical
guard anchored on the JSON *head* (``"actions":`` / ``"type":"proactive.X"``), so
once the head is torn into the reasoning channel the tail leaks into a chat
bubble as innocuous-looking prose.

This module is a **pure detector**: no I/O, no business deps (per CONTRIBUTING it
lives in ``core/`` so V1 resident-consumer, Proactive Runtime V2 and Model API
Runtime V2 can all import it). It returns an *evidence enum*, never an executable
action — the whole point is that a turn we know was corrupted in transit must not
have its "reconstructed" action executed. Lane policy (drop vs keep, fallback vs
silence) lives in each runtime, not here.

Evidence tiers, strongest first:

- ``JOINED_KNOWN_PROTOCOL`` — reasoning-suffix + visible rejoin to a complete,
  known protocol envelope that consumes to the end. Proof of a single-cut tear.
- ``HEAD_IN_REASONING`` — the rejoin does not parse (multi-cut / lost middle) but
  the reasoning channel independently carries a protocol head, and the visible
  text is a JSON fragment. Users never author a protocol head into the reasoning
  channel; only the torn relay does.
- ``TRANSPORT_CUT`` — the transport itself reported a stream cut this turn and the
  visible text is a JSON fragment. Independent proof the pipe broke.
- ``ORPHAN_JSON_TAIL`` — WEAK: the visible text alone looks like a structural JSON
  fragment (string-ignoring bracket balance goes negative + a JSON token). High
  recall, but indistinguishable from a user pasting JSON / discussing code, so it
  is proactive-lane-only and never a foreground drop.
- ``NONE`` — no evidence.
"""
from __future__ import annotations

import json
import re
from typing import Any

JOINED_KNOWN_PROTOCOL = "joined_known_protocol"
HEAD_IN_REASONING = "head_in_reasoning"
TRANSPORT_CUT = "transport_cut"
ORPHAN_JSON_TAIL = "orphan_json_tail"
NONE = "none"

_STRONG = frozenset({JOINED_KNOWN_PROTOCOL, HEAD_IN_REASONING, TRANSPORT_CUT})

# Known protocol action vocabulary (union across V1 + both V2 subsystems). Used
# only to decide "is a rejoined object a *known* protocol envelope", never to
# execute anything.
_KNOWN_ACTION_TYPES = frozenset({
    "sleep", "send_message", "schedule_wake", "cancel_wake",
    "request_broadcast", "needs_background",
})
_KNOWN_ACTION_PREFIXES = ("identity.", "memory.", "proactive.")

# A protocol key sitting in a JSON key position, or an identity/memory/proactive
# typed action — search-anywhere (NOT head-anchored). A prose mention like
# `the "actions" field` never matches (needs a `{`/`,`/line-start before it).
_HEAD_KEY_RE = re.compile(r'(?:[{,]|^|\n)\s*"(?:actions|messages|tool_calls|cards)"\s*:')
_HEAD_TYPED_RE = re.compile(r'"type"\s*:\s*"(?:identity|memory|proactive)\.\w+"')
# A protocol-closing tail like `..."}]}` / `...}]` — a quoted value immediately
# followed by two structural closers.
_TAIL_CLOSE_RE = re.compile(r'"\s*[}\]]\s*[}\]]')


def _naive_min_depth(text: str) -> int:
    """Running bracket balance IGNORING string context. A torn tail closes
    brackets it never opened -> the balance dips below zero. String-aware
    counting is deliberately NOT used: a tail that begins mid-string-value
    (`active.sleep","reason":...`) misaligns quote parity and reads as balanced,
    hiding the very leak we are trying to catch."""
    depth = 0
    mind = 0
    for ch in text:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < mind:
                mind = depth
    return mind


def _has_json_token(text: str) -> bool:
    return ('":' in text) or bool(_TAIL_CLOSE_RE.search(text))


def is_orphan_json_tail(text: Any) -> bool:
    """WEAK signal: the visible text alone is a structural JSON fragment — it
    closes more brackets than it opens AND carries a JSON token. High recall
    (some legit JSON-in-prose trips it too), so callers must gate it by lane."""
    t = str(text or "")
    if _naive_min_depth(t) >= 0:
        return False
    return _has_json_token(t)


def looks_like_protocol_head(text: Any) -> bool:
    """Structural evidence of a protocol payload's HEAD: a protocol key in a JSON
    key position, or an identity/memory/proactive typed action. Used on the
    reasoning channel (model-authored thinking), where a match is a reliable
    fingerprint of a torn envelope."""
    t = str(text or "")
    if not (_HEAD_KEY_RE.search(t) or _HEAD_TYPED_RE.search(t)):
        return False
    return "{" in t or "[" in t


def _is_supported_action(action: Any) -> bool:
    if not isinstance(action, dict):
        return False
    typ = str(action.get("type") or action.get("action") or "").strip()
    if not typ:
        return False
    return typ.startswith(_KNOWN_ACTION_PREFIXES) or typ in _KNOWN_ACTION_TYPES


def _is_protocol_object(obj: Any) -> bool:
    """A top-level protocol envelope: a messages/tool_calls/cards list, or an
    actions list carrying at least one known action type."""
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("messages"), list):
        return True
    actions = obj.get("actions")
    if isinstance(actions, list) and any(_is_supported_action(a) for a in actions):
        return True
    if isinstance(obj.get("tool_calls"), list) and obj.get("tool_calls"):
        return True
    if isinstance(obj.get("cards"), list):
        return True
    return False


def _rejoins_to_protocol(reasoning_text: str, visible_text: str) -> bool:
    """True iff some `{`/`[` start in the reasoning channel, concatenated with
    the visible text, parses as a complete known protocol envelope consuming to
    the very end. Proof of a cross-channel tear. Never returns the object — the
    reconstructed action must not be executed."""
    r = str(reasoning_text or "")
    v = str(visible_text or "")
    if not v:
        return False
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", r):
        candidate = r[m.start():] + v
        try:
            obj, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if end == len(candidate) and _is_protocol_object(obj):
            return True
    return False


def _visible_is_fragmentish(visible_text: str) -> bool:
    return is_orphan_json_tail(visible_text) or looks_like_protocol_head(visible_text)


def classify(visible_text: Any, *, reasoning_text: Any = "", transport_cut: bool = False) -> str:
    """Classify a visible reply into an evidence tier (see module docstring).
    Returns an enum string, never an action."""
    v = str(visible_text or "")
    if not v.strip():
        return NONE
    r = str(reasoning_text or "")
    if r and _rejoins_to_protocol(r, v):
        return JOINED_KNOWN_PROTOCOL
    if r and looks_like_protocol_head(r) and _visible_is_fragmentish(v):
        return HEAD_IN_REASONING
    if transport_cut and _visible_is_fragmentish(v):
        return TRANSPORT_CUT
    if is_orphan_json_tail(v):
        return ORPHAN_JSON_TAIL
    return NONE


def is_leak(evidence: str) -> bool:
    return evidence != NONE


def is_strong(evidence: str) -> bool:
    return evidence in _STRONG


def should_suppress(evidence: str, *, lane: str) -> bool:
    """Lane policy. Proactive/wake suppress any leak (silence is the correct
    proactive outcome anyway, and a bracket-junky bubble at 5am is never a real
    message). Foreground suppresses only on STRONG evidence — a weak orphan tail
    might be a user pasting JSON, and dropping it would eat a real message."""
    if evidence == NONE:
        return False
    if lane == "proactive":
        return True
    return is_strong(evidence)
