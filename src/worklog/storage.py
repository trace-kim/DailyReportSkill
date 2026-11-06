"""Workspace layout, encrypted text vault (DPAPI), hashing and file locks."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import tempfile
import time
import zlib
from pathlib import Path

WORKLOG_DIRNAME = ".worklog"

_MAGIC_DPAPI = b"WLD1"  # zlib payload encrypted with DPAPI (current user)
_MAGIC_PLAIN = b"WLP1"  # zlib payload, not encrypted


# --------------------------------------------------------------------------
# hashing / atomic writes
# --------------------------------------------------------------------------
def sha256_file(path: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


# --------------------------------------------------------------------------
# DPAPI (Windows Data Protection API) via ctypes - no pywin32 needed
# --------------------------------------------------------------------------
if sys.platform == "win32":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _dpapi(data: bytes, protect: bool) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        if protect:
            ok = _crypt32.CryptProtectData(
                ctypes.byref(blob_in), "worklog", None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
            )
        else:
            ok = _crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
            )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

else:  # pragma: no cover - non-Windows development only

    def _dpapi(data: bytes, protect: bool) -> bytes:
        raise OSError("DPAPI is only available on Windows")


def dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return _dpapi(_dpapi(b"worklog", True), False) == b"worklog"
    except OSError:
        return False


class TextVault:
    """Stores extracted text compressed, and DPAPI-encrypted when enabled."""

    def __init__(self, protect: bool):
        self.protect = bool(protect) and sys.platform == "win32"

    def dumps(self, text: str) -> bytes:
        payload = zlib.compress(text.encode("utf-8"), 6)
        if self.protect:
            return _MAGIC_DPAPI + _dpapi(payload, True)
        return _MAGIC_PLAIN + payload

    def loads(self, blob: bytes) -> str:
        magic, body = blob[:4], blob[4:]
        if magic == _MAGIC_DPAPI:
            body = _dpapi(body, False)
        elif magic != _MAGIC_PLAIN:
            raise ValueError("unknown text blob format")
        return zlib.decompress(body).decode("utf-8")

    def write(self, path: Path, text: str) -> None:
        atomic_write_bytes(path, self.dumps(text))

    def read(self, path: Path) -> str | None:
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        return self.loads(blob)


# --------------------------------------------------------------------------
# inter-process lock (released automatically when the process dies)
# --------------------------------------------------------------------------
class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def acquire_wait(self, timeout: float) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while True:
            if self.acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


class JobMarker:
    """Tells other processes that a long job (the baseline) is running, and since when.

    The running process holds a lock next to a small JSON file. A file whose lock is
    free was left by a process that died, so it never reports a job that is gone.
    """

    def __init__(self, ws: "Workspace", name: str):
        self.lock = FileLock(ws.lock_path(f"job-{name}"))
        self.info_path = ws.locks_dir / f"job-{name}.json"

    def begin(self, **info) -> None:
        if self.lock.acquire_wait(2):  # a probe may hold the lock for a moment
            atomic_write_text(self.info_path, json.dumps({"pid": os.getpid(), "started": time.time(), **info}))

    def end(self) -> None:
        try:
            self.info_path.unlink()
        except OSError:
            pass
        self.lock.release()

    @classmethod
    def probe(cls, ws: "Workspace", name: str) -> dict | None:
        """The running job's info, or None when no process runs it."""
        m = cls(ws, name)
        if not m.info_path.exists():
            return None
        if m.lock.acquire():
            m.lock.release()
            return None
        try:
            return json.loads(m.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


# --------------------------------------------------------------------------
# workspace layout
# --------------------------------------------------------------------------
class Workspace:
    def __init__(self, root: Path, reports_dir: str = ""):
        self.root = Path(root).resolve()
        self.dir = self.root / WORKLOG_DIRNAME
        self.config_path = self.dir / "config.toml"
        self.db_path = self.dir / "index.db"
        self.text_dir = self.dir / "text"
        self.diff_dir = self.dir / "diffs"
        self.media_dir = self.dir / "media"
        self.context_dir = self.dir / "context"
        self.notes_dir = self.dir / "notes"
        self.logs_dir = self.dir / "logs"
        self.locks_dir = self.dir / "locks"
        self.topics_path = self.dir / "topics.json"
        self.folder_map_path = self.context_dir / "folder_map.md"
        self.reports_dir = self.dir / "reports"
        self.set_reports_dir(reports_dir)

    def set_reports_dir(self, value: str) -> None:
        if value:
            p = Path(value).expanduser()
            if not p.is_absolute():
                p = self.root / p
            self.reports_dir = p.resolve()
        else:
            self.reports_dir = self.dir / "reports"

    def reports_dir_rel(self) -> str | None:
        """Reports folder relative to the root when it must be ignored by scans."""
        try:
            rel = self.reports_dir.relative_to(self.root).as_posix()
        except ValueError:
            return None
        if rel == "." or rel.split("/")[0] == WORKLOG_DIRNAME:
            return None
        return rel

    def abs(self, rel: str) -> Path:
        return self.root / Path(rel)

    def text_blob(self, rel: str) -> Path:
        h = hashlib.sha1(rel.encode("utf-8")).hexdigest()
        return self.text_dir / h[:2] / f"{h}.bin"

    def lock_path(self, name: str) -> Path:
        return self.locks_dir / f"{name}.lock"

    def to_rel(self, path: str, allow_root: bool = False) -> str:
        """Normalize a user/agent supplied path to a root-relative POSIX path.

        The work folder itself is an error unless ``allow_root``; then it is ".".
        """
        p = Path(str(path).strip().strip('"'))
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        try:
            rel = p.relative_to(self.root).as_posix()
        except ValueError:
            raise ValueError(f"path is outside the work folder: {path}") from None
        if rel == ".":
            if allow_root:
                return rel
            raise ValueError("path is the work folder itself")
        if rel.split("/")[0] == WORKLOG_DIRNAME:
            raise ValueError("the .worklog folder is internal to worklog")
        return rel


def hide_path(path: Path) -> None:
    """Set the Windows 'hidden' attribute (best effort)."""
    if sys.platform != "win32":
        return
    try:
        FILE_ATTRIBUTE_HIDDEN = 0x02
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and not attrs & FILE_ATTRIBUTE_HIDDEN:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass
