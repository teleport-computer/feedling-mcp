"""Experience / 体感 deep probe for pre Runtime V2 (docs/testing §4.5 / §4.7 / P1#12).

These are the "all green but still a disaster" checks: a reply arriving is not
enough — the wording, language, and error attribution have to be right.

- error_bubble_attribution (P1#12, §7): a bad provider key must surface an error
  bubble blamed on the user/provider ("your model service"), never on us and
  never a generic `unknown` class.
- injected_text_audit (§4.7, usr_fee1): model-visible products (memory cards)
  must not carry transcript/system labels (`user:` / `agent:` / role prefixes).
- language_adherence (§4.5): an English persona stays English across turns and
  mirrors a Chinese message only for that turn.
"""
from __future__ import annotations

import re
import time
import uuid

from .client import E2EClient
from .continuity_probe import _reply_for, _send
from .probe_common import (
    BLOCKED_EVIDENCE, PASS, PRODUCT_FAIL, Probe, install_identity, new_marker,
)

_CJK = re.compile(r"[一-鿿]")
# transcript/system labels that must never leak into a user-visible card
_FORBIDDEN_LABEL = re.compile(r"(?im)^\s*(user|agent|assistant|openclaw|system|role)\s*[:：]")
# tolerate a stray CJK proper noun in an English reply; only a real run of CJK is drift
_CJK_DRIFT_MIN = 4


def _cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def run_experience_probe(c: E2EClient, cfg: dict) -> dict:
    p = Probe("experience")
    p.guard("injected_text_audit", lambda: _injected_text(c))
    p.guard("language_adherence", lambda: _language(c))
    # error attribution mutates the account's provider config, so run it LAST
    p.guard("error_bubble_attribution", lambda: _error_attribution(c, cfg))
    return p.result()


def _injected_text(c: E2EClient):
    """Capture a fact via chat, then assert the stored card has no transcript
    labels (the usr_fee1 bug: a card summarized as `user: ...`)."""
    mk = new_marker()
    c.post("/v1/model_api/chat/send", json={
        "message": f"记一件事：我养了一只叫 {mk} 的橘猫，很重要。",
        "client_msg_id": str(uuid.uuid4())})
    c.post("/v1/capture/force", json={})
    deadline = time.time() + 240
    card = None
    while time.time() < deadline:
        r = c.post("/v1/memory/index", json={"limit": 100})
        r.raise_for_status()
        card = next((it for it in (r.json().get("items") or [])
                     if mk in str(it.get("summary") or "")), None)
        if card:
            break
        time.sleep(15)
    if not card:
        return BLOCKED_EVIDENCE, f"no card captured for marker {mk} to audit"
    r = c.post("/v1/memory/fetch", json={"ids": [card.get("id")], "limit": 5})
    r.raise_for_status()
    full = (r.json().get("items") or [{}])[0]
    # audit every user-visible text field of the stored card, not just the summary
    parts = [str(full.get("summary") or ""), str(full.get("content") or ""),
             str(full.get("bucket") or "")]
    parts += [str(t) for t in (full.get("threads") or [])]
    hit = next((m for m in (_FORBIDDEN_LABEL.search(p) for p in parts) if m), None)
    return (PRODUCT_FAIL if hit else PASS,
            f"card field carries transcript label {hit.group(0)!r}" if hit
            else "captured card (summary/content/bucket/threads) free of role labels")


def _language(c: E2EClient):
    st, body = install_identity(c, {
        "agent_name": "Ivy",
        "self_introduction": "I am Ivy, a calm English-speaking companion. I always reply in English.",
        "category": "companion",
        "signature": "— Ivy",
        "language_preference": "en",
        "tone_style": "Warm, concise English. Never switch to Chinese unless the user does.",
        "dimensions": [{"name": "warmth", "value": 80, "description": "gentle and present"}],
    })
    if st not in (200, 201):
        return BLOCKED_EVIDENCE, f"could not install English persona ({st} {str(body)[:80]})"
    # three English turns must each stay English (tolerate a stray CJK proper noun)
    for i in range(3):
        text = _one_reply(c, f"Turn {i+1}: tell me one short encouraging sentence in English.")
        if text is None:
            return PRODUCT_FAIL, f"no reply on English turn {i+1}"
        if _cjk_count(text) >= _CJK_DRIFT_MIN:
            return PRODUCT_FAIL, f"English persona drifted to Chinese on turn {i+1}: {text[:60]!r}"
    # one Chinese message → that turn mirrors Chinese
    mirror = _one_reply(c, "换成中文回我一句就好，谢谢。")
    if mirror is None:
        return PRODUCT_FAIL, "no reply on the Chinese mirror turn"
    return (PASS if _cjk_count(mirror) >= _CJK_DRIFT_MIN else PRODUCT_FAIL,
            f"3 English turns stayed English; Chinese turn CJK={_cjk_count(mirror)}")


def _error_attribution(c: E2EClient, cfg: dict):
    """The real P1#12 case — a stopped/expired provider key producing a
    user-actionable, correctly-blamed error bubble (never "our fault"/unknown) —
    CANNOT be driven on hosted V2: `/v1/model_api/setup` validates the key up front
    and refuses to save a bad one, and there is no seam to make a validated key fail
    on a turn. That live provider-auth fault is the mock-relay work (§4.6), so this
    is an honest BLOCKED_EVIDENCE, not a faked PASS.

    We DO verify the one adjacent behavior that IS drivable: a send with the model
    config removed is rejected CLEANLY and synchronously (a real 503
    runtime_policy_not_ready, not a 500 and not a silent 202-then-nothing). Runs last
    and teardown uses the account API key, so the removed model config is harmless."""
    if c._request("DELETE", "/v1/model_api/delete").status_code not in (200, 204):
        return BLOCKED_EVIDENCE, "could not remove model config; auth-fault attribution needs mock relay (§4.6)"
    r = c.post("/v1/model_api/chat/send",
               json={"message": "no-model send", "client_msg_id": str(uuid.uuid4())})
    if r.status_code >= 500 and r.status_code != 503:
        return PRODUCT_FAIL, f"no-model send returned {r.status_code} (server error, not a clean reject)"
    if r.status_code == 202:
        return PRODUCT_FAIL, "no-model send was ACCEPTED (202) — should reject, not silently drop the turn"
    return (BLOCKED_EVIDENCE,
            f"no-model send cleanly rejected ({r.status_code} {r.text[:60]}); but the true "
            f"stopped-key attribution bubble needs the mock relay (§4.6) — not covered here")


def _one_reply(c: E2EClient, text: str) -> str | None:
    """Send one turn and return its CORRELATED reply text (by user_message.id, so a
    lagging earlier turn's reply can't be misattributed as this turn's language)."""
    try:
        sent, uid = _send(c, text)
    except Exception:  # noqa: BLE001
        return None
    reply = _reply_for(c, uid, sent, timeout=180.0)
    return c.message_text(reply) if reply else None
