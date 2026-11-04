"""`worklog doctor`: environment and DRM/Office readiness checks for a target PC."""

from __future__ import annotations

import ctypes
import datetime as dt
import getpass
import glob
import importlib
import importlib.metadata
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import Config
from .extract import COM_APPS, DIRECT, Extractor
from .extract.com import OfficeCom, com_available
from .ignore import IgnoreRules
from .scanner import walk
from .storage import WORKLOG_DIRNAME, dpapi_available

DOC_EXTENSIONS = frozenset(DIRECT) | frozenset(COM_APPS)
_WILDCARDS = ("*", "?", "[")

_DEPS = [
    ("docx", "python-docx"), ("openpyxl", "openpyxl"), ("pptx", "python-pptx"), ("pypdf", "pypdf"),
    ("PIL", "Pillow"), ("pathspec", "pathspec"), ("mcp", "mcp"), ("win32com.client", "pywin32"),
]


def pick_samples(paths: list[str], per_type: int = 3) -> tuple[list[Path], dict]:
    """Expand files, folders and wildcard patterns into the files to test.

    Folders are searched recursively for Office and PDF files (ignore rules apply,
    so Office lock files and ``.worklog`` are skipped); the ``per_type`` most
    recently modified files of each extension are kept (0 = all). Files named
    explicitly are always kept. Wildcards are expanded here because PowerShell
    passes them to programs unexpanded.
    """
    picked: list[Path] = []
    seen: set[str] = set()
    info: dict = {"searched": [], "not_found": [], "left_out": {}}

    def add(p: Path) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            picked.append(p)

    for raw in paths:
        p = Path(raw)
        if p.is_file():
            add(p)
        elif p.is_dir():
            info["searched"].append(str(p))
            by_ext: dict[str, list[tuple[float, Path]]] = {}
            try:
                for rel, _size, mtime in walk(p, IgnoreRules(p)):
                    ext = os.path.splitext(rel)[1].lower()
                    if ext in DOC_EXTENSIONS:
                        by_ext.setdefault(ext, []).append((mtime, p / rel))
            except OSError as e:
                info["not_found"].append(f"{raw} ({e})")
                continue
            for ext in sorted(by_ext):
                items = sorted(by_ext[ext], key=lambda t: t[0], reverse=True)
                keep = items[:per_type] if per_type > 0 else items
                for _mtime, fp in keep:
                    add(fp)
                if len(items) > len(keep):
                    info["left_out"][ext] = info["left_out"].get(ext, 0) + len(items) - len(keep)
        elif any(ch in raw for ch in _WILDCARDS):
            matches = [Path(m) for m in sorted(glob.glob(raw, recursive=True)) if Path(m).is_file()]
            if not matches:
                info["not_found"].append(raw)
            for m in matches:
                add(m)
        else:
            info["not_found"].append(raw)
    return picked, info


def _session() -> dict:
    info: dict = {"user": getpass.getuser()}
    if sys.platform == "win32":
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            info["session_id"] = sid.value
            info["interactive"] = sid.value != 0
    return info


def _write_test(root: Path) -> dict:
    target = root / WORKLOG_DIRNAME if (root / WORKLOG_DIRNAME).is_dir() else root
    out: dict = {"dir": str(target)}
    txt = target / "doctor-write-test.txt"
    payload = f"worklog write test 한글 {time.time()}"
    try:
        txt.write_text(payload, encoding="utf-8")
        time.sleep(1.0)  # give an on-access DRM agent time to act
        out["text_roundtrip"] = txt.read_bytes().decode("utf-8", errors="replace") == payload
    except OSError as e:
        out["text_roundtrip"] = False
        out["error"] = str(e)
    finally:
        try:
            txt.unlink()
        except OSError:
            pass
    try:
        import docx

        dx = target / "doctor-write-test.docx"
        d = docx.Document()
        d.add_paragraph("worklog write test")
        d.save(str(dx))
        time.sleep(1.0)
        out["docx_still_zip"] = zipfile.is_zipfile(dx)
        dx.unlink()
    except Exception as e:
        out["docx_error"] = f"{type(e).__name__}: {e}"
    return out


def _verdicts(r: dict) -> list[str]:
    v: list[str] = []
    if r["session"].get("session_id") == 0:
        v.append("Running in session 0 (background): Office COM and DRM clients usually fail here. "
                 "Scheduled tasks must use 'Run only when user is logged on'.")
    missing = [k for k, val in r["dependencies"].items() if str(val).startswith("MISSING")]
    if missing:
        v.append(f"Missing Python packages: {', '.join(missing)}")
    if r.get("dpapi") != "ok":
        v.append("DPAPI unavailable: set protect_text = false or check the Windows profile.")
    for app, info in (r.get("office") or {}).items():
        if "error" in info:
            v.append(f"{app}: cannot be automated ({info['error']}). Files of this type will fall back to "
                     "direct parsing only.")
    s = r.get("samples") or {}
    for missing_path in s.get("not_found", []):
        v.append(f"Not found: {missing_path}")
    if s.get("searched") and not r.get("files"):
        v.append(f"No Office or PDF files found in {', '.join(s['searched'])}. Copy a few work files there "
                 "or pass file paths.")
    if s.get("left_out"):
        more = ", ".join(f"{n} {ext}" for ext, n in sorted(s["left_out"].items()))
        v.append(f"Tested the {s.get('per_type')} most recently modified files of each type; left out {more} "
                 "(--per-type 0 tests all).")
    for f in r.get("files", []):
        d, c = f.get("direct", {}), f.get("com", {})
        name = Path(f["path"]).name
        if "error" in f and not d:
            v.append(f"{name}: {f['error']}")
        elif d.get("kind") == "text":
            v.append(f"{name}: readable directly ({d.get('chars', 0)} chars).")
        elif c.get("kind") == "text":
            v.append(f"{name}: not readable directly (DRM or legacy format) but readable through Office "
                     f"({c.get('chars', 0)} chars, {c.get('seconds')}s).")
        else:
            reasons = " | ".join(str(x.get("error")) for x in (d, c) if x.get("error"))
            v.append(f"{name}: NOT readable. {reasons}")
    w = r.get("write_test")
    if w:
        if w.get("text_roundtrip") is False or w.get("docx_still_zip") is False:
            v.append("Files written by Python in the work folder change after writing (DRM auto-encryption?). "
                     "Set [reports] dir to a folder outside the DRM scope.")
        else:
            v.append("Files written by Python in the work folder stay readable.")
    if not v:
        v.append("No problems found.")
    return v


def run_doctor(root: Path | None, paths: list[str], preview: bool = True, com: bool = True,
               per_type: int = 3) -> dict:
    """Environment checks, then direct and Office reads of each sample file.

    ``paths`` may hold files, folders or wildcard patterns; with none given, the
    work folder ``root`` is searched for samples.
    """
    targets = list(paths) or ([str(root)] if root is not None else [])
    files, samples = pick_samples(targets, per_type)
    cfg = Config()
    if root is not None and (root / WORKLOG_DIRNAME / "config.toml").exists():
        cfg = Config.load(root / WORKLOG_DIRNAME / "config.toml")
    report: dict = {
        "worklog": __version__,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "session": _session(),
    }
    deps = {}
    for module, dist in _DEPS:
        try:
            importlib.import_module(module)
            deps[dist] = importlib.metadata.version(dist)
        except Exception as e:
            deps[dist] = f"MISSING: {type(e).__name__}"
    report["dependencies"] = deps
    report["dpapi"] = "ok" if dpapi_available() else "unavailable"

    use_com = com and com_available() and cfg.com_enabled
    if use_com:
        apps = {}
        office = OfficeCom(timeout_sec=cfg.com_timeout_sec)
        try:
            for key in ("word", "excel", "powerpoint"):
                t0 = time.monotonic()
                try:
                    info = office.app_info(key)
                    info["start_seconds"] = round(time.monotonic() - t0, 2)
                    apps[key] = info
                except Exception as e:
                    apps[key] = {"error": str(e)}
        finally:
            office.shutdown()
        report["office"] = apps

    report["samples"] = {**samples, "per_type": per_type, "picked": len(files)}
    entries = []
    with Extractor(cfg) as ex:
        for p in files:
            entry: dict = {"path": str(p)}
            try:
                with p.open("rb") as fh:
                    head = fh.read(16)
            except OSError as e:
                entry["error"] = f"cannot open: {e}"
                entries.append(entry)
                continue
            entry.update(size=p.stat().st_size, header_hex=head.hex(" "), is_zip=zipfile.is_zipfile(p))
            for method in ("direct", "com") if use_com else ("direct",):
                t0 = time.monotonic()
                res = ex.extract(p, method=method)
                r: dict = {"kind": res.kind, "seconds": round(time.monotonic() - t0, 2)}
                if res.kind == "text":
                    r["chars"] = len(res.text or "")
                    if preview:
                        r["preview"] = (res.text or "")[:120].replace("\n", " ")
                if res.error:
                    r["error"] = res.error
                entry[method] = r
            entries.append(entry)
    report["files"] = entries
    if root is not None:
        report["write_test"] = _write_test(root)
    report["verdicts"] = _verdicts(report)
    return report
