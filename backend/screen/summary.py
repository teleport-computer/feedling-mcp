"""Stateless aggregation of frame metadata into iOS screen-time data."""

import time
from collections import defaultdict
from datetime import datetime

from core.store import UserStore

TODAY = datetime.now().strftime("%Y-%m-%d")

IOS_FALLBACK_DATA = {
    "date": TODAY,
    "total_screen_time_minutes": 0,
    "scroll_distance_meters": 0.0,
    "pickups": 0,
    "unlock_count": 0,
    "apps": [],
    "categories": {},
    "frame_count": 0,
    "data_source": "mock_fallback",
}


def _humanize_app_name(raw: str) -> str:
    value = (raw or "unknown").strip()
    if not value:
        return "Unknown"
    if value.startswith("com."):
        tail = value.split(".")[-1]
        if not tail:
            return value
        return tail.replace("_", " ").replace("-", " ").title()
    return value


def _category_for_app(app_name_or_bundle: str) -> str:
    key = (app_name_or_bundle or "").lower()
    if any(x in key for x in ["tiktok", "youtube", "bili", "netflix"]):
        return "Entertainment"
    if any(x in key for x in ["instagram", "twitter", "x.com", "xiaohong", "reddit"]):
        return "Social"
    if any(x in key for x in ["wechat", "telegram", "whatsapp", "messages", "slack", "feishu", "lark"]):
        return "Communication"
    if any(x in key for x in ["safari", "chrome", "browser"]):
        return "Browsing"
    if any(x in key for x in ["maps", "map", "gaode", "waze"]):
        return "Navigation"
    if any(x in key for x in ["camera", "photos", "settings", "preference", "clock", "calendar"]):
        return "Utility"
    return "Other"


def _to_hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _build_ios_data(store: UserStore, window_sec: float = 86400.0) -> dict:
    now = time.time()
    with store.frames_lock:
        frames = [f.copy() for f in store.frames_meta if now - float(f.get("ts", 0)) <= window_sec]

    if not frames:
        fallback = IOS_FALLBACK_DATA.copy()
        fallback["date"] = datetime.now().strftime("%Y-%m-%d")
        return fallback

    frames.sort(key=lambda f: float(f.get("ts", 0)))

    per_app = defaultdict(lambda: {
        "name": "Unknown",
        "bundle_id": "",
        "category": "Other",
        "duration_seconds": 0.0,
        "sessions": 0,
        "first_ts": 0.0,
        "last_ts": 0.0,
    })
    categories_seconds = defaultdict(float)

    MAX_STEP_SECONDS = 8.0
    NEW_SESSION_GAP_SECONDS = 45.0

    session_count = 0
    prev_app_key = None
    prev_ts = None

    for frame in frames:
        ts = float(frame.get("ts", 0.0))
        app_raw = frame.get("app") or "unknown"
        app_key = str(app_raw)

        row = per_app[app_key]
        row["name"] = _humanize_app_name(app_key)
        row["bundle_id"] = app_key
        row["category"] = _category_for_app(app_key)
        row["last_ts"] = ts
        if not row["first_ts"]:
            row["first_ts"] = ts

        if prev_ts is None:
            session_count += 1
            row["sessions"] += 1
        else:
            gap = max(0.0, ts - prev_ts)
            if app_key != prev_app_key or gap > NEW_SESSION_GAP_SECONDS:
                session_count += 1
                row["sessions"] += 1

            if prev_app_key is not None:
                step = min(gap, MAX_STEP_SECONDS)
                per_app[prev_app_key]["duration_seconds"] += step
                categories_seconds[per_app[prev_app_key]["category"]] += step

        prev_app_key = app_key
        prev_ts = ts

    if prev_app_key is not None:
        per_app[prev_app_key]["duration_seconds"] += 1.0
        categories_seconds[per_app[prev_app_key]["category"]] += 1.0

    apps = []
    total_seconds = 0.0
    for app_key, row in per_app.items():
        dur_min = round(row["duration_seconds"] / 60.0, 1)
        total_seconds += row["duration_seconds"]
        apps.append({
            "name": row["name"],
            "bundle_id": row["bundle_id"],
            "category": row["category"],
            "duration_minutes": dur_min,
            "sessions": int(row["sessions"]),
            "first_used": _to_hhmm(row["first_ts"]),
            "last_used": _to_hhmm(row["last_ts"]),
        })

    apps.sort(key=lambda a: a["duration_minutes"], reverse=True)

    categories = {
        cat: round(sec / 60.0, 1)
        for cat, sec in sorted(categories_seconds.items(), key=lambda kv: kv[1], reverse=True)
        if sec > 0
    }

    total_minutes = round(total_seconds / 60.0, 1)
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_screen_time_minutes": total_minutes,
        "scroll_distance_meters": round(total_minutes * 0.02, 2),
        "pickups": int(session_count),
        "unlock_count": int(session_count),
        "apps": apps,
        "categories": categories,
        "frame_count": len(frames),
        "window_sec": int(window_sec),
        "data_source": "real_frames",
    }


MAC_DATA = {
    "date": TODAY,
    "total_active_minutes": 395,
    "deep_work_minutes": 175,
    "focus_score": 72,
    "context_switches": 34,
    "apps": [
        {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "category": "Browsing",
         "duration_minutes": 120, "window_titles": ["Notion – feedling roadmap", "Linear – Sprint 3",
                                                      "Figma Community", "Stack Overflow"]},
        {"name": "Figma", "bundle_id": "com.figma.Desktop", "category": "Design",
         "duration_minutes": 95, "window_titles": ["Feedling iOS – v2 screens", "Component library"]},
        {"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92", "category": "Development",
         "duration_minutes": 85, "window_titles": ["feedling-mcp-v1 – app.py", "feedling-mcp-v1 – SKILL.md"]},
        {"name": "Zoom", "bundle_id": "us.zoom.xos", "category": "Communication",
         "duration_minutes": 45, "window_titles": ["Weekly sync", "Design review"]},
        {"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap", "category": "Communication",
         "duration_minutes": 40, "window_titles": ["#design", "#eng", "#general", "DMs"]},
        {"name": "Terminal", "bundle_id": "com.apple.Terminal", "category": "Development",
         "duration_minutes": 10, "window_titles": ["zsh – feedling-mcp-v1"]},
    ],
    "categories": {"Browsing": 120, "Design": 95, "Development": 95, "Communication": 85},
}

SOURCES_DATA = {
    "sources": [
        {"id": "ios_pip", "name": "iPhone PIP Recording", "status": "connected",
         "last_sync": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "device": "iPhone 16 Pro"},
        {"id": "mac_monitor", "name": "Mac Screen Monitor", "status": "connected",
         "last_sync": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "device": "MacBook Pro M3"},
    ]
}
