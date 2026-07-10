import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def test_caption_envelope_rebuilds_from_prefixed_keys():
    m = {
        "id": "msg1", "owner_user_id": "u1", "v": "1",
        "caption_id": "cap1", "caption_v": "1", "caption_body_ct": "CT",
        "caption_nonce": "N", "caption_K_enclave": "KE",
        "caption_owner_user_id": "u1",
    }
    env = serve_worker._caption_envelope(m)
    # AEAD AAD is owner_user_id||v||id -> MUST use the caption's own id, not the message's.
    assert env["id"] == "cap1"
    assert env["body_ct"] == "CT"
    assert env["K_enclave"] == "KE"
    assert env["owner_user_id"] == "u1"


def test_caption_envelope_none_without_ciphertext():
    assert serve_worker._caption_envelope({"id": "m1"}) is None
    assert serve_worker._caption_envelope({"id": "m1", "caption_body_ct": ""}) is None


def test_caption_envelope_falls_back_to_message_owner_and_id():
    env = serve_worker._caption_envelope(
        {"id": "m1", "owner_user_id": "u9", "v": "2", "caption_body_ct": "CT"})
    assert env["id"] == "m1"        # no caption_id -> message id
    assert env["owner_user_id"] == "u9"
    assert env["v"] == 2


# ---------------------------------------------------------------------------
# `content_type == "file"` (upstream file-upload x V2 read path)
# ---------------------------------------------------------------------------

def test_file_row_renders_a_marker_and_never_decrypts_the_body():
    """A file message's plaintext is RAW FILE BYTES. Decoding it as utf-8 raises and takes
    the whole _read_tail down with it (chat + wake + extraction + compaction). The file row
    must be rendered from plaintext `file_name` alone — no enclave round-trip."""
    from core import enclave as core_enclave

    called = []
    orig = core_enclave._decrypt_envelope_via_enclave
    core_enclave._decrypt_envelope_via_enclave = lambda *a, **k: called.append(a) or b""
    try:
        row = serve_worker._file_row(
            {"id": "m1", "file_name": "report.pdf", "file_mime": "application/pdf"},
            mid="m1", ts=1.0, role="user", token="t", caption_budget=[8])
    finally:
        core_enclave._decrypt_envelope_via_enclave = orig

    assert called == []                       # zero enclave calls: no caption envelope present
    assert row["content"] == "[file: report.pdf]"
    assert row["has_file"] is True
    assert row["file_mime"] == "application/pdf"
    assert "has_image" not in row             # must not be injected as an image


def test_file_row_prefers_the_user_caption_when_present(monkeypatch):
    """The text a user sends WITH a file lives in the same caption_* envelope as an image's."""
    from core import enclave as core_enclave
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **k: "这个报告哪里有问题".encode())
    row = serve_worker._file_row(
        {"id": "m1", "file_name": "report.pdf", "caption_body_ct": "CT",
         "caption_id": "cap1", "owner_user_id": "u1"},
        mid="m1", ts=1.0, role="user", token="t", caption_budget=[8])
    assert row["content"] == "这个报告哪里有问题"


def test_file_row_degrades_to_the_marker_when_caption_decrypt_fails(monkeypatch):
    from core import enclave as core_enclave

    def _boom(*a, **k):
        raise RuntimeError("enclave down")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _boom)
    row = serve_worker._file_row(
        {"id": "m1", "file_name": "a.bin", "caption_body_ct": "CT", "caption_id": "c"},
        mid="m1", ts=1.0, role="user", token="t", caption_budget=[8])
    assert row["content"] == "[file: a.bin]"   # no-filler: never fail the turn


def test_a_pdf_body_would_have_crashed_the_old_generic_branch():
    """Documents the bug this branch exists to prevent. If someone deletes the `file`
    branch, `_read_tail`'s generic path does exactly this on real file bytes."""
    import pytest
    pdf = b"%PDF-1.4\n1 0 obj\n<</Type/Catalog>>\xff\xfe\x00"
    with pytest.raises(UnicodeDecodeError):
        pdf.decode("utf-8")
