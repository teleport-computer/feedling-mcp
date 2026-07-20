#!/usr/bin/env python3
"""Fixed-contract artifact extractor for the Runtime V2 E2B template.

The runner writes exactly three fixed files before invoking this program:

* ``/tmp/feedling-artifact.bin`` (untrusted bytes)
* ``/tmp/feedling-artifact-meta.json`` (display name and MIME type)
* ``/tmp/feedling-artifact-text.utf8`` (this program's bounded output)

No model/user string is ever interpolated into the command line.  The microVM
is still the security boundary; the limits below additionally bound common zip
bombs and pathological documents so one extraction cannot consume the whole
sandbox lifetime.
"""

from __future__ import annotations

import io
from itertools import islice
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


ARTIFACT_PATH = Path("/tmp/feedling-artifact.bin")
META_PATH = Path("/tmp/feedling-artifact-meta.json")
OUTPUT_PATH = Path("/tmp/feedling-artifact-text.utf8")

# Must match backend.hosted.turn.MODEL_API_MAX_FILE_BYTES and the iOS upload
# ceiling. Keep this binary MiB value in the content-addressed template.
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_CHARS = 64_000
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_MEMBER_BYTES = 20_000_000
MAX_ZIP_TOTAL_BYTES = 80_000_000
MAX_PDF_PAGES = 50
MAX_XLSX_SHEETS = 8
MAX_XLSX_ROWS = 500

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExtractionRejected(RuntimeError):
    """The artifact is unsupported, malformed, or outside a resource bound."""


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _bounded_zip(data: bytes) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(data))
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        archive.close()
        raise ExtractionRejected("archive has too many members")
    total = 0
    for info in infos:
        if info.file_size < 0 or info.file_size > MAX_ZIP_MEMBER_BYTES:
            archive.close()
            raise ExtractionRejected("archive member is too large")
        total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            archive.close()
            raise ExtractionRejected("archive expansion is too large")
    return archive


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ExtractionRejected("required document member is missing") from exc
    if info.is_dir() or info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise ExtractionRejected("invalid document member")
    value = archive.read(info)
    if len(value) != info.file_size:
        raise ExtractionRejected("truncated document member")
    return value


def _looks_like_text(data: bytes) -> bool:
    if not data or b"\x00" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _extract_docx(data: bytes) -> str:
    with _bounded_zip(data) as archive:
        root = ET.fromstring(_read_zip_member(archive, "word/document.xml"))
    paragraphs: list[str] = []
    for node in root.iter():
        if _strip_ns(node.tag) != "p":
            continue
        line = "".join(
            child.text or ""
            for child in node.iter()
            if _strip_ns(child.tag) == "t"
        ).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _extract_xlsx(data: bytes) -> str:
    with _bounded_zip(data) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(_read_zip_member(archive, "xl/sharedStrings.xml"))
            for item in root:
                if _strip_ns(item.tag) == "si":
                    shared.append(
                        "".join(
                            child.text or ""
                            for child in item.iter()
                            if _strip_ns(child.tag) == "t"
                        )
                    )

        sheets = sorted(
            name
            for name in names
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )[:MAX_XLSX_SHEETS]
        lines: list[str] = []
        for sheet in sheets:
            root = ET.fromstring(_read_zip_member(archive, sheet))
            rows = (
                node for node in root.iter() if _strip_ns(node.tag) == "row"
            )
            for row_index, row in enumerate(rows):
                if row_index >= MAX_XLSX_ROWS:
                    break
                cells: list[str] = []
                for cell in row:
                    if _strip_ns(cell.tag) != "c":
                        continue
                    cell_type = cell.get("t")
                    if cell_type == "s":
                        value = cell.find("{*}v")
                        raw = value.text if value is not None else ""
                        index = int(raw) if raw and raw.isdigit() else -1
                        cells.append(shared[index] if 0 <= index < len(shared) else "")
                    elif cell_type == "inlineStr":
                        cells.append(
                            "".join(
                                child.text or ""
                                for child in cell.iter()
                                if _strip_ns(child.tag) == "t"
                            )
                        )
                    else:
                        value = cell.find("{*}v")
                        cells.append(value.text or "" if value is not None else "")
                lines.append("\t".join(cells))
        return "\n".join(lines)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # template build contract is broken
        raise ExtractionRejected("PDF extractor is unavailable") from exc
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001 - untrusted document parser
            raise ExtractionRejected("encrypted PDF is not readable") from exc
    parts: list[str] = []
    for page in islice(reader.pages, MAX_PDF_PAGES):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - isolate a malformed page
            continue
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def extract_artifact(data: bytes, *, name: str = "", mime_type: str = "") -> str:
    """Extract bounded UTF-8 text without reading or writing any host path."""
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ExtractionRejected("artifact size is outside the allowed range")
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = str(mime_type or "").strip().lower()
    if suffix == "docx" or mime == DOCX_MIME:
        text = _extract_docx(data)
    elif suffix == "xlsx" or mime == XLSX_MIME:
        text = _extract_xlsx(data)
    elif suffix == "pdf" or mime == "application/pdf":
        text = _extract_pdf(data)
    elif _looks_like_text(data):
        text = data.decode("utf-8")
    else:
        raise ExtractionRejected("artifact format has no supported text view")
    text = text.strip()
    if not text:
        raise ExtractionRejected("artifact contains no extractable text")
    return text[:MAX_OUTPUT_CHARS]


def main() -> int:
    try:
        metadata = json.loads(META_PATH.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ExtractionRejected("artifact metadata is invalid")
        data = ARTIFACT_PATH.read_bytes()
        text = extract_artifact(
            data,
            name=str(metadata.get("name") or "")[:512],
            mime_type=str(metadata.get("mime_type") or "")[:200],
        )
        OUTPUT_PATH.write_text(text, encoding="utf-8")
        return 0
    except Exception as exc:  # content-free stderr; caller maps to a stable error
        OUTPUT_PATH.write_bytes(b"")
        print(f"artifact extraction failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
