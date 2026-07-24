"""Server-side document text extraction (backend/hosted/file_text.py).

These are the V2 file-reading extractors that let a tool-less HTTP model read an
uploaded docx/xlsx/pdf/txt by inlining its text into the prompt.
"""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import file_text


# ---- fixture builders (real minimal documents, no external tooling) ---------

def _docx(paragraphs):
    doc = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>'
        + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        + "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def _xlsx(rows):
    # shared strings for every cell value, referenced by index from the sheet
    flat = [c for row in rows for c in row]
    shared = ('<?xml version="1.0"?>'
              '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              + "".join(f"<si><t>{v}</t></si>" for v in flat) + "</sst>")
    idx = 0
    sheet_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c in row:
            cells.append(f'<c t="s"><v>{idx}</v></c>')
            idx += 1
        sheet_rows.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    sheet = ('<?xml version="1.0"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>' + "".join(sheet_rows) + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def _pdf(text):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
              % (len(objs) + 1, xref))
    return out.getvalue()


# ---- tests ------------------------------------------------------------------

def test_docx_extracts_paragraph_text():
    r = file_text.extract_file_text(_docx(["第一行", "second line"]), name="报告.docx")
    assert r.method == "docx"
    assert r.text == "第一行\nsecond line"
    assert not r.truncated


def test_xlsx_extracts_cells_tab_joined():
    r = file_text.extract_file_text(_xlsx([["日期", "金额"], ["07-01", "12.50"]]),
                                    name="账单.xlsx")
    assert r.method == "xlsx"
    assert r.text == "日期\t金额\n07-01\t12.50"


def test_pdf_extracts_text_via_pypdf():
    r = file_text.extract_file_text(_pdf("Alipay 12345 CNY"), name="明细.pdf",
                                    mime="application/pdf")
    assert r.method == "pdf"
    assert "Alipay 12345 CNY" in (r.text or "")


def test_plain_text_is_decoded():
    r = file_text.extract_file_text("hello 世界\nline2".encode("utf-8"), name="notes.txt")
    assert r.method == "text"
    assert r.text == "hello 世界\nline2"


def test_dispatch_falls_back_to_mime_when_ext_missing():
    r = file_text.extract_file_text(_docx(["ok"]), name="noext",
                                    mime=file_text._DOCX_MIME)
    assert r.method == "docx" and r.text == "ok"


def test_unsupported_binary_returns_none():
    r = file_text.extract_file_text(b"\x00\x01\x02\xff\xfe garbage", name="blob.bin")
    assert r.text is None and r.method == "none"


def test_empty_input_returns_none():
    assert file_text.extract_file_text(b"", name="x.pdf").text is None


def test_scanned_pdf_with_no_text_returns_none_not_crash():
    # a valid PDF whose page has no text operators → nothing extractable
    r = file_text.extract_file_text(_pdf(""), name="scan.pdf", mime="application/pdf")
    assert r.text is None and r.method == "pdf"


def test_corrupt_document_never_raises_and_returns_none():
    # bytes that claim to be docx/pdf but aren't → best-effort None, no exception
    assert file_text.extract_file_text(b"not a real zip", name="x.docx").text is None
    assert file_text.extract_file_text(b"%PDF-1.4 broken", name="x.pdf").text is None


def test_oversized_text_is_truncated_to_budget():
    big = ("a" * (file_text.FILE_INLINE_MAX_CHARS + 5000)).encode("utf-8")
    r = file_text.extract_file_text(big, name="big.txt")
    assert r.truncated
    assert len(r.text) == file_text.FILE_INLINE_MAX_CHARS
