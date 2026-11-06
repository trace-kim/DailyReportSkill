from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from worklog.config import DEFAULT_CONFIG_TOML
from worklog.service import Worklog


def write_config(root: Path, com: bool = False, extra: str = "") -> None:
    d = root / ".worklog"
    d.mkdir(parents=True, exist_ok=True)
    text = DEFAULT_CONFIG_TOML.replace("com_enabled = true", f"com_enabled = {'true' if com else 'false'}")
    (d / "config.toml").write_text(text + extra, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_task_scheduler(monkeypatch):
    """Tests do not depend on the Task Scheduler of the PC they run on."""
    monkeypatch.setattr("worklog.service.find_scheduled_tasks", lambda root: None)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "업무 폴더"  # Korean name with a space on purpose
    r.mkdir()
    return r


@pytest.fixture
def make_wl(root: Path):
    """Initialize worklog on the root (COM disabled unless asked) and return it."""
    created: list[Worklog] = []

    def factory(com: bool = False) -> Worklog:
        write_config(root, com=com)
        wl, _ = Worklog.init(root)
        created.append(wl)
        return wl

    yield factory
    for wl in created:
        wl.close_db()


def make_docx(path: Path, paragraphs: list[str], table: list[list[str]] | None = None) -> Path:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, val in enumerate(row):
                t.cell(i, j).text = val
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    return path


def make_xlsx(path: Path, sheets: dict[str, list[list]]) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


def make_pptx(path: Path, slides: list[list[str]], notes: list[str] | None = None) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    layout = prs.slide_layouts[5]  # title only
    for i, texts in enumerate(slides):
        s = prs.slides.add_slide(layout)
        s.shapes.title.text = texts[0]
        if len(texts) > 1:
            box = s.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
            box.text_frame.text = texts[1]
            for extra in texts[2:]:
                box.text_frame.add_paragraph().text = extra
        if notes and i < len(notes):
            s.notes_slide.notes_text_frame.text = notes[i]
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return path


def make_pdf(path: Path, lines: list[str]) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in lines:
        c.drawString(72, y, line)
        y -= 20
    c.save()
    return path


def set_mtime(path: Path, when: dt.datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def backdate_tracking(wl: Worklog, when: dt.datetime) -> None:
    """Pretend tracking (install + baseline) happened at `when`."""
    ts = when.timestamp()
    wl.con.execute("UPDATE meta SET value=? WHERE key='installed_at'", (repr(ts),))
    wl.con.execute("UPDATE files SET ext_at=? WHERE ext_at IS NOT NULL", (ts,))
    wl.con.execute("UPDATE files SET first_seen=?", (ts,))
    wl.con.execute("UPDATE scans SET taken_at=?", (ts,))
    wl.con.commit()
