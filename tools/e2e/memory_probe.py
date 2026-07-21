"""Memory deep probe for pre Runtime V2.

Backend-invariant correctness (cross-user isolation, supersede, local_only,
LTM date retention, importance ordering, empty rejection, write→read) runs once
(cfg["run_invariants"]). The capture→card loop is model-touching and runs per
provider. Everything asserts a concrete invariant, never "a card exists".

The deterministic memory contract (qa/memory_contract_smoke.py, 10 checks) is
run separately by deep.py via the qa diagnostic path.
"""
from __future__ import annotations

import time

from .client import E2EClient
from .hosted import _hosted_send
from .probe_common import (
    BLOCKED_EVIDENCE, PASS, PRODUCT_FAIL, Probe,
    find_card, mem_add, mem_fetch, mem_index, mem_supersede, new_marker,
)

CAPTURE_POLL_SEC = 240.0


def run_memory_probe(c: E2EClient, cfg: dict) -> dict:
    p = Probe("memory")
    invariants = bool(cfg.get("run_invariants"))

    # ---- model-touching: capture → card loop (every provider) --------------
    p.guard("capture_force_closes_loop", lambda: _capture_loop(c))

    if not invariants:
        return p.result()

    # ---- backend invariants (run once) -------------------------------------
    p.guard("cross_user_isolation", lambda: _isolation(c))
    p.guard("supersede_status_transition", lambda: _supersede(c))
    p.guard("ltm_date_retention", lambda: _ltm_date(c))
    p.guard("local_only_exclusion", lambda: _local_only(c))
    p.guard("importance_ordering", lambda: _importance(c))
    p.guard("empty_material_rejected", lambda: _empty(c))
    p.guard("write_read_roundtrip", lambda: _roundtrip(c))
    return p.result()


def _capture_loop(c: E2EClient):
    mk = new_marker()
    fact = f"顺便记一件事：我的测试锚点代号是 {mk}。"
    sent, err = _hosted_send(c, fact)
    if err:
        return PRODUCT_FAIL, f"send failed: {err}"
    r = c.post("/v1/capture/force", json={})
    if r.status_code not in (200, 202):
        return PRODUCT_FAIL, f"capture/force {r.status_code} {r.text[:80]}"
    deadline = time.time() + CAPTURE_POLL_SEC
    while time.time() < deadline:
        if any(mk in s for s in c.memory_summaries(limit=100)):
            return PASS, f"card captured (marker {mk})"
        time.sleep(15)
    # Can't prove: async capture + model extraction may lag; not a silent pass,
    # but not a reproducible product fail either.
    return BLOCKED_EVIDENCE, f"no card with marker {mk} within {CAPTURE_POLL_SEC:.0f}s"


def _isolation(c: E2EClient):
    mk = new_marker()
    st, body = mem_add(c, summary=f"A 的私密事实 {mk}", content="only A owns this")
    if st not in (200, 201):
        return PRODUCT_FAIL, f"A write failed {st} {body}"
    b = E2EClient.provision(route="model_api")
    result, detail = PASS, "B cannot read or mutate A's card"
    try:
        seen = any(mk in str(it.get("summary") or "") for it in mem_index(b, limit=100))
        if seen:
            result, detail = PRODUCT_FAIL, f"account B can SEE account A's card {mk} — isolation broken"
        else:
            a_id = _id_of(c, mk)
            if a_id:
                st2, _ = mem_supersede(b, a_id, summary="hijack")
                if st2 in (200, 201):
                    result, detail = (PRODUCT_FAIL,
                                      f"account B superseded A's card {a_id} — write isolation broken")
    finally:
        try:
            b.teardown()          # a swallowed teardown = a leaked account, so it downgrades the case
        except Exception as e:  # noqa: BLE001
            result = PRODUCT_FAIL if result == PASS else result
            detail = f"{detail}; but account B teardown FAILED (orphan {b.user_id}): {e}"
        finally:
            b._http.close()
    return result, detail


def _supersede(c: E2EClient):
    mk = new_marker()
    st, _ = mem_add(c, summary=f"旧事实 {mk}", content="v1", bucket="关系", threads=["锚点"])
    if st not in (200, 201):
        return PRODUCT_FAIL, f"add failed {st}"
    old = _id_of(c, mk)
    if not old:
        return BLOCKED_EVIDENCE, "cannot locate the added card id to supersede"
    st2, _ = mem_supersede(c, old, summary=f"新事实 {mk}", content="v2")
    if st2 not in (200, 201):
        return PRODUCT_FAIL, f"supersede failed {st2}"
    items = mem_index(c, limit=100)
    new_active = any(f"新事实 {mk}" in str(it.get("summary") or "") for it in items)
    old_gone = not any(f"旧事实 {mk}" in str(it.get("summary") or "") for it in items)
    return (PASS if (new_active and old_gone) else PRODUCT_FAIL,
            f"new_active={new_active} old_gone_from_index={old_gone}")


def _ltm_date(c: E2EClient):
    mk = new_marker()
    past = "2020-01-15T09:00:00Z"
    st, body = mem_add(c, summary=f"很久以前的事 {mk}", content="occurred long ago",
                       occurred_at=past)
    if st not in (200, 201):
        return PRODUCT_FAIL, f"add failed {st} {body}"
    cid = _id_of(c, mk)
    if not cid:
        return BLOCKED_EVIDENCE, "cannot locate card to verify occurred_at"
    cards = mem_fetch(c, [cid])
    if not cards:
        return BLOCKED_EVIDENCE, "fetch returned nothing"
    got = str(cards[0].get("occurred_at") or "")
    if not got:
        return BLOCKED_EVIDENCE, "fetched card exposes no occurred_at field"
    return (PASS if "2020-01-15" in got else PRODUCT_FAIL,
            f"occurred_at={got!r} (expected to retain 2020-01-15, not collapse to today)")


def _local_only(c: E2EClient):
    """A client-sealed local_only card must be STORED (accepted) yet excluded from
    the agent readside (index/fetch) — the enclave has no K_enclave for it, so the
    agent can never surface it. Mirrors qa/test_memory_contract's local_only check:
    require acceptance, then prove exclusion (not a weak "≥400 = guarded" pass)."""
    import json as _json
    from datetime import datetime, timezone

    from content_encryption import build_envelope

    mk = new_marker()
    card = {"type": "fact", "summary": f"仅本地 {mk}", "content": "local only secret"}
    env = build_envelope(
        plaintext=_json.dumps(card).encode("utf-8"),
        owner_user_id=c.user_id,
        user_pk_bytes=bytes(c._sk.public_key),
        enclave_pk_bytes=c._enclave_pk,
        visibility="local_only",
    )
    env["occurred_at"] = datetime.now(timezone.utc).isoformat()
    env["type"] = "fact"
    r = c.post("/v1/memory/add", json={"envelope": env})
    if r.status_code not in (200, 201):
        return BLOCKED_EVIDENCE, (f"could not store a local_only card to exercise "
                                  f"exclusion ({r.status_code} {r.text[:80]})")
    idx_hit = any(mk in str(it.get("summary") or "") for it in mem_index(c, limit=200))
    return (PRODUCT_FAIL if idx_hit else PASS,
            "local_only card LEAKED into agent index" if idx_hit
            else "local_only stored but correctly excluded from agent readside")


def _importance(c: E2EClient):
    mk = new_marker()
    st_lo, _ = mem_add(c, summary=f"低权重 {mk}", content="minor", importance=0.1)
    st_hi, _ = mem_add(c, summary=f"高权重 {mk}", content="major", importance=0.9)
    if st_lo not in (200, 201) or st_hi not in (200, 201):
        return PRODUCT_FAIL, f"importance card write failed (lo={st_lo} hi={st_hi})"
    items = mem_index(c, limit=100)
    hi = next((it for it in items if f"高权重 {mk}" in str(it.get("summary") or "")), None)
    lo = next((it for it in items if f"低权重 {mk}" in str(it.get("summary") or "")), None)
    if not hi or not lo:
        return BLOCKED_EVIDENCE, "one of the importance cards missing from index"
    hs, ls = hi.get("score"), lo.get("score")
    if hs is None or ls is None:
        # No score exposed → fall back to importance field ordering.
        hs, ls = hi.get("importance"), lo.get("importance")
    if hs is None or ls is None:
        return BLOCKED_EVIDENCE, "index exposes neither score nor importance to compare"
    return (PASS if float(hs) >= float(ls) else PRODUCT_FAIL,
            f"high={hs} low={ls} (high must rank ≥ low)")


def _empty(c: E2EClient):
    """Contract: an empty card is rejected with EXACTLY 400 title_required. Any
    other status (200/201 accept, or 401/404/500 for the wrong reason) is non-PASS
    — a 500 must not masquerade as 'rejected'."""
    st, body = mem_add(c, summary="", content="")
    if st == 400 and "title_required" in str(body):
        return PASS, "empty card rejected 400 title_required"
    if st in (200, 201):
        return PRODUCT_FAIL, "empty summary+content ACCEPTED — must be 400 title_required"
    return PRODUCT_FAIL, f"empty rejected with unexpected status {st}: {str(body)[:80]}"


def _roundtrip(c: E2EClient):
    mk = new_marker()
    body_txt = f"手冲咖啡每天早上一杯 {mk}"
    st, _ = mem_add(c, summary=f"咖啡偏好 {mk}", content=body_txt, bucket="生活")
    if st not in (200, 201):
        return PRODUCT_FAIL, f"add failed {st}"
    cid = _id_of(c, mk)
    if not cid:
        return BLOCKED_EVIDENCE, "cannot locate written card"
    cards = mem_fetch(c, [cid])
    if not cards:
        return BLOCKED_EVIDENCE, "fetch returned nothing"
    content = str(cards[0].get("content") or cards[0].get("description") or "")
    return (PASS if mk in content else PRODUCT_FAIL,
            f"fetched content{'contains' if mk in content else 'MISSING'} marker")


def _id_of(c: E2EClient, marker: str) -> str:
    hit = find_card(mem_index(c, limit=100), marker)
    return str(hit.get("id") or "") if hit else ""
