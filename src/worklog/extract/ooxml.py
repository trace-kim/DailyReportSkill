"""Direct (no Office) extraction for .docx / .xlsx / .pptx / .pdf."""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from .common import one_line, row_line

log = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_CELLS = 300_000


class NotOOXML(ValueError):
    """The file is not a zip container: typically DRM-wrapped or legacy binary."""


class NoTextLayer(ValueError):
    """A PDF without extractable text (scanned). Office will not help either."""


def _require_zip(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise NotOOXML("not an Office Open XML (zip) file - likely DRM-protected")


# --- Word ---------------------------------------------------------------
def docx_text(path: Path) -> str:
    _require_zip(path)
    try:
        return _docx_via_lib(path)
    except Exception as e:  # malformed tables etc.
        log.debug("python-docx failed for %s (%s); falling back to raw XML", path, e)
        return _docx_via_xml(path)


def _docx_via_lib(path: Path) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    out: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append(Paragraph(child, document).text)
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells, prev = [], None
                for cell in row.cells:
                    if cell._tc is prev:  # horizontally merged cell repeats
                        continue
                    prev = cell._tc
                    cells.append(one_line(cell.text))
                line = " | ".join(cells)
                if line.replace("|", "").strip():
                    out.append(line)
            out.append("")
    return "\n".join(out)


def _docx_via_xml(path: Path) -> str:
    # python-docx's lxml parser is configured with resolve_entities=False.
    from docx.oxml.parser import parse_xml

    with zipfile.ZipFile(path) as z:
        root = parse_xml(z.read("word/document.xml"))
    return "\n".join(
        "".join(t.text or "" for t in p.iter(_W + "t")) for p in root.iter(_W + "p")
    )


# --- Excel --------------------------------------------------------------
def xlsx_text(path: Path, max_cells: int = MAX_CELLS) -> str:
    _require_zip(path)
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out: list[str] = []
    budget = max_cells
    try:
        for ws in wb.worksheets:
            out.append(f"[시트] {ws.title}")
            for row in ws.iter_rows(values_only=True):
                budget -= len(row)
                line = row_line(row)
                if line:
                    out.append(line)
                if budget <= 0:
                    out.append("…(셀 수 제한으로 이하 생략)")
                    break
            out.append("")
            if budget <= 0:
                break
    finally:
        wb.close()
    return "\n".join(out)


# --- PowerPoint ---------------------------------------------------------
def pptx_text(path: Path) -> str:
    _require_zip(path)
    from pptx import Presentation

    prs = Presentation(str(path))
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"[슬라이드 {i}]")
        for shape in slide.shapes:
            _pptx_shape(shape, out)
        if slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            notes = one_line(frame.text) if frame is not None else ""
            if notes:
                out.append(f"[노트] {notes}")
        out.append("")
    return "\n".join(out)


def _pptx_shape(shape, out: list[str]) -> None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    try:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for sub in shape.shapes:
                _pptx_shape(sub, out)
            return
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                line = " | ".join(one_line(cell.text) for cell in row.cells)
                if line.replace("|", "").strip():
                    out.append(line)
            return
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                if para.text.strip():
                    out.append(para.text)
    except Exception as e:  # unusual shapes should never break extraction
        log.debug("pptx shape skipped: %s", e)


# --- PDF ----------------------------------------------------------------
def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception as e:
            raise ValueError(f"encrypted PDF: {e}") from None
        if not ok:
            raise ValueError("password-protected PDF")
    out: list[str] = []
    chars = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            log.debug("pdf page %d of %s failed: %s", i, path, e)
            text = ""
        out.append(f"[페이지 {i}]")
        out.append(text)
        chars += len(text.strip())
    if chars == 0:
        raise NoTextLayer("PDF has no text layer (scanned image?)")
    return "\n".join(out)
