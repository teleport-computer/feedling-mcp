"""Received AI messages deliver via the system alert only — the Dynamic Island
expansion (Live Activity push) is gated off by default. The Live Activity module
and its explicit /v1/push/* routes are untouched; only this AI-message trigger is
gated (FEEDLING_AI_MSG_LIVE_ACTIVITY restores the old dual behavior)."""
from unittest.mock import patch, MagicMock

from push import service as push_service
from proactive import dashboard


def _store(uid="usr_la_gate"):
    s = MagicMock()
    s.user_id = uid
    return s


_BACKGROUND = {"should_push": True, "reason": "app_background",
               "phase": "background", "age_sec": "5"}


def test_ai_message_live_activity_gated_off_by_default_alert_still_fires():
    store = _store("usr_la_off")
    with patch("push.service.AI_MSG_LIVE_ACTIVITY", False), \
         patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=True), \
         patch("push.service._ai_push_decision", return_value=dict(_BACKGROUND)), \
         patch("push.service.live_activity.push_live_activity_hybrid_dict") as la, \
         patch("push.service._send_chat_alert", return_value={"status": "ok"}) as alert:
        fields = push_service._deliver_ai_message_push_if_background(store, body="hello", title="IO")

    la.assert_not_called()                               # no Dynamic Island expansion
    alert.assert_called_once()                            # system alert still delivered
    assert fields["live_activity_status"] == "disabled"
    assert fields["live_activity_reason"] == "ai_msg_live_activity_off"
    assert fields.get("alert_status") == "ok"


def test_ai_message_live_activity_restored_when_flag_on():
    store = _store("usr_la_on")
    with patch("push.service.AI_MSG_LIVE_ACTIVITY", True), \
         patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=True), \
         patch("push.service._ai_push_decision", return_value=dict(_BACKGROUND)), \
         patch("push.service.live_activity.push_live_activity_hybrid_dict",
               return_value={"status": "started", "reason": "", "activity_id": "a1", "mode": "start"}) as la, \
         patch("push.service._send_chat_alert", return_value={"status": "ok"}) as alert:
        fields = push_service._deliver_ai_message_push_if_background(store, body="hello", title="IO")

    la.assert_called_once()                               # old dual behavior restored
    alert.assert_called_once()
    assert fields["live_activity_status"] == "started"


def test_foreground_still_suppresses_both_regardless_of_flag():
    # The gate only touches the send path; foreground suppression (both channels)
    # is unchanged.
    store = _store("usr_la_fg")
    suppress = {"should_push": False, "reason": "app_foreground_chat_visible",
                "phase": "active", "age_sec": "1"}
    with patch("push.service.AI_MSG_LIVE_ACTIVITY", False), \
         patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=True), \
         patch("push.service._ai_push_decision", return_value=suppress), \
         patch("push.service.live_activity.push_live_activity_hybrid_dict") as la, \
         patch("push.service._send_chat_alert") as alert:
        fields = push_service._deliver_ai_message_push_if_background(store, body="hello", title="IO")

    la.assert_not_called()
    alert.assert_not_called()
    assert fields["live_activity_status"] == "suppressed"
    assert fields["alert_status"] == "suppressed"


def test_env_flag_parsing():
    with patch.dict("os.environ", {"X_LA": "1"}):
        assert push_service._env_flag("X_LA", False) is True
    with patch.dict("os.environ", {"X_LA": "off"}):
        assert push_service._env_flag("X_LA", True) is False
    import os
    os.environ.pop("X_LA", None)
    assert push_service._env_flag("X_LA", False) is False   # unset → default
    assert push_service._env_flag("X_LA", True) is True


# --- proactive dashboard delivery derivation must stay alert-aware once the
#     Dynamic Island (Live Activity) is gated off, so a failed sole channel does
#     not read as a green "chat_written*" headline. ---

def test_derive_status_disabled_alert_delivered_is_delivered():
    # New normal success: Live Activity disabled, system alert landed.
    assert dashboard._derive_message_delivery_status("disabled", "delivered") == "delivered"


def test_derive_status_disabled_alert_error_is_not_green():
    # New sole-channel failure must NOT be a green chat_written* headline.
    s = dashboard._derive_message_delivery_status("disabled", "error")
    assert s == "alert_failed"
    assert not s.startswith("chat_written")     # status_class greens chat_written*
    assert "failed" in s                        # status_class reds *failed*


def test_derive_status_live_delivered_stays_delivered_regardless_of_alert():
    # Gate-on path unchanged: a delivered Live Activity is delivered even if the
    # alert push errored (the user saw the Island).
    assert dashboard._derive_message_delivery_status("delivered", "") == "delivered"
    assert dashboard._derive_message_delivery_status("delivered", "error") == "delivered"


def test_derive_status_disabled_alert_skipped_is_chat_written():
    # No device token etc. → message written, no push, but not an error.
    assert dashboard._derive_message_delivery_status("disabled", "skipped") == "chat_written"


def test_derive_status_both_channels_error_is_not_green():
    assert dashboard._derive_message_delivery_status("error", "error") == "alert_failed"
