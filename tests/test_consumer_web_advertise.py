"""Batch 5: the consumer advertises the io_cli web verbs to the model only while
the backend's poll says web is effectively on, and corrects a resume-capable
session once when it flips off.

Display only — the real block is the server-side execution gate (web/execution_
core, fail closed). This file pins the pure decision helpers so they hold
regardless of the driver/env the surrounding process was imported under.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Module bootstrap — consumer reads env at import scope (mirrors
# test_consumer_web_capability.py). Must be set before the import.
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "CHECKPOINT_FILE": "/tmp/feedling_test_web_advertise_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


def setup_function(_fn):
    crc._web_policy = {}
    crc._web_advertised_session_id = None
    crc._web_off_notice_session_id = None


_CATALOG = "\n".join([
    "写操作前建议...",              # D8 header
    "修改依据只认用户对话...",       # D3 header
    "IO CLI INVOCATION: ...",
    "memory-index --limit  List memory cards",
    "web-search <query> --limit  Search the web",
    "web-fetch <url>  Fetch a URL",
    "photo-recent --limit  Recent photos",
])


# --- policy plumbing -------------------------------------------------------

def test_update_web_policy_reads_effective():
    crc._update_web_policy({"effective": True, "search": True, "fetch": True})
    assert crc._web_tools_effective() is True


def test_update_web_policy_off_and_garbage_are_not_effective():
    crc._update_web_policy({"effective": False})
    assert crc._web_tools_effective() is False
    crc._update_web_policy(None)          # missing/garbage field on the poll
    assert crc._web_tools_effective() is False
    crc._update_web_policy("nonsense")
    assert crc._web_tools_effective() is False


# --- catalog filtering -----------------------------------------------------

def test_strip_web_verbs_removes_only_the_web_lines():
    stripped = crc._strip_web_verbs_from_catalog(_CATALOG)
    assert "web-search" not in stripped
    assert "web-fetch" not in stripped
    # everything else stays, in order
    assert "memory-index" in stripped
    assert "photo-recent" in stripped
    assert "IO CLI INVOCATION" in stripped
    assert "修改依据只认用户对话..." in stripped


def test_strip_web_verbs_keeps_full_catalog_intact_when_present():
    # sanity: the source catalog really does carry the web lines we strip
    assert "web-search" in _CATALOG and "web-fetch" in _CATALOG


# --- the web-off correction state machine ----------------------------------

def test_no_notice_while_web_is_effective():
    crc._update_web_policy({"effective": True})
    crc._web_advertised_session_id = "s1"
    assert crc._web_off_notice_for_turn("s1") == ""


def test_no_notice_for_a_session_that_never_saw_the_web_verbs():
    crc._update_web_policy({"effective": False})
    crc._web_advertised_session_id = None  # never advertised
    assert crc._web_off_notice_for_turn("s1") == ""


def test_notice_fires_once_when_a_session_that_saw_web_goes_off():
    crc._update_web_policy({"effective": False})
    crc._web_advertised_session_id = "s1"  # this session already saw the verbs
    first = crc._web_off_notice_for_turn("s1")
    assert first == crc._WEB_OFF_NOTICE
    # second turn of the same session: already corrected, stays quiet
    assert crc._web_off_notice_for_turn("s1") == ""


def test_codex_style_none_session_never_gets_a_notice():
    """codex has no resume, so the model never carried the verbs forward — the
    freshly filtered catalog is the whole truth, no correction needed."""
    crc._update_web_policy({"effective": False})
    crc._web_advertised_session_id = None
    assert crc._web_off_notice_for_turn(None) == ""


def test_a_different_session_off_does_not_borrow_another_sessions_advertise():
    crc._update_web_policy({"effective": False})
    crc._web_advertised_session_id = "s1"
    # s2 never advertised web → no notice even though s1 did
    assert crc._web_off_notice_for_turn("s2") == ""
