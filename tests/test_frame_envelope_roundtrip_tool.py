from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from enclave.envelope import box_seal_open_hkdf  # noqa: E402


TOOL = REPO / "tools" / "frame_envelope_roundtrip_test.py"


def _load_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("frame_roundtrip_tool", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_import_has_no_file_or_network_side_effects() -> None:
    code = (
        "import importlib.util, json, urllib.request, websockets; "
        "forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('import side effect')); "
        "json.load = forbidden; urllib.request.urlopen = forbidden; websockets.connect = forbidden; "
        f"spec = importlib.util.spec_from_file_location('frame_roundtrip_tool', {str(TOOL)!r}); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "backend")

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_tool_seal_is_opened_by_current_enclave_codec() -> None:
    tool = _load_tool()
    recipient_sk = X25519PrivateKey.generate()
    recipient_sk_bytes = recipient_sk.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )

    sealed = tool.box_seal(
        b"current-frame-box-seal-contract",
        recipient_sk.public_key(),
    )

    assert box_seal_open_hkdf(sealed, recipient_sk_bytes) == b"current-frame-box-seal-contract"


def test_required_frame_rejects_a_missing_item() -> None:
    tool = _load_tool()

    with pytest.raises(AssertionError, match="missing from frame list"):
        tool.require_stored_frame([], "frame-1", "usr-1")


@pytest.mark.parametrize(
    "frame, error",
    [
        ({"id": "frame-1", "encrypted": False, "owner_user_id": "usr-1"}, "encrypted"),
        ({"id": "frame-1", "encrypted": True, "owner_user_id": "usr-2"}, "owner"),
    ],
)
def test_required_frame_rejects_invalid_persistence(frame: dict, error: str) -> None:
    tool = _load_tool()

    with pytest.raises(AssertionError, match=error):
        tool.require_stored_frame([frame], "frame-1", "usr-1")


def test_required_frame_returns_a_valid_match() -> None:
    tool = _load_tool()
    expected = {"id": "frame-1", "encrypted": True, "owner_user_id": "usr-1"}

    assert tool.require_stored_frame([expected], "frame-1", "usr-1") is expected
