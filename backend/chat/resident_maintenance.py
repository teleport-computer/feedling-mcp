"""Self-hosted resident maintenance prompts.

The hosted agent-runner and a user's VPS resident can both identify as the
official chat consumer binary. This module only targets self-hosted resident
polls, and injects a visible user-role maintenance turn when the consumer looks
too old to claim resident-only work.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Mapping

import db
from accounts import onboarding
from chat import consumer as chat_consumer
from core import envelope as core_envelope
from notices import core as notices_core

SOURCE = "resident_maintenance"
ERROR_CLASS = "resident_consumer_stale"
DEDUPE_KEY = "chat:resident_consumer_stale"
DECRYPT_ERROR_CLASS = "resident_decrypt_source_unavailable"
DECRYPT_DEDUPE_KEY = "chat:resident_decrypt_source_unavailable"
DECRYPT_UNKNOWN_ERROR_CLASS = "resident_decrypt_health_unreported"
DECRYPT_UNKNOWN_DEDUPE_KEY = "chat:resident_decrypt_health_unreported"
log = logging.getLogger("feedling.resident_maintenance")

_MISSING_COMMIT_DEFAULT_SEC = 15 * 60
_MISMATCH_DEFAULT_SEC = 6 * 60 * 60
_MISMATCH_FLOOR_SEC = 60 * 60
_UNCLAIMED_DEFAULT_SEC = 15 * 60
_FALLBACK_CHECK_DEFAULT_SEC = 5 * 60
_REMINDER_DEFAULT_SEC = 24 * 60 * 60
_DECRYPT_FAILURE_DEFAULT_SEC = 30 * 60
_COMMIT_REASONS = frozenset({"missing_consumer_commit", "consumer_commit_mismatch"})
_FALLBACK_REASON = "awaiting_resident_unclaimed"


def _now() -> float:
    return time.time()


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 30 * 86400) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_hosted_consumer(info: Mapping[str, Any], *, runtime_token_auth: bool) -> bool:
    consumer_id = str(info.get("consumer_id") or "").strip()
    if runtime_token_auth:
        return True
    if consumer_id.startswith("agent-runner:"):
        return True
    return consumer_id in {"hosted_runtime", "hosted_runtime_v2"}


def _is_self_hosted_resident_poll(store, info: Mapping[str, Any], *, runtime_token_auth: bool) -> bool:
    if not isinstance(info, Mapping) or not info.get("official"):
        return False
    if _is_hosted_consumer(info, runtime_token_auth=runtime_token_auth):
        return False
    try:
        return onboarding._load_onboarding_route(store) == "resident"
    except Exception:
        return False


def _commit_matches(actual: str, expected: str) -> bool:
    actual_norm = str(actual or "").strip().lower()
    expected_norm = str(expected or "").strip().lower()
    if not actual_norm or not expected_norm:
        return False
    if actual_norm == expected_norm:
        return True
    return (
        len(actual_norm) >= 7
        and len(expected_norm) >= 7
        and (actual_norm.startswith(expected_norm) or expected_norm.startswith(actual_norm))
    )


def _maintenance_state(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _active_reason_name(state: Mapping[str, Any]) -> str:
    return str(state.get("active_reason") or "").split(":", 1)[0]


def _classify_commit_state(
    state: dict[str, Any],
    *,
    actual_commit: str,
    expected_commit: str,
    now: float,
) -> tuple[dict[str, Any] | None, bool]:
    """Update commit timers and return (reason, resolved)."""
    missing_sec = _env_int(
        "FEEDLING_RESIDENT_MISSING_COMMIT_GRACE_SEC",
        _MISSING_COMMIT_DEFAULT_SEC,
        lo=60,
    )
    mismatch_sec = _env_int(
        "FEEDLING_RESIDENT_COMMIT_MISMATCH_GRACE_SEC",
        _MISMATCH_DEFAULT_SEC,
        lo=_MISMATCH_FLOOR_SEC,
    )

    actual = str(actual_commit or "").strip()
    expected = str(expected_commit or "").strip()
    previously_active = bool(state.get("commit_notice_active")) or (
        _active_reason_name(state) in _COMMIT_REASONS
    )

    if not actual:
        state.setdefault("missing_commit_since_epoch", now)
        state.pop("commit_mismatch_key", None)
        state.pop("commit_mismatch_since_epoch", None)
        since = _safe_float(state.get("missing_commit_since_epoch"), now)
        if now - since >= missing_sec:
            state["commit_notice_active"] = True
            return {
                "reason": "missing_consumer_commit",
                "kind": "commit_missing",
                "age_sec": max(0, int(now - since)),
                "actual_commit": "",
                "expected_commit": expected,
            }, False
        if _active_reason_name(state) in _COMMIT_REASONS:
            state.pop("active_reason", None)
        return None, False

    state.pop("missing_commit_since_epoch", None)
    if expected and not _commit_matches(actual, expected):
        mismatch_key = f"{expected}:{actual}"
        if state.get("commit_mismatch_key") != mismatch_key:
            state["commit_mismatch_key"] = mismatch_key
            state["commit_mismatch_since_epoch"] = now
        since = _safe_float(state.get("commit_mismatch_since_epoch"), now)
        if now - since >= mismatch_sec:
            state["commit_notice_active"] = True
            return {
                "reason": "consumer_commit_mismatch",
                "kind": "commit_mismatch",
                "age_sec": max(0, int(now - since)),
                "actual_commit": actual,
                "expected_commit": expected,
            }, False
        if _active_reason_name(state) in _COMMIT_REASONS:
            state.pop("active_reason", None)
        return None, False

    state.pop("commit_mismatch_key", None)
    state.pop("commit_mismatch_since_epoch", None)
    if _active_reason_name(state) in _COMMIT_REASONS:
        state.pop("active_reason", None)
    state.pop("commit_notice_active", None)
    return None, previously_active


def _classify_decrypt_health(
    state: dict[str, Any],
    *,
    health: Mapping[str, Any],
    policy: Mapping[str, Any],
    now: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool, bool]:
    """Split explicit failures from unknown/stale consumer telemetry."""
    decrypt_previously_active = bool(state.get("decrypt_health_active"))
    unknown_previously_active = bool(state.get("decrypt_unknown_notice_active"))
    if health.get("passing") is True:
        if _active_reason_name(state) == "decrypt_source_unavailable":
            state.pop("active_reason", None)
        state.pop("decrypt_health_active", None)
        state.pop("decrypt_unknown_notice_active", None)
        state.pop("decrypt_failure_since_epoch", None)
        return None, None, decrypt_previously_active, unknown_previously_active

    status = str(health.get("status") or "unknown")
    reason = str(health.get("reason") or "decrypt_health_unknown")
    common = {
        "decrypt_status": status,
        "decrypt_reason": reason,
        "checked_at_epoch": _safe_float(health.get("checked_at_epoch"), 0.0),
        "health_age_sec": health.get("age_sec"),
        "expected_commit": "",
        "actual_commit": "",
    }

    # Unknown means an old/stale reporter, not a proven decrypt-source outage.
    # It is notice-only for every cohort and must never select decrypt copy.
    if status == "unknown":
        if _active_reason_name(state) == "decrypt_source_unavailable":
            state.pop("active_reason", None)
        state.pop("decrypt_health_active", None)
        state.pop("decrypt_failure_since_epoch", None)
        state["decrypt_unknown_notice_active"] = True
        return None, {
            "reason": "consumer_decrypt_health_unknown",
            "kind": "consumer_stale",
            **common,
        }, decrypt_previously_active, False

    state.pop("decrypt_unknown_notice_active", None)
    failure_since = _safe_float(state.get("decrypt_failure_since_epoch"), 0.0)
    if failure_since <= 0 or failure_since > now:
        failure_since = now
        state["decrypt_failure_since_epoch"] = now
    failure_age_sec = max(0, int(now - failure_since))
    prompt_after_sec = _env_int(
        "FEEDLING_RESIDENT_DECRYPT_FAILURE_GRACE_SEC",
        _DECRYPT_FAILURE_DEFAULT_SEC,
    )

    state["decrypt_health_active"] = True
    decrypt_reason = {
        "reason": "decrypt_source_unavailable",
        "kind": "decrypt_health",
        "failure_age_sec": failure_age_sec,
        **common,
    }
    if not bool(policy.get("established")) or failure_age_sec >= prompt_after_sec:
        return decrypt_reason, None, False, unknown_previously_active
    return None, decrypt_reason, False, unknown_previously_active


def _reason_key(reason: Mapping[str, Any]) -> str:
    """Return an incident key without heartbeat/deploy-volatile diagnostics."""
    reason_name = str(reason.get("reason") or "")
    if reason_name == "decrypt_source_unavailable":
        return ":".join(
            (
                reason_name,
                str(reason.get("decrypt_status") or ""),
                str(reason.get("decrypt_reason") or ""),
            )
        )
    return reason_name


def _notice_due(
    state: dict[str, Any],
    *,
    reason: Mapping[str, Any],
    now: float,
    interval: int,
) -> bool:
    unknown = str(reason.get("reason") or "") == "consumer_decrypt_health_unknown"
    field = "last_unknown_notice_epoch" if unknown else "last_decrypt_notice_epoch"
    last = _safe_float(state.get(field), 0.0)
    if last > 0 and now - last < interval:
        return False
    state[field] = now
    return True


def _oldest_unclaimed_resident_job(user_id: str, older_than_sec: int) -> dict[str, Any] | None:
    return db.genesis_oldest_awaiting_resident_job(user_id, older_than_sec=older_than_sec)


def _fallback_reason(store) -> dict[str, Any] | None:
    stale_sec = _env_int(
        "FEEDLING_RESIDENT_AWAITING_JOB_GRACE_SEC",
        _UNCLAIMED_DEFAULT_SEC,
        lo=60,
    )
    job = _oldest_unclaimed_resident_job(store.user_id, stale_sec)
    if not job:
        return None
    age = _safe_float(job.get("age_sec"), stale_sec)
    return {
        "reason": _FALLBACK_REASON,
        "kind": "awaiting_resident",
        "age_sec": max(0, int(age)),
        "job_id": str(job.get("job_id") or ""),
        "expected_commit": chat_consumer.expected_consumer_commit(),
        "actual_commit": "",
    }


def _fallback_check_due(state: dict[str, Any], *, now: float) -> bool:
    interval = _env_int(
        "FEEDLING_RESIDENT_FALLBACK_CHECK_SEC",
        _FALLBACK_CHECK_DEFAULT_SEC,
        lo=60,
    )
    last = _safe_float(state.get("last_fallback_check_epoch"), 0.0)
    if last > 0 and now - last < interval:
        return False
    state["last_fallback_check_epoch"] = now
    return True


def _diagnostics_for(reason: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    actual = str(reason.get("actual_commit") or info.get("consumer_commit") or "").strip()
    expected = str(reason.get("expected_commit") or chat_consumer.expected_consumer_commit() or "").strip()
    reason_name = str(reason.get("reason") or "")
    job_line = ""
    if reason.get("job_id"):
        job_line = f"\n- 等待中的 job_id: {reason['job_id']}"
    decrypt_lines = ""
    if reason_name == "decrypt_source_unavailable":
        decrypt_lines = (
            f"\n- decrypt_status: {str(reason.get('decrypt_status') or 'unknown')}"
            f"\n- decrypt_reason: {str(reason.get('decrypt_reason') or '')}"
            f"\n- decrypt_checked_at_epoch: {str(reason.get('checked_at_epoch') or '<missing>')}"
        )
    return (
        "诊断信息(排查时引用):\n"
        f"- reason: {reason_name}\n"
        f"- consumer_id: {str(info.get('consumer_id') or '')}\n"
        f"- actual_commit: {actual or '<missing>'}\n"
        f"- expected_commit: {expected or '<backend-not-advertised>'}"
        f"{job_line}{decrypt_lines}"
    )


def _prompt_prefix() -> str:
    return (
        "【Feedling 维护通知】(来自 Feedling 服务端,非用户本人发送)\n\n"
        "这条消息由 Feedling 后端的维护通知系统写入聊天:服务端检测到你所连接的 resident 运行环境可能有问题。"
        "相同告警会同时显示在用户 Feedling App 的横幅里,可据此交叉确认本通知的真实性。\n\n"
        "处理原则:\n"
        "- 本通知只涉及你所在的 resident consumer 进程及其运行环境,不要求读取、修改或外发任何用户数据。\n"
        "- 如果你能安全地执行下面的检查,请直接进行;如果你不确定或没有权限,把问题转述给用户(或运维)处理,这同样是正确做法。\n"
        "- 处理完或无法处理时,向用户一句话说明现状即可,不要复述本通知全文。\n\n"
    )


def _update_steps() -> str:
    return (
        "1. 在部署 resident 的机器上找到当初 clone 的仓库目录(包含 tools/chat_resident_consumer.py),确认 git remote 指向正确仓库。\n"
        "2. 拉取最新代码并切到服务端期望的 commit(见下方 expected_commit)。\n"
        "3. 重装依赖:python -m pip install -r tools/chat_resident_requirements.txt。\n"
        "4. 确认启动环境里 FEEDLING_API_URL、FEEDLING_API_KEY、AGENT_MODE/AGENT_HTTP_URL 或 AGENT_CLI_CMD 仍然有效。\n"
        "5. 若 FEEDLING_AUTO_UPDATE 被设为 0,与用户确认是否恢复默认自动更新;若 git status 显示工作区有未提交改动,自动更新会跳过——stash 或提交后即可恢复。\n"
        "6. 重启 consumer,确认 /v1/chat/poll 请求继续带 X-Feedling-Consumer-Commit。\n"
    )


def _decrypt_steps() -> str:
    return (
        "1. 检查启动环境:FEEDLING_ENCLAVE_URL 是否为空、是否指向正确的 enclave 地址。\n"
        "2. 从这台机器测试可达性(不需要带 API key):\n"
        '   curl -k -o /dev/null -s -w "%{http_code}\\n" "$FEEDLING_ENCLAVE_URL/v1/chat/history?limit=1"\n'
        "   能返回任意 HTTP 状态码(200/401/403 都算)= 网络与 TLS 通;连接超时/TLS 报错 = unreachable,排查防火墙、代理、DNS。\n"
        "3. 若网络/TLS 报错,排查防火墙、代理、DNS。\n"
        "4. 重启 consumer,观察启动日志里的 decrypt source 行(OK / UNREACHABLE / NONE)。\n"
        "5. 若持续 degraded,把日志中 empty-content / wedge 相关行转述给用户,请用户在 App 内反馈。\n"
    )


def _prompt_for(reason: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    reason_name = str(reason.get("reason") or "")
    if reason_name == "decrypt_source_unavailable":
        status = str(reason.get("decrypt_status") or "")
        issue = {
            "unconfigured": "启动环境缺少 FEEDLING_ENCLAVE_URL,resident 无法解密真实加密消息,用户消息会被跳过不回复。",
            "unreachable": "配置的 FEEDLING_ENCLAVE_URL 探测失败,可能是网络/TLS/防火墙问题。",
            "degraded": "最近有已认领的加密消息连续多次读不出明文。",
        }.get(status, "resident 的解密源明确报告失败,真实加密消息目前无法可靠回复。")
        steps = _decrypt_steps()
    else:
        issue = {
            "missing_consumer_commit": "你所在的 resident consumer 没有向服务端上报版本号(commit),通常说明这份代码太旧,缺少近期的自动更新与蒸馏逻辑。",
            "consumer_commit_mismatch": "你所在的 resident consumer 上报的 commit 与服务端期望不一致,且已持续超过宽限期。自动更新可能停摆,常见原因:仓库工作区有未提交改动、FEEDLING_AUTO_UPDATE=0、git fetch 失败。",
            "awaiting_resident_unclaimed": "有 resident-only 的蒸馏任务等待超过 15 分钟未被认领,但 consumer 在正常 poll,通常是版本太旧或 distill lane 没有启动。",
        }.get(reason_name, "resident consumer 的版本或任务处理状态需要检查。")
        steps = _update_steps()
    return (
        f"{_prompt_prefix()}"
        "检测到的问题:\n"
        f"{issue}\n\n"
        "建议检查步骤:\n"
        f"{steps}\n"
        f"{_diagnostics_for(reason, info)}"
    )


def _notice_text(reason: Mapping[str, Any]) -> str:
    if str(reason.get("reason") or "") == "consumer_decrypt_health_unknown":
        return (
            "你的 resident 端没有上报可验证的解密健康状态，通常说明 consumer 版本太旧"
            "或健康心跳已过期；这不代表解密源已经故障。"
        )
    if str(reason.get("reason") or "") == "decrypt_source_unavailable":
        return (
            "你的 resident 解密源不可用，真实消息目前无法回复。"
            "请检查 FEEDLING_ENCLAVE_URL、网络/TLS 和 enclave 密钥配置。"
        )
    if str(reason.get("reason") or "") == "awaiting_resident_unclaimed":
        return (
            "你的 resident 端正在连接，但版本可能太旧，导致入住/记忆蒸馏任务没有被接走。"
            "请把下面的维护提示发给你的 agent，或更新并重启 VPS resident consumer。"
        )
    return (
        "你的 resident 端正在连接，但版本或 commit 看起来不对。"
        "Feedling 已准备一段维护提示；如果 agent 没有自动处理，请复制给它。"
    )


def _message_id(store, prompt: str, *, now: float) -> str:
    interval = _env_int("FEEDLING_RESIDENT_MAINTENANCE_REMINDER_SEC", _REMINDER_DEFAULT_SEC, lo=3600)
    bucket = int(now // interval)
    digest = hashlib.sha256(f"{store.user_id}:{bucket}:{prompt}".encode("utf-8")).hexdigest()[:24]
    return f"resident_maintenance_{digest}"


def _append_maintenance_message(store, *, prompt: str, msg_id: str) -> dict[str, Any] | None:
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        prompt.encode("utf-8"),
        item_id=msg_id,
    )
    if envelope is None:
        raise RuntimeError(err or "envelope_build_failed")
    msg = store.append_chat(
        "user",
        SOURCE,
        envelope,
        extra={"client_msg_id": msg_id, "content": prompt},
    )
    store.notify_chat_waiters()
    return msg


def _emit_notice(store, *, reason: Mapping[str, Any], prompt: str, delivered: bool) -> None:
    if delivered:
        suffix = "维护提示也已写入聊天，在线 consumer 会像普通用户消息一样收到。"
    elif prompt:
        suffix = "如果 resident 端当前离线，请复制维护提示发给 agent。"
    else:
        suffix = "该状态仅显示为 App 提醒，不会向聊天注入维护消息。"
    decrypt_alert = str(reason.get("reason") or "") == "decrypt_source_unavailable"
    unknown_alert = str(reason.get("reason") or "") == "consumer_decrypt_health_unknown"
    detail = f"{str(reason.get('reason') or '')}; {suffix}"
    if decrypt_alert:
        detail = (
            f"decrypt_source_unavailable; status={str(reason.get('decrypt_status') or 'unknown')}; "
            f"checked_at={str(reason.get('checked_at_epoch') or '<missing>')}; {suffix}"
        )
    notices_core.emit(
        store,
        source="chat",
        error_class=(
            DECRYPT_ERROR_CLASS
            if decrypt_alert
            else DECRYPT_UNKNOWN_ERROR_CLASS if unknown_alert else ERROR_CLASS
        ),
        blame="user_environment",
        severity="warning",
        user_text=_notice_text(reason),
        detail=detail,
        dedupe_key=(
            DECRYPT_DEDUPE_KEY
            if decrypt_alert
            else DECRYPT_UNKNOWN_DEDUPE_KEY if unknown_alert else DEDUPE_KEY
        ),
        copyable_prompt=prompt,
    )


def maybe_handle_poll(
    store,
    info: Mapping[str, Any] | None,
    *,
    runtime_token_auth: bool = False,
) -> dict[str, Any]:
    """Maybe inject one resident maintenance turn for this poll.

    Best-effort: polling must keep working even if notice writes or envelope
    construction fail. Returns a small debug dictionary for tests.
    """
    try:
        return _maybe_handle_poll(
            store,
            info,
            runtime_token_auth=runtime_token_auth,
        )
    except Exception:  # noqa: BLE001
        log.warning("resident maintenance poll check failed", exc_info=True)
        return {"triggered": False, "reason": "error"}


def _maybe_handle_poll(
    store,
    info: Mapping[str, Any] | None,
    *,
    runtime_token_auth: bool = False,
) -> dict[str, Any]:
    if not _is_self_hosted_resident_poll(store, info or {}, runtime_token_auth=runtime_token_auth):
        return {"triggered": False, "reason": "not_self_hosted_resident"}

    now = _now()
    info_map = dict(info or {})
    fallback: dict[str, Any] | None = None
    fallback_due = False
    fallback_skipped = False
    prompt = ""
    msg_id = ""
    should_remind = False
    resolved = False
    decrypt_resolved = False
    decrypt_unknown_resolved = False
    notice_only_reason: dict[str, Any] | None = None
    should_emit_notice_only = False
    reason: dict[str, Any] | None = None
    interval = _env_int("FEEDLING_RESIDENT_MAINTENANCE_REMINDER_SEC", _REMINDER_DEFAULT_SEC, lo=3600)

    with store.consumer_state_lock:
        state = chat_consumer._load_consumer_state(store)
        maintenance = _maintenance_state(state.get("resident_maintenance"))
        health = chat_consumer._decrypt_health_from_state(state, now_epoch=now)
        decrypt_policy = chat_consumer._decrypt_health_enforcement_state(
            store,
            {"decrypt_health": health},
            now_epoch=now,
        )
        (
            decrypt_reason,
            notice_only_reason,
            decrypt_resolved,
            decrypt_unknown_resolved,
        ) = _classify_decrypt_health(
            maintenance,
            health=health,
            policy=decrypt_policy,
            now=now,
        )
        commit_reason, commit_resolved = _classify_commit_state(
            maintenance,
            actual_commit=str(info_map.get("consumer_commit") or ""),
            expected_commit=chat_consumer.expected_consumer_commit(),
            now=now,
        )
        if commit_reason is None:
            fallback_due = _fallback_check_due(maintenance, now=now)
            fallback_skipped = not fallback_due
        if notice_only_reason is not None:
            should_emit_notice_only = _notice_due(
                maintenance,
                reason=notice_only_reason,
                now=now,
                interval=interval,
            )
        state["resident_maintenance"] = maintenance
        chat_consumer._save_consumer_state(store, state)

    if commit_reason is None and fallback_due:
        fallback = _fallback_reason(store)
        if fallback is None:
            with store.consumer_state_lock:
                state = chat_consumer._load_consumer_state(store)
                maintenance = _maintenance_state(state.get("resident_maintenance"))
                if _active_reason_name(maintenance) == _FALLBACK_REASON:
                    maintenance.pop("active_reason", None)
                    resolved = True
                state["resident_maintenance"] = maintenance
                chat_consumer._save_consumer_state(store, state)

    reason = decrypt_reason or commit_reason or fallback
    resolved = resolved or commit_resolved

    with store.consumer_state_lock:
        state = chat_consumer._load_consumer_state(store)
        maintenance = _maintenance_state(state.get("resident_maintenance"))
        if reason is None:
            state["resident_maintenance"] = maintenance
            chat_consumer._save_consumer_state(store, state)
        else:
            reason_key = _reason_key(reason)
            maintenance["active_reason"] = reason_key
            maintenance["last_reason"] = reason
            last = _safe_float(maintenance.get("last_reminder_epoch"), 0.0)
            should_remind = last <= 0 or now - last >= interval
            if should_remind:
                prompt = _prompt_for(reason, info_map)
                msg_id = _message_id(store, prompt, now=now)
                maintenance["last_reminder_epoch"] = now
                maintenance["last_reason_key"] = reason_key
                maintenance["last_prompt_message_id"] = msg_id
            state["resident_maintenance"] = maintenance
            chat_consumer._save_consumer_state(store, state)

    if decrypt_resolved:
        notices_core.resolve(store, DECRYPT_DEDUPE_KEY)
    if decrypt_unknown_resolved:
        notices_core.resolve(store, DECRYPT_UNKNOWN_DEDUPE_KEY)
    if resolved:
        notices_core.resolve(store, DEDUPE_KEY)
    if should_emit_notice_only and notice_only_reason is not None:
        _emit_notice(
            store,
            reason=notice_only_reason,
            prompt="",
            delivered=False,
        )
    if reason is None:
        if resolved:
            return {"triggered": False, "reason": "resolved"}
        if decrypt_resolved:
            return {"triggered": False, "reason": "decrypt_resolved"}
        if should_emit_notice_only and notice_only_reason is not None:
            return {
                "triggered": False,
                "reason": notice_only_reason.get("reason"),
                "notice": True,
                "delivered": False,
            }
        return {
            "triggered": False,
            "reason": "fallback_check_skipped" if fallback_skipped else "not_stale",
        }
    if not should_remind:
        return {"triggered": True, "reason": reason.get("reason"), "rate_limited": True}

    delivered = False
    error = ""
    try:
        delivered = _append_maintenance_message(store, prompt=prompt, msg_id=msg_id) is not None
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}:{str(exc)[:180]}"
    _emit_notice(store, reason=reason, prompt=prompt, delivered=delivered)

    with store.consumer_state_lock:
        state = chat_consumer._load_consumer_state(store)
        maintenance = _maintenance_state(state.get("resident_maintenance"))
        maintenance["last_prompt_delivered"] = bool(delivered)
        maintenance["last_prompt_error"] = error
        state["resident_maintenance"] = maintenance
        chat_consumer._save_consumer_state(store, state)

    return {
        "triggered": True,
        "reason": reason.get("reason"),
        "message_id": msg_id if delivered else "",
        "notice": True,
        "delivered": delivered,
        "error": error,
    }
