"""Dream account-failure cooldown: preserve cards and the consolidation ledger.

Capture may skip a chat window after seven days; Dream has no such cursor to
advance. It instead stops repeated nightly attempts and leaves one recovery
probe per normal Dream interval. An explicit forced run can probe sooner.
"""
from memory import capture_failure


PAUSED_REASON = "dream_account_paused"
SUCCESS_RESET = {
    "dream_account_error_code": "",
    "dream_account_fail_since": 0.0,
    "dream_account_paused": False,
}


def failure_patch(state, *, reason: str, now: float):
    code = capture_failure.account_error_code(reason)
    since, expired = capture_failure.account_failure_clock(
        since=float(state.get("dream_account_fail_since") or 0),
        last_failed=float(state.get("last_dream_failed_at") or 0),
        now_ts=now, is_account=bool(code))
    return {
        "dream_account_error_code": code,
        "dream_account_fail_since": since,
        # Once in recovery mode, a gap before its scheduled probe must not
        # rearm four attempts each night. Success or a non-account failure
        # exits that mode; the ordinary account clock still obeys the gap rule.
        "dream_account_paused": bool(code and (expired or state.get("dream_account_paused"))),
    }


def probe_not_due(state, *, now: float, interval: float) -> bool:
    return bool(state.get("dream_account_paused")
                and state.get("dream_account_error_code")
                and now - float(state.get("last_dream_failed_at") or 0) < interval)
