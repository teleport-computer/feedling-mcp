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
    AGENT_ERROR, BLOCKED_EVIDENCE, PASS, PRODUCT_FAIL, Probe, install_identity, new_marker,
)

_CJK = re.compile(r"[一-鿿]")
# transcript/system labels (EN + zh) that must never head a user-visible card line
# — the usr_fee1 bug was a card whose text began `user: ...`.
_FORBIDDEN_LABEL = re.compile(
    r"(?im)^\s*(user|agent|assistant|openclaw|system|role|用户|助手|系统|角色)\s*[:：]")
# bare "用户"/"TA" standing in for the person (usr_fee1) — a card/message calling the
# user "用户"/"TA" instead of their name. Matched only before a person-predicate, so
# common compounds (用户体验 / 用户界面 / 用户端 / 用户名) are NOT over-blocked.
_PERSON_PRED = "是喜欢爱愛会會想觉得叫认為为很不在有说做喝吃住偏好习惯應应该的"
_BARE_YONGHU = re.compile(f"用户(?=[{_PERSON_PRED}])")
# TA as a bare Chinese pronoun subject (not embedded in a Latin word like "data").
_BARE_TA = re.compile(f"(?<![0-9A-Za-z一-鿿])[Tt][Aa](?=[{_PERSON_PRED}\\s，,。])")


def _bare_person_label(text: str):
    return _BARE_YONGHU.search(text or "") or _BARE_TA.search(text or "")
# tolerate a stray CJK proper noun in an English reply; only a real run of CJK is drift
_CJK_DRIFT_MIN = 4


def _cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def run_experience_probe(c: E2EClient, cfg: dict) -> dict:
    p = Probe("experience")
    p.guard("injected_text_audit", lambda: _injected_text(c))
    # language checks MUST run on a dedicated fresh account: the shared account's
    # history is full of Chinese turns from the other probes, and a model rightly
    # mirrors recent language — testing English-persona drift there measures
    # contamination, not drift. One clean English account isolates the real question.
    _language_isolated(cfg, p)
    # error attribution mutates the account's provider config, so run it LAST
    p.guard("error_bubble_attribution", lambda: _error_attribution(c, cfg))
    return p.result()


def _english_persona(c: E2EClient) -> tuple[int, dict]:
    return install_identity(c, {
        "agent_name": "Ivy",
        "self_introduction": "I am Ivy, a calm English-speaking companion. I always reply in English.",
        "category": "companion",
        "signature": "— Ivy",
        "language_preference": "en",
        "tone_style": "Warm, concise English. Never switch to Chinese unless the user does.",
        "custom_persona_prompt": "Always speak English, including when you open a conversation proactively.",
        "dimensions": [{"name": "warmth", "value": 80, "description": "gentle and present"}],
    })


def _no_cjk_drift(text: str) -> bool:
    """The §4.5 bug is Chinese leaking into a non-Chinese persona ("设英文人设却冒
    中文"). This predicate detects exactly that: no run of CJK, and Latin letters
    present/dominant. It deliberately does NOT claim to tell English from French or
    other Latin scripts — that needs a language-ID dependency. So a PASS means "did
    not drift to Chinese", never "is English"."""
    if _cjk_count(text) >= _CJK_DRIFT_MIN:
        return False
    letters = [ch for ch in text if ch.isalpha()]
    latin = [ch for ch in letters if ch.isascii()]
    return len(latin) >= 8 and len(latin) / max(1, len(letters)) >= 0.7


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
    cid = str(card.get("id") or "")
    r = c.post("/v1/memory/fetch", json={"ids": [cid], "limit": 5})
    r.raise_for_status()
    full = next((it for it in (r.json().get("items") or [])
                 if str(it.get("id") or "") == cid), None)
    if full is None:
        return BLOCKED_EVIDENCE, f"fetch returned no item for the captured card id {cid[:8]}"
    # audit every user-visible text field of the stored card, not just the summary
    parts = [str(full.get("summary") or ""), str(full.get("content") or ""),
             str(full.get("bucket") or "")]
    parts += [str(t) for t in (full.get("threads") or [])]
    hit = next((m for p in parts
                for m in (_FORBIDDEN_LABEL.search(p) or _bare_person_label(p),) if m), None)
    return (PRODUCT_FAIL if hit else PASS,
            f"card field carries a transcript/bare-person label {hit.group(0)!r}" if hit
            else "captured card (summary/content/bucket/threads) free of role/bare-person labels")


def _language_isolated(cfg: dict, p: Probe) -> None:
    """Run BOTH the reactive and proactive no-Chinese-drift checks (§4.5) on ONE
    dedicated fresh account, so the shared account's Chinese history from other
    probes can't contaminate them. Adds two cases to `p`; a teardown failure adds a
    cleanup PRODUCT_FAIL (account hygiene)."""
    b = E2EClient.provision(route="model_api")
    try:
        reactive, proactive = _language_checks(b, cfg)
    except Exception as e:  # noqa: BLE001
        reactive = proactive = (AGENT_ERROR, f"{type(e).__name__}: {e}")
    note = ""
    try:
        b.teardown()
    except Exception as e:  # noqa: BLE001
        note = f"isolated language account teardown FAILED (orphan {b.user_id}): {e}"
    finally:
        b._http.close()
    p.add("no_chinese_drift_reactive", *reactive)
    p.add("no_chinese_drift_proactive", *proactive)
    if note:
        p.add("language_account_cleanup", PRODUCT_FAIL, note)


def _language_checks(b: E2EClient, cfg: dict):
    """Returns (reactive_case, proactive_case). Order matters: 3 English turns →
    PROACTIVE check (while the context is still English-only) → the Chinese mirror
    LAST, so the mirror can't pollute the proactive opener's language."""
    payload = {"provider": cfg.get("provider"), "model": cfg.get("model"), "api_key": cfg.get("key")}
    if cfg.get("base_url"):
        payload["base_url"] = cfg["base_url"]
    if b.post("/v1/model_api/setup", json=payload).status_code != 200:
        r = (BLOCKED_EVIDENCE, "could not set up the isolated language account")
        return r, r
    st, _ = _english_persona(b)
    if st not in (200, 201):
        r = (BLOCKED_EVIDENCE, "could not install English persona on the isolated account")
        return r, r

    reactive_fail = None
    for i in range(3):
        text = _one_reply(b, f"Turn {i+1}: tell me one short encouraging sentence in English.")
        if text is None:
            reactive_fail = (PRODUCT_FAIL, f"no reply on English turn {i+1}")
            break
        if not _no_cjk_drift(text):
            reactive_fail = (PRODUCT_FAIL, f"English persona drifted to Chinese on turn {i+1}: {text[:60]!r}")
            break

    # proactive while the account has spoken only English so far
    proactive = _proactive_check(b)

    # Chinese mirror LAST — it must not pollute the proactive language check above
    if reactive_fail is not None:
        return reactive_fail, proactive
    mirror = _one_reply(b, "换成中文回我一句就好，谢谢。")
    if mirror is None:
        return (PRODUCT_FAIL, "no reply on the Chinese mirror turn"), proactive
    reactive = (PASS if _cjk_count(mirror) >= _CJK_DRIFT_MIN else PRODUCT_FAIL,
                f"3 turns had no Chinese drift; Chinese turn mirrored CJK={_cjk_count(mirror)}")
    return reactive, proactive


def _proactive_check(b: E2EClient):
    b.post("/v1/proactive/settings", json={"ambient": True, "timezone": "Asia/Shanghai"})
    # prime a next-proactive-message intent so the wake actually produces an opener.
    # The priming reply MUST land — otherwise there is no valid setup to language-check.
    if _one_reply(b, "Later, when you reach out to me first, just send a short warm "
                     "check-in. For now, only confirm you got this.") is None:
        return BLOCKED_EVIDENCE, "priming reply never arrived; cannot set up the proactive check"
    since = time.time()
    tick = b.post("/v1/proactive/tick", json={"force": True})
    if tick.status_code != 200:
        return BLOCKED_EVIDENCE, f"could not force a proactive wake ({tick.status_code})"
    job = tick.json().get("job") if isinstance(tick.json().get("job"), dict) else {}
    job_id = str(job.get("id") or "")
    if job.get("lane") != "manual_wake" or not job_id:
        return BLOCKED_EVIDENCE, f"tick did not admit a manual_wake job: {tick.text[:80]}"
    # correlate the OPENER by proactive_job_id — never a late ordinary reply by timestamp
    reply = _wait_proactive_reply(b, job_id, since, timeout=180.0)
    if reply is None:
        return BLOCKED_EVIDENCE, f"no proactive message correlated to wake job {job_id[:8]}"
    text = b.message_text(reply)
    if not text.strip():
        return BLOCKED_EVIDENCE, "proactive message did not decrypt for a language check"
    if _FORBIDDEN_LABEL.search(text) or _bare_person_label(text):
        return PRODUCT_FAIL, f"proactive opener carries a system/bare-person label: {text[:60]!r}"
    return (PASS if _no_cjk_drift(text) else PRODUCT_FAIL,
            f"isolated English-persona proactive opener (job {job_id[:8]}) "
            f"{'had no Chinese drift' if _no_cjk_drift(text) else 'DRIFTED to Chinese'}: {text[:60]!r}")


def _wait_proactive_reply(b: E2EClient, job_id: str, since: float, *, timeout: float):
    """An agent row correlated to THIS wake job by proactive_job_id — not any later
    agent row (which could be a lagging ordinary reply)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = b.get("/v1/chat/history", params={"since": max(0, since - 1), "limit": 100})
        r.raise_for_status()
        for m in (r.json().get("messages") or []):
            if (str(m.get("role") or "") in ("agent", "openclaw")
                    and str(m.get("proactive_job_id") or "") == job_id):
                return m
        time.sleep(3)
    return None


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
    # the adjacent drivable assertion: exact 503 runtime_policy_not_ready (not a 500,
    # not a 202-then-silence, not a wrong-route 404/401).
    ok_reject = False
    if r.status_code == 503:
        try:
            ok_reject = r.json().get("error") == "runtime_policy_not_ready"
        except Exception:  # noqa: BLE001
            ok_reject = False
    if r.status_code == 202:
        return PRODUCT_FAIL, "no-model send was ACCEPTED (202) — should reject, not silently drop the turn"
    if r.status_code >= 500 and r.status_code != 503:
        return PRODUCT_FAIL, f"no-model send returned {r.status_code} (server error, not a clean 503)"
    if not ok_reject:
        return PRODUCT_FAIL, f"no-model send off-contract: {r.status_code} {r.text[:80]} (want 503 runtime_policy_not_ready)"
    return (BLOCKED_EVIDENCE,
            "no-model send cleanly rejected 503 runtime_policy_not_ready; but the true "
            "stopped-key attribution bubble needs the mock relay (§4.6) — not covered here")


def _one_reply(c: E2EClient, text: str) -> str | None:
    """Send one turn and return its CORRELATED reply text (by user_message.id, so a
    lagging earlier turn's reply can't be misattributed as this turn's language)."""
    try:
        sent, uid = _send(c, text)
    except Exception:  # noqa: BLE001
        return None
    reply = _reply_for(c, uid, sent, timeout=180.0)
    return c.message_text(reply) if reply else None
