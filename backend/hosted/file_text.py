"""Legacy in-process document text extraction helpers.

This module is retained for compatibility tests only. Runtime V2 deliberately
does not import it: an attachment cache miss must materialize and parse through
an audited sandbox provider, or fail closed before decrypting physical bytes.

- docx / xlsx are zip+XML → pure stdlib (no dependency), mirroring the resident
  extractors so the two paths agree on what text a document yields.
- pdf → ``pypdf`` (pure-Python, no system deps). Scanned/image-only PDFs yield no
  text; that returns ``text=None`` so the caller keeps the ``[file: name]`` marker.
- anything that sniffs as UTF-8 text is decoded verbatim.

Every extractor is best-effort and NEVER raises: a failure or empty result returns
``FileText(text=None, ...)`` so ``_read_tail`` cannot be killed by one bad file and
the turn still answers.
"""
from __future__ import annotations

import io
import logging
import os
import xml.etree.ElementTree as _ET
import zipfile
from typing import NamedTuple

log = logging.getLogger("feedling.hosted.file_text")

# Inline budget for one document's extracted text. Matches the resident
# consumer's FILE_INLINE_MAX_CHARS default so both runtimes truncate alike.
FILE_INLINE_MAX_CHARS = int(os.environ.get("FEEDLING_V2_FILE_INLINE_MAX_CHARS", "30000"))
_XLSX_MAX_SHEETS = int(os.environ.get("FEEDLING_V2_XLSX_MAX_SHEETS", "8"))
_XLSX_MAX_ROWS = int(os.environ.get("FEEDLING_V2_XLSX_MAX_ROWS", "500"))
_PDF_MAX_PAGES = int(os.environ.get("FEEDLING_V2_PDF_MAX_PAGES", "50"))

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FileText(NamedTuple):
    """Result of a document extraction.

    ``text`` is None when nothing was extractable (unsupported binary, empty, or a
    scanned/image-only PDF). ``truncated`` marks that content was cut to a budget.
    ``method`` names the branch taken: docx | xlsx | pdf | text | none.
    """
    text: str | None
    truncated: bool
    method: str


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _file_ext(name: str) -> str:
    name = str(name or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _looks_like_text(data: bytes) -> bool:
    """UTF-8 decodable and free of NUL/binary control noise → treat as text."""
    if not data:
        return False
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _extract_docx_text(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
        root = _ET.fromstring(xml)
    except Exception as e:  # noqa: BLE001 — best-effort, never raise
        log.warning("docx extract failed: %s", e)
        return None
    paras = []
    for p in root.iter():
        if _strip_ns(p.tag) != "p":
            continue
        line = "".join(t.text or "" for t in p.iter() if _strip_ns(t.tag) == "t").strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def _extract_xlsx_text(data: bytes) -> tuple[str, bool]:
    truncated = False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in z.namelist():
                sroot = _ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sroot:
                    if _strip_ns(si.tag) != "si":
                        continue
                    shared.append("".join(t.text or "" for t in si.iter()
                                          if _strip_ns(t.tag) == "t"))
            sheets = sorted(n for n in z.namelist()
                            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
            if len(sheets) > _XLSX_MAX_SHEETS:
                sheets, truncated = sheets[:_XLSX_MAX_SHEETS], True
            out_lines: list[str] = []
            for sn in sheets:
                root = _ET.fromstring(z.read(sn))
                rows = [r for r in root.iter() if _strip_ns(r.tag) == "row"]
                if len(rows) > _XLSX_MAX_ROWS:
                    rows, truncated = rows[:_XLSX_MAX_ROWS], True
                for r in rows:
                    cells = []
                    for c in r:
                        if _strip_ns(c.tag) != "c":
                            continue
                        t = c.get("t")
                        if t == "s":
                            v = c.find("{*}v")
                            idx = int(v.text) if (v is not None and v.text and v.text.isdigit()) else -1
                            cells.append(shared[idx] if 0 <= idx < len(shared) else "")
                        elif t == "inlineStr":
                            cells.append("".join(x.text or "" for x in c.iter()
                                                 if _strip_ns(x.tag) == "t"))
                        else:
                            v = c.find("{*}v")
                            cells.append((v.text or "") if v is not None else "")
                    out_lines.append("\t".join(cells))
            return "\n".join(out_lines), truncated
    except Exception as e:  # noqa: BLE001
        log.warning("xlsx extract failed: %s", e)
        return "", False


def _extract_pdf_text(data: bytes) -> tuple[str | None, bool]:
    try:
        from pypdf import PdfReader
    except Exception as e:  # noqa: BLE001 — dep missing shouldn't kill a turn
        log.warning("pypdf import failed: %s", e)
        return None, False
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many statements are "encrypted" with an empty owner pw
            except Exception:  # noqa: BLE001
                return None, False
        pages = reader.pages
        truncated = False
        if len(pages) > _PDF_MAX_PAGES:
            pages, truncated = pages[:_PDF_MAX_PAGES], True
        parts = []
        for page in pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 — skip a bad page, keep the rest
                continue
        text = "\n".join(s for s in parts if s.strip())
        return (text or None), truncated
    except Exception as e:  # noqa: BLE001
        log.warning("pdf extract failed: %s", e)
        return None, False


def extract_file_text(data: bytes, name: str = "", mime: str = "") -> FileText:
    """Best-effort extract a document's text for prompt inlining.

    Dispatch by extension first (the display name is authoritative for what the
    user sent), falling back to mime, then a text sniff. Returns text truncated to
    FILE_INLINE_MAX_CHARS. Never raises.
    """
    if not data:
        return FileText(None, False, "none")
    ext = _file_ext(name)
    mime = (mime or "").lower()

    if ext == "docx" or mime == _DOCX_MIME:
        text, truncated, method = _extract_docx_text(data), False, "docx"
    elif ext == "xlsx" or mime == _XLSX_MIME:
        text, truncated = _extract_xlsx_text(data)
        method = "xlsx"
    elif ext == "pdf" or mime == "application/pdf":
        text, truncated = _extract_pdf_text(data)
        method = "pdf"
    elif _looks_like_text(data):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        truncated, method = False, "text"
    else:
        return FileText(None, False, "none")

    if not text or not text.strip():
        return FileText(None, truncated, method)
    if len(text) > FILE_INLINE_MAX_CHARS:
        text, truncated = text[:FILE_INLINE_MAX_CHARS], True
    return FileText(text, truncated, method)
