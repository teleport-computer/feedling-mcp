"""Render model-authored workspace text into real downloadable documents."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape as html_escape
from io import BytesIO
import re
import threading
from xml.sax.saxutils import escape as xml_escape
import zipfile


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"

_TABLE_DIVIDER_RE = re.compile(r"^:?-{3,}:?$")
_NUMBERED_RE = re.compile(r"^\s*(\d+[.)])\s+(.+)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_INLINE_MARKERS_RE = re.compile(r"(?<!\\)(\*\*|__|~~|`)")
_PDF_FONT_LOCK = threading.Lock()


@dataclass(frozen=True)
class DocumentBlock:
    kind: str
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()


def _plain_inline(text: str) -> str:
    value = _IMAGE_RE.sub(lambda match: match.group(1) or "image", str(text or ""))
    value = _LINK_RE.sub(
        lambda match: f"{match.group(1)} ({match.group(2)})", value
    )
    value = _INLINE_MARKERS_RE.sub("", value)
    return value.replace("\\*", "*").replace("\\_", "_").strip()


def _table_cells(line: str) -> tuple[str, ...]:
    value = str(line or "").strip().strip("|")
    return tuple(_plain_inline(cell.strip()) for cell in value.split("|"))


def _is_table_divider(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(_TABLE_DIVIDER_RE.fullmatch(cell) for cell in cells)


def parse_document_source(source: str) -> tuple[DocumentBlock, ...]:
    """Parse the Markdown-like source shape models naturally produce."""
    lines = str(source or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[DocumentBlock] = []
    index = 0
    in_code = False
    code_lines: list[str] = []

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append(DocumentBlock("code", "\n".join(code_lines)))
                code_lines = []
                in_code = False
            else:
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(raw)
            index += 1
            continue
        if not stripped:
            index += 1
            continue
        if (
            "|" in stripped
            and index + 1 < len(lines)
            and _is_table_divider(lines[index + 1])
        ):
            rows = [_table_cells(raw)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index]))
                index += 1
            width = max(len(row) for row in rows)
            normalized = tuple(row + ("",) * (width - len(row)) for row in rows)
            blocks.append(DocumentBlock("table", rows=normalized))
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            blocks.append(
                DocumentBlock(
                    "heading",
                    _plain_inline(heading.group(2)),
                    min(len(heading.group(1)), 3),
                )
            )
            index += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            blocks.append(DocumentBlock("rule"))
            index += 1
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            blocks.append(DocumentBlock("bullet", _plain_inline(stripped[2:])))
            index += 1
            continue
        numbered = _NUMBERED_RE.match(stripped)
        if numbered:
            blocks.append(
                DocumentBlock(
                    "numbered",
                    f"{numbered.group(1)} {_plain_inline(numbered.group(2))}",
                )
            )
            index += 1
            continue
        if stripped.startswith(">"):
            blocks.append(DocumentBlock("quote", _plain_inline(stripped.lstrip("> "))))
            index += 1
            continue

        paragraph = [_plain_inline(stripped)]
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if (
                not candidate
                or candidate.startswith(("#", "```", ">", "- ", "* ", "+ "))
                or re.fullmatch(r"[-*_]{3,}", candidate)
                or _NUMBERED_RE.match(candidate)
                or (
                    "|" in candidate
                    and index + 1 < len(lines)
                    and _is_table_divider(lines[index + 1])
                )
            ):
                break
            paragraph.append(_plain_inline(candidate))
            index += 1
        blocks.append(DocumentBlock("paragraph", " ".join(part for part in paragraph if part)))

    if in_code:
        blocks.append(DocumentBlock("code", "\n".join(code_lines)))
    return tuple(blocks)


def _word_run(text: str, *, bold: bool = False, size: int = 22) -> str:
    properties = [
        '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/>',
        f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>',
    ]
    if bold:
        properties.append("<w:b/><w:bCs/>")
    return (
        "<w:r><w:rPr>"
        + "".join(properties)
        + '</w:rPr><w:t xml:space="preserve">'
        + xml_escape(text)
        + "</w:t></w:r>"
    )


def _word_paragraph(block: DocumentBlock) -> str:
    text = block.text
    size = 22
    bold = False
    before = 0
    after = 120
    if block.kind == "heading":
        size = {1: 34, 2: 28, 3: 24}.get(block.level, 24)
        bold = True
        before, after = 240, 120
    elif block.kind == "bullet":
        text = "• " + text
    elif block.kind == "quote":
        text = "│ " + text
    elif block.kind == "rule":
        text = "────────────────────────"
    elif block.kind == "code":
        text = block.text
    elif block.kind == "table_header":
        bold = True
        after = 40
    lines = text.split("\n") or [""]
    runs = []
    for index, line in enumerate(lines):
        if index:
            runs.append("<w:r><w:br/></w:r>")
        runs.append(_word_run(line, bold=bold, size=size))
    return (
        "<w:p><w:pPr>"
        f'<w:spacing w:before="{before}" w:after="{after}" w:line="320" '
        'w:lineRule="auto"/>'
        "</w:pPr>"
        + "".join(runs)
        + "</w:p>"
    )


def _word_table(rows: tuple[tuple[str, ...], ...]) -> str:
    border = '<w:top w:val="single" w:sz="4" w:color="D9D9D9"/>'
    border += '<w:left w:val="single" w:sz="4" w:color="D9D9D9"/>'
    border += '<w:bottom w:val="single" w:sz="4" w:color="D9D9D9"/>'
    border += '<w:right w:val="single" w:sz="4" w:color="D9D9D9"/>'
    border += '<w:insideH w:val="single" w:sz="4" w:color="D9D9D9"/>'
    border += '<w:insideV w:val="single" w:sz="4" w:color="D9D9D9"/>'
    rendered_rows = []
    for row_index, row in enumerate(rows):
        cells = []
        for cell in row:
            shade = '<w:shd w:fill="F2F2F2"/>' if row_index == 0 else ""
            cells.append(
                "<w:tc><w:tcPr>"
                + shade
                + "</w:tcPr>"
                + _word_paragraph(
                    DocumentBlock("table_header" if row_index == 0 else "paragraph", cell)
                )
                + "</w:tc>"
            )
        rendered_rows.append("<w:tr>" + "".join(cells) + "</w:tr>")
    return (
        "<w:tbl><w:tblPr><w:tblBorders>"
        + border
        + "</w:tblBorders><w:tblCellMar>"
        '<w:top w:w="80" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
        '<w:bottom w:w="80" w:type="dxa"/><w:right w:w="100" w:type="dxa"/>'
        "</w:tblCellMar></w:tblPr>"
        + "".join(rendered_rows)
        + "</w:tbl>"
    )


def _zip_member(archive: zipfile.ZipFile, name: str, content: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content.encode("utf-8"))


def render_docx(source: str) -> bytes:
    blocks = parse_document_source(source)
    body = "".join(
        _word_table(block.rows) if block.kind == "table" else _word_paragraph(block)
        for block in blocks
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + body
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
        "</w:sectPr></w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" '
        'w:eastAsia="Microsoft YaHei"/><w:sz w:val="22"/></w:rPr></w:style>'
        "</w:styles>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>IO</dc:creator><cp:lastModifiedBy>IO</cp:lastModifiedBy>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">2000-01-01T00:00:00Z</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">2000-01-01T00:00:00Z</dcterms:modified>'
        "</cp:coreProperties>"
    )
    app = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>IO</Application></Properties>"
    )

    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        _zip_member(archive, "[Content_Types].xml", content_types)
        _zip_member(archive, "_rels/.rels", root_rels)
        _zip_member(archive, "word/document.xml", document)
        _zip_member(archive, "word/styles.xml", styles)
        _zip_member(archive, "word/_rels/document.xml.rels", document_rels)
        _zip_member(archive, "docProps/core.xml", core)
        _zip_member(archive, "docProps/app.xml", app)
    return output.getvalue()


def _pdf_safe_text(text: str) -> str:
    replacements = {
        "🏋️": "训练",
        "🏋": "训练",
        "🥗": "饮食",
        "🏃": "有氧",
        "⚠️": "注意",
        "⚠": "注意",
        "💧": "饮水",
    }
    value = str(text or "")
    for original, replacement in replacements.items():
        value = value.replace(original, replacement)
    return "".join(char for char in value if ord(char) <= 0xFFFF and char != "\ufe0f")


def render_pdf(source: str, *, title: str = "IO Document") -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError("PDF rendering dependency is unavailable") from exc

    font_name = "STSong-Light"
    with _PDF_FONT_LOCK:
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    normal = ParagraphStyle(
        "Body",
        fontName=font_name,
        fontSize=10.5,
        leading=17,
        textColor=colors.HexColor("#222222"),
        wordWrap="CJK",
        spaceAfter=6,
    )
    heading_styles = {
        1: ParagraphStyle(
            "H1", parent=normal, fontSize=19, leading=26, spaceBefore=8,
            spaceAfter=12, alignment=TA_CENTER,
        ),
        2: ParagraphStyle(
            "H2", parent=normal, fontSize=15, leading=22, spaceBefore=10,
            spaceAfter=7,
        ),
        3: ParagraphStyle(
            "H3", parent=normal, fontSize=12.5, leading=19, spaceBefore=7,
            spaceAfter=5,
        ),
    }
    quote = ParagraphStyle(
        "Quote", parent=normal, leftIndent=10 * mm,
        textColor=colors.HexColor("#555555"),
    )
    code = ParagraphStyle(
        "Code", parent=normal, leftIndent=4 * mm, rightIndent=4 * mm,
        backColor=colors.HexColor("#F5F5F5"), fontSize=9, leading=14,
    )

    story = []
    available_width = A4[0] - (36 * mm)
    for block in parse_document_source(source):
        if block.kind == "table":
            column_count = len(block.rows[0]) if block.rows else 1
            cell_style = ParagraphStyle("Cell", parent=normal, fontSize=9, leading=13)
            table_data = [
                [
                    Paragraph(html_escape(_pdf_safe_text(cell)), cell_style)
                    for cell in row
                ]
                for row in block.rows
            ]
            table = Table(
                table_data,
                colWidths=[available_width / max(1, column_count)] * column_count,
                repeatRows=1,
                hAlign="LEFT",
            )
            table.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), font_name),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F2F2")),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D0D0")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.extend([table, Spacer(1, 7)])
            continue
        if block.kind == "rule":
            story.append(Spacer(1, 5))
            continue
        text = _pdf_safe_text(block.text)
        style = normal
        if block.kind == "heading":
            style = heading_styles.get(block.level, heading_styles[3])
        elif block.kind == "quote":
            style = quote
            text = "注意：" + text
        elif block.kind == "code":
            style = code
            text = text.replace("\n", "<br/>")
        elif block.kind == "bullet":
            text = "• " + text
        paragraph = Paragraph(html_escape(text).replace("&lt;br/&gt;", "<br/>"), style)
        story.append(paragraph)

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=str(title or "IO Document")[:200],
        author="IO",
    )

    def page_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawCentredString(A4[0] / 2, 9 * mm, str(doc.page))
        canvas.restoreState()

    document.build(story or [Paragraph(" ", normal)], onFirstPage=page_footer, onLaterPages=page_footer)
    return output.getvalue()


def render_download(name: str, source: str) -> tuple[bytes, str] | None:
    lower = str(name or "").lower()
    if lower.endswith(".docx"):
        return render_docx(source), DOCX_MIME
    if lower.endswith(".pdf"):
        return render_pdf(source, title=name), PDF_MIME
    return None
