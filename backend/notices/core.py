"""User notices stream：系统错误的可回溯通知面（spec 2026-07-07 Phase B / B1）。

明文存储：内容是系统错误信息非用户内容，与 gate_decisions 同级，不走加密信封。
emit/resolve 绝不抛出——观测性设施不得拖垮主流程。存于既有 user_logs 表的
`user_notices` stream（不建新表），item_key=dedupe_key 做 upsert 去重。

db log 原语（backend/db.py）：
- log_append(user_id, stream, doc, ts=None, item_key=None)  → INSERT 一行
- log_read_all(user_id, stream) -> list[dict]               → 按 seq 升序全量
- log_patch_item(user_id, stream, item_key, patch) -> dict|None  → patch 最新命中行
- log_trim(user_id, stream, max_rows)                       → 只留最新 max_rows 行
"""
from __future__ import annotations

import logging
import time
import uuid

import db

log = logging.getLogger("feedling.notices")

NOTICES_STREAM = "user_notices"
NOTICES_MAX = 200
RESOLVED_WINDOW_SEC = 7 * 86400

VALID_SOURCES = ("genesis", "history_import", "memory", "runner", "chat", "model_api")
VALID_BLAME = ("user_provider", "provider_transient", "user_environment", "system")
VALID_SEVERITY = ("error", "warning")


def _now() -> float:
    return time.time()


def emit(store, *, source, error_class, blame, severity, user_text,
         detail="", dedupe_key, copyable_prompt="") -> None:
    """Upsert 一条通知。dedupe_key 命中一条**未 resolved** 的现存通知 →
    occurrences+1、刷新 last_ts/detail/user_text/blame/severity/error_class；
    否则（不存在，或最新一条已 resolved）→ 新建（occurrences=1，新 notice_id）。
    绝不抛出。"""
    try:
        if (source not in VALID_SOURCES or blame not in VALID_BLAME
                or severity not in VALID_SEVERITY):
            log.warning("notices.emit dropped: bad enum source=%r blame=%r severity=%r",
                        source, blame, severity)
            return
        uid = store.user_id
        rows = db.log_read_all(uid, NOTICES_STREAM)
        existing = None
        for r in rows:
            if r.get("dedupe_key") == dedupe_key:
                existing = r          # rows 按 seq 升序，保留最新一条（= log_patch_item 会命中的那条）
        now = _now()
        clipped = str(detail or "")[:300]
        prompt = str(copyable_prompt or "")[:4000]
        if existing is not None and not existing.get("resolved"):
            patch = {
                "occurrences": int(existing.get("occurrences", 1)) + 1,
                "last_ts": now,
                "detail": clipped,
                "user_text": user_text,
                "blame": blame,
                "severity": severity,
                "error_class": error_class,
            }
            if prompt:
                patch["copyable_prompt"] = prompt
            db.log_patch_item(uid, NOTICES_STREAM, dedupe_key, patch)
            return
        doc = {
            "notice_id": "ntc_" + uuid.uuid4().hex[:12],
            "source": source,
            "error_class": error_class,
            "blame": blame,
            "severity": severity,
            "user_text": user_text,
            "detail": clipped,
            "dedupe_key": dedupe_key,
            "occurrences": 1,
            "first_ts": now,
            "last_ts": now,
            "resolved": False,
            "resolved_ts": None,
        }
        if prompt:
            doc["copyable_prompt"] = prompt
        db.log_append(uid, NOTICES_STREAM, doc, ts=now, item_key=dedupe_key)
        db.log_trim(uid, NOTICES_STREAM, NOTICES_MAX)
    except Exception:
        log.warning("notices.emit failed (swallowed)", exc_info=True)


def resolve(store, dedupe_key_prefix: str) -> None:
    """把 dedupe_key 以 prefix 开头的所有**未 resolved** 通知标记 resolved +
    resolved_ts。前缀匹配支持 'chat:'、'runner:' 这类按域清空。绝不抛出。"""
    try:
        uid = store.user_id
        rows = db.log_read_all(uid, NOTICES_STREAM)
        now = _now()
        seen = set()
        for r in rows:
            key = r.get("dedupe_key", "")
            if (key and key.startswith(dedupe_key_prefix)
                    and not r.get("resolved") and key not in seen):
                seen.add(key)
                db.log_patch_item(uid, NOTICES_STREAM, key,
                                  {"resolved": True, "resolved_ts": now})
    except Exception:
        log.warning("notices.resolve failed (swallowed)", exc_info=True)


def list_notices(store, *, include_resolved: bool = True) -> tuple[dict, int]:
    """快照式读取：按 dedupe_key 去重保留最新一条（兑现契约「同 key 始终只有一条」，
    并自愈 emit 读-改非原子 / DB 瞬时读失败可能留下的重复行）；活跃通知全给；
    resolved 仅给近 7 天且 include_resolved 时。按 last_ts 倒序。

    注意：底层 db.log_read 对 DB 故障是吞异常返回 []（见 backend/db.py），所以 DB
    宕机时本端点返回 200 空快照而非 500——静默空是实际语义，不是本函数不吞异常
    就能改变的。"""
    uid = store.user_id
    rows = db.log_read_all(uid, NOTICES_STREAM)
    latest_by_key = {}
    for r in rows:
        key = r.get("dedupe_key")
        if key is not None:
            latest_by_key[key] = r      # rows 按 seq 升序，后写覆盖 → 保留最新一条
    cutoff = _now() - RESOLVED_WINDOW_SEC
    out = []
    for r in latest_by_key.values():
        if not r.get("resolved"):
            out.append(r)
        elif include_resolved and float(r.get("resolved_ts") or 0) >= cutoff:
            out.append(r)
    out.sort(key=lambda r: float(r.get("last_ts") or 0), reverse=True)
    return {"notices": out}, 200
