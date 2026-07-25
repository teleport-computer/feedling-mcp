"""
File extraction helper tests for tools/chat_resident_consumer.py
==================================================================

Covers:
  - docx text extraction (word/document.xml paragraph parsing)
  - xlsx tsv extraction (shared strings + inline strings + truncation)
  - friendly file type labels

Run with:
    cd backend && PYTHONPATH=. /path/to/venv/python -m pytest \
        ../tests/test_chat_resident_consumer_file.py -v
"""

import io
import os
import sys
import types
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars BEFORE importing consumer.
# consumer reads env at module scope; these must exist first.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_image_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Ensure repo root + backend on path (mirrors existing test suite).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Stub content_encryption when backend tree is absent.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------


def _make_docx(paragraphs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _make_xlsx(rows):
    # minimal: inline string cells, one sheet
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="x"></sst>')
        sheet_rows = ""
        for r in rows:
            cells = "".join(f'<c t="inlineStr"><is><t>{v}</t></is></c>' for v in r)
            sheet_rows += f"<row>{cells}</row>"
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="x"><sheetData>{sheet_rows}</sheetData></worksheet>')
    return buf.getvalue()


def test_extract_docx_text():
    from tools import chat_resident_consumer as c
    data = _make_docx(["Hello", "第二段"])
    text = c._extract_docx_text(data)
    assert "Hello" in text and "第二段" in text


def test_extract_docx_bad_zip_returns_none():
    from tools import chat_resident_consumer as c
    assert c._extract_docx_text(b"not-a-zip") is None


def test_extract_xlsx_tsv_and_truncation():
    from tools import chat_resident_consumer as c
    rows = [["a", "b"], ["c", "d"]]
    text, truncated = c._extract_xlsx_text(_make_xlsx(rows))
    assert "a\tb" in text and truncated is False

    big = [["x", str(i)] for i in range(c._XLSX_MAX_ROWS + 50)]
    text2, truncated2 = c._extract_xlsx_text(_make_xlsx(big))
    assert truncated2 is True


def test_friendly_file_type():
    from tools import chat_resident_consumer as c
    assert "Word" in c._friendly_file_type("a.docx", "")
    assert "PDF" in c._friendly_file_type("a.pdf", "application/pdf")


def test_prepare_text_file_lands_and_names_original(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    msg = {"id": "m1", "content_type": "file", "file_name": "笔记.md",
           "file_mime": "text/markdown",
           "file_b64": base64.b64encode(b"# hi\n").decode()}
    prep = c._prepare_file_for_agent(msg)
    assert prep.original_name == "笔记.md"
    assert prep.local_path is not None and prep.local_path.endswith(".md")
    # instruction names the original file
    assert "笔记.md" in prep.cli_instruction


def test_prepare_docx_declares_extraction(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    docx = _make_docx(["Body para"])
    msg = {"id": "m2", "content_type": "file", "file_name": "报告.docx",
           "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "file_b64": base64.b64encode(docx).decode()}
    prep = c._prepare_file_for_agent(msg)
    assert prep.extracted is True
    assert prep.inline_text and "Body para" in prep.inline_text
    assert "抽取" in prep.cli_instruction  # declares system extracted to text
    assert prep.local_path is not None and prep.local_path.endswith(".txt")


def test_prepare_pdf_http_inline_declines(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    msg = {"id": "m3", "content_type": "file", "file_name": "a.pdf",
           "file_mime": "application/pdf",
           "file_b64": base64.b64encode(b"%PDF-1.4 ...").decode()}
    prep = c._prepare_file_for_agent(msg)
    # PDF cannot be inlined for a tool-less HTTP agent
    assert prep.inline_text is None
    assert prep.http_fallback_note and "PDF" in prep.http_fallback_note


def test_prepare_docx_empty_extraction_no_false_claim(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    # valid docx zip whose document.xml has no <w:t> text → extracts to ""
    empty_docx = _make_docx([])
    msg = {"id": "de", "content_type": "file", "file_name": "empty.docx",
           "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "file_b64": base64.b64encode(empty_docx).decode()}
    prep = c._prepare_file_for_agent(msg)
    assert prep.extracted is False
    assert "抽取" not in prep.cli_instruction
    assert prep.inline_text is None


def test_prepare_xlsx_failure_no_false_extraction_claim(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    msg = {"id": "mx", "content_type": "file", "file_name": "broken.xlsx",
           "file_mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           "file_b64": base64.b64encode(b"not-a-zip-at-all").decode()}
    prep = c._prepare_file_for_agent(msg)
    assert prep.extracted is False
    assert "抽取" not in prep.cli_instruction        # no false extraction claim
    assert prep.inline_text is None
