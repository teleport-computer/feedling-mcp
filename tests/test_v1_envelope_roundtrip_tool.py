from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from content_encryption import box_seal as current_box_seal  # noqa: E402
from enclave.envelope import box_seal_open_hkdf  # noqa: E402


TOOL = REPO / "tools" / "v1_envelope_roundtrip_test.py"


def _load_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("v1_roundtrip_tool", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_import_has_no_file_or_network_side_effects() -> None:
    code = (
        "import importlib.util, json, urllib.request, websockets; "
        "forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('import side effect')); "
        "json.load = forbidden; urllib.request.urlopen = forbidden; websockets.connect = forbidden; "
        f"spec = importlib.util.spec_from_file_location('v1_roundtrip_tool', {str(TOOL)!r}); "
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


def test_main_does_not_require_a_preexisting_user_file(monkeypatch) -> None:
    tool = _load_tool()

    class FirstNetworkCall(RuntimeError):
        pass

    def reject_file_read(*args, **kwargs):
        raise AssertionError(f"unexpected local user-file read: {args!r}")

    def stop_at_network(*args, **kwargs):
        raise FirstNetworkCall

    monkeypatch.setattr("builtins.open", reject_file_read)
    monkeypatch.setattr(tool.urllib.request, "urlopen", stop_at_network)

    with pytest.raises(FirstNetworkCall):
        tool.main()


def test_tool_seal_is_opened_by_current_enclave_codec() -> None:
    tool = _load_tool()
    recipient_sk = X25519PrivateKey.generate()
    recipient_pk = recipient_sk.public_key()
    recipient_sk_bytes = recipient_sk.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )

    sealed = tool.box_seal(b"current-box-seal-contract", recipient_pk)

    assert box_seal_open_hkdf(sealed, recipient_sk_bytes) == b"current-box-seal-contract"


def test_tool_open_accepts_current_backend_seal() -> None:
    tool = _load_tool()
    recipient_sk = X25519PrivateKey.generate()
    recipient_pk = recipient_sk.public_key()
    recipient_pk_bytes = recipient_pk.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    sealed = current_box_seal(b"current-box-seal-contract", recipient_pk_bytes)

    assert tool.box_open(sealed, recipient_sk, recipient_pk) == b"current-box-seal-contract"


def test_required_message_rejects_a_missing_roundtrip_item() -> None:
    tool = _load_tool()

    with pytest.raises(AssertionError, match="missing from chat history"):
        tool.require_message([], "item-1", "chat history")


def test_required_message_returns_the_matching_item() -> None:
    tool = _load_tool()
    expected = {"id": "item-1", "content": "hello"}

    assert tool.require_message([{"id": "other"}, expected], "item-1", "history") is expected
