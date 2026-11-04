"""Helpers shared by the extractors."""

from __future__ import annotations

import datetime as dt
import re

_CTRL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


def normalize_text(text: str) -> str:
    """Unify newlines, drop control characters and collapse blank-line runs."""
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x0b", "\n")
        .replace("\x0c", "\n")
        .replace("\xa0", " ")
    )
    text = _CTRL.sub("", text)
    out: list[str] = []
    blank = 0
    for line in text.split("\n"):
        line = line.rstrip()
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip("\n")


def fmt_cell(v) -> str:
    """Format a spreadsheet value the way a person would read it."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.10g}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, dt.datetime):
        if (v.hour, v.minute, v.second) == (0, 0, 0):
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    s = str(v)
    if "\n" in s or "\r" in s:
        s = " / ".join(part.strip() for part in s.splitlines() if part.strip())
    return s.strip()


def row_line(values) -> str | None:
    """One spreadsheet/table row as 'a | b | c' (None when the row is empty).

    Rows are kept without cell addresses on purpose: inserting a row then only
    adds one diff line instead of shifting every address below it.
    """
    cells = [fmt_cell(v) for v in values]
    while cells and cells[-1] == "":
        cells.pop()
    if not any(cells):
        return None
    return " | ".join(cells)


def one_line(text: str) -> str:
    return " / ".join(part.strip() for part in str(text).splitlines() if part.strip())
