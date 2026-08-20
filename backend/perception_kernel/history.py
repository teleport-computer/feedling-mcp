"""Quantitative perception history — incremental daily aggregation (Tier 2).

Field-agnostic by design (docs/PERCEPTION_HISTORY_SPEC, since removed — see git history; principle #2): each signal
declares ONE *shape* in ``SHAPE``; a per-shape merge function folds new
observations into the running daily doc. Adding a field flows through
automatically (numeric fields are discovered from the values dict); adding a
signal is one ``SHAPE`` line. There is no per-field list to keep in sync with
iOS — exactly so the ``temperature_bucket -> temperature`` class of rename
never breaks history again.

Everything here is a PURE function (no DB / no I/O): ``record_daily`` takes the
previous day-doc + a new observation and returns the next day-doc;
``read_trend`` derives a baseline/delta from a list of day-docs. Storage (the
``perception_daily`` table), the ingest hook, and the read endpoint wire these
in separately so the math is unit-testable without Postgres.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

# --- shapes -----------------------------------------------------------------
NUMERIC_DIST = "numeric_dist"        # per numeric field: min/max/sum/count -> avg
CUMULATIVE = "cumulative"            # per numeric field: running max (= daily total)
MAIN_OF_DAY = "main_of_day"          # latest non-null point values (replace)
DURATION_BY_STATE = "duration_by_state"  # minutes spent in each categorical state
EVENT_LIST = "event_list"            # discrete items, deduped by id/key
SUBJECTIVE = "subjective"            # append each self-report entry
PLACE_DWELL = "place_dwell"          # minutes spent at each place label
TALLY = "tally"                      # daily digest: total minutes + top artists/tracks

_TALLY_CAP = 30                      # keep only the top-N artists/tracks per day

# Signal (canonical catalog input key) -> shape. ONE line per signal; fields are
# discovered from the observation. Signals absent here are NOT historized
# (pure-instant / no daily pattern: time, battery, broadcast, now, app).
SHAPE: dict[str, str] = {
    "health_vitals": NUMERIC_DIST,
    "health_metabolic": NUMERIC_DIST,
    "weather": NUMERIC_DIST,
    "health_activity": CUMULATIVE,
    "health_sleep": MAIN_OF_DAY,
    "health_body": MAIN_OF_DAY,
    "health_cycle": MAIN_OF_DAY,
    "health_mood": SUBJECTIVE,
    "motion_state": DURATION_BY_STATE,
    "focus": DURATION_BY_STATE,
    "audio_route": DURATION_BY_STATE,
    "location_signal": PLACE_DWELL,
    "playback": TALLY,
    "health_workout": EVENT_LIST,
    "calendar_next_event": EVENT_LIST,
    "reminders": EVENT_LIST,
}

# The single categorical field that names the "state" for duration/place shapes.
# (Field-agnostic everywhere else; these two shapes need to know which key is the
# state label vs. the timestamp accounting.)
_STATE_FIELD = {
    "motion_state": "motion_state",
    "focus": "in_focus",
    "audio_route": "output_type",
    "location_signal": "place_label",
}
# Numeric fields that are cumulative-within-the-day (monotonic), so their daily
# representative is max(=total), not the average. Read-side hint only — they
# still aggregate through numeric_dist's {min,max,sum,count}.
_NUMERIC_MAX_FIELDS = {"step_count"}
_COMPARABLE_SHAPES = {NUMERIC_DIST, CUMULATIVE, MAIN_OF_DAY}
_SIGNIFICANCE_FLOOR = 1.0


def is_historized(signal: str) -> bool:
    return signal in SHAPE


def comparable_signals() -> list[str]:
    """Historized signals that can yield per-day numeric trend values."""
    return [signal for signal, shape in SHAPE.items() if shape in _COMPARABLE_SHAPES]


def _numeric(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _flatten_state(v: Any) -> str | None:
    """Coerce a state value to a categorical label (motion_state is a nested
    dict {state, confidence, ...}; focus in_focus is a bool)."""
    if isinstance(v, Mapping):
        s = v.get("state")
        return str(s) if s is not None else None
    if isinstance(v, bool):
        return "focused" if v else "unfocused"
    if v is None:
        return None
    return str(v)


# --- per-shape incremental merges ------------------------------------------
def _merge_numeric_dist(doc: dict, values: Mapping, **_) -> dict:
    out = dict(doc)
    for field, raw in values.items():
        n = _numeric(raw)
        if n is None:
            continue
        cell = out.get(field) or {}
        out[field] = {
            "min": n if cell.get("min") is None else min(cell["min"], n),
            "max": n if cell.get("max") is None else max(cell["max"], n),
            "sum": (cell.get("sum") or 0.0) + n,
            "count": (cell.get("count") or 0) + 1,
        }
    return out


def _merge_cumulative(doc: dict, values: Mapping, **_) -> dict:
    out = dict(doc)
    for field, raw in values.items():
        n = _numeric(raw)
        if n is None:
            continue
        prev = out.get(field)
        out[field] = {"total": n if prev is None else max(prev.get("total", n), n)}
    return out


def _merge_main_of_day(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    for field, raw in values.items():
        if raw is not None:
            out[field] = raw
    if ts is not None:
        out["_at"] = ts
    return out


def _merge_duration_by_state(doc: dict, values: Mapping, *, signal: str, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    buckets = dict(out.get("minutes") or {})
    state = _flatten_state(values.get(_STATE_FIELD.get(signal, "")))
    last_state = out.get("_last_state")
    last_ts = out.get("_last_ts")
    if last_state is not None and last_ts is not None and ts is not None and ts >= last_ts:
        mins = (ts - last_ts) / 60.0
        buckets[last_state] = round((buckets.get(last_state) or 0.0) + mins, 2)
    out["minutes"] = buckets
    if state is not None:
        out["_last_state"] = state
    if ts is not None:
        out["_last_ts"] = ts
    return out


def _merge_place_dwell(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    buckets = dict(out.get("minutes") or {})
    place = values.get("place_label")
    last_place = out.get("_last_place")
    last_ts = out.get("_last_ts")
    if last_place and last_ts is not None and ts is not None and ts >= last_ts:
        mins = (ts - last_ts) / 60.0
        buckets[last_place] = round((buckets.get(last_place) or 0.0) + mins, 2)
    out["minutes"] = buckets
    if place:
        out["_last_place"] = place
        visited = set(out.get("visited") or [])
        visited.add(place)
        out["visited"] = sorted(visited)
    if ts is not None:
        out["_last_ts"] = ts
    return out


def _event_key(ev: Mapping) -> str:
    for k in ("id", "event_id", "identifier"):
        if ev.get(k):
            return str(ev[k])
    return "|".join(str(ev.get(k) or "") for k in ("title", "next_event_time", "start_time", "due_time"))


def _merge_event_list(doc: dict, values: Mapping, **_) -> dict:
    out = dict(doc)
    items = list(out.get("events") or [])
    seen = {_event_key(e) for e in items if isinstance(e, Mapping)}
    # event-list signals carry their events under a list-valued field (e.g.
    # calendar_events / reminders); fall back to the values dict as one event.
    candidates: list = []
    for v in values.values():
        if isinstance(v, list):
            candidates.extend(x for x in v if isinstance(x, Mapping))
    if not candidates and any(values.get(k) for k in ("title", "workout_type")):
        candidates = [dict(values)]
    for ev in candidates:
        key = _event_key(ev)
        if key and key not in seen:
            seen.add(key)
            items.append(ev)
    out["events"] = items
    return out


def _cap_top(d: dict, n: int = _TALLY_CAP) -> dict:
    if len(d) <= n:
        return d
    return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n])


def _merge_tally(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    """now_playing daily music digest: credit each listening interval (between
    observations, while playing) to the previously-playing track + artist; track
    distinct titles. Stores total_minutes + by_artist/by_track minutes (top-N) +
    distinct titles — taste over time, not a per-play stream."""
    out = dict(doc)
    np = values.get("now_playing")
    np = np if isinstance(np, Mapping) else {}
    playing = str(np.get("playback_state") or "").lower() == "playing"
    title = np.get("title")
    artist = np.get("artist")
    last_ts = out.get("_last_ts")
    if out.get("_last_playing") and last_ts is not None and ts is not None and ts >= last_ts:
        mins = round((ts - last_ts) / 60.0, 2)
        out["total_minutes"] = round((out.get("total_minutes") or 0.0) + mins, 2)
        la, lt = out.get("_last_artist"), out.get("_last_track")
        if la:
            by_a = dict(out.get("by_artist") or {})
            by_a[la] = round((by_a.get(la) or 0.0) + mins, 2)
            out["by_artist"] = _cap_top(by_a)
        if lt:
            by_t = dict(out.get("by_track") or {})
            by_t[lt] = round((by_t.get(lt) or 0.0) + mins, 2)
            out["by_track"] = _cap_top(by_t)
    if playing and title:
        distinct = set(out.get("distinct") or [])
        distinct.add(title)
        out["distinct"] = sorted(distinct)[:200]
    out["_last_ts"] = ts
    out["_last_playing"] = playing
    out["_last_track"] = title if playing else None
    out["_last_artist"] = artist if playing else None
    return out


def _merge_subjective(doc: dict, values: Mapping, *, ts: float | None = None, **_) -> dict:
    out = dict(doc)
    entries = list(out.get("entries") or [])
    entry = {k: v for k, v in values.items() if v is not None}
    if ts is not None:
        entry["_at"] = ts
    if entry:
        entries.append(entry)
    out["entries"] = entries
    return out


_MERGERS = {
    NUMERIC_DIST: _merge_numeric_dist,
    CUMULATIVE: _merge_cumulative,
    MAIN_OF_DAY: _merge_main_of_day,
    DURATION_BY_STATE: _merge_duration_by_state,
    PLACE_DWELL: _merge_place_dwell,
    EVENT_LIST: _merge_event_list,
    SUBJECTIVE: _merge_subjective,
    TALLY: _merge_tally,
}


def record_daily(prev_doc: Mapping | None, signal: str, values: Mapping, *, ts: float | None = None) -> dict:
    """Fold one observation of ``signal`` into its running day-doc and return the
    next day-doc. Caller is responsible for keying by (user, local-date, signal)
    and for resetting prev_doc to {} when the local date rolls over."""
    shape = SHAPE.get(signal)
    if shape is None:
        raise ValueError(f"signal {signal!r} is not historized; guard with is_historized()")
    if not isinstance(values, Mapping):
        return dict(prev_doc or {})
    merge = _MERGERS[shape]
    return merge(dict(prev_doc or {}), values, signal=signal, ts=ts)


# --- read side: trend / baseline -------------------------------------------
def _series_value(doc: Mapping, shape: str, field: str | None) -> float | None:
    """Pull a single comparable daily number out of a day-doc for trending."""
    if shape == NUMERIC_DIST:
        cell = doc.get(field) if field else None
        if not isinstance(cell, Mapping):
            return None
        if field in _NUMERIC_MAX_FIELDS:        # cumulative-within-day -> daily total
            return cell.get("max")
        if cell.get("count"):
            return round(cell["sum"] / cell["count"], 3)
        return None
    if shape == CUMULATIVE:
        cell = doc.get(field) if field else None
        return cell.get("total") if isinstance(cell, Mapping) else None
    if shape == MAIN_OF_DAY:
        v = doc.get(field) if field else None
        return _numeric(v)
    if shape == TALLY:                          # e.g. field=total_minutes
        return _numeric(doc.get(field)) if field else None
    return None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def read_trend(rows: list[Mapping], signal: str, field: str | None = None) -> dict:
    """rows: [{date, doc}] ascending by date. Returns daily series + rolling
    baseline (median/p25/p75) + current + delta vs baseline + direction."""
    shape = SHAPE.get(signal)
    daily = []
    for r in rows:
        v = _series_value(r.get("doc") or {}, shape, field) if shape else None
        if v is not None:
            daily.append({"date": r.get("date"), "value": v})
    vals = [d["value"] for d in daily]
    baseline_vals = vals[:-1] if len(vals) > 1 else vals
    median = _median(baseline_vals)
    current = vals[-1] if vals else None
    delta = round(current - median, 3) if (current is not None and median is not None) else None
    direction = "flat"
    if delta is not None:
        direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {
        "signal": signal,
        "field": field,
        "daily": daily,
        "baseline": {
            "median": median,
            "p25": _percentile(baseline_vals, 0.25),
            "p75": _percentile(baseline_vals, 0.75),
            "n": len(baseline_vals),
        },
        "current": current,
        "delta": delta,
        "direction": direction,
    }


def _numeric_fields(rows: list[Mapping], signal: str) -> list[str]:
    shape = SHAPE.get(signal)
    if shape not in _COMPARABLE_SHAPES:
        return []
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        doc = row.get("doc") or {}
        if not isinstance(doc, Mapping):
            continue
        for field in doc:
            if not isinstance(field, str) or field.startswith("_") or field in seen:
                continue
            if _series_value(doc, shape, field) is None:
                continue
            seen.add(field)
            fields.append(field)
    return fields


def _finite_number(v: Any) -> float | None:
    n = _numeric(v)
    if n is None or not math.isfinite(n):
        return None
    return n


def notable_changes(rows_by_signal: Mapping[str, list[Mapping]], *, max_changes: int = 8) -> list[dict]:
    """Return top-N relative numeric changes across all comparable history.

    ``rows_by_signal`` maps canonical catalog signals to ascending daily rows,
    the same shape consumed by ``read_trend``. Fields are discovered from the
    stored day-docs, then each (signal, field) delegates baseline/current/delta
    calculation to ``read_trend`` so digest semantics stay aligned with the
    existing trend endpoint.
    """
    cap = max(0, int(max_changes or 0))
    if cap <= 0:
        return []
    changes: list[dict] = []
    for signal in comparable_signals():
        rows = list(rows_by_signal.get(signal) or [])
        for field in _numeric_fields(rows, signal):
            trend = read_trend(rows, signal, field)
            baseline = trend.get("baseline") if isinstance(trend.get("baseline"), Mapping) else {}
            baseline_median = _finite_number(baseline.get("median"))
            current = _finite_number(trend.get("current"))
            delta = _finite_number(trend.get("delta"))
            if (baseline.get("n") or 0) < 2 or baseline_median is None or current is None or delta is None:
                continue
            denom = max(abs(baseline_median), _SIGNIFICANCE_FLOOR)
            magnitude = round(abs(delta) / denom, 6)
            changes.append({
                "signal": signal,
                "field": field,
                "current": current,
                "baseline_median": baseline_median,
                "delta": delta,
                "direction": trend.get("direction") or "flat",
                "magnitude": magnitude,
            })
    changes.sort(key=lambda c: (-c["magnitude"], c["signal"], c["field"]))
    return changes[:cap]


# --- cross-domain digest board ---------------------------------------------
# notable_changes() ranks health/numeric deltas only, so the wake digest skews
# into a body-monitoring readout. cross_domain_recent() instead lays out ONE
# compact entry per life-context domain (location / media / app / health /
# weather / mood / reminders / calendar / photos / screen) so the agent keeps
# music/place/app/photo context. The backend does NOT pick the 2-3 things that
# matter — it only sets a balanced table; the agent reads it and judges. Light,
# factual per-domain `novelty` hints (new_artist / long_dwell) are context, not
# a cross-domain ranking. Pure function (no I/O): the route fetches
# snapshot/pull_snapshot/history/photos and passes them in.
_LONG_DWELL_MIN = 240.0  # >=4h at one place today -> a light "long_dwell" hint


def _as_mapping(v: Any) -> dict:
    return dict(v) if isinstance(v, Mapping) else {}


def _media_domain(snapshot: Mapping, rows: list[Mapping]) -> dict:
    np = _as_mapping(snapshot.get("now_playing"))
    title = np.get("title")
    artist = np.get("artist")
    title = str(title) if title else None
    artist = str(artist) if artist else None
    today = _as_mapping(rows[-1].get("doc")) if rows else {}
    by_artist = _as_mapping(today.get("by_artist"))
    top_artists = [a for a, _ in sorted(by_artist.items(), key=lambda kv: kv[1], reverse=True)[:3]]
    distinct = today.get("distinct")
    novelty = None
    if artist:
        prior: set[str] = set()
        for r in rows[:-1]:
            prior.update(_as_mapping(_as_mapping(r.get("doc")).get("by_artist")).keys())
        if artist not in prior:
            novelty = "new_artist"
    return {
        "now": ({"title": title, "artist": artist} if (title or artist) else None),
        "top_artists_today": top_artists,
        "minutes_today": _finite_number(today.get("total_minutes")),
        "distinct_today": (len(distinct) if isinstance(distinct, list) else None),
        "novelty": novelty,
    }


def _location_domain(snapshot: Mapping, rows: list[Mapping]) -> dict:
    place = snapshot.get("place_label")
    today = _as_mapping(rows[-1].get("doc")) if rows else {}
    minutes = _as_mapping(today.get("minutes"))
    visited = today.get("visited")
    minutes_here = _finite_number(minutes.get(place)) if place else None
    novelty = "long_dwell" if (minutes_here is not None and minutes_here >= _LONG_DWELL_MIN) else None
    return {
        "now": place,
        "minutes_today": minutes_here,
        "visited_today": (list(visited) if isinstance(visited, list) else []),
        "novelty": novelty,
    }


def _app_domain(snapshot: Mapping) -> dict:
    recent = snapshot.get("recent_apps")
    entries = [e for e in recent if isinstance(e, Mapping)] if isinstance(recent, list) else []
    entries.sort(key=lambda e: (_finite_number(e.get("ts")) or 0.0), reverse=True)
    now_app = entries[0].get("app") if entries else None
    names: list[str] = []
    seen: set[str] = set()
    for e in entries:
        a = e.get("app")
        if a and a not in seen:
            seen.add(a)
            names.append(str(a))
    return {"now": now_app, "recent": names[:5], "novelty": None}


def _weather_domain(pull: Mapping) -> dict:
    cond, temp = pull.get("condition"), pull.get("temperature")
    if cond is None and temp is None:
        return {"status": "none"}
    return {"condition": cond, "temperature": temp}


def _mood_domain(pull: Mapping) -> dict:
    val, cls = pull.get("valence"), pull.get("valence_classification")
    if val is None and cls is None and not pull.get("recorded_today"):
        return {"status": "none"}
    return {"valence": val, "classification": cls}


def _reminders_domain(pull: Mapping) -> dict:
    reminders = pull.get("reminders")
    overdue: list[str] = []
    if isinstance(reminders, list):
        for r in reminders:
            if isinstance(r, Mapping) and r.get("overdue") and r.get("title"):
                overdue.append(str(r.get("title")))
    return {
        "due_today": pull.get("due_today_count"),
        "overdue_count": pull.get("overdue_count"),
        "overdue": overdue[:5],
        "next": pull.get("next_reminder"),
    }


def _photos_domain(photos: Any) -> dict:
    items = photos if isinstance(photos, list) else []
    scenes: list[str] = []
    seen: set[str] = set()
    for p in items:
        meta = _as_mapping(p.get("metadata")) if isinstance(p, Mapping) else {}
        sc = meta.get("scene_hint")
        if sc and sc not in seen:
            seen.add(sc)
            scenes.append(str(sc))
    return {"recent_count": len(items), "scenes": scenes[:5]}


def cross_domain_recent(
    *,
    snapshot: Mapping | None,
    pull_snapshot: Mapping | None,
    rows_by_signal: Mapping[str, list[Mapping]] | None,
    photos: Any = None,
    max_health_notable: int = 8,
) -> dict:
    """Balanced cross-domain digest board for the wake turn (see module note)."""
    snap = _as_mapping(snapshot)
    pull = _as_mapping(pull_snapshot)
    rbs = rows_by_signal if isinstance(rows_by_signal, Mapping) else {}
    return {
        "location": _location_domain(snap, list(rbs.get("location_signal") or [])),
        "media": _media_domain(snap, list(rbs.get("playback") or [])),
        "app": _app_domain(snap),
        "health": {"notable": notable_changes(rbs, max_changes=max_health_notable)},
        "weather": _weather_domain(pull),
        "mood": _mood_domain(pull),
        "reminders": _reminders_domain(pull),
        "calendar": {"next": snap.get("calendar_next_event")},
        "photos": _photos_domain(photos),
        "screen": {"state": snap.get("broadcast_state")},
    }
