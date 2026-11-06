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
* On a PowerPoint instance the user can be in, alerts and macro security are
  changed only while a file is being read and restored right after.
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
# Waits are never cut below this, even when the caller's time is nearly up.
MIN_WAIT_SEC = 5.0
# Only a wait at least this long proves that Office hangs (rather than that the
# caller's time ran out); only such failures trip the circuit breaker and are remembered.
CONCLUSIVE_WAIT_SEC = 30.0
# How long a remembered failure keeps an app from being started again (shared memory).
UNAVAILABLE_TTL_SEC = 600.0


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
    if isinstance(e, ComError):
        return str(e)
    args = getattr(e, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 3 and args[2][2]:
        return f"{args[1]}: {str(args[2][2]).strip()}"
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1]
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# application settings
# --------------------------------------------------------------------------
# AutomationSecurity 3 = msoAutomationSecurityForceDisable: never run macros in opened files.
_SETTINGS: dict[str, list[tuple[str, object]]] = {
    "word": [("Visible", False), ("ScreenUpdating", False), ("DisplayAlerts", 0),  # wdAlertsNone
             ("AutomationSecurity", 3)],
    "excel": [("Visible", False), ("ScreenUpdating", False), ("DisplayAlerts", False),
              ("EnableEvents", False), ("AutomationSecurity", 3)],
    "powerpoint": [("DisplayAlerts", 1), ("AutomationSecurity", 3)],  # ppAlertsNone
}
# Never touched on an instance the user works in: it would hide or freeze their windows.
_OWN_INSTANCE_ONLY = {"Visible", "ScreenUpdating"}


def app_settings(app_key: str, shared: bool) -> list[tuple[str, object]]:
    return [(k, v) for k, v in _SETTINGS[app_key] if not (shared and k in _OWN_INSTANCE_ONLY)]


def apply_settings(app, settings) -> list[tuple[str, object]]:
    """Set application properties (best effort); return the previous values of those changed."""
    previous: list[tuple[str, object]] = []
    for name, value in settings:
        try:
            old = getattr(app, name)
        except Exception:
            old = None
        if old == value:
            continue
        try:
            setattr(app, name, value)
        except Exception as e:
            log.debug("cannot set %s: %s", name, com_error_text(e))
            continue
        if old is not None:
            previous.append((name, old))
    return previous


def restore_settings(app, previous) -> None:
    for name, value in reversed(previous):
        try:
            setattr(app, name, value)
        except Exception as e:
            log.warning("could not restore the Office setting %s=%r: %s", name, value, com_error_text(e))


def run_job(app_key: str, app, path: str, settings=()) -> str:
    """Extract one file with ``settings`` in force for this job only.

    Used for instances the user can see: the user's own values come back as
    soon as the file is closed, even when extraction fails.
    """
    previous = apply_settings(app, settings) if settings else []
    try:
        return _EXTRACT[app_key](app, path)
    finally:
        restore_settings(app, previous)


# --------------------------------------------------------------------------
# per-application extraction (runs inside the worker thread)
# --------------------------------------------------------------------------
def looks_like_pdf_source(text: str) -> bool:
    """Word loads a PDF its converter cannot read (DRM-protected, damaged) as plain text.

    The "text" is then the PDF syntax itself, possibly after a DRM header, so the
    object keywords are checked as well as the "%PDF-" start.
    """
    if text.lstrip("\ufeff \t\r\n").startswith("%PDF-"):
        return True
    return text.count("endobj") >= 2 and "startxref" in text


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
    text = _clean_word(text, converted)
    if looks_like_pdf_source(text):
        raise ComError("Word could not convert this PDF and returned its raw PDF source as plain text "
                       "(usual for DRM-protected PDFs)")
    return text


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
def _user_joined(app_key: str, app) -> bool:
    """True when the user opened presentations in the PowerPoint instance worklog started.

    Every job closes its own presentation, so any left open are the user's: Quit() would close them.
    """
    if app_key != "powerpoint":
        return False
    try:
        count = app.Presentations.Count
    except Exception:
        return False
    if count:
        log.info("PowerPoint now has %d presentation(s) of the user open; leaving it running", count)
    return bool(count)


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
        self.job_settings: list[tuple[str, object]] = []  # applied around each job, then restored
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
            self.pid = _hwnd_pid(self.app_key, app)
            if self.pid is None:
                new = list_pids(exe) - self.before
                if len(new) == 1:
                    self.pid = new.pop()
            if self.pid is not None:
                self.owned = self.pid not in self.before
            else:
                self.owned = not self.before
            # A private hidden instance is configured once. A shared one gets worklog's settings
            # only while a job runs. PowerPoint counts as shared even when worklog started it:
            # it is single-instance, so presentations the user opens meanwhile land in it.
            if self.owned and self.app_key != "powerpoint":
                apply_settings(app, app_settings(self.app_key, shared=False))
            else:
                self.job_settings = app_settings(self.app_key, shared=not self.owned)
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
                    self.results.put((job_id, run_job(self.app_key, app, path, self.job_settings), None))
                except Exception as e:
                    self.results.put((job_id, None, com_error_text(e)))
        finally:
            if self.owned and not _user_joined(self.app_key, app):
                try:
                    app.Quit()
                except Exception:
                    pass
            app = None
            pythoncom.CoUninitialize()


class OfficeCom:
    """Runs extraction jobs on per-application worker threads with timeouts.

    ``memory`` (optional, shared between instances of one process, e.g. the MCP
    server) remembers apps that failed to start or kept hanging, so the next calls
    fail fast for a while instead of paying the full timeout again.
    """

    def __init__(self, timeout_sec: float = 90, start_timeout_sec: float = 60, pdf_via_word: bool = False,
                 memory: dict | None = None):
        self.timeout = float(timeout_sec)
        self.start_timeout = float(start_timeout_sec)
        self._pdf_prompt = _PdfPromptOverride() if pdf_via_word else None
        self._workers: dict[str, _AppWorker] = {}
        self._unavailable: dict[str, str] = {}
        self._timeouts: dict[str, int] = {}
        self._ids = itertools.count(1)
        self.memory = memory

    def _mark_unavailable(self, app_key: str, msg: str, conclusive: bool = True) -> None:
        self._unavailable[app_key] = msg
        if conclusive and self.memory is not None:
            self.memory[app_key] = (time.time() + UNAVAILABLE_TTL_SEC, msg, time.time())

    def _worker(self, app_key: str, wait_limit: float | None = None) -> _AppWorker:
        if app_key in self._unavailable:
            raise ComUnavailable(self._unavailable[app_key])
        remembered = self.memory.get(app_key) if self.memory is not None else None
        if remembered and remembered[0] > time.time():
            until, msg, at = remembered
            raise ComUnavailable(f"{msg} (at {time.strftime('%H:%M', time.localtime(at))}; worklog tries "
                                 f"Office again after {time.strftime('%H:%M', time.localtime(until))})")
        w = self._workers.get(app_key)
        if w is not None and w.is_alive():
            return w
        if app_key == "word" and self._pdf_prompt is not None:
            self._pdf_prompt.apply()  # must be in place before Word starts
        wait = self.start_timeout if wait_limit is None else max(MIN_WAIT_SEC, min(self.start_timeout, wait_limit))
        w = _AppWorker(app_key)
        w.start()
        if not w.ready.wait(wait):
            # Probably stuck on a first-run/activation dialog. Kill the
            # instance only if exactly one new process appeared (ours).
            new = list_pids(EXES[app_key]) - w.before
            if len(new) == 1:
                kill_pid(new.pop())
            msg = f"{PROGIDS[app_key]} did not start within {wait:.0f}s"
            self._mark_unavailable(app_key, msg, conclusive=wait >= CONCLUSIVE_WAIT_SEC)
            raise ComTimeout(msg)
        if w.start_error:
            msg = f"cannot start {PROGIDS[app_key]}: {w.start_error}"
            self._mark_unavailable(app_key, msg)
            raise ComUnavailable(msg)
        self._workers[app_key] = w
        return w

    def app_info(self, app_key: str) -> dict:
        w = self._worker(app_key)
        return {"app": PROGIDS[app_key], "version": w.version, "pid": w.pid, "owned": w.owned}

    def extract(self, app_key: str, path: Path, timeout: float | None = None) -> str:
        """Extract one file. ``timeout`` caps the whole wait (start + job) below the
        configured timeout, for callers that must return in time (agent calls)."""
        limit = self.timeout if timeout is None else max(MIN_WAIT_SEC, min(self.timeout, timeout))
        capped = limit < self.timeout
        t_start = time.monotonic()
        w = self._worker(app_key, wait_limit=None if timeout is None else limit)
        job_id = next(self._ids)
        w.jobs.put((job_id, str(Path(path).resolve())))
        t_job = time.monotonic()
        deadline = max(t_start + limit, t_job + MIN_WAIT_SEC)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                waited = time.monotonic() - t_job
                self._abandon(app_key)
                # Circuit breaker: if Office keeps hanging (e.g. the DRM client
                # waits for a login), stop paying the timeout for every file.
                if waited >= min(self.timeout, CONCLUSIVE_WAIT_SEC):
                    self._timeouts[app_key] = self._timeouts.get(app_key, 0) + 1
                    if self._timeouts[app_key] >= 3:
                        self._mark_unavailable(app_key, f"{PROGIDS[app_key]} timed out 3 times in a row")
                note = " (the time limit of this agent call; a later close-out retries it)" if capped else ""
                raise ComTimeout(f"{PROGIDS[app_key]} did not finish within {waited:.0f}s{note}")
            try:
                jid, text, err = w.results.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if jid != job_id:  # stale result of an abandoned job
                continue
            self._timeouts[app_key] = 0
            if self.memory is not None:
                self.memory.pop(app_key, None)
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

    def shutdown(self, wait: float = 30) -> None:
        """Quit the instances worklog started. ``wait`` bounds the wait per app; an owned
        instance that has not quit by then is killed, a shared one is left alone. After a
        short wait a slow PowerPoint is left to quit on its own: the user may be in it."""
        for key, w in list(self._workers.items()):
            w.jobs.put(None)
            w.join(timeout=wait)
            if w.is_alive() and w.owned and w.pid and (key != "powerpoint" or wait >= CONCLUSIVE_WAIT_SEC):
                kill_pid(w.pid)
        self._workers.clear()
        if self._pdf_prompt is not None:
            self._pdf_prompt.restore()


__all__ = ["OfficeCom", "ComError", "ComTimeout", "ComUnavailable", "com_available"]
