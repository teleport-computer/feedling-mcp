"""User-visible cleanup for replies accompanied by downloadable files."""

from __future__ import annotations

import re


_CODEX_FILE_CITATION_RE = re.compile(
    r":codex-file-citation\{[^{}\r\n]*\}"
)
_INTERNAL_MARKDOWN_LINK_RE = re.compile(
    r"\[[^\]\r\n]*\]\(\s*(?:sandbox:|file:|/(?:Users|private|tmp|workspace)/)"
    r"[^)\r\n]*\)",
    re.IGNORECASE,
)
_INTERNAL_URI_RE = re.compile(
    r"(?:sandbox:|file:)(?://)?[^\s<>\]\)]+",
    re.IGNORECASE,
)
_INTERNAL_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w])/(?:Users|private|tmp|workspace)/(?:[^\s<>\]\),，。；;]+)",
    re.IGNORECASE,
)
_INTERNAL_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w])[A-Za-z]:\\(?:[^\s<>\]\),，。；;]+)",
)


def _ready_reply(source: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", source):
        return "文件已生成，可在下方下载。"
    return "The file is ready. You can download it below."


def sanitize_downloadable_reply(
    text: str,
    *,
    attachment_staged: bool,
) -> tuple[str, bool]:
    """Remove runtime-local attachment references from visible reply text.

    Once a structured attachment exists, any reply that leaked an internal
    reference is replaced with a short confirmation. Without an attachment,
    callers can use ``removed=True`` to turn the response into a real failure.
    """

    value = str(text or "")
    cleaned = value
    removed = False
    for pattern in (
        _CODEX_FILE_CITATION_RE,
        _INTERNAL_MARKDOWN_LINK_RE,
        _INTERNAL_URI_RE,
        _INTERNAL_ABSOLUTE_PATH_RE,
        _INTERNAL_WINDOWS_PATH_RE,
    ):
        cleaned, count = pattern.subn("", cleaned)
        removed = removed or count > 0
    if not removed:
        return value, False
    if attachment_staged:
        return _ready_reply(value), True
    cleaned = re.sub(r"[ \t]+(?=\r?$)", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[：:]\s*(?=\r?\n|$)", "。", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True
