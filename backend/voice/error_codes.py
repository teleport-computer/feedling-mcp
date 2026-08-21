"""Closed public failure vocabulary for the cross-runtime voice gateway."""

from __future__ import annotations

from notices import catalog as notices_catalog


# The gateway straddles both hosted runtimes: resident voice admission produces
# ``user_message_envelope_failed`` while the V2 send path can return the runtime
# control/admission codes below. Terminal reply failures come from the shared
# notices catalog.
VOICE_GATEWAY_ERROR_CODES = frozenset(
    set(notices_catalog.ERROR_CLASSES)
    | {
        "hosting_runtime_unavailable",
        "provider_not_configured",
        "runtime_control_changed",
        "runtime_control_invalid",
        "runtime_control_unavailable",
        "runtime_policy_not_ready",
        "runtime_switching",
        "turn_timeout",
        "turns_halted",
        "unknown",
        "user_message_envelope_failed",
        "voice_turn_not_accepted",
        "workers_unavailable",
    }
)


def public_voice_error_code(value: object) -> str:
    """Normalize an error body before it is written to a public debug trace."""
    candidate = str(value or "").strip()
    return candidate if candidate in VOICE_GATEWAY_ERROR_CODES else "unknown"
