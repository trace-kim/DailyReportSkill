"""`worklog doctor`: environment and DRM/Office readiness checks for a target PC."""

from __future__ import annotations

import ctypes
import datetime as dt
import getpass
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
from .extract import Extractor
from .extract.com import OfficeCom, com_available
from .storage import WORKLOG_DIRNAME, dpapi_available

_DEPS = [
    ("docx", "python-docx"), ("openpyxl", "openpyxl"), ("pptx", "python-pptx"), ("pypdf", "pypdf"),
    ("PIL", "Pillow"), ("pathspec", "pathspec"), ("mcp", "mcp"), ("win32com.client", "pywin32"),
]


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


def run_doctor(root: Path | None, files: list[str], preview: bool = True, com: bool = True) -> dict:
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

    entries = []
    with Extractor(cfg) as ex:
        for f in files:
            p = Path(f)
            entry: dict = {"path": str(p)}
            if not p.is_file():
                entry["error"] = "not found"
                entries.append(entry)
                continue
            with p.open("rb") as fh:
                head = fh.read(16)
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
