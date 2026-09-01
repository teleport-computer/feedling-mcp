"""The producers that are not the snapshot.

The shadow started on ``/v1/perception/report`` because that is where most of
perception arrives. It is not where all of it arrives. Photos come in at
``/v1/perception/photo/evaluate``, screen and unlock events at the device-event
endpoint, app usage from two iOS Shortcut automations, and the city and Wi-Fi
anchor are resolved out of ``location_signal`` rather than passed through. Six
of the manifest's twenty-three signals only ever appear on those paths.

Leaving them out did not look like a gap. It looked like agreement: nothing was
compared, so nothing disagreed, and the divergence report stayed clean for a
reason that had nothing to do with the kit being right. That is why
``compare.NOT_SHADOWED`` existed -- to name what the clean report was not
covering. This module is what lets those entries be removed.

## One rule shapes every builder here

**Nothing precise crosses this boundary.** iOS resolves coordinates to a city
and an SSID to an anchor id on the device; the backend never receives the
originals. The manifest marks ``location_city.coordinate`` and
``proximity_anchor.raw_identifier`` ``restricted`` and the kit drops them at the
write boundary, but relying on that would be building the fence on the wrong
side: these builders never populate them, so there is nothing for the kit's
guard to catch.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

#: iOS reports one photo per call, so a call is one photo added. The manifest
#: models a count because other producers batch; ours is always 1, and saying
#: so is better than leaving the field to be inferred.
PHOTOS_PER_EVALUATE = 1

#: Which Wi-Fi anchors iOS gives us. Bluetooth anchors are not available to a
#: third-party app on iOS at all, so ``anchor_type`` is never "bluetooth" from
#: this producer -- a fact worth writing down, since an empty bluetooth series
#: otherwise reads as "the user has no bluetooth devices".
ANCHOR_TYPE = "wifi"


def _iso(ts: float | datetime) -> str:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).isoformat()
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _report_id(kind: str, *parts: Any) -> str:
    """Deterministic, derived from the event. No clock, no randomness.

    A retransmitted photo or a replayed device event has to land on the same
    report id, or the ingest receipt stops meaning "we have seen this" and the
    kit counts the same fact twice.
    """
    canonical = "\x1f".join(str(p) for p in parts)
    return f"{kind}-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _envelope(producer: str, report_id: str,
              observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "report_id": report_id,
            "producer": producer, "observations": observations}


def _observation(signal: str, value: Any, *, occurred_at: str,
                 timezone_id: str | None = None,
                 source_event_id: str | None = None) -> dict[str, Any]:
    obs: dict[str, Any] = {
        "signal": signal,
        "signal_schema_version": 1,
        "occurred_at": occurred_at,
        "availability": "observed",
        "value": value,
    }
    if timezone_id:
        obs["timezone"] = timezone_id
    if source_event_id:
        obs["source_event_id"] = source_event_id
    return obs


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

def photo_envelope(photo_id: str, *, occurred_at: float | datetime,
                   timezone_id: str | None = None) -> dict[str, Any]:
    """One confirmed photo -> one ``photo_library_added`` observation.

    Only the count and the time. The scene hint, face count and whether it is a
    screenshot stay out: the manifest does not declare them, and they are the
    most revealing part of a photo's metadata. Sending them to be dropped as
    undeclared would put them in a log line on the way.
    """
    at = _iso(occurred_at)
    return _envelope("ios_photo", _report_id("photo", photo_id), [
        _observation("photo_library_added",
                     {"count": PHOTOS_PER_EVALUATE, "added_at": at},
                     occurred_at=at, timezone_id=timezone_id,
                     source_event_id=photo_id),
    ])


# ---------------------------------------------------------------------------
# Device events
# ---------------------------------------------------------------------------

def device_event_envelope(event: Mapping[str, Any], *,
                          occurred_at: float | datetime,
                          timezone_id: str | None = None) -> dict[str, Any] | None:
    """Screen changes and unlock-after-absence.

    Returns None when the event carries neither -- the endpoint accepts more
    kinds than the manifest models, and an envelope with no observations would
    cost an ingest receipt for nothing.
    """
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    event_id = str(event.get("event_id") or "")
    event_type = str(event.get("type") or "").strip().lower()
    at = _iso(occurred_at)
    observations: list[dict[str, Any]] = []

    wake_trigger = str(payload.get("wake_trigger") or "").strip().lower()
    if event_type == "unlock_after_absence" or wake_trigger == "unlock_after_absence":
        seconds = payload.get("absence_seconds")
        observations.append(_observation(
            "presence_recovery",
            {
                "recovered_at": at,
                # iOS reports that an absence ended, not how long it was. The
                # field is nullable for exactly this case, and `absence_quality`
                # says which it is -- so a downstream reader can tell "we
                # measured 40 minutes" from "we know they came back". Inventing
                # a duration here would make the second look like the first.
                "absence_seconds": float(seconds) if isinstance(
                    seconds, (int, float)) and not isinstance(seconds, bool) else None,
                "absence_quality": "measured" if isinstance(
                    seconds, (int, float)) and not isinstance(seconds, bool)
                    else "estimated",
            },
            occurred_at=at, timezone_id=timezone_id,
            source_event_id=event_id or None,
        ))

    phash = payload.get("safe_screen_phash") or payload.get("screen_phash")
    broadcast_state = str(payload.get("broadcast_state") or "").strip().lower()
    if phash and broadcast_state in {"on", "broadcasting"}:
        # The manifest models "did the screen change", not what was on it. The
        # perceptual hash is the live path's way of answering that question and
        # it stays on the live path: it is derived from the user's screen, and
        # a boolean is the whole of what this signal declares.
        observations.append(_observation(
            "screen_change", {"changed": True},
            occurred_at=at, timezone_id=timezone_id,
        ))

    if not observations:
        return None
    return _envelope("ios_device_event",
                     _report_id("devevt", event_id or event_type, at),
                     observations)


# ---------------------------------------------------------------------------
# App usage
# ---------------------------------------------------------------------------

def app_event_envelope(app: str, category: str | None, *, action: str,
                       occurred_at: float | datetime,
                       timezone_id: str | None = None) -> dict[str, Any]:
    """One app open or close.

    ``app_id`` and ``app_name`` are the same string here. iOS Shortcut
    automations report a display name typed by hand -- there is no bundle id on
    this path -- so filing the display name as the id is honest about what we
    have, where synthesising a fake bundle id would not be.
    """
    at = _iso(occurred_at)
    name = (app or "").strip()
    return _envelope("ios_app_shortcut",
                     _report_id("app", name.casefold(), action, at), [
        _observation("app_usage",
                     {"app_id": name, "app_name": name,
                      "category": (category or None), "action": action,
                      # 一次打开贡献 1，当天求和 = 「今天打开了几次」。
                      # 不发这个字段的话那个聚合永远是空的 —— 而这正是
                      # app_usage 唯一保证答得准的问题。close 不计数。
                      **({"open_count": 1} if action == "open" else {})},
                     occurred_at=at, timezone_id=timezone_id,
                     source_event_id=_report_id("app", name.casefold(), action, at)),
    ])


# ---------------------------------------------------------------------------
# Location -- city and anchor, never coordinates
# ---------------------------------------------------------------------------

def location_envelope(values: Mapping[str, Any], *,
                      occurred_at: float | datetime,
                      timezone_id: str | None = None) -> dict[str, Any] | None:
    """The decrypted ``location_signal`` -> city and Wi-Fi anchor.

    Fed the plaintext the live path already decrypted, and reading only the
    coarse labels iOS deliberately released: the locality it resolved on
    device, the country, and the anchor id it derived from the SSID. Latitude,
    longitude, BSSID and the full placemark are in that same payload and none
    of them are read here.
    """
    at = _iso(occurred_at)
    observations: list[dict[str, Any]] = []

    locality = values.get("locality")
    crc = values.get("country_region_change") if isinstance(
        values.get("country_region_change"), Mapping) else {}
    placemark = values.get("placemark") if isinstance(
        values.get("placemark"), Mapping) else {}
    country = (values.get("country") or crc.get("locale_region")
               or placemark.get("iso_country_code"))
    if locality or country:
        observations.append(_observation(
            "location_city",
            {
                "locality": locality or None,
                "country_code": country or None,
                # The administrative region and the fix accuracy are in the
                # payload but not released to us as coarse labels; left null
                # rather than reconstructed from anything precise.
                "region": None,
                "accuracy_m": None,
                "placemark_source": "ios_device_resolver",
            },
            occurred_at=at, timezone_id=timezone_id,
        ))

    anchor = values.get("wifi_anchor_id")
    if isinstance(anchor, str) and anchor:
        observations.append(_observation(
            "proximity_anchor",
            {
                "anchor_id": anchor,
                "anchor_type": ANCHOR_TYPE,
                # The user-facing label the device resolved (home_wifi,
                # work_wifi...). The SSID it came from stays on the device.
                "label": values.get("wifi_label") or None,
                "is_connected": True,
            },
            occurred_at=at, timezone_id=timezone_id,
        ))

    if not observations:
        return None
    return _envelope("ios_location", _report_id("loc", at, locality or "",
                                                anchor or ""), observations)


# ---------------------------------------------------------------------------
# Calendar and reminders -- the source mirror, not the signal path
# ---------------------------------------------------------------------------

def calendar_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The decrypted ``calendar_next_event`` payload -> mirror rows.

    §7.13 puts calendar and reminders on the source-mirror path rather than the
    signal path, because they are a collection that is edited upstream: an
    event moved an hour later is the same event, and a signal timeline cannot
    say that. The mirror keys on the source's own id so a revision replaces the
    row instead of appending a second truth.
    """
    events = payload.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        one = payload.get("calendar_next_event")
        events = [one] if isinstance(one, Mapping) else []
    rows: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("event_id") or item.get("id") or "")
        if not source_id:
            # Without the source's id there is no way to tell a revision from a
            # new event, which is the one thing the mirror exists to do.
            continue
        rows.append({
            "source_account_id": "ios",
            "source_calendar_id": str(item.get("calendar_id") or "default"),
            "source_event_id": source_id,
            "event_fields": {
                "title": item.get("title"),
                "start_at": item.get("start_time") or item.get("start_at"),
                "end_at": item.get("end_time") or item.get("end_at"),
                "is_all_day": bool(item.get("is_all_day")),
                "location_label": item.get("location"),
            },
            "source_revision": item.get("revision") or item.get("last_modified"),
        })
    return rows


def reminder_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The decrypted ``reminders`` payload -> mirror rows. See ``calendar_rows``."""
    items = payload.get("reminders")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        items = []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("reminder_id") or item.get("id") or "")
        if not source_id:
            continue
        rows.append({
            "source_account_id": "ios",
            "source_list_id": str(item.get("list_id") or "default"),
            "source_reminder_id": source_id,
            "reminder_fields": {
                "title": item.get("title"),
                "due_at": item.get("due_time") or item.get("due_at"),
                "is_completed": bool(item.get("is_completed")),
                "priority": item.get("priority"),
            },
            "source_revision": item.get("revision") or item.get("last_modified"),
        })
    return rows


__all__ = [
    "PHOTOS_PER_EVALUATE", "ANCHOR_TYPE",
    "photo_envelope", "device_event_envelope", "app_event_envelope",
    "location_envelope", "calendar_rows", "reminder_rows",
]
