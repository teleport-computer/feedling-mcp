"""The consumer must advertise the V1 web capability (batch 4).

A build whose io_cli carries the web verbs (tools/io_cli.py cmd_web_search /
cmd_web_fetch) lists ``web_search_v1,web_fetch_v1`` in its
X-Feedling-Consumer-Capabilities header. That claim is what the settings page's
``_runtime_supported`` reads to decide the switch is really in effect for a V1
account. An older build simply omits it, so those accounts fail closed.

Pure unit: imports the consumer module (no DB, no network) and reads the static
header dict built at import time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Module bootstrap — consumer reads env at import scope (mirrors
# test_chat_resident_consumer_file.py). Must be set before the import.
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_web_cap_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import consumer as backend_consumer  # noqa: E402
import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


def _advertised() -> set[str]:
    raw = crc._HEADERS["X-Feedling-Consumer-Capabilities"]
    return {item.strip() for item in raw.split(",") if item.strip()}


def test_consumer_advertises_both_web_capabilities():
    advertised = _advertised()
    assert backend_consumer.WEB_SEARCH_CAPABILITY in advertised
    assert backend_consumer.WEB_FETCH_CAPABILITY in advertised


def test_web_capabilities_use_the_backend_constant_strings():
    """The advertised strings must be exactly what the backend gate matches on;
    a typo on either side would silently make the switch look inert."""
    assert backend_consumer.WEB_SEARCH_CAPABILITY == "web_search_v1"
    assert backend_consumer.WEB_FETCH_CAPABILITY == "web_fetch_v1"


def test_existing_vision_capabilities_are_untouched():
    """Adding web must not drop the capabilities the consumer already shipped."""
    advertised = _advertised()
    assert "vision_observer_v1" in advertised
    assert "vision_probe_v2" in advertised
