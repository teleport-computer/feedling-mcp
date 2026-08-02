"""The consumer advertises the V1 web capability ONLY when hosted (batch 4 +
cloud-only correction).

Our web-search / web-fetch is a CLOUD-ONLY product. The HOSTED consumer
(per-user runtime token) advertises ``web_search_v1,web_fetch_v1`` in its
X-Feedling-Consumer-Capabilities header; a VPS / self-hosted resident must NOT —
it uses its own model provider's built-in web capability. That header is what
the settings page's ``_runtime_supported`` reads, so omitting the web caps makes
web read ``effective = false`` for self-hosted accounts, which is the intended
boundary.

Pure unit: imports the consumer module (no DB, no network). The header is built
at import time from the process env; the module is imported here WITHOUT
``FEEDLING_RUNTIME_TOKEN_FILE`` set, i.e. in the VPS (non-hosted) shape, so the
static header must omit the web caps. The hosted vs VPS split itself is pinned
against the pure ``_consumer_capabilities`` helper, which needs no re-import.
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


def _caps(hosted: bool) -> set[str]:
    raw = crc._consumer_capabilities(hosted)
    return {item.strip() for item in raw.split(",") if item.strip()}


def test_hosted_consumer_advertises_both_web_capabilities():
    advertised = _caps(hosted=True)
    assert backend_consumer.WEB_SEARCH_CAPABILITY in advertised
    assert backend_consumer.WEB_FETCH_CAPABILITY in advertised


def test_vps_consumer_never_advertises_web_capabilities():
    """The cloud-only boundary: a self-hosted resident (no runtime-token file)
    must NOT advertise web caps, so _runtime_supported reads false for it."""
    advertised = _caps(hosted=False)
    assert backend_consumer.WEB_SEARCH_CAPABILITY not in advertised
    assert backend_consumer.WEB_FETCH_CAPABILITY not in advertised


def test_import_time_header_is_vps_shape_without_web():
    """This module is imported without FEEDLING_RUNTIME_TOKEN_FILE, i.e. VPS —
    the static header baked at import time must carry no web caps."""
    advertised = _advertised()
    assert backend_consumer.WEB_SEARCH_CAPABILITY not in advertised
    assert backend_consumer.WEB_FETCH_CAPABILITY not in advertised


def test_web_capabilities_use_the_backend_constant_strings():
    """The advertised strings must be exactly what the backend gate matches on;
    a typo on either side would silently make the switch look inert."""
    assert backend_consumer.WEB_SEARCH_CAPABILITY == "web_search_v1"
    assert backend_consumer.WEB_FETCH_CAPABILITY == "web_fetch_v1"


def test_existing_vision_capabilities_are_untouched():
    """The cloud-only change must not drop the caps the consumer already shipped —
    vision is advertised on BOTH lines."""
    for hosted in (True, False):
        advertised = _caps(hosted=hosted)
        assert "vision_observer_v1" in advertised
        assert "vision_probe_v2" in advertised
