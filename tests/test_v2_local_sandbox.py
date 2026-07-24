"""Unit tests for the in-process ``local`` sandbox provider.

Selected via ``FEEDLING_V2_SANDBOX_PROVIDER=local``, it parses untrusted
document bytes *in the worker process* (no external sandbox, no egress) using
the pure-Python extractors in ``hosted.file_text``, guarded by size and
zip-expansion limits. This deliberately trades the 6ddc9676 isolation gate for
zero external dependency; the guards below bound the resulting zip-bomb /
oversize DoS surface. Extraction never raises (file_text is best-effort), so one
bad file cannot kill a turn.

Covers: provider identity, docx/xlsx/text routing, name-based dispatch when the
mime is octet-stream (extract_text only gets path+mime, so the session must
remember the name from materialize), size guard, zip-bomb guard, shell/code
refusal, closed-session and unknown-path fail-closed, and the
``configured_sandbox_provider`` wiring that makes the env switch selectable.
"""
import io
import zipfile

import pytest

from workspace import sandbox as workspace_sandbox
from workspace.local_sandbox import LocalSandboxProvider
from workspace.sandbox import SandboxUnavailable


def _docx_bytes(text: str) -> bytes:
    doc = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def _xlsx_bytes(value: str) -> bytes:
    shared = (
        '<?xml version="1.0"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">'
        f"<si><t>{value}</t></si></sst>"
    )
    sheet = (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def _session():
    return LocalSandboxProvider().acquire(user_id="u_local", purpose="materialize_artifact")


def _roundtrip(session, *, name, data, mime="application/octet-stream"):
    path = session.materialize(name=name, data=data, mime_type=mime)
    return session.extract_text(path=path, mime_type=mime)


def test_provider_identity_is_local():
    assert LocalSandboxProvider().name == "local"


def test_docx_roundtrip_returns_text():
    assert "hello ζ world" in _roundtrip(
        _session(), name="report.docx", data=_docx_bytes("hello ζ world")
    )


def test_xlsx_roundtrip_returns_text():
    assert "CELL42" in _roundtrip(_session(), name="sheet.xlsx", data=_xlsx_bytes("CELL42"))


def test_plain_text_roundtrip():
    assert "just text ✓" in _roundtrip(
        _session(), name="notes.txt", data="just text ✓".encode(), mime="text/plain"
    )


def test_name_dispatch_when_mime_is_octet_stream():
    # extract_text only receives path+mime; the provider must remember the name
    # from materialize so file_text can dispatch on the .docx extension.
    assert "named dispatch" in _roundtrip(
        _session(), name="q.docx", data=_docx_bytes("named dispatch"),
        mime="application/octet-stream",
    )


def test_oversize_materialize_fails_closed(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_LOCAL_ARTIFACT_MAX_BYTES", "1024")
    with pytest.raises(SandboxUnavailable):
        _session().materialize(name="big.txt", data=b"x" * 2048, mime_type="text/plain")


def test_zip_bomb_guard_returns_empty(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_LOCAL_MAX_UNCOMPRESSED_BYTES", "4096")
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"a" * 1_000_000)  # 1 MB uncompressed
    session = _session()
    path = session.materialize(
        name="bomb.docx", data=bomb.getvalue(), mime_type="application/octet-stream"
    )
    assert session.extract_text(path=path, mime_type="application/octet-stream") == ""


def test_shell_and_code_are_refused():
    session = _session()
    with pytest.raises(SandboxUnavailable):
        session.run_shell("ls")
    with pytest.raises(SandboxUnavailable):
        session.run_code(language="python", source="print(1)")


def test_unknown_path_fails_closed():
    with pytest.raises(SandboxUnavailable):
        _session().extract_text(path="/local/artifacts/9999", mime_type="text/plain")


def test_closed_session_refuses():
    session = _session()
    session.close()
    with pytest.raises(SandboxUnavailable):
        session.materialize(name="x.txt", data=b"y", mime_type="text/plain")


def test_configured_sandbox_provider_selects_local(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_SANDBOX_PROVIDER", "local")
    provider = workspace_sandbox.configured_sandbox_provider()
    assert provider.name == "local"
    assert isinstance(provider, LocalSandboxProvider)
