import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import screen_watch as sw

_NOW = 1_000_000.0


def _call(**over):
    kw = dict(latest_frame_id="f2", latest_ts=_NOW - 10, last_frame_id="f1",
              last_user_msg_ts=None, now=_NOW)
    kw.update(over)
    return sw.should_watch(**kw)


def test_fresh_changed_and_quiet_watches():
    should, reason = _call()
    assert should is True and reason == "ok"


def test_no_frame_at_all_does_not_watch():
    should, reason = _call(latest_frame_id="")
    assert should is False and reason == "no_frames"


def test_stale_frame_does_not_watch():
    """A frame older than FRESH_SEC means sharing is not live right now."""
    should, reason = _call(latest_ts=_NOW - (sw.FRESH_SEC + 1))
    assert should is False and reason == "not_fresh"


def test_frame_exactly_at_the_freshness_boundary_is_fresh():
    should, _ = _call(latest_ts=_NOW - sw.FRESH_SEC)
    assert should is True


def test_unchanged_frame_does_not_watch():
    """Only act on genuinely new content — otherwise every tick re-wakes on the same frame."""
    should, reason = _call(latest_frame_id="f1", last_frame_id="f1")
    assert should is False and reason == "unchanged"


def test_first_ever_tick_treats_any_frame_as_changed():
    """Persisted frame id is NULL on the first tick; matches the resident's empty-string start."""
    should, _ = _call(last_frame_id="")
    assert should is True


def test_active_chat_suppresses_the_watch():
    should, reason = _call(last_user_msg_ts=_NOW - (sw.CHAT_SUPPRESS_SEC - 1))
    assert should is False and reason == "chatting"


def test_chat_exactly_at_the_suppress_boundary_does_not_suppress():
    should, _ = _call(last_user_msg_ts=_NOW - sw.CHAT_SUPPRESS_SEC)
    assert should is True


def test_old_chat_does_not_suppress():
    should, _ = _call(last_user_msg_ts=_NOW - (sw.CHAT_SUPPRESS_SEC + 1))
    assert should is True


def test_never_chatted_does_not_suppress():
    should, _ = _call(last_user_msg_ts=None)
    assert should is True


def test_freshness_is_checked_before_chatting():
    """A stale frame reports `not_fresh`, not `chatting`, even while the user is typing.
    The reason string is an observability contract — keep the resident's precedence."""
    should, reason = _call(latest_ts=_NOW - 10_000, last_user_msg_ts=_NOW - 1)
    assert should is False and reason == "not_fresh"


def test_screen_watch_gate_is_pure():
    import pathlib
    src = pathlib.Path(sw.__file__).read_text()
    for forbidden in ("provider_client", "jobs_store", "import hosted", "from hosted",
                      "agent_runtime", "core.store", "psycopg", "import db"):
        assert forbidden not in src, f"screen_watch.py must not reference {forbidden}"
