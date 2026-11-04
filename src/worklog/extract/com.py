"""Text extraction through Office COM automation (Word / Excel / PowerPoint).

Company DRM clients typically decrypt files only for whitelisted programs such
as WINWORD.EXE; python.exe sees ciphertext. Asking Office to open the file
through COM therefore reads DRM-protected documents.

Safety rules implemented here:
* Every app runs in a dedicated worker thread that owns its COM objects, so a
  hung call (an unexpected dialog) can be abandoned with a timeout.
* Word and Excel are started as *separate* hidden instances (DispatchEx).
  PowerPoint is single-instance, so if the user already has it open we share
  it: then we never Quit() it and never kill it.
* Files are opened read-only, without alerts, macros, link updates or MRU
  entries, and closed without saving.
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .common import normalize_text, row_line

log = logging.getLogger(__name__)

PROGIDS = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}
EXES = {"word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE"}
# A bogus password makes Office fail fast on password-protected files instead
# of showing a password prompt that would hang the automation. It is ignored
# for files without a password.
NO_PASSWORD = "worklog-no-password"
MAX_CELLS = 300_000
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class ComError(Exception):
    pass


class ComUnavailable(ComError):
    pass


class ComTimeout(ComError):
    pass


def com_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------
def list_pids(exe: str) -> set[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return set()
    pids = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].lower() == exe.lower():
            try:
                pids.add(int(row[1]))
            except ValueError:
                pass
    return pids


def kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        log.warning("killed Office process %s", pid)
    except OSError as e:
        log.warning("could not kill process %s: %s", pid, e)


def _hwnd_pid(app_key: str, app) -> int | None:
    try:
        import win32process

        if app_key == "excel":
            hwnd = app.Hwnd
        elif app_key == "powerpoint":
            hwnd = app.HWND
        else:
            return None
        return win32process.GetWindowThreadProcessId(int(hwnd))[1] if hwnd else None
    except Exception:
        return None


def com_error_text(e: BaseException) -> str:
    """Readable message for pywintypes.com_error (which nests the real cause)."""
    args = getattr(e, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 3 and args[2][2]:
        return f"{args[1]}: {str(args[2][2]).strip()}"
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1]
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# per-application extraction (runs inside the worker thread)
# --------------------------------------------------------------------------
def _configure(app_key: str, app) -> None:
    def attempt(fn):
        try:
            fn()
        except Exception:
            pass

    if app_key == "word":
        attempt(lambda: setattr(app, "Visible", False))
        attempt(lambda: setattr(app, "DisplayAlerts", 0))  # wdAlertsNone
        attempt(lambda: setattr(app, "ScreenUpdating", False))
    elif app_key == "excel":
        attempt(lambda: setattr(app, "Visible", False))
        attempt(lambda: setattr(app, "DisplayAlerts", False))
        attempt(lambda: setattr(app, "ScreenUpdating", False))
        attempt(lambda: setattr(app, "EnableEvents", False))
    elif app_key == "powerpoint":
        attempt(lambda: setattr(app, "DisplayAlerts", 1))  # ppAlertsNone
    # msoAutomationSecurityForceDisable: never run macros in opened files.
    attempt(lambda: setattr(app, "AutomationSecurity", 3))


def _clean_word(text: str, tables_converted: bool) -> str:
    for code in ("\x13", "\x14", "\x15", "\x01", "\x02", "\x05", "\x08", "\x1f"):
        text = text.replace(code, "")
    text = text.replace("\x1e", "-").replace("\r\x07", " | ").replace("\x07", "")
    if tables_converted:
        text = text.replace("\t", " | ")
    return normalize_text(text)


def word_text(app, path: str) -> str:
    doc = app.Documents.Open(
        FileName=path, ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
        PasswordDocument=NO_PASSWORD, Visible=False, NoEncodingDialog=True,
    )
    try:
        try:
            doc.TrackRevisions = False
        except Exception:
            pass
        # Converting tables to tab-separated text (in memory only, never
        # saved) gives one line per row even with merged cells.
        converted = False
        try:
            for i in range(doc.Tables.Count, 0, -1):
                doc.Tables(i).ConvertToText(Separator=1, NestedTables=True)  # wdSeparateByTabs
            converted = True
        except Exception as e:
            log.debug("table conversion failed for %s: %s", path, com_error_text(e))
        text = doc.Content.Text
    finally:
        doc.Close(SaveChanges=0)  # wdDoNotSaveChanges
    return _clean_word(text, converted)


def excel_text(app, path: str, max_cells: int = MAX_CELLS) -> str:
    wb = app.Workbooks.Open(
        Filename=path, UpdateLinks=0, ReadOnly=True, Password=NO_PASSWORD,
        IgnoreReadOnlyRecommended=True, Notify=False, AddToMru=False,
    )
    out: list[str] = []
    budget = max_cells
    try:
        for ws in wb.Worksheets:
            out.append(f"[시트] {ws.Name}")
            used = ws.UsedRange
            rows, cols = used.Rows.Count, used.Columns.Count
            truncated = False
            if rows * cols > budget:
                rows = max(1, budget // max(cols, 1))
                used = ws.Range(used.Cells(1, 1), used.Cells(rows, cols))
                truncated = True
            # One bulk read: every COM property access is a cross-process call.
            values = used.Value
            if values is not None:
                if not isinstance(values, tuple):
                    values = ((values,),)
                for row in values:
                    line = row_line(row)
                    if line:
                        out.append(line)
            budget -= rows * cols
            if truncated or budget <= 0:
                out.append("…(셀 수 제한으로 이하 생략)")
                break
            out.append("")
    finally:
        wb.Close(SaveChanges=False)
    return normalize_text("\n".join(out))


def _ppt_clean(text: str) -> str:
    return " / ".join(p.strip() for p in text.replace("\x0b", "\r").split("\r") if p.strip())


def _ppt_shape(shape, out: list[str]) -> None:
    try:
        if shape.Type == 6:  # msoGroup: GroupItems are the leaf shapes
            for j in range(1, shape.GroupItems.Count + 1):
                _ppt_shape(shape.GroupItems(j), out)
            return
        if shape.HasTable:
            table = shape.Table
            for r in range(1, table.Rows.Count + 1):
                cells = [
                    _ppt_clean(table.Cell(r, c).Shape.TextFrame.TextRange.Text)
                    for c in range(1, table.Columns.Count + 1)
                ]
                line = " | ".join(cells)
                if line.replace("|", "").strip():
                    out.append(line)
            return
        if getattr(shape, "HasSmartArt", 0):
            nodes = shape.SmartArt.AllNodes
            for k in range(1, nodes.Count + 1):
                t = _ppt_clean(nodes(k).TextFrame2.TextRange.Text)
                if t:
                    out.append(t)
            return
        if shape.HasTextFrame and shape.TextFrame.HasText:
            for para in shape.TextFrame.TextRange.Text.replace("\x0b", "\r").split("\r"):
                if para.strip():
                    out.append(para.strip())
    except Exception as e:
        log.debug("ppt shape skipped: %s", com_error_text(e))


def powerpoint_text(app, path: str) -> str:
    # Positional: FileName, ReadOnly, Untitled, WithWindow (MsoTriState: -1 true, 0 false)
    pres = app.Presentations.Open(path, -1, 0, 0)
    out: list[str] = []
    try:
        for i in range(1, pres.Slides.Count + 1):
            slide = pres.Slides(i)
            out.append(f"[슬라이드 {i}]")
            for j in range(1, slide.Shapes.Count + 1):
                _ppt_shape(slide.Shapes(j), out)
            try:
                notes = _ppt_clean(slide.NotesPage.Shapes.Placeholders(2).TextFrame.TextRange.Text)
            except Exception:
                notes = ""
            if notes:
                out.append(f"[노트] {notes}")
            out.append("")
    finally:
        pres.Close()
    return normalize_text("\n".join(out))


_EXTRACT = {"word": word_text, "excel": excel_text, "powerpoint": powerpoint_text}


# --------------------------------------------------------------------------
# Word PDF conversion prompt
# --------------------------------------------------------------------------
_PDF_WARNING = "DisableConvertPdfWarning"


class _PdfPromptOverride:
    """Temporarily suppress Word's "Word will now convert your PDF..." dialog.

    DisplayAlerts does not cover that dialog, so without this a PDF hangs Word
    until the timeout. The per-user value is set only while worklog's own Word
    session runs and is restored afterwards.
    """

    def __init__(self) -> None:
        self._restore: list[tuple[str, object]] = []

    def apply(self) -> None:
        if sys.platform != "win32":
            return
        import winreg

        for ver in ("16.0", "15.0"):
            base = rf"Software\Microsoft\Office\{ver}\Word"
            try:
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, base).Close()
            except OSError:
                continue
            opts = base + r"\Options"
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, opts) as k:
                    try:
                        prev, _ = winreg.QueryValueEx(k, _PDF_WARNING)
                    except FileNotFoundError:
                        prev = None
                    if prev != 1:
                        winreg.SetValueEx(k, _PDF_WARNING, 0, winreg.REG_DWORD, 1)
                        self._restore.append((opts, prev))
            except OSError as e:
                log.debug("cannot set %s for Word %s: %s", _PDF_WARNING, ver, e)

    def restore(self) -> None:
        if not self._restore:
            return
        import winreg

        for opts, prev in self._restore:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, opts, 0, winreg.KEY_SET_VALUE) as k:
                    if prev is None:
                        winreg.DeleteValue(k, _PDF_WARNING)
                    else:
                        winreg.SetValueEx(k, _PDF_WARNING, 0, winreg.REG_DWORD, prev)
            except OSError as e:
                log.warning("could not restore %s: %s", _PDF_WARNING, e)
        self._restore.clear()


# --------------------------------------------------------------------------
# worker thread + manager
# --------------------------------------------------------------------------
class _AppWorker(threading.Thread):
    def __init__(self, app_key: str):
        super().__init__(daemon=True, name=f"worklog-com-{app_key}")
        self.app_key = app_key
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.start_error: str | None = None
        self.before: set[int] = set()
        self.pid: int | None = None
        self.owned = False  # True when we started the process ourselves
        self.version = ""

    def run(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        app = None
        try:
            exe = EXES[self.app_key]
            self.before = list_pids(exe)
            app = win32com.client.DispatchEx(PROGIDS[self.app_key])
            _configure(self.app_key, app)
            self.pid = _hwnd_pid(self.app_key, app)
            if self.pid is None:
                new = list_pids(exe) - self.before
                if len(new) == 1:
                    self.pid = new.pop()
            if self.pid is not None:
                self.owned = self.pid not in self.before
            else:
                self.owned = not self.before
            try:
                self.version = str(app.Version)
            except Exception:
                pass
        except Exception as e:
            self.start_error = com_error_text(e)
            self.ready.set()
            pythoncom.CoUninitialize()
            return
        self.ready.set()
        try:
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                job_id, path = job
                try:
                    self.results.put((job_id, _EXTRACT[self.app_key](app, path), None))
                except Exception as e:
                    self.results.put((job_id, None, com_error_text(e)))
        finally:
            if self.owned:
                try:
                    app.Quit()
                except Exception:
                    pass
            app = None
            pythoncom.CoUninitialize()


class OfficeCom:
    """Runs extraction jobs on per-application worker threads with timeouts."""

    def __init__(self, timeout_sec: float = 90, start_timeout_sec: float = 60, pdf_via_word: bool = False):
        self.timeout = float(timeout_sec)
        self.start_timeout = float(start_timeout_sec)
        self._pdf_prompt = _PdfPromptOverride() if pdf_via_word else None
        self._workers: dict[str, _AppWorker] = {}
        self._unavailable: dict[str, str] = {}
        self._timeouts: dict[str, int] = {}
        self._ids = itertools.count(1)

    def _worker(self, app_key: str) -> _AppWorker:
        if app_key in self._unavailable:
            raise ComUnavailable(self._unavailable[app_key])
        w = self._workers.get(app_key)
        if w is not None and w.is_alive():
            return w
        if app_key == "word" and self._pdf_prompt is not None:
            self._pdf_prompt.apply()  # must be in place before Word starts
        w = _AppWorker(app_key)
        w.start()
        if not w.ready.wait(self.start_timeout):
            # Probably stuck on a first-run/activation dialog. Kill the
            # instance only if exactly one new process appeared (ours).
            new = list_pids(EXES[app_key]) - w.before
            if len(new) == 1:
                kill_pid(new.pop())
            msg = f"{PROGIDS[app_key]} did not start within {self.start_timeout:.0f}s"
            self._unavailable[app_key] = msg
            raise ComTimeout(msg)
        if w.start_error:
            msg = f"cannot start {PROGIDS[app_key]}: {w.start_error}"
            self._unavailable[app_key] = msg
            raise ComUnavailable(msg)
        self._workers[app_key] = w
        return w

    def app_info(self, app_key: str) -> dict:
        w = self._worker(app_key)
        return {"app": PROGIDS[app_key], "version": w.version, "pid": w.pid, "owned": w.owned}

    def extract(self, app_key: str, path: Path) -> str:
        w = self._worker(app_key)
        job_id = next(self._ids)
        w.jobs.put((job_id, str(Path(path).resolve())))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._abandon(app_key)
                # Circuit breaker: if Office keeps hanging (e.g. the DRM client
                # waits for a login), stop paying the timeout for every file.
                self._timeouts[app_key] = self._timeouts.get(app_key, 0) + 1
                if self._timeouts[app_key] >= 3:
                    self._unavailable[app_key] = f"{PROGIDS[app_key]} timed out 3 times in a row"
                raise ComTimeout(f"{PROGIDS[app_key]} did not finish within {self.timeout:.0f}s")
            try:
                jid, text, err = w.results.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if jid != job_id:  # stale result of an abandoned job
                continue
            self._timeouts[app_key] = 0
            if err:
                raise ComError(err)
            return text

    def _abandon(self, app_key: str) -> None:
        w = self._workers.pop(app_key, None)
        if w is None:
            return
        if w.owned and w.pid:
            kill_pid(w.pid)
        else:
            log.warning("%s is shared with the user; abandoning without killing", PROGIDS[app_key])
        w.jobs.put(None)

    def shutdown(self) -> None:
        for key, w in list(self._workers.items()):
            w.jobs.put(None)
            w.join(timeout=30)
            if w.is_alive() and w.owned and w.pid:
                kill_pid(w.pid)
        self._workers.clear()
        if self._pdf_prompt is not None:
            self._pdf_prompt.restore()


__all__ = ["OfficeCom", "ComError", "ComTimeout", "ComUnavailable", "com_available"]
