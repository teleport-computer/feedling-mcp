from unittest.mock import MagicMock, patch

from notify_relay.relay_core import _build_alert
from push import push_core
from push import service as push_service
from push.sounds import NOTIFICATION_SOUND_NAME


def _store(uid: str = "usr_notification_sound"):
    store = MagicMock()
    store.user_id = uid
    return store


def _sent_payload(send_mock):
    return send_mock.call_args.args[2]


def test_notification_endpoint_uses_device_selected_sound_name():
    store = _store()
    with patch("push.push_core.push_tokens._select_token", return_value={"token": "abc"}), \
         patch(
             "push.push_core.apns._send_apns_to_active_tokens",
             return_value={"status": "delivered"},
         ) as send:
        push_core.notification(store, payload={"title": "IO", "body": "hello"})

    assert _sent_payload(send)["aps"]["sound"] == NOTIFICATION_SOUND_NAME


def test_chat_alert_uses_same_device_selected_sound_name():
    store = _store()
    with patch("push.service.registry._user_entry_snapshot", return_value={"user_id": store.user_id}), \
         patch("push.service.db.user_exists", return_value=True), \
         patch("push.service.push_tokens._select_token", return_value={"token": "abc"}), \
         patch(
             "push.service.apns._send_apns_to_active_tokens",
             return_value={"status": "delivered"},
         ) as send:
        push_service._send_chat_alert(store, "hello", alert_title="IO")

    assert _sent_payload(send)["aps"]["sound"] == NOTIFICATION_SOUND_NAME


def test_notify_relay_defaults_to_device_selected_sound_name():
    payload, push_type, _ = _build_alert({"title": "IO", "body": "hello"})

    assert push_type == "alert"
    assert payload["aps"]["sound"] == NOTIFICATION_SOUND_NAME


def test_notify_relay_preserves_an_explicit_sound_name():
    payload, _, _ = _build_alert(
        {"title": "IO", "body": "hello", "sound": "sender-selected.wav"}
    )

    assert payload["aps"]["sound"] == "sender-selected.wav"
