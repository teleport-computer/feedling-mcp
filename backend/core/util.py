"""Small dependency-free helpers shared across domains."""

import json
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _now_iso() -> str:
    return datetime.now().isoformat()


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _strip_json_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _json_from_model_text(text: str):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    try:
        return json.loads(raw)
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
            return obj
        except Exception:
            continue
    raise ValueError("no json object found")


def _to_epoch(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _epoch_to_iso(epoch: float) -> str:
    try:
        if epoch and epoch > 0:
            return datetime.fromtimestamp(float(epoch)).isoformat()
    except Exception:
        pass
    return ""
