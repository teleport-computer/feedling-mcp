from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import zipfile

import pytest


_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "e2b"
    / "runtime-v2"
    / "extract_artifact.py"
)
_SPEC = importlib.util.spec_from_file_location("feedling_e2b_extractor", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
extractor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(extractor)


def _archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue()


def test_plain_text_and_docx_extract_without_user_controlled_commands() -> None:
    assert extractor.extract_artifact(
        b"hello\nworld", name="notes.txt", mime_type="text/plain"
    ) == "hello\nworld"

    docx = _archive(
        {
            "word/document.xml": (
                '<w:document xmlns:w="urn:w"><w:body><w:p>'
                "<w:r><w:t>hello docx</w:t></w:r>"
                "</w:p></w:body></w:document>"
            )
        }
    )
    assert extractor.extract_artifact(
        docx, name="../../bad;name.docx", mime_type=extractor.DOCX_MIME
    ) == "hello docx"


def test_binary_and_oversized_archive_fail_closed() -> None:
    with pytest.raises(extractor.ExtractionRejected):
        extractor.extract_artifact(b"\x00\x01\x02", name="unknown.bin")

    archive = _archive({"word/document.xml": "x" * 100})
    original = extractor.MAX_ZIP_TOTAL_BYTES
    try:
        extractor.MAX_ZIP_TOTAL_BYTES = 10
        with pytest.raises(extractor.ExtractionRejected, match="expansion"):
            extractor.extract_artifact(archive, name="bomb.docx")
    finally:
        extractor.MAX_ZIP_TOTAL_BYTES = original


def test_input_limit_matches_ios_25_mib() -> None:
    assert extractor.MAX_INPUT_BYTES == 25 * 1024 * 1024


def test_output_is_bounded() -> None:
    value = extractor.extract_artifact(
        b"x" * (extractor.MAX_OUTPUT_CHARS + 100),
        name="large.txt",
        mime_type="text/plain",
    )
    assert len(value) == extractor.MAX_OUTPUT_CHARS
