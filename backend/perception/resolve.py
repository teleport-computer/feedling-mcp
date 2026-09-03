"""Raw -> coarse-label resolvers.

These run on ingest. They take a RAW reported value (lat/lon, ssid, bundle id,
device context) plus the user's perception_config, and return only the resolved
coarse label(s). The caller persists the returned labels and DISCARDS the raw
value, so the agent never sees an address / SSID / exact bundle.

Each resolver returns a dict {output_field: value}. A value of None signals
"do not store this output" (e.g. a sensitive app bundle is reported as category
`sensitive` with the bundle id withheld).

The resolution logic is intentionally simple (user-marked home/work geofences +
known-network map). It lives here behind a clean seam so it can later move
wholesale into the enclave without touching service/routes.
"""
from __future__ import annotations

import math


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def resolve_geofence(value, config: dict) -> dict:
    """value (all forms accepted; values may be strings — see "默认字符串"):
      - "lat,lon"        e.g. "37.42,-122.08"
      - "home"           a direct place label hint
      - {"lat","lon"}    lat/lon may be str or number
      - {"place_hint"}   a direct label
    Raw coordinates are used transiently and discarded.
    config["geofences"]: [{"label": "home", "lat": .., "lon": .., "radius_m": 150}].
    Returns {"place_label": <home|work|gym|transit|unknown_place|unknown>}.
    ("unknown_place" = has a fix but matched no geofence; not "outdoors".)
    """
    if isinstance(value, str):
        s = value.strip()
        if "," in s:
            parts = s.split(",")
            try:
                value = {"lat": float(parts[0]), "lon": float(parts[1])}
            except (ValueError, IndexError):
                return {"place_label": "unknown"}
        else:
            return {"place_label": s or "unknown"}  # treated as a label hint
    if not isinstance(value, dict):
        return {"place_label": "unknown"}
    hint = value.get("place_hint")
    if isinstance(hint, str) and hint:
        return {"place_label": hint}
    lat, lon = value.get("lat"), value.get("lon")
    if lat is None or lon is None:
        return {"place_label": "unknown"}
    best_label, best_d = None, float("inf")
    for gf in (config.get("geofences") or []):
        try:
            d = _haversine_m(float(lat), float(lon), float(gf["lat"]), float(gf["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        radius = float(gf.get("radius_m", 150))
        if d <= radius and d < best_d:
            best_label, best_d = str(gf.get("label") or "unknown"), d
    return {"place_label": best_label or "unknown_place"}


def resolve_ssid(value, config: dict) -> dict:
    """value: {"ssid": ".."} or "ssid" (raw, discarded).
    config["ssid_labels"]: {"<ssid>": "home_wifi"}.
    Returns {"wifi_label": <home_wifi|work_wifi|public_wifi|unknown>}.
    """
    ssid = value.get("ssid") if isinstance(value, dict) else value
    if not isinstance(ssid, str) or not ssid:
        return {"wifi_label": "unknown"}
    mapped = (config.get("ssid_labels") or {}).get(ssid)
    return {"wifi_label": mapped or "public_wifi"}


def resolve_bundle(value, config: dict) -> dict:
    """value: {"bundle_id": ".."} or "bundle_id".
    config["sensitive_bundles"]: ["com.x.health", ...] (reported as `sensitive`).
    config["bundle_categories"]: {"<bundle>": "social"} overrides built-ins.
    Returns {"app_category": <..|sensitive>, "app_bundle": <bundle|None>}.
    """
    bundle = value.get("bundle_id") if isinstance(value, dict) else value
    if not isinstance(bundle, str) or not bundle:
        return {"app_category": "unknown", "app_bundle": None}
    if bundle in set(config.get("sensitive_bundles") or []):
        # category reported, identity withheld
        return {"app_category": "sensitive", "app_bundle": None}
    cat = (config.get("bundle_categories") or {}).get(bundle)
    if not cat:
        cat = _DEFAULT_BUNDLE_CATEGORIES.get(bundle, "unknown")
    return {"app_category": cat, "app_bundle": bundle}


# A tiny starter category map. Real lists live in DESIGN/config; this is just a
# sane fallback so the feature works before a user customizes bundle_categories.
_DEFAULT_BUNDLE_CATEGORIES = {
    "com.apple.mobilecal": "productivity",
    "com.apple.reminders": "productivity",
    "com.apple.mobilenotes": "productivity",
    "com.tinyspeck.chatlyio": "communication",     # Slack
    "com.apple.MobileSMS": "communication",
    "com.apple.mobilemail": "communication",
    "net.whatsapp.WhatsApp": "communication",
    "com.atebits.Tweetie2": "social",              # Twitter/X
    "com.burbn.instagram": "social",
    "com.zhiliaoapp.musically": "social",          # TikTok
    "com.google.ios.youtube": "entertainment",
    "com.netflix.Netflix": "entertainment",
    "com.spotify.client": "entertainment",
}


# ---------------------------------------------------------------------------
# Resolvers for the iOS context_snapshot keys (perception-report-fields.md).
# Each takes the parsed `data` object and returns only the state fields to keep;
# precise/raw fields (coordinates, BSSID, placemark address) are DISCARDED.
# ---------------------------------------------------------------------------

def resolve_time(value, config: dict) -> dict:
    """`time` data: {local_time, timezone, locale}. Stored as-is."""
    if not isinstance(value, dict):
        return {}
    return {k: value.get(k) for k in ("local_time", "timezone", "locale")}


def resolve_battery(value, config: dict) -> dict:
    """`battery` data: {level, charging, low_power_mode} → the three state fields.

    ``low_power_mode`` arrives only from clients new enough to send it; older
    ones leave it None, which reads as "we don't know", not "it's off".
    """
    if not isinstance(value, dict):
        return {}
    return {
        "battery_level": value.get("level"),
        "charging": value.get("charging"),
        "low_power_mode": value.get("low_power_mode"),
    }


def resolve_broadcast(value, config: dict) -> dict:
    """`broadcast` data: {state, active} → broadcast_state / broadcast_active."""
    if not isinstance(value, dict):
        return {}
    return {"broadcast_state": value.get("state"),
            "broadcast_active": value.get("active")}


def resolve_focus_presence(value, config: dict) -> dict:
    """`focus` data: {authorization_status, focused}.

    Focus is pull-only presence context. It must never write user_state or
    revive the old away/focused platform gate.
    """
    if not isinstance(value, dict):
        return {"focus_authorization_status": None, "in_focus": None}
    auth = value.get("authorization_status")
    focused = value.get("focused")
    return {
        "focus_authorization_status": auth,
        "in_focus": bool(focused) if isinstance(focused, bool) else None,
    }


def _copy_fields(value, fields: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {field: value.get(field) for field in fields if field in value}


def resolve_weather(value, config: dict) -> dict:
    return _copy_fields(value, (
        "condition", "temperature", "apparent_temperature", "humidity",
        "precipitation_chance", "uv_index", "is_daylight", "alerts",
    ))


def resolve_audio_route(value, config: dict) -> dict:
    return _copy_fields(value, ("output_type", "is_bluetooth", "device_name"))


def resolve_health_sleep(value, config: dict) -> dict:
    return _copy_fields(value, ("asleep_minutes", "core_minutes", "deep_minutes", "rem_minutes"))


def resolve_health_workout(value, config: dict) -> dict:
    return _copy_fields(value, ("workout_type", "duration_min", "count_today"))


def resolve_health_vitals(value, config: dict) -> dict:
    return _copy_fields(value, (
        "resting_heart_rate", "step_count", "current_heart_rate", "hrv_sdnn_ms",
        "respiratory_rate", "oxygen_saturation_pct", "vo2_max",
    ))


def resolve_location_signal(value, config: dict) -> dict:
    """`location_signal` data is a rich object that (per the iOS contract) carries
    PRECISE fields — exact lat/lon, Wi-Fi BSSID, full placemark address. We keep
    only coarse labels and DROP everything precise:
      - place_label: re-derived from the raw fix via the user's geofences (so it
        matches the home/work/... vocabulary); raw coords discarded.
      - wifi_label: trusted from the device (backend has no SSID to map); BSSID dropped.
      - country: from country_region_change.locale_region or placemark ISO code.
      - locality: city-level coarse label explicitly released by iOS.
    """
    if not isinstance(value, dict):
        return {"place_label": "unknown", "wifi_label": None, "country": None, "locality": None}
    out: dict = {}
    sig = value.get("signal") or {}
    lat, lon = sig.get("latitude"), sig.get("longitude")
    if lat is not None and lon is not None:
        out["place_label"] = resolve_geofence({"lat": lat, "lon": lon}, config).get("place_label")
    else:
        out["place_label"] = value.get("place_label") or "unknown"
    out["wifi_label"] = value.get("wifi_label")
    out["wifi_anchor_id"] = value.get("wifi_anchor_id")
    out["locality"] = value.get("locality")
    crc = value.get("country_region_change") or {}
    pm = value.get("placemark") or {}
    out["country"] = crc.get("locale_region") or pm.get("iso_country_code")
    return out


RESOLVERS = {
    "geofence": resolve_geofence,
    "ssid": resolve_ssid,
    "bundle": resolve_bundle,
    "time": resolve_time,
    "battery": resolve_battery,
    "broadcast": resolve_broadcast,
    "focus_presence": resolve_focus_presence,
    "weather": resolve_weather,
    "audio_route": resolve_audio_route,
    "health_sleep": resolve_health_sleep,
    "health_workout": resolve_health_workout,
    "health_vitals": resolve_health_vitals,
    "location_signal": resolve_location_signal,
}
