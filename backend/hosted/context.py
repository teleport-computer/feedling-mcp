"""Hosted chat context assembly (history + identity + memories + screen)."""






def _context_refs_from_payload(payload: dict) -> list[dict]:
    refs = payload.get("context_refs") or payload.get("contextRefs") or []
    if not isinstance(refs, list):
        return []
    out: list[dict] = []
    for ref in refs[:8]:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type") or "").strip()
        ref_id = str(ref.get("id") or "").strip()
        if not ref_type or not ref_id:
            continue
        clean = {"type": ref_type[:40], "id": ref_id[:160]}
        if ref.get("title"):
            clean["title"] = str(ref.get("title") or "")[:240]
        out.append(clean)
    return out
