"""Unit tests for the Extended Perception service logic.

These exercise the generic machinery (sparse report, implicit authorization via
reported values, raw->label resolution + raw discard, snapshot TTL, the
user_state override stack,
debounced wakes, and the encrypted photo flow) WITHOUT a real
Postgres: the store layer is replaced with an in-memory fake, and the wake
trigger is captured instead of enqueuing a real proactive job.

Run:  python -m pytest tests/test_perception.py -q
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import perception.service as service
from perception import perception_read_core


# The Flask /v1/perception/* blueprint was deleted in the ASGI cutover; this tiny
# client mimics the old Flask test-client response for the two routes these tests
# exercise (/report, /app_open), routing straight to the framework-neutral
# perception_read_core the ASGI routes delegate to.
class _PResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def get_json(self):
        return self._body


class _PerceptionClient:
    def __init__(self, store, api_key="apikey-perc"):
        # The old Flask route resolved a separate auth store carrying ``user_id``;
        # the neutral core reads ``store.user_id`` then does all state I/O via the
        # (monkeypatched) ``service.store``. The FakeStore is that service store, so
        # just stamp the same UID on it.
        if not hasattr(store, "user_id"):
            store.user_id = UID
        self._store = store
        self._api_key = api_key

    def post(self, path, json=None):
        p = urlparse(path).path
        if p == "/v1/perception/report":
            return _PResp(*perception_read_core.report(self._store, json or {}, api_key=self._api_key))
        raise AssertionError(f"unrouted path: {p}")

    def get(self, path):
        u = urlparse(path)
        if u.path == "/v1/perception/app_open":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            return _PResp(*perception_read_core.app_open(self._store, q))
        raise AssertionError(f"unrouted path: {u.path}")


class FakeStore:
    """In-memory stand-in for perception.store."""
    def __init__(self):
        self.blobs = {}   # (uid, kind) -> dict   (kinds collapsed onto attrs below)
        self.state = {}
        self.config = {}
        self.user_state = {}
        self.items = {}   # (uid, kind) -> {item_id: {"ts","expires_at","doc"}}
        self.events = {}  # uid -> [event...]
        self.frames = {}  # (uid, frame_id) -> envelope
        self.app_opens = {}  # uid -> [event...]

    # singletons
    def get_state(self, uid): return {k: dict(v) for k, v in self.state.get(uid, {}).items()}
    def merge_state(self, uid, patch):
        self.state.setdefault(uid, {}).update(patch); return self.get_state(uid)
    def merge_state_guarded(self, uid, patch):
        cur = self.state.setdefault(uid, {})
        written = set()
        for f, cell in patch.items():
            old = cur.get(f)
            old_ts = old.get("ts") if isinstance(old, dict) else None
            new_ts = cell.get("ts")
            if old_ts is None or new_ts is None or float(new_ts) >= float(old_ts):
                cur[f] = dict(cell); written.add(f)
        return written
    def clear_state_fields(self, uid, fields):
        for f in fields:
            self.state.get(uid, {}).pop(f, None)

    def get_config(self, uid): return dict(self.config.get(uid, {}))
    def merge_config(self, uid, patch):
        self.config.setdefault(uid, {}).update(patch); return dict(self.config[uid])

    def get_user_state_doc(self, uid): return dict(self.user_state.get(uid, {}))
    def set_user_state_doc(self, uid, doc): self.user_state[uid] = dict(doc)
    def set_manual_user_state_guarded(self, uid, value, ts):
        doc = dict(self.user_state.get(uid, {}))
        prev_ts = doc.get("manual_ts")
        if prev_ts is None or float(ts) >= float(prev_ts):
            doc["manual"] = str(value or "default")
            doc["manual_ts"] = float(ts)
            self.user_state[uid] = doc
        return dict(self.user_state.get(uid, {}))

    # collections
    def item_upsert(self, uid, kind, item_id, ts, doc, expires_at=None):
        self.items.setdefault((uid, kind), {})[item_id] = {
            "ts": ts, "expires_at": expires_at, "doc": dict(doc)}
    def item_get(self, uid, kind, item_id, now=None):
        row = self.items.get((uid, kind), {}).get(item_id)
        if not row:
            return None
        if now is not None and row["expires_at"] is not None and row["expires_at"] <= now:
            return None
        return dict(row["doc"])
    def item_list(self, uid, kind, limit=20, now=None):
        rows = list(self.items.get((uid, kind), {}).values())
        if now is not None:
            rows = [r for r in rows if r["expires_at"] is None or r["expires_at"] > now]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return [dict(r["doc"]) for r in rows[:limit]]
    def item_patch(self, uid, kind, item_id, patch, expires_at="__keep__"):
        row = self.items.get((uid, kind), {}).get(item_id)
        if not row:
            return None
        row["doc"].update(patch)
        if expires_at != "__keep__":
            row["expires_at"] = expires_at
        return dict(row["doc"])

    # events
    def append_event(self, uid, event, ts):
        self.events.setdefault(uid, []).append(dict(event))
    def read_events(self, uid, limit=50):
        return [dict(e) for e in self.events.get(uid, [])[-limit:]]

    # app-usage time series
    def append_app_open(self, uid, doc, ts):
        self.app_opens.setdefault(uid, []).append(dict(doc))
    def read_app_opens(self, uid, limit=100, since_epoch=0.0):
        rows = self.app_opens.get(uid, [])
        if since_epoch:
            rows = [r for r in rows if (r.get("ts") or 0) > since_epoch]
        return [dict(r) for r in rows[-limit:]]

    # photo ciphertext channel (reuses frame_envelopes in prod)
    def put_photo_envelope(self, uid, frame_id, ts, env):
        self.frames[(uid, frame_id)] = dict(env)
    def get_photo_envelope(self, uid, frame_id):
        return self.frames.get((uid, frame_id))
    def delete_photo_envelope(self, uid, frame_id):
        self.frames.pop((uid, frame_id), None)


@pytest.fixture
def env(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(service, "store", fake)
    wakes = []
    monkeypatch.setattr(service, "_fire_wake",
                        lambda uid, cap, hint, now: wakes.append((cap, hint)))
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {})
    monkeypatch.setattr(service, "_settings_v2_for_user", lambda uid: None)
    monkeypatch.setattr(service, "_fire_wake_event_v2",
                        lambda event: wakes.append((event.trigger, event.change_digest)))
    monkeypatch.setattr(service, "_proactive_activation_ready", lambda uid: True)
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: False)
    return fake, wakes


UID = "u1"


# ---------------------------------------------------------------------------

def _item(key, obj, message=""):
    """Build one context_snapshot item; obj=None -> data:"null"."""
    return {"key": key, "data": ("null" if obj is None else json.dumps(obj)), "message": message}


def test_always_on_signals_stored(env):
    fake, _ = env  # time/battery/broadcast always available
    service.ingest_snapshot(UID, [
        _item("time", {"local_time": "2026-06-08T15:30:45Z",
                       "timezone": "America/Los_Angeles", "locale": "en"}),
        _item("battery", {"level": "0.85", "charging": "true"}),
        _item("broadcast", {"state": "inactive", "active": "false"}),
    ])
    snap = service.snapshot(UID)
    assert snap["local_time"] == "2026-06-08T15:30:45Z"
    assert snap["battery_level"] == "0.85"
    assert snap["broadcast_state"] == "inactive"


def test_report_no_setup_accepted_and_stored(env):
    """无任何权限设置：上报即被接受并入库（report 值=有权限）。"""
    fake, _ = env
    res = service.ingest_snapshot(UID, [_item("motion_state", {"state": "walking"})])
    assert res["motion_state"] == "accepted"
    assert service.snapshot(UID)["motion_state"] == {"state": "walking"}


def test_location_signal_keeps_labels_drops_precise(env):
    """location_signal carries PRECISE fields; backend keeps only coarse labels
    (place_label via geofence, wifi_label, country) and drops coords/BSSID/address."""
    fake, _ = env
    fake.merge_config(UID, {"geofences": [
        {"label": "home", "lat": 37.0, "lon": -122.0, "radius_m": 150}]})
    service.ingest_snapshot(UID, [_item("location_signal", {
        "place_label": "Apple Park",                 # iOS reverse-geocode (overridden by geofence)
        "wifi_label": "home_wifi",
        "wifi_bssid": "a4:b1:c2:d3:e4:f5",           # precise — must be dropped
        "signal": {"latitude": 37.0, "longitude": -122.0, "horizontal_accuracy": 5.0},
        "country_region_change": {"locale_region": "US"},
        "placemark": {"locality": "Cupertino", "iso_country_code": "US", "postal_code": "95014"},
    })])
    st = fake.get_state(UID)
    assert st["place_label"]["v"] == "home"           # geofence from the raw fix
    assert st["wifi_label"]["v"] == "home_wifi"
    assert st["country"]["v"] == "US"
    blob = str(st)                                    # no precise field anywhere
    for precise in ("latitude", "37.0", "bssid", "a4:b1", "postal", "95014", "Cupertino"):
        assert precise not in blob


def test_snapshot_ttl_nulls_stale(env):
    fake, _ = env
    service.ingest_snapshot(UID, [_item("motion_state", {"state": "running"})])
    # backdate the stored ts beyond the motion ttl (300s)
    fake.state[UID]["motion_state"]["ts"] = time.time() - 10_000
    assert service.snapshot(UID)["motion_state"] is None


def test_snapshot_timezone_survives_ttl_but_local_time_expires(env):
    """Stable identity fields (timezone/locale) must NOT be nulled out by the
    freshness TTL: a proactive wake fires long after the last foreground report,
    and the resident consumer's current_time anchor needs the real timezone or it
    silently falls back to UTC. The volatile local_time in the same signal still
    expires normally."""
    fake, _ = env
    service.ingest_snapshot(UID, [
        _item("time", {"local_time": "2026-06-08T15:30:45Z",
                       "timezone": "Asia/Shanghai", "locale": "zh"}),
    ])
    # backdate every stored `time` field beyond the time ttl (300s)
    for field in ("local_time", "timezone", "locale"):
        fake.state[UID][field]["ts"] = time.time() - 10_000
    snap = service.snapshot(UID)
    assert snap["timezone"] == "Asia/Shanghai"   # stable -> survives
    assert snap["locale"] == "zh"                # stable -> survives
    assert snap["local_time"] is None            # volatile -> expires


def test_record_context_timezone_writes_locale_to_snapshot(env):
    """The app-presence channel must still populate the perception snapshot with
    timezone AND locale: the resident consumer's reply-language guardrail
    (_reply_language_line) reads `locale` from /v1/agent/perception?signals=now,
    so a perception-upload-OFF user relies on this write for it."""
    fake, _ = env
    assert service.record_context_timezone(UID, "Asia/Shanghai", "zh") is True
    snap = service.snapshot(UID)
    assert snap["timezone"] == "Asia/Shanghai"
    assert snap["locale"] == "zh"


def test_record_context_timezone_rejects_junk(env):
    """Empty/unknown zones are no-ops — never poison state with a junk timezone
    (which would then be TTL-exempt and stick)."""
    fake, _ = env
    assert service.record_context_timezone(UID, "", "en") is False
    assert service.record_context_timezone(UID, "Not/AZone", "en") is False
    assert service.snapshot(UID)["timezone"] is None


def test_manual_user_state(env):
    fake, _ = env
    assert service.snapshot(UID)["user_state"] == "default"   # always present
    service.set_manual_user_state(UID, "focused")
    assert service.snapshot(UID)["user_state"] == "focused"


def test_unsupported_ignored(env):
    fake, _ = env
    res = service.ingest_snapshot(UID, [_item("unsupported", {
        "frontmost_app": None, "silent_mode": None, "focus": None, "precise_unlock": None})])
    assert res["unsupported"] == "ignored"


def test_playback_and_calendar_via_report(env):
    fake, _ = env
    service.ingest_snapshot(UID, [
        _item("playback", {"playback_state": "playing", "title": "晴天", "artist": "周杰伦"}),
        _item("calendar_next_event", {"title": "Team Sync", "minutes_until_start": 45,
                                      "event_kind": "virtual"}),
    ])
    snap = service.snapshot(UID)
    assert snap["now_playing"]["title"] == "晴天"
    assert snap["calendar_next_event"]["minutes_until_start"] == 45


def test_wake_debounce(env):
    fake, wakes = env
    fake.merge_config(UID, {"geofences": [
        {"label": "home", "lat": 37.0, "lon": -122.0, "radius_m": 150},
        {"label": "work", "lat": 40.0, "lon": -73.0, "radius_m": 150}]})
    service.ingest_snapshot(UID, [_item("location_signal",
                                        {"signal": {"latitude": 37.0, "longitude": -122.0}})])  # home, wake
    service.ingest_snapshot(UID, [_item("location_signal",
                                        {"signal": {"latitude": 40.0, "longitude": -73.0}})])   # work, debounced
    assert len([w for w in wakes if w[0] == "location"]) == 1


def test_photo_sensitive_scene_stored_without_hard_block(env):
    """V2 removes the old sensitive-scene hard block: even an id_card/document
    hint is stored encrypted and left to the agent's expression policy."""
    fake, _ = env
    out, code = service.photo_evaluate(
        UID, {"scene_hint": "id_card"}, {"id": "p_bad", "body_ct": "x"})
    assert code == 200 and out["usable"] is True and out["status"] == "stored"
    assert out["sensitive"] is True
    assert fake.get_photo_envelope(UID, "p_bad")["body_ct"] == "x"
    assert service.photos_recent(UID)[0]["photos"][0]["photo_id"] == "p_bad"


def test_photo_contextual_scene_stored_and_reaches_agent(env):
    """A 'private' scene is NOT hard-blocked — it's stored and reaches the agent
    (which self-censors per prompt)."""
    fake, _ = env
    out, _ = service.photo_evaluate(
        UID, {"scene_hint": "private", "is_indoor": True}, {"id": "p_priv", "body_ct": "c"})
    assert out["usable"] is True and out["status"] == "stored"
    assert out["sensitive"] is True
    assert out["metadata"]["scene_hint"] == "private"
    assert fake.get_photo_envelope(UID, "p_priv")["body_ct"] == "c"


def test_photo_one_step_store(env):
    """Normal photo: a single evaluate call stores the encrypted image in the
    frame channel and fires a wake."""
    fake, wakes = env
    out, code = service.photo_evaluate(
        UID, {"has_faces": "true", "scene_hint": "landscape"},
        {"id": "p_ok", "body_ct": "cipher"})
    assert code == 200 and out["status"] == "stored" and out["photo_id"] == "p_ok"
    listed = service.photos_recent(UID)[0]["photos"]
    assert len(listed) == 1 and listed[0]["photo_id"] == "p_ok"
    assert "envelope" not in listed[0]                       # no pixels in the list
    content, c2 = service.photo_content(UID, "p_ok")
    assert c2 == 200 and content["frame_id"] == "p_ok" and "envelope" not in content
    assert fake.get_photo_envelope(UID, "p_ok")["body_ct"] == "cipher"  # in frame channel
    assert any(c == "photos" for c, _ in wakes)               # legacy dormant fallback fired


def test_photo_v2_burst_dedup_collapses_rapid_captures(env, monkeypatch):
    """V2 photo path reapplies the 30s burst/cluster dedup: rapid captures must
    collapse to a single wake (regression — the v2 differ keys on a unique
    photo_id and would otherwise fire one wake per photo)."""
    fake, wakes = env
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: True)
    clock = {"t": 1000.0}
    monkeypatch.setattr(service, "_now", lambda: clock["t"])

    service.photo_evaluate(UID, {"scene_hint": "food"}, {"id": "p1", "body_ct": "c1"})
    clock["t"] += 5.0  # within the 30s cluster -> debounced, no new wake
    service.photo_evaluate(UID, {"scene_hint": "food"}, {"id": "p2", "body_ct": "c2"})

    assert len([w for w in wakes if w[0] == "photo_added"]) == 1
    # Both photos are stored regardless of the wake dedup.
    assert fake.get_photo_envelope(UID, "p1") and fake.get_photo_envelope(UID, "p2")
    assert any(e.get("type") == "debounced" and e.get("trigger") == "photo_added"
               for e in fake.read_events(UID))

    clock["t"] += 30.0  # past the window -> a fresh capture wakes again
    service.photo_evaluate(UID, {"scene_hint": "food"}, {"id": "p3", "body_ct": "c3"})
    assert len([w for w in wakes if w[0] == "photo_added"]) == 2


def test_photo_meta_envelope_is_preserved_only_on_content_read(env):
    fake, _ = env
    meta_env = {"id": "meta_1", "body_ct": "encrypted-place-label"}
    out, code = service.photo_evaluate(
        UID,
        {"scene_hint": "food"},
        {"id": "p_meta", "body_ct": "cipher"},
        meta_envelope=meta_env,
    )
    assert code == 200 and out["status"] == "stored"
    listed = service.photos_recent(UID)[0]["photos"]
    assert "meta_envelope" not in listed[0]
    content, c2 = service.photo_content(UID, "p_meta")
    assert c2 == 200
    assert content["meta_envelope"] == meta_env


def test_photo_usable_requires_envelope(env):
    """A usable photo with no content_envelope is a 400."""
    fake, _ = env
    out, code = service.photo_evaluate(UID, {"scene_hint": "food"}, None)
    assert code == 400 and out["error"] == "content_envelope_required"


def test_photos_no_setup_stored(env):
    """无任何权限设置：photo_evaluate 直接成功（photos 恒开）。"""
    fake, _ = env
    out, code = service.photo_evaluate(
        UID, {"scene_hint": "landscape"}, {"id": "p_ns", "body_ct": "c"})
    assert code == 200 and out["status"] == "stored"
    assert fake.get_photo_envelope(UID, "p_ns")["body_ct"] == "c"


def test_items_rejects_photo_kind(env):
    """photo must NOT be ingestable via the generic /items endpoint (would bypass
    the dedicated encrypted envelope path). Only /photo/evaluate stores photos."""
    fake, _ = env
    out, code = service.items_ingest(UID, "photo", [
        {"item_id": "p_x", "doc": {"status": "confirmed", "metadata": {"scene_hint": "id_card"}}}])
    assert code == 400 and out["error"] == "unknown_kind"
    assert service.photos_recent(UID)[0]["photos"] == []   # nothing leaked through


def test_app_open_records_current_and_history(env):
    """The Shortcut GET endpoint records current app (snapshot) + a usage event
    surfaced via snapshot.recent_apps."""
    fake, _ = env  # `app` always available
    out, code = service.app_open(UID, "Instagram", category="social")  # ts defaults to now
    assert code == 200 and out["app"] == "Instagram"
    snap = service.snapshot(UID)
    assert snap["app_name"] == "Instagram" and snap["app_category"] == "social"
    assert snap["recent_apps"][-1]["app"] == "Instagram"


def test_app_open_requires_app(env):
    fake, _ = env
    out, code = service.app_open(UID, "")
    assert code == 400 and out["error"] == "app_required"


def test_app_open_via_get_route(env, monkeypatch):
    """End-to-end: GET with everything (incl. key) in the URL query string."""
    import sys
    import types

    fake, _ = env
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: False)

    client = _PerceptionClient(fake)
    r = client.get("/v1/perception/app_open?key=APIKEY&app=Instagram&category=social&ts=1000")
    assert r.status_code == 200 and r.get_json()["app"] == "Instagram"
    assert fake.get_state(UID)["app_name"]["v"] == "Instagram"


def test_future_client_ts_clamped(env):
    """A far-future client_ts (clock skew / ms) is clamped to now, so it can't
    freeze state by making the ordering guard reject later correct reports."""
    fake, _ = env
    future = time.time() + 10 * 365 * 86400        # ~10 years ahead (or ms-as-s)
    service.ingest_snapshot(UID, [_item("motion_state", {"state": "walking"})], client_ts=future)
    stored_ts = fake.get_state(UID)["motion_state"]["ts"]
    assert stored_ts <= time.time() + 1             # clamped to ~now, not the future
    # a normal, current report right after is still accepted (not blocked)
    res = service.ingest_snapshot(UID, [_item("motion_state", {"state": "still"})])
    assert res["motion_state"] == "accepted"
    assert fake.get_state(UID)["motion_state"]["v"] == {"state": "still"}


def test_photo_metadata_string_bools(env):
    """默认字符串: is_screenshot as string 'false' must NOT be treated truthy."""
    fake, _ = env
    out, _ = service.photo_evaluate(
        UID, {"scene_hint": "food", "is_screenshot": "false"}, {"id": "p1", "body_ct": "c"})
    assert out["usable"] is True
    assert out["sensitive"] is False
    out2, _ = service.photo_evaluate(
        UID, {"scene_hint": "food", "is_screenshot": "true"}, {"id": "p2", "body_ct": "c"})
    assert out2["usable"] is True
    assert out2["sensitive"] is True


def test_string_scalar_values_stored(env):
    """默认字符串: scalar values may arrive as strings; stored verbatim."""
    fake, _ = env  # battery is always-on
    service.ingest_snapshot(UID, [_item("battery", {"level": "0.55", "charging": "false"})])
    st = fake.get_state(UID)
    assert st["battery_level"]["v"] == "0.55"
    assert st["charging"]["v"] == "false"


def test_client_ts_is_logical_time(env):
    fake, _ = env
    service.ingest_snapshot(UID, [_item("motion_state", {"state": "walking"})], client_ts=1000.0)
    assert fake.get_state(UID)["motion_state"]["ts"] == 1000.0


def test_stale_report_does_not_overwrite_newer(env):
    """A late-arriving OLDER record must not clobber a newer value."""
    fake, _ = env
    service.ingest_snapshot(UID, [_item("motion_state", {"state": "running"})], client_ts=200.0)
    res = service.ingest_snapshot(UID, [_item("motion_state", {"state": "still"})], client_ts=100.0)
    assert res["motion_state"] == "stale_ignored"
    cell = fake.get_state(UID)["motion_state"]
    assert cell["v"] == {"state": "running"} and cell["ts"] == 200.0   # newer kept


def test_ingest_snapshot_alias_and_message(env):
    """Aliases resolve (location -> location_signal); message is stored."""
    fake, _ = env
    fake.merge_config(UID, {"geofences": [
        {"label": "home", "lat": 37.0, "lon": -122.0, "radius_m": 150}]})
    res = service.ingest_snapshot(UID, [
        {"key": "motion_state", "data": '{"state":"walking","confidence":"high"}', "message": "运动"},
        {"key": "location", "data": '{"signal":{"latitude":37.0,"longitude":-122.0}}', "message": "位置"},
    ])
    assert res["motion_state"] == "accepted" and res["location"] == "accepted"
    st = fake.get_state(UID)
    assert st["motion_state"]["v"] == {"state": "walking", "confidence": "high"}
    assert st["motion_state"]["msg"] == "运动"
    assert st["place_label"]["v"] == "home"


def test_ingest_snapshot_null_records_unavailable(env):
    """An authorized capability reported with data:"null" makes its field null
    (unavailable now) and keeps the message."""
    fake, _ = env
    fake.merge_config(UID, {"geofences": [
        {"label": "home", "lat": 37.0, "lon": -122.0, "radius_m": 150}]})
    service.ingest_snapshot(UID, [_item("location_signal",
                                        {"signal": {"latitude": 37.0, "longitude": -122.0}})])
    assert fake.get_state(UID)["place_label"]["v"] == "home"
    service.ingest_snapshot(UID, [{"key": "location_signal", "data": "null", "message": "未授权定位"}])
    cell = fake.get_state(UID)["place_label"]
    assert cell["v"] is None and cell["msg"] == "未授权定位"
    assert service.snapshot(UID)["place_label"] is None


def test_report_endpoint_context_snapshot(env, monkeypatch):
    """End-to-end through the /report route with the context_snapshot body."""
    import sys
    import types

    fake, _ = env

    client = _PerceptionClient(fake)
    r = client.post("/v1/perception/report", json={"context_snapshot": [
        {"key": "motion_state", "data": json.dumps({"state": "walking"})},
    ]})
    assert r.status_code == 200
    assert r.get_json()["results"]["motion_state"] == "accepted"
    assert fake.get_state(UID)["motion_state"]["v"] == {"state": "walking"}

    # missing context_snapshot -> 400
    bad = client.post("/v1/perception/report", json={"signals": {}})
    assert bad.status_code == 400


def test_report_endpoint_context_snapshot_v2_flag_on(env, monkeypatch):
    """When the per-user rollout flag is on, /report dispatches to V2 ingress."""
    import types

    fake, _ = env
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: True)

    client = _PerceptionClient(fake)
    r = client.post("/v1/perception/report", json={"context_snapshot": [
        {"key": "motion_state", "envelope": {
            "v": 1, "id": "motion_env", "body_ct": "x", "nonce": "n",
            "K_user": "ku", "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": UID,
        }, "changed": True},
    ]})
    assert r.status_code == 200
    assert r.get_json()["results"]["motion_state"] == "accepted"
    assert "motion_state" not in fake.get_state(UID)


def test_report_user_state_key_sets_manual(env):
    """/report 携带显式 user_state 键 -> 设置手动 user_state（折叠 POST /user_state）。"""
    fake, _ = env
    res = service.ingest_snapshot(UID, [
        {"key": "user_state", "data": '"focused"', "message": ""}])
    assert res["user_state"] == "accepted"
    assert service.snapshot(UID)["user_state"] == "focused"


def test_snapshot_includes_recent_apps(env):
    """app_open 后，snapshot 带 recent_apps（折叠 app 历史读取面）。"""
    fake, _ = env
    service.app_open(UID, "Instagram", category="social", client_ts=1000.0)
    service.app_open(UID, "Maps", category="navigation", client_ts=1001.0)
    snap = service.snapshot(UID)
    apps = [e["app"] for e in snap["recent_apps"]]
    assert "Instagram" in apps and "Maps" in apps


def _report_client(env, monkeypatch, *, ingress_v2=False):
    import sys
    import types
    fake, _ = env
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: ingress_v2)
    return fake, _PerceptionClient(fake)


def test_report_multiplex_items(env, monkeypatch):
    """/report 携带 items -> 写入集合，items_recent 读回。"""
    fake, client = _report_client(env, monkeypatch)
    r = client.post("/v1/perception/report", json={"items": {
        "workout": [{"item_id": "w1", "doc": {"kind": "run", "minutes": 30}}]}})
    assert r.status_code == 200
    assert r.get_json()["results"]["items"]["workout"]["written"] == 1
    got, code = service.items_recent(UID, "workout")
    assert code == 200 and got["items"][0]["minutes"] == 30


def test_report_multiplex_config(env, monkeypatch):
    """/report 携带 config -> 合并进配置。"""
    fake, client = _report_client(env, monkeypatch)
    r = client.post("/v1/perception/report", json={"config": {
        "geofences": [{"label": "home", "lat": 37.0, "lon": -122.0, "radius_m": 150}]}})
    assert r.status_code == 200
    assert fake.get_config(UID)["geofences"][0]["label"] == "home"


def test_report_empty_body_400(env, monkeypatch):
    """三者全空 -> 400。"""
    fake, client = _report_client(env, monkeypatch)
    r = client.post("/v1/perception/report", json={"signals": {}})
    assert r.status_code == 400


def test_report_user_state_stale_ignored(env):
    """更晚 client_ts 设定的 user_state 不被更早(延迟到达)的报告覆盖。"""
    fake, _ = env
    service.ingest_snapshot(
        UID, [{"key": "user_state", "data": '"focused"', "message": ""}], client_ts=200.0)
    res = service.ingest_snapshot(
        UID, [{"key": "user_state", "data": '"away"', "message": ""}], client_ts=100.0)
    assert res["user_state"] == "stale_ignored"
    assert service.snapshot(UID)["user_state"] == "focused"


def test_report_items_invalid_kind_400(env, monkeypatch):
    """items 含非法 kind -> /report 返回 400 并暴露错误（不静默成功）。"""
    fake, client = _report_client(env, monkeypatch)
    r = client.post("/v1/perception/report", json={"items": {
        "photo": [{"item_id": "x", "doc": {"status": "confirmed"}}]}})
    assert r.status_code == 400
    assert r.get_json()["results"]["items"]["photo"]["error"] == "unknown_kind"


def test_report_empty_sections_400(env, monkeypatch):
    """空 section（[] / {} / {}）不算 provided -> 400。"""
    fake, client = _report_client(env, monkeypatch)
    for body in ({"context_snapshot": []}, {"items": {}}, {"config": {}}):
        r = client.post("/v1/perception/report", json=body)
        assert r.status_code == 400, body


def test_report_items_malformed_400(env, monkeypatch):
    """items 的 collection 值不是 list-of-objects -> 400（不 500）。"""
    fake, client = _report_client(env, monkeypatch)
    for bad in ({"workout": {"a": 1}}, {"workout": [123]}, {"workout": "x"}):
        r = client.post("/v1/perception/report", json={"items": bad})
        assert r.status_code == 400, bad


# ---------------------------------------------------------------------------
# Mechanical wake gate (enabled / dnd / away) — mirrors the tick path
# ---------------------------------------------------------------------------

def _ingest_location(uid, label):
    return service.ingest_snapshot(uid, [{
        "key": "location_signal",
        "data": json.dumps({"place_label": label}),
    }])


def test_wake_suppressed_when_perception_user_state_away(env, monkeypatch):
    fake, wakes = env
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {})
    uid = "u_away"
    service.ingest_snapshot(uid, [{"key": "user_state", "data": json.dumps("away")}])
    _ingest_location(uid, "gym")
    assert wakes == []
    events = fake.read_events(uid)
    assert any(e.get("type") == "suppressed" and e.get("reason") == "user_away"
               for e in events)
    assert not any(e.get("type") == "wake" for e in events)


def test_wake_suppressed_when_settings_disabled_or_dnd_or_away(env, monkeypatch):
    fake, wakes = env
    cases = [
        ({"enabled": False}, "proactive_disabled"),
        ({"enabled": True, "dnd": True}, "dnd_enabled"),
        ({"enabled": True, "dnd": False, "user_state": "away"}, "user_away"),
    ]
    for i, (settings, reason) in enumerate(cases):
        monkeypatch.setattr(service, "_app_proactive_settings", lambda uid, s=settings: s)
        uid = f"u_gate_{i}"
        _ingest_location(uid, "gym")
        assert wakes == [], f"case {reason}: wake should be suppressed"
        assert any(e.get("type") == "suppressed" and e.get("reason") == reason
                   for e in fake.read_events(uid)), f"case {reason}"


def test_wake_suppressed_until_proactive_activation(env, monkeypatch):
    fake, wakes = env
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {})
    monkeypatch.setattr(service, "_proactive_activation_ready", lambda uid: False)
    uid = "u_activation_pending"
    _ingest_location(uid, "gym")
    assert wakes == []
    events = fake.read_events(uid)
    assert any(e.get("type") == "suppressed" and e.get("reason") == "activation_pending"
               for e in events)
    assert not any(e.get("type") == "wake" for e in events)


def test_wake_fires_normally_when_gate_open(env, monkeypatch):
    fake, wakes = env
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {"enabled": True})
    uid = "u_open"
    _ingest_location(uid, "gym")
    assert len(wakes) == 1
    assert any(e.get("type") == "wake" for e in fake.read_events(uid))


def test_wake_recovers_after_away_clears(env, monkeypatch):
    fake, wakes = env
    monkeypatch.setattr(service, "_app_proactive_settings", lambda uid: {})
    uid = "u_recover"
    service.ingest_snapshot(uid, [{"key": "user_state", "data": json.dumps("away")}])
    _ingest_location(uid, "gym")
    assert wakes == []
    service.ingest_snapshot(uid, [{"key": "user_state", "data": json.dumps("default")}])
    _ingest_location(uid, "home")
    assert len(wakes) == 1


def test_app_settings_failure_does_not_block_wake(env, monkeypatch):
    """app 不可达（如单测环境 import 失败）时不拦截——拦截是 best-effort。"""
    fake, wakes = env

    def boom(uid):
        raise RuntimeError("no app here")
    monkeypatch.setattr(service, "_app_proactive_settings", boom)
    uid = "u_no_app"
    _ingest_location(uid, "gym")
    assert len(wakes) == 1


def test_photo_suppressed_when_photo_wake_disabled(env, monkeypatch):
    """Photo wake off blocks self-initiated photo wakes; ambient no longer does."""
    fake, wakes = env
    monkeypatch.setattr(service, "perception_ingress_runtime_v2_enabled",
                        lambda user_or_store: True)
    monkeypatch.setattr(service, "_settings_v2_for_user",
                        lambda uid: {"switches": {"photo_wake_enabled": False}})
    uid = "u_photo_dnd"
    out, code = service.photo_evaluate(
        uid, {"scene_hint": "food"}, {"id": "p_dnd", "body_ct": "cipher"})
    assert code == 200 and out["status"] == "stored"
    assert wakes == []
    events = fake.read_events(uid)
    suppressed = [e for e in events
                  if e.get("cap") == "runtime_v2" and e.get("type") == "suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["reason"] == "photo_wake_disabled"
    assert suppressed[0]["origin_refs"] == ["photo:p_dnd"]
    assert not any(e.get("cap") == "runtime_v2" and e.get("type") == "wake"
                   for e in events)
