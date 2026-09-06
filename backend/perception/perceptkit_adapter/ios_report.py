"""FeedlingIOSReportAdapter -- turn one iOS snapshot into a ``ReportEnvelope``.

The spec (§16) names this as one of the three adapters that belong to the
host rather than to the kit: it has to understand one specific producer's
shape, and the kit only knows the standard envelope. It started life as the
worked example shipped with the package; this is the host's copy of it.

**It is fed already-decrypted values.** The live v2 ingest decrypts the
sensitive signals through the enclave and collects everything into
``storage_items`` as ``{key, data}`` pairs -- exactly the shape this adapter
reads. Running from that point costs no extra enclave calls, which matters:
one report is roughly seven decrypts already.

## The one thing here that really matters: absence must not become zero

iOS encodes three states in ``data``:

    an object   a real reading        -> observed
    ``""``      authorized, nothing read this round -> no_data
    ``null``    permission withheld   -> unavailable

Collapse the last two into 0 and every layer downstream processes a
fabrication faithfully: rules fire, trends compute, and the agent says "you
didn't walk at all today". Nothing crashes and nothing is logged. Only the
user knows the sentence is wrong.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

#: iOS snapshot key -> kit signal. Names that differ get reconciled here,
#: never in the kit: every producer calls things something else, and changing
#: the kit would be deciding for every other host.
KEY_TO_SIGNAL: dict[str, str] = {
    "time": "time_context",
    "battery": "battery",
    "broadcast": "broadcast",
    "motion_state": "motion_state",
    "focus": "focus_state",
    "audio_route": "audio_route",
    "weather": "weather",
    "playback": "music_playback",
    # 🔴 左边是 **iOS 送来的 key**，右边是 manifest 的信号名。左边不能动 ——
    # 那是设备上的契约，改它等于让所有已安装的 app 送来的东西认不出来。
    #
    # perceptkit 0.4.0 把三个多指标信号拆成了十二个，所以 iOS 的一个快照
    # 现在要变成好几条观测：主信号在这里认领，其余走 SPLIT_OFF。
    # 为什么拆见 kit 那边 —— 一句话是：逐条样本一次只带一个指标，
    # 挤在一个信号里会把兄弟字段从当前值里静默抹掉。
    "health_vitals": "health_resting_hr",
    "health_sleep": "health_sleep",
    "health_workout": "health_workout",
    "health_activity": "health_activity",
    "health_body": "health_weight",
    "health_metabolic": "health_glucose",
    "health_cycle": "health_cycle",
    "health_mood": "health_mood",
}

#: Keys that do not enter the standard pipeline. Written down so nobody
#: later reads their absence as an oversight.
IGNORED_KEYS = {
    # What third-party apps cannot read. iOS sends this to say so explicitly.
    "unsupported",
    # Calendar and reminders take the source-mirror path, not the signal path
    # -- which is what §7.13 specifies too.
    "calendar_next_event", "reminders",
    # Location is resolved to a city or anchor on the device; coordinates never
    # leave it, so this is not a pass-through observation.
    "location_signal",
}

#: Per-signal field renames, where iOS's name differs from the manifest's.
FIELD_ALIASES: dict[str, dict[str, str]] = {
    "battery": {"level": "level_ratio", "charging": "is_charging",
                "low_power_mode": "is_low_power_mode_enabled"},
    "focus_state": {"focused": "is_active"},
    # iOS sends {state, active}; the manifest declares broadcast_state and
    # is_active. Without this the whole observation is rejected for a missing
    # required field -- which is how the shadow run found it.
    "broadcast": {"active": "is_active"},
    "time_context": {"timezone": "time_zone_id", "local_time": None},
    "weather": {"temperature": "temperature_c",
                "apparent_temperature": "apparent_temperature_c",
                "humidity": "humidity_ratio",
                "precipitation_chance": "precipitation_probability"},
    # The four below were found by the shadow's first comparison against real
    # data: iOS was sending each of these and the manifest was dropping it as
    # undeclared, so blood pressure, body fat and workout duration reached the
    # live path and never reached the kit at all.
    "health_weight": {"body_fat_pct": "body_fat_ratio"},
    "health_glucose": {"blood_pressure_systolic": "blood_pressure_systolic_mmhg",
                       "blood_pressure_diastolic": "blood_pressure_diastolic_mmhg"},
    "health_workout": {"duration_min": "duration_minutes"},
    "audio_route": {"device_name": "device_label"},
    "music_playback": {"album_title": "album"},
}

#: Renames that are not only renames. Applied **after** the alias, keyed by
#: the manifest's name.
#:
#: ``body_fat_pct`` -> ``body_fat_ratio`` is the reason this exists. Renaming
#: it alone stores 18.4 in a field the manifest declares as a ratio in
#: ``[0, 1]`` -- 1840% body fat. Nothing crashes: the range check rejects it,
#: and body fat silently never arrives. A pair of names this similar with a
#: 100x difference between them is exactly what a hand-maintained alias table
#: gets wrong.
FIELD_TRANSFORMS: dict[str, dict[str, Any]] = {
    "health_weight": {"body_fat_ratio": lambda v: v / 100.0},
}

#: Fields iOS sends that the manifest does not declare. Dropped explicitly
#: rather than silently filtered, so "why can't I find the
#: authorization_status I sent" has an answer.
DROPPED_FIELDS: dict[str, set[str]] = {
    # Authorization is expressed through availability, not as its own field.
    "focus_state": {"authorization_status"},
    # iOS sends a broadcast state string; the manifest models only the boolean
    # (the spec's §5 table lists is_active and nothing else). Dropped here so
    # the reason is visible, rather than silently filtered at the boundary.
    "broadcast": {"state"},
    # Local time is derivable from time_zone_id plus occurred_at.
    "time_context": {"local_time"},
    # ⚠️ step_count 曾经列在这里（键 "health_vitals"）。**不要加回来**：
    # 这张表是按**信号名**查的，而 0.4.0 拆分之后体征的主信号叫
    # health_resting_hr —— 这一条早就查不到、成了死条目。真正防重复的是
    # 下面 SPLIT_OFF 的 `moved` 过滤。
    # 把它按新名字"修好"反而会出事：拆分读的是 _rename 之后的结果，
    # step_count 在那一步被丢掉的话，`steps` 信号就再也收不到数据。
    # The manifest counts workouts by aggregating the records themselves, so
    # a device-supplied daily count would be a second, disagreeing answer to
    # the same question.
    "health_workout": {"count_today"},
    # iOS deliberately sends only how many mood labels there were, never which
    # ones -- there are ~40 categories and they are unusually revealing. The
    # manifest declares `labels`, which iOS will not send, and `recorded_at`,
    # for which iOS sends a same-day boolean. Dropped here rather than
    # half-mapped; reconciling the two is sevenfloor's call, not a rename.
    "health_mood": {"label_count", "recorded_today"},
    # iOS reports how long the state has been running; the manifest models the
    # label only and derives duration from adjacent observations.
    "motion_state": {"started_at", "confidence"},
    # `duration` is the track's total length. The manifest's position_seconds
    # is where playback currently is -- a different fact, not a rename, and
    # iOS does not report it. Dropped rather than aliased into the wrong field.
    "music_playback": {"duration", "media_type"},
}


#: Producer vocabulary -> manifest vocabulary, per field.
#:
#: **This is where the most expensive class of bug in this file lives.** A name
#: mismatch fails loudly on every report; a *value* mismatch fails only for the
#: values that differ, so it hides behind whichever value the test fixture
#: happened to use. Both entries below were invisible until the shadow compared
#: real data:
#:
#:   motion "still"       iOS's word for standing still; the manifest says
#:                        "stationary". Every observation of the single most
#:                        common state a person is in was being rejected, while
#:                        "walking" -- the value the fixture used -- passed.
#:   motion "in_vehicle"  same, against "automotive".
#:
#: Left unmapped deliberately: playback "unknown". The manifest has no state
#: for it and guessing one puts a fabricated answer in front of the user; a
#: rejection is visible in the shadow report, a wrong guess is not.
VALUE_MAPS: dict[str, dict[str, dict[str, str]]] = {
    "motion_state": {"state": {"still": "stationary",
                               "in_vehicle": "automotive"}},
    "music_playback": {"playback_state": {
        # Stopped by something outside the app -- a call, another player.
        # Indistinguishable from paused to anyone reading it.
        "interrupted": "paused",
        # Scrubbing. Audio is engaged; the manifest has no seeking state.
        "seeking_forward": "playing",
        "seeking_backward": "playing",
    }},
}


#: Fields iOS packs into one signal that the manifest models as a signal of
#: its own: ``{ios signal: {ios field: (target signal, target field)}}``.
#:
#: Steps is the case. iOS puts ``step_count`` inside ``health_vitals``; the
#: manifest gives it its own signal because a running day total aggregates
#: differently from a vitals reading -- it is monotonic within the day, so the
#: day's value is the last one, not the average of the readings.
#:
#: Without this the field is simply dropped as undeclared, which is how it
#: went missing: steps reached the live path and never reached the kit. It
#: cost one line in the divergence report to notice and would have cost
#: nothing to keep missing.
SPLIT_OFF: dict[str, dict[str, tuple[str, str]]] = {
    "health_vitals": {
        "step_count": ("steps", "step_count"),
        # 0.4.0 的拆分。静息心率留在主信号（见 KEY_TO_SIGNAL），
        # 其余各自成信号。
        "current_heart_rate": ("health_current_hr", "current_heart_rate"),
        "hrv_sdnn_ms": ("health_hrv", "hrv_sdnn_ms"),
        "respiratory_rate": ("health_respiratory", "respiratory_rate"),
        "oxygen_saturation_pct": ("health_oxygen", "oxygen_saturation_pct"),
        "vo2_max": ("health_vo2max", "vo2_max"),
    },
    # ⚠️ 这里写的是**归一之后**的字段名（manifest 侧），不是 iOS 的原名。
    # iOS 送 body_fat_pct，别名表把它归一成 body_fat_ratio —— 拆分在归一
    # 之后做，否则这一条永远匹配不上，体脂静默丢失（写这段时真踩到）。
    "health_body": {
        "bmi": ("health_bmi", "bmi"),
        "body_fat_ratio": ("health_body_fat", "body_fat_ratio"),
        "height_cm": ("health_height", "height_cm"),
    },
    "health_metabolic": {
        # 收缩压和舒张压一起走 —— 来源侧它们是一次读数，拆成两条就丢了
        # 「这是同一次量的」。两个字段都指向同一个目标信号，
        # 下面的拆分循环会把它们合成一条观测。
        "blood_pressure_systolic_mmhg": ("health_blood_pressure",
                                         "blood_pressure_systolic_mmhg"),
        "blood_pressure_diastolic_mmhg": ("health_blood_pressure",
                                          "blood_pressure_diastolic_mmhg"),
    },
}


#: Some signals put the authorization state in a field inside `data` rather
#: than using `data: null`. Focus does:
#: `{"authorization_status": "denied", "focused": null}`.
#: Read naively that is an observed reading whose required field is null, so
#: the pipeline rejects it as malformed -- reported as "bad data format" when
#: the truth is "the user withheld permission", which sends anyone debugging
#: it entirely the wrong way.
AUTH_STATUS_FIELDS: dict[str, str] = {
    "focus_state": "authorization_status",
}

#: Which values of that field count as authorized.
AUTHORIZED_VALUES = {"authorized", "granted", "allowed"}


def snapshot_timezone(payload: Mapping[str, Any]) -> str | None:
    """The IANA timezone the whole snapshot was taken in.

    iOS puts it in the ``time`` item, and every other item in the same
    snapshot was sampled at the same moment in the same place. Not carrying it
    across means every observation falls back to the UTC offset in
    ``occurred_at`` -- and an offset is not a timezone: New York's -04:00 and
    -05:00 are the same zone in different seasons, so the day a DST transition
    happens gets attributed wrong, silently.
    """
    for item in payload.get("context_snapshot", []):
        if item.get("key") != "time":
            continue
        data = item.get("data")
        if isinstance(data, Mapping):
            tz = data.get("timezone")
            return tz if isinstance(tz, str) and tz else None
    return None


#: A signal whose decrypted payload arrives as a bare scalar, and the manifest
#: field that scalar belongs to.
#:
#: `storage_items` is **not uniform**, which is easy to miss and impossible to
#: see from a hand-written fixture. Plain operation signals arrive with `data`
#: as an object. Decrypted sensitive signals arrive with `data` as a **JSON
#: string**, and for a single-output signal the live path unwraps it further to
#: just the value -- so motion_state shows up as the string `"walking"`, not
#: `{"state": "walking"}`.
#:
#: Read naively, those become `observed` observations with no value, and the
#: contract rejects every one of them. That is most of perception: location,
#: playback, motion and all of health ride this path.
SCALAR_FIELD: dict[str, str] = {
    "motion_state": "state",
    "music_playback": "playback_state",
}


def _unwrap(data: Any, signal: str) -> Any:
    """Bring both shapes of `data` back to one object.

    A JSON string is parsed; a bare scalar is put back under the field the
    signal declares for it. Anything still unrecognisable is returned as-is so
    the pipeline rejects it loudly rather than storing a shape nobody expects.
    """
    if isinstance(data, str) and data not in ("",):
        try:
            data = json.loads(data)
        except ValueError:
            return data
    if data is not None and not isinstance(data, Mapping):
        field = SCALAR_FIELD.get(signal)
        if field:
            return {field: data}
    return data


def _availability(data: Any, signal: str | None = None) -> str:
    """The three-state decision -- the most important few lines in here."""
    if data is None:
        return "unavailable"        # permission withheld
    if data == "":
        return "no_data"            # authorized, nothing read this round
    field = AUTH_STATUS_FIELDS.get(signal or "")
    if field and isinstance(data, Mapping):
        status = data.get(field)
        if status is not None and str(status).lower() not in AUTHORIZED_VALUES:
            return "unavailable"    # the in-payload authorization case
    return "observed"


def _music_fields(value: Mapping[str, Any], *, reason: str | None) -> dict[str, Any]:
    """Fill the two fields the manifest requires but iOS cannot send.

    ``track_key`` replaces Apple's persistent song id, which the iOS side
    deliberately dropped years ago because it can be walked back to the user's
    whole library. Hashing (title, artist) gives a stable identity instead. The
    cost is real and worth stating: a live version and a studio version of the
    same song by the same artist become one track to us.

    ``edge_quality`` is per-record because iOS gives real playback edges only
    for the system player. A report triggered by `playback_changed` saw an
    actual event, so its start and end are measured; anything sampled by the
    keepalive snapshot is inferred from adjacent points and is estimated.
    Collapsing both to "estimated" would throw away the half that is accurate.
    """
    title = str(value.get("title") or "")
    artist = str(value.get("artist") or "")
    out: dict[str, Any] = {}
    if title or artist:
        out["track_key"] = hashlib.sha256(
            f"{title}\u0000{artist}".encode()).hexdigest()[:16]
    out["edge_quality"] = "measured" if reason == "playback_changed" else "estimated"
    return out


def _rename(signal: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """iOS field names -> manifest field names.

    Get this wrong and the pipeline rejects the whole observation for a
    missing required field. That is the good outcome: a loud rejection beats
    quietly storing rows whose fields nothing will ever query.
    """
    alias = FIELD_ALIASES.get(signal, {})
    dropped = DROPPED_FIELDS.get(signal, set())
    transforms = FIELD_TRANSFORMS.get(signal, {})
    values_map = VALUE_MAPS.get(signal, {})
    out: dict[str, Any] = {}
    for k, v in value.items():
        if v is None or k in dropped:
            continue
        name = alias.get(k, k)
        fn = transforms.get(name)
        if fn is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
            v = fn(v)
        vocab = values_map.get(name)
        if vocab and isinstance(v, str):
            v = vocab.get(v, v)
        out[name] = v
    return out


#: iOS 送的样本身份字段。**现在 iOS 一个都不送** —— 这条通路是先接上，
#: 等 anchored query 那批上线就能直接用（后端先兼容、producer 不启用）。
#:
#: 没有它，健康信号的身份会退回确定性摘要（subject+source+signal+occurred_at），
#: 于是"删掉那条体重"在本地没有任何指向，修订也认不出是同一件事。
_IDENTITY_KEYS = ("source_event_id", "source_revision")


def _sample_identity(data: Any) -> dict[str, Any]:
    """把 iOS 送的样本身份提升成 Observation 的顶层字段。

    留在 value 里没用：normalize 只读顶层的 ``source_event_id``，
    value 里的会被当成一个未声明字段丢掉 —— 安静地。
    """
    if not isinstance(data, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in _IDENTITY_KEYS:
        raw = data.get(key)
        if raw is not None and str(raw).strip():
            out[key] = raw
    return out


def report_id_for(payload: Mapping[str, Any]) -> str:
    """Derived from the payload, with no clock and no randomness.

    Mixing either in makes every retransmission look like a new report and
    idempotency stops meaning anything -- and retransmission is normal: a
    flaky network or a suspended app produces one.
    """
    canonical = repr(sorted(
        (i.get("key"), repr(i.get("data")))
        for i in payload.get("context_snapshot", [])
    )) + str(payload.get("client_ts", ""))
    return "ios-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def to_envelope(payload: Mapping[str, Any], *, occurred_at: str,
                reason: str | None = None) -> dict[str, Any]:
    """Turn one iOS snapshot into a standard report envelope.

    ``occurred_at`` comes from the caller: the whole snapshot is sampled at one
    moment, and iOS carries the device local time in its ``time`` item. Use
    that, not the host's own clock.
    """
    observations: list[dict[str, Any]] = []
    timezone_id = snapshot_timezone(payload)
    for item in payload.get("context_snapshot", []):
        key = item.get("key")
        if key in IGNORED_KEYS:
            continue
        signal = KEY_TO_SIGNAL.get(key)
        if signal is None:
            # An unknown key is skipped, not an error: iOS may ship a new field
            # a release before the backend understands it. Erroring instead
            # would fail the whole report.
            continue

        data = _unwrap(item.get("data"), signal)
        availability = _availability(data, signal)
        obs: dict[str, Any] = {
            "signal": signal,
            "signal_schema_version": 1,
            "occurred_at": occurred_at,
            "availability": availability,
        }
        if timezone_id:
            obs["timezone"] = timezone_id
        obs.update(_sample_identity(data))
        emit_main = True
        if availability == "observed" and isinstance(data, Mapping):
            value = _rename(signal, data)
            if signal == "music_playback":
                value.update(_music_fields(data, reason=reason))
            # 已经拆到别的信号名下的字段，主信号不再带 —— 否则主信号会带着
            # 一堆不属于它的字段，manifest 校验会把它们当未声明字段丢掉
            # （安静地），而更糟的是万一某个名字碰巧合法，就成了两条观测
            # 都声称拥有同一个指标。
            moved = set(SPLIT_OFF.get(key, {}))
            value = {k2: v2 for k2, v2 in value.items() if k2 not in moved}
            if moved and not value:
                # 这一趟上报里主信号的字段**全部**被拆走了（只测了血压、
                # 没测血糖）。照旧发一条 `observed` + 空 value，等于替设备
                # 说了一句「我看了血糖，结果是空」—— 那不是设备说的话。
                # 下游会把它当成一次真实测量：当前值被一条没有数值的记录
                # 顶掉，日聚合多一次 count，而错误不会以报错的形式出现。
                emit_main = False
            obs["value"] = value
        if emit_main:
            observations.append(obs)

        # Fields that belong to a signal of their own. Emitted as separate
        # observations so each gets the aggregation its own signal declares.
        #
        # Keyed by the **iOS key**, not the mapped signal: one iOS payload now
        # fans out to several kit signals, so the mapped name is one of the
        # outputs and cannot also be the lookup.
        #
        # Grouped by target before emitting: blood pressure sends systolic and
        # diastolic to the same signal, and they have to arrive as one
        # observation. Two observations each carrying half is not "the same
        # reading twice" -- it is two half readings, and the second overwrites
        # the first's current value with a record that is missing the other number.
        # 先过一遍别名/单位换算，再按归一后的名字拆 —— 顺序反了的话，
        # iOS 原名（body_fat_pct）匹配不上拆分表里的归一名，字段静默丢失。
        normalized = _rename(signal, data) if isinstance(data, Mapping) else {}
        grouped: dict[str, dict[str, Any]] = {}
        for src, (target, field) in SPLIT_OFF.get(key, {}).items():
            raw = normalized.get(src)
            if raw is None:
                # Absence stays absence. Emitting `no_data` here would claim
                # the device reported "no steps" every time a vitals reading
                # arrives without one, which is a different sentence.
                continue
            grouped.setdefault(target, {})[field] = raw
        for target, value in grouped.items():
            split_obs: dict[str, Any] = {
                "signal": target,
                "signal_schema_version": 1,
                "occurred_at": occurred_at,
                "availability": "observed",
                "value": value,
            }
            if timezone_id:
                split_obs["timezone"] = timezone_id
            # 拆出来的也要带身份，否则它们退回确定性摘要 ——
            # 撤回和修订就永远落不到这些信号头上。血压那组两个字段
            # 共用同一个样本身份（来源侧它们本来就是一次读数）。
            split_obs.update(_sample_identity(data))
            observations.append(split_obs)

    return {
        "schema_version": 1,
        "report_id": report_id_for(payload),
        "producer": "ios",
        "observations": observations,
    }


__all__ = ["KEY_TO_SIGNAL", "IGNORED_KEYS", "FIELD_ALIASES", "DROPPED_FIELDS",
           "FIELD_TRANSFORMS", "VALUE_MAPS", "AUTH_STATUS_FIELDS", "AUTHORIZED_VALUES",
           "SPLIT_OFF", "report_id_for", "to_envelope"]
