"""把老路攒的日聚合搬进 kit 的表。

切换的最后一件事。kit 只有影子跑起来之后的数据；老路一停写，
`perception_daily` 就冻在那天，而「你这周步数比上周多」读的正是它 ——
**用户的感知历史会从那天断掉**。

## 这是转格式，不是重算

两边的日聚合用的是同一批 merger（kit 的聚合分派就是调 `history` 里那几个）。
所以绝大多数信号是「读一行、改个信号名和字段名、写一行」，数字一个不动。

真正要动脑子的只有四个，各自的理由写在 `_CONVERTERS` 上。

## 三条硬约束

**可以重复跑。** 按 (用户, 日期, 信号) 覆盖写。中途失败、连接断了、跑一半
发现映射写错了 —— 改完从头再跑一遍就行。不可重跑的搬迁没人敢跑第二次。

**不动老表。** 只读老的、只写 kit 的。老路继续正常工作；真出问题，把 kit
的日聚合清掉重来，老数据一行没碰。

**认不出来的不搬。** 老表里有 kit 不认识的信号（改过名的、下线的），
**跳过并计数**，不猜。猜错了不会报错，只会让某个趋势多出一段假数据。
"""
from __future__ import annotations

import logging
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

#: 老信号名 -> kit 信号名。和适配层那张表同源，不另写一份。
def _key_map() -> dict[str, str]:
    from .ios_report import KEY_TO_SIGNAL
    return dict(KEY_TO_SIGNAL)


#: 老表里有、但**历史搬不过去**的信号，各自的真实理由。
#:
#: 和「kit 不认识这个信号」不是一回事，写在一起会掩盖真正的原因 ——
#: 而排查「为什么这段历史没了」的时候，理由就是答案本身。
UNCONVERTIBLE: dict[str, str] = {
    "location_signal": (
        "维度不同：老的按 place_label 分桶（在家 6 小时），kit 按城市分桶"
        "（在上海 6 小时）。从前者推不出后者 —— 家在哪个城市，这份数据里没有。"
        "城市历史从切换那天重新开始攒。"
    ),
    "calendar_next_event": (
        "日历改走来源镜像，不再有「每天一份事件列表」这种历史 —— "
        "规范 §5.4 明确要求不保存每次同步的快照。老的那份按设计就该消失。"
    ),
    "reminders": "同日历：改走来源镜像，不保存同步快照的历史。",
}


#: 一次写多少行。批小了慢，批大了一个事务锁太久。
BATCH = 500

#: 聚合版本。搬进来的这批和管线现算的是同一套算法，所以用同一个版本号 ——
#: 标成别的版本会让「重算」逻辑以为它们需要被重算。
AGGREGATION_VERSION = 1
CONVERTER_REVISION = "v010-1"


@dataclass
class BackfillPlan:
    """搬了什么、跳过了什么。**跳过的必须能说出名字**，不然「搬完了」
    和「搬了一半剩下的悄悄没了」长得一样。"""

    migrated: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    rows_read: int = 0
    applied: bool = False
    subject_scope: str | None = None
    generation_ids: dict[str, str] = field(default_factory=dict)
    active_generations: int = 0
    reused_generations: int = 0
    incomplete_generations: int = 0
    incomplete_dates: dict[str, list[str]] = field(default_factory=dict)
    unconvertible_rows: list[dict[str, str]] = field(default_factory=list)
    failed_keys: list[dict[str, str]] = field(default_factory=list)
    legacy_reports_without_v2_digest: int = 0
    legacy_identities_missing_fact_evidence: int = 0
    legacy_outbox_missing_provenance: int = 0
    unknown_runtime_attempts: int = 0
    rerun_identity: str = ""

    @property
    def total(self) -> int:
        return sum(self.migrated.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "applied": self.applied,
            "subject_scope": (
                None if self.subject_scope is None else {
                    "subject_hash": hashlib.sha256(
                        self.subject_scope.encode()).hexdigest()[:12],
                }
            ),
            "rows_read": self.rows_read,
            "recoverable_rows": self.total,
            "migrated": dict(sorted(self.migrated.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "reasons": dict(sorted(self.reasons.items())),
            "generation_ids": dict(sorted(self.generation_ids.items())),
            "active_generations": self.active_generations,
            "reused_generations": self.reused_generations,
            "incomplete_generations": self.incomplete_generations,
            "incomplete_date_count": sum(
                len(days) for days in self.incomplete_dates.values()),
            "incomplete_dates": dict(sorted(self.incomplete_dates.items())),
            "unconvertible_rows": self.unconvertible_rows,
            "legacy_reports_without_v2_digest": self.legacy_reports_without_v2_digest,
            "legacy_identities_missing_fact_evidence": self.legacy_identities_missing_fact_evidence,
            "legacy_outbox_missing_provenance": self.legacy_outbox_missing_provenance,
            "unknown_runtime_attempts": self.unknown_runtime_attempts,
            "failed_keys": self.failed_keys,
            "rerun_identity": self.rerun_identity,
            "converter_revision": CONVERTER_REVISION,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# 四个形状不一样的
# ---------------------------------------------------------------------------

def _rename_fields(doc: Mapping[str, Any], mapping: Mapping[str, str],
                   *, scale: Mapping[str, float] = {}) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in doc.items():
        name = mapping.get(k, k)
        factor = scale.get(name)
        if factor and isinstance(v, Mapping):
            v = {kk: (vv * factor if isinstance(vv, (int, float))
                      and not isinstance(vv, bool) else vv) for kk, vv in v.items()}
        elif factor and isinstance(v, (int, float)) and not isinstance(v, bool):
            v = v * factor
        out[name] = v
    return out


def _health_vitals(doc: Mapping[str, Any]) -> dict[str, Any]:
    """步数搬出去、vo2_max 从分布压成代表值。

    老路把整条 health_vitals 按 numeric_dist 存，kit 里 vo2_max 声明的是
    main_of_day（当天代表值）。分布里取哪个当代表值是个选择：**取最后一次**
    —— 和 main_of_day 自己的语义一致（它就是「最后写进来的那个值」），
    而老 doc 里没留最后一次，只能拿 max 近似。这一点在报告里说明白，
    不假装它是精确的。
    """
    out = {k: v for k, v in doc.items() if k not in ("step_count", "vo2_max")}
    vo2 = doc.get("vo2_max")
    if isinstance(vo2, Mapping) and vo2.get("max") is not None:
        out["vo2_max"] = vo2["max"]
    return out


def _steps_from_vitals(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """步数在老路里住在 health_vitals 里，kit 给了它独立信号。"""
    sc = doc.get("step_count")
    if not isinstance(sc, Mapping) or sc.get("max") is None:
        return None
    # 老的按 numeric_dist 存，当天累计的代表值就是 max；kit 的 steps 是
    # cumulative，形状正好是 {"total": n}。
    return {"step_count": {"total": sc["max"]}}


def _health_sleep(doc: Mapping[str, Any]) -> dict[str, Any]:
    """四个当天分钟总数 -> 每个阶段的时长。

    老路存 `{asleep_minutes, core_minutes, deep_minutes, rem_minutes}`，
    kit 按阶段存时长。有分期数据时用分期；只有总数时记成一条 `asleep`
    —— manifest 的 stage 枚举里本来就有这个值，正是给「没分期」准备的。
    **两者不叠加**，否则总睡眠会翻倍。
    """
    stages = {"core": doc.get("core_minutes"), "deep": doc.get("deep_minutes"),
              "rem": doc.get("rem_minutes")}
    have = {k: v for k, v in stages.items() if isinstance(v, (int, float))}
    out: dict[str, Any] = {}
    if have:
        # **不转成 float**：老路存的是整数分钟，转回去的时候 110 会变成 110.0。
        # 数值上一样，但「老路给什么、新路就给什么」是这次切换唯一的验收标准，
        # 放过一个类型差异，标准就松一格。
        out = {"minutes": dict(have)}
    else:
        total = doc.get("asleep_minutes")
        if isinstance(total, (int, float)):
            out = {"minutes": {"asleep": total}}
    if not out:
        return {}
    # 聚合自己的记账（这天最后一次写入的时刻）。搬家时丢掉的话，
    # 读回来的文档就和老路的差一个字段 —— 而差一个字段就不叫「逐字段相同」。
    at = doc.get("_at")
    if at is not None:
        out["_at"] = at
    return out


def _music(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """老的存「今天听了多少分钟、每个歌手各多少」，kit 只存「播放/暂停各多久」。

    **有损，而且不亏**：trend 工具认的信号表里根本没有音乐，老路这份日聚合
    算了几个月、没有任何地方读它。搬过去丢掉的是一份没人看的数据。
    """
    total = doc.get("total_minutes")
    if not isinstance(total, (int, float)):
        return None
    return {"minutes": {"playing": float(total)}}


def _mood(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """老的存每条自评的明细列表，kit 按 valence 存分布。"""
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return None
    values = [e.get("valence") for e in entries
              if isinstance(e, Mapping) and isinstance(e.get("valence"), (int, float))]
    if not values:
        return None
    return {"valence": {"min": min(values), "max": max(values),
                        "sum": sum(values), "count": len(values)}}


#: 老信号 -> 转换函数。不在这里的按「改字段名」处理。
_CONVERTERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any] | None]] = {
    "health_vitals": _health_vitals,
    "health_sleep": _health_sleep,
    "playback": _music,
    "health_mood": _mood,
}

#: 只改名（和单位）的那些。和适配层的 FIELD_ALIASES / FIELD_TRANSFORMS 同源。
_FIELD_RENAMES: dict[str, dict[str, str]] = {
    "health_metabolic": {"blood_pressure_systolic": "blood_pressure_systolic_mmhg",
                         "blood_pressure_diastolic": "blood_pressure_diastolic_mmhg"},
    "health_body": {"body_fat_pct": "body_fat_ratio"},
    "health_workout": {"duration_min": "duration_minutes"},
}
_FIELD_SCALES: dict[str, dict[str, float]] = {
    "health_body": {"body_fat_ratio": 0.01},     # 百分比 -> 比率
}

#: 从一条老记录额外拆出来的信号。
_SPLITS: dict[str, tuple[str, Callable[[Mapping[str, Any]], dict[str, Any] | None]]] = {
    "health_vitals": ("steps", _steps_from_vitals),
}


def convert(old_signal: str, doc: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """一条老记录 -> ``[(kit 信号, 聚合文档), ...]``。认不出来返回空。"""
    kit_signal = _key_map().get(old_signal)
    if kit_signal is None:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    converter = _CONVERTERS.get(old_signal)
    if converter is not None:
        converted = converter(doc)
    else:
        converted = _rename_fields(doc, _FIELD_RENAMES.get(old_signal, {}),
                                   scale=_FIELD_SCALES.get(old_signal, {}))
    if converted:
        # 已经拆到别的信号名下的字段，主信号不再带 —— 否则同一个指标
        # 会出现在两个信号的历史里，而趋势读的是哪一个取决于谁先被查到。
        # （adapter 那边踩过同一个坑。）
        from .ios_report import SPLIT_OFF
        moved = {f for _t, f in SPLIT_OFF.get(old_signal, {}).values()}
        kept = {k: v for k, v in converted.items() if k not in moved}
        if kept:
            out.append((kit_signal, kept))
    split = _SPLITS.get(old_signal)
    if split is not None:
        extra_signal, extra_fn = split
        extra = extra_fn(doc)
        if extra:
            out.append((extra_signal, extra))
    # perceptkit 0.4.0 把三个多指标信号拆成了十二个，所以一条老记录现在
    # 要落到好几个 kit 信号上。复用 adapter 那张拆分表，**不另建一份** ——
    # 两份会漂，而漂了之后表现是"某个指标的历史静默丢失"。
    out.extend(_split_by_metric(old_signal, converted or doc))
    return out


def _split_by_metric(old_signal: str,
                     doc: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """按 adapter 的拆分表，把已经归一过的文档分到各自的 kit 信号上。

    ``converted`` 是归一之后的（字段名已经是 manifest 侧的），所以这里
    直接按目标字段名取 —— 和 adapter 里"先归一再拆"是同一个顺序。
    """
    from .ios_report import SPLIT_OFF
    grouped: dict[str, dict[str, Any]] = {}
    for _src, (target, metric_field) in SPLIT_OFF.get(old_signal, {}).items():
        if metric_field in doc:
            grouped.setdefault(target, {})[metric_field] = doc[metric_field]
    return sorted(grouped.items())


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------

_READ = """
SELECT user_id, date, signal, doc
FROM perception_daily
WHERE (%s::text IS NULL OR user_id = %s)
ORDER BY user_id, date, signal
"""

def run(conn: Any, *, subject_id: str | None = None,
        dry_run: bool = True, now: datetime | None = None) -> BackfillPlan:
    """Classify legacy rows, then publish immutable aggregate generations."""
    from perceptkit.contracts.mutation import aggregate_generation_key
    from perceptkit.contracts.records import AggregateGeneration, DailyAggregate
    from .storage import PostgresStorage

    plan = BackfillPlan(applied=not dry_run, subject_scope=subject_id)
    stamp = now or datetime.now(timezone.utc)
    known = _key_map()
    groups: dict[tuple[str, str], dict[date, dict[str, Any]]] = {}
    blocked_groups: set[tuple[str, str]] = set()

    def safe_key(user_id: str, old_signal: str, day: date) -> dict[str, str]:
        subject_hash = hashlib.sha256(user_id.encode()).hexdigest()[:12]
        return {"subject_hash": subject_hash, "signal": old_signal,
                "local_date": day.isoformat()}

    with conn.cursor() as cur:
        cur.execute(_READ, (subject_id, subject_id))
        for user_id, day, signal, doc in cur:
            plan.rows_read += 1
            if signal in UNCONVERTIBLE:
                plan.skipped[signal] = plan.skipped.get(signal, 0) + 1
                plan.reasons.setdefault(signal, UNCONVERTIBLE[signal])
                plan.unconvertible_rows.append({
                    **safe_key(user_id, signal, day), "reason": UNCONVERTIBLE[signal],
                })
                continue
            if signal not in known:
                plan.skipped[signal] = plan.skipped.get(signal, 0) + 1
                plan.reasons.setdefault(
                    signal, "kit 的 manifest 里没有这个信号 —— 不猜，跳过")
                plan.unconvertible_rows.append({
                    **safe_key(user_id, signal, day),
                    "reason": "signal_not_in_manifest",
                })
                continue
            try:
                produced = convert(signal, doc if isinstance(doc, Mapping) else {})
            except Exception as exc:  # one bad legacy row must be reportable/rerunnable
                plan.failed_keys.append({
                    **safe_key(user_id, signal, day),
                    "reason": f"converter_error:{type(exc).__name__}",
                })
                continue
            if not produced:
                plan.skipped[signal] = plan.skipped.get(signal, 0) + 1
                plan.reasons.setdefault(
                    signal, "这一行转换后是空的（老记录里没有可搬的数字）")
                plan.unconvertible_rows.append({
                    **safe_key(user_id, signal, day), "reason": "empty_conversion",
                })
                continue
            for kit_signal, aggregate in produced:
                plan.migrated[kit_signal] = plan.migrated.get(kit_signal, 0) + 1
                key = (user_id, kit_signal)
                old = groups.setdefault(key, {}).get(day)
                if old is not None and old != aggregate:
                    blocked_groups.add(key)
                    plan.failed_keys.append({
                        **safe_key(user_id, signal, day),
                        "reason": "multiple_legacy_rows_map_to_one_aggregate_day",
                    })
                    continue
                groups[key][day] = aggregate

    storage = PostgresStorage(conn)
    all_generation_ids: list[str] = []
    for (user_id, kit_signal), rows in sorted(groups.items()):
        if not rows:
            continue
        days = sorted(rows)
        start, end = days[0], days[-1]
        required = {start + timedelta(days=i) for i in range((end - start).days + 1)}
        missing = sorted(required - set(days))
        material = {
            "converter_revision": CONVERTER_REVISION,
            "subject_id": user_id,
            "signal": kit_signal,
            "rows": [(day.isoformat(), rows[day]) for day in days],
        }
        digest = hashlib.sha256(json.dumps(
            material, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode()).hexdigest()
        generation_id = f"legacybf_{digest[:24]}"
        report_key = f"{hashlib.sha256(user_id.encode()).hexdigest()[:12]}:{kit_signal}"
        plan.generation_ids[report_key] = generation_id
        all_generation_ids.append(generation_id)
        if missing:
            plan.incomplete_dates[report_key] = [day.isoformat() for day in missing]
        conversion_blocked = (user_id, kit_signal) in blocked_groups
        if missing or conversion_blocked:
            plan.incomplete_generations += 1

        active = storage.get_active_aggregate_generation(
            subject_id=user_id, signal=kit_signal, aggregation_kind="daily")
        cutover_blocked = bool(
            active is not None
            and (start > active.requested_start_date or end < active.requested_end_date)
        )
        if cutover_blocked:
            plan.failed_keys.append({
                "subject_hash": report_key.split(":", 1)[0],
                "signal": kit_signal,
                "local_date": f"{start.isoformat()}..{end.isoformat()}",
                "reason": "candidate_does_not_cover_active_generation",
            })
        if dry_run:
            if not missing and not conversion_blocked and not cutover_blocked:
                plan.active_generations += 1
            continue

        existing = storage.get_aggregate_generation(
            subject_id=user_id, signal=kit_signal, aggregation_kind="daily",
            generation_id=generation_id,
        )
        if existing is not None and existing.status in {"active", "complete", "incomplete"}:
            plan.reused_generations += 1
            if existing.status == "active":
                plan.active_generations += 1
            elif (existing.status == "complete" and not missing
                  and not conversion_blocked and not cutover_blocked):
                with storage.mutation_transaction() as owner:
                    owner.acquire((aggregate_generation_key(
                        user_id, kit_signal, "daily"),))
                    if storage.activate_aggregate_generation(
                        subject_id=user_id, signal=kit_signal,
                        aggregation_kind="daily", generation_id=generation_id,
                        expected_active_generation_id=(
                            active.generation_id if active is not None else None),
                        activated_at=stamp,
                    ):
                        plan.active_generations += 1
            continue
        if existing is not None and existing.status == "failed":
            plan.failed_keys.append({
                "subject_hash": report_key.split(":", 1)[0],
                "signal": kit_signal,
                "local_date": f"{start.isoformat()}..{end.isoformat()}",
                "reason": "matching_generation_already_failed",
            })
            continue

        with storage.mutation_transaction() as owner:
            owner.acquire((aggregate_generation_key(user_id, kit_signal, "daily"),))
            building = AggregateGeneration(
                generation_id, user_id, kit_signal, "daily", AGGREGATION_VERSION,
                start, end, status="building", completeness="unknown",
                created_at=stamp, updated_at=stamp,
            )
            storage.put_aggregate_generation(building)
            coverage = {
                "backfilled_from": "perception_daily",
                "converter_revision": CONVERTER_REVISION,
                "rerun_identity": generation_id,
                "accounted_dates": [day.isoformat() for day in days],
            }
            for day in days:
                storage.put_aggregate(DailyAggregate(
                    user_id, kit_signal, day, "daily", AGGREGATION_VERSION,
                    rows[day], generation_id=generation_id,
                    source_coverage=coverage, updated_at=stamp,
                ))
            final = AggregateGeneration(
                generation_id, user_id, kit_signal, "daily", AGGREGATION_VERSION,
                start, end,
                status="incomplete" if missing or conversion_blocked else "complete",
                completeness=("incomplete"
                              if missing or conversion_blocked else "complete"),
                accounted_dates=tuple(days), incomplete_dates=tuple(missing),
                incomplete_reasons=tuple(
                    reason for reason, applies in (
                        ("legacy_sparse_coverage_unverified", bool(missing)),
                        ("legacy_conversion_conflict", conversion_blocked),
                    ) if applies
                ),
                created_at=stamp, updated_at=stamp,
            )
            storage.update_aggregate_generation(final)
            if not missing and not conversion_blocked and not cutover_blocked:
                expected = active.generation_id if active is not None else None
                if storage.activate_aggregate_generation(
                    subject_id=user_id, signal=kit_signal,
                    aggregation_kind="daily", generation_id=generation_id,
                    expected_active_generation_id=expected, activated_at=stamp,
                ):
                    plan.active_generations += 1

    plan.rerun_identity = "legacybf_run_" + hashlib.sha256(json.dumps(
        {"subject_scope": subject_id, "generations": sorted(all_generation_ids),
         "converter_revision": CONVERTER_REVISION},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()[:24]
    plan.legacy_reports_without_v2_digest = conn.execute(
        "SELECT count(*) FROM perceptkit_ingest_receipt "
        "WHERE LEFT(payload_digest, 3) <> 'v2:'"
        + (" AND subject_id=%s" if subject_id else ""),
        ((subject_id,) if subject_id else ()),
    ).fetchone()[0]
    plan.legacy_identities_missing_fact_evidence = conn.execute(
        "SELECT count(*) FROM perceptkit_dedupe_identity "
        "WHERE (fact_key IS NULL OR semantic_digest IS NULL)"
        + (" AND subject_id=%s" if subject_id else ""),
        ((subject_id,) if subject_id else ()),
    ).fetchone()[0]
    plan.legacy_outbox_missing_provenance = conn.execute(
        "SELECT count(*) FROM perceptkit_event_outbox "
        "WHERE (legacy_scope_unknown OR NOT fact_dependencies_complete)"
        + (" AND subject_id=%s" if subject_id else ""),
        ((subject_id,) if subject_id else ()),
    ).fetchone()[0]
    plan.unknown_runtime_attempts = conn.execute(
        "SELECT count(*) FROM perceptkit_event_outbox WHERE delivery_state='unknown'"
        + (" AND subject_id=%s" if subject_id else ""),
        ((subject_id,) if subject_id else ()),
    ).fetchone()[0]
    log.info("perceptkit backfill %s: read=%s migrated=%s skipped=%s",
             "applied" if plan.applied else "dry-run",
             plan.rows_read, plan.total, sum(plan.skipped.values()))
    return plan


def format_plan(plan: BackfillPlan) -> str:
    lines = [f"{'搬了' if plan.applied else '将要搬（试跑）'}："]
    for signal, n in sorted(plan.migrated.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {signal:22} {n:>7} 行")
    lines.append(f"  {'合计':22} {plan.total:>7} 行（读了 {plan.rows_read} 行）")
    if plan.skipped:
        lines.append("跳过：")
        for signal, n in sorted(plan.skipped.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {signal:22} {n:>7} 行 — {plan.reasons.get(signal, '')}")
    lines.extend([
        f"generation：{len(plan.generation_ids)} 个，active={plan.active_generations}，"
        f"reused={plan.reused_generations}",
        f"incomplete generations：{plan.incomplete_generations}",
        f"incomplete dates：{sum(len(v) for v in plan.incomplete_dates.values())}",
        f"legacy reports without v2 digest：{plan.legacy_reports_without_v2_digest}",
        f"legacy identities missing fact evidence："
        f"{plan.legacy_identities_missing_fact_evidence}",
        f"legacy outbox missing provenance：{plan.legacy_outbox_missing_provenance}",
        f"unknown runtime attempts：{plan.unknown_runtime_attempts}",
        f"failed keys：{len(plan.failed_keys)}",
        f"rerun identity：{plan.rerun_identity}",
    ])
    return "\n".join(lines)


__all__ = ["BackfillPlan", "AGGREGATION_VERSION", "CONVERTER_REVISION", "UNCONVERTIBLE",
           "convert", "run", "format_plan"]
