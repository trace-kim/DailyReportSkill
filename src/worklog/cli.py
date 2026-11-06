"""Command line interface: ``worklog <command> --root <work folder>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__

log = logging.getLogger("worklog")


def _setup_logging(data_dir: Path | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("pypdf", "PIL", "mcp", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if data_dir is not None:
        add_file_log(data_dir)


def add_file_log(data_dir: Path) -> None:
    """Log to ``<data_dir>/logs/worklog.log`` (once per process; no-op until the folder exists)."""
    if not data_dir.is_dir():
        return
    root = logging.getLogger()
    path = (data_dir / "logs" / "worklog.log").resolve()
    if any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path for h in root.handlers):
        return
    try:
        path.parent.mkdir(exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(process)d] %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:
        pass


def _print(obj) -> None:
    if sys.stdout is None:  # pythonw / worklogw: no console
        return
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if not text.endswith("\n"):
        text += "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    sys.stdout.flush()


def _ok(obj) -> bool:
    return not (isinstance(obj, dict) and obj.get("ok") is False)


def _opencode_report_command(model: str | None = None) -> list[str] | None:
    """Unattended daily-report run: `opencode run [--model M] --command daily-report --auto auto`.

    --auto approves tool permissions that are not explicitly denied, so the
    scheduled run never stalls on a prompt nobody can answer. Without --model,
    opencode uses the model last picked in its TUI, which may not call tools.
    """
    import shutil

    exe = shutil.which("opencode")
    if not exe:
        return None
    return [exe, "run", *(["--model", model] if model else []), "--command", "daily-report", "--auto", "auto"]


def changes_text(wl, day: str | None = None, max_lines: int = 200) -> str:
    """Every change of a day with its diff, readable in a console."""
    from .service import format_diff

    res = wl.changes(day, limit=100_000)
    counts = ", ".join(f"{k} {n}" for k, n in res["summary"].items() if n)
    out = [f"=== {res['day']}({res['weekday']}): {res['total']} change(s)" + (f" [{counts}]" if counts else "")]
    for c in res["changes"]:
        out.append("")
        d = wl.diff(c["id"], max_lines=max_lines)
        out.append(format_diff(d).rstrip() if d.get("ok") else f"# change {c['id']}: {d.get('errors')}")
    if res["touched_only"]:
        out += ["", "=== edited this day, net change recorded elsewhere or not yet:"]
        out += [f"  {t['path']} ({t['seen']}): {t['note']}" for t in res["touched_only"]]
    if not res["total"] and not res["touched_only"]:
        out += ["", "No changes recorded. Run 'worklog close' first if files were edited."]
    return "\n".join(out)


def latest_report_key(wl, kind: str) -> str:
    """Key of the most recent saved daily or weekly report."""
    from .service import WorklogError

    if kind == "daily":
        reports = wl.reports.list_daily()
        if reports:
            return reports[-1]["date"]
    else:
        reports = [w for w in wl.reports.list_weekly() if w.get("period_to")]
        if reports:
            return max(reports, key=lambda w: w["period_to"])["key"]
    raise WorklogError(f"no {kind} report saved yet")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=os.environ.get("WORKLOG_ROOT"),
                        help="work folder to track (default: $WORKLOG_ROOT or the current folder)")
    common.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(prog="worklog", description="Track work-folder changes and write Korean work reports.")
    p.add_argument("--version", action="version", version=f"worklog {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", parents=[common], help="initialize tracking for a work folder (first scan)")
    s.add_argument("--report", choices=["keep", "none", "opencode", "auto"], default="keep",
                   help="agent started by 'morning' to write daily logs: opencode, auto (opencode if found), "
                        "none, or keep the configured one (default)")
    s.add_argument("--model", metavar="PROVIDER/MODEL",
                   help="model for the unattended opencode run (with --report opencode or auto); list them with "
                        "`opencode models`. Without it opencode uses the model last picked in its TUI")
    s.add_argument("--report-command", help='custom JSON array instead of --report, e.g. ["my-agent","run","/daily-report"]')
    sub.add_parser("scan", parents=[common], help="hourly metadata scan (size/mtime only)")
    s = sub.add_parser("close", parents=[common], help="close out changes: extract text, store diffs")
    s.add_argument("--budget", type=float, default=None, help="time budget in seconds (default: unlimited)")
    s = sub.add_parser("baseline", parents=[common], help="capture text of files that existed at install")
    s.add_argument("--budget", type=float, default=None, help="time budget in seconds (default: unlimited)")
    sub.add_parser("morning", parents=[common], help="scheduled routine: close, baseline, run report command")
    sub.add_parser("status", parents=[common], help="tracking and report status")
    s = sub.add_parser("changes", parents=[common], help="changes recorded for a day")
    s.add_argument("day", nargs="?", help="YYYY-MM-DD (default: today)")
    s.add_argument("--diffs", action="store_true", help="print every change with its diff, as text")
    s = sub.add_parser("diff", parents=[common], help="diff of one change record")
    s.add_argument("change_id", type=int)
    s.add_argument("--max-lines", type=int, default=400)
    s = sub.add_parser("read", parents=[common], help="current text of a file")
    s.add_argument("path")
    s.add_argument("--max-chars", type=int, default=20000)
    s = sub.add_parser("search", parents=[common], help="search file names and stored document text")
    s.add_argument("query")
    s = sub.add_parser("render", parents=[common], help="print a saved report")
    s.add_argument("kind", choices=["daily", "weekly"])
    s.add_argument("key", nargs="?", help="date for daily, FROM_TO for weekly (default: the latest one)")
    s.add_argument("--format", default="text", choices=["text", "md"])
    sub.add_parser("mcp", parents=[common], help="run the MCP server on stdio")
    s = sub.add_parser("doctor", parents=[common], help="check Python, Office/COM, DRM readability")
    s.add_argument("paths", nargs="*",
                   help="files, folders or wildcard patterns to test (default: sample the --root folder)")
    s.add_argument("--per-type", type=int, default=3,
                   help="files tested per extension when searching a folder (default 3, 0 = all)")
    s.add_argument("--out", help="also write the JSON report to this file")
    s.add_argument("--no-preview", action="store_true", help="do not include text previews")
    s.add_argument("--no-com", action="store_true", help="skip Office automation checks")
    s = sub.add_parser("setup-opencode", parents=[common], help="register MCP server, skill and commands in opencode")
    s.add_argument("--workspace", nargs="?", const="", metavar="DIR",
                   help="register in a workspace folder instead of the global opencode config: DIR/opencode.jsonc "
                        "and DIR/.opencode/ (what DS Cowork reads). DIR defaults to the --root folder")
    s.add_argument("--config", help="opencode config file (default: opencode.jsonc or opencode.json in "
                                    "~/.config/opencode, or in the workspace with --workspace)")
    s.add_argument("--python", help="python.exe used to launch the MCP server (default: this interpreter)")
    s.add_argument("--no-skill", action="store_true")
    s.add_argument("--no-commands", action="store_true")
    s.add_argument("--timeout-ms", type=int, default=120_000)
    s.add_argument("--remove", action="store_true", help="unregister instead")
    s = sub.add_parser("export-assets", help="copy the skill and command files to a folder (for other agents)")
    s.add_argument("dest")
    return p


def main(argv: list[str] | None = None) -> None:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    if args.cmd == "export-assets":
        from .opencode_setup import export_assets

        _print({"ok": True, "written": export_assets(Path(args.dest))})
        return

    root = Path(args.root or os.getcwd()).expanduser().resolve()
    _setup_logging(root / ".worklog", args.verbose)

    if args.cmd == "doctor":
        from .doctor import run_doctor

        report = run_doctor(root if (args.root or (root / ".worklog").exists()) else None, args.paths,
                            preview=not args.no_preview, com=not args.no_com, per_type=args.per_type)
        if args.out:
            Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _print(report)
        return

    if args.cmd == "setup-opencode":
        from .opencode_setup import setup_opencode

        workspace = None
        if args.workspace is not None:
            workspace = Path(args.workspace).expanduser().resolve() if args.workspace else root
        _print(setup_opencode(root, Path(args.config) if args.config else None, args.python,
                              not args.no_skill, not args.no_commands, args.remove, args.timeout_ms,
                              workspace=workspace))
        return

    from .service import Worklog, WorklogError, format_diff

    try:
        if args.cmd == "mcp":
            from .mcp_server import run

            run(str(root))
            return
        if args.cmd == "init":
            cmd = None
            warnings: list[str] = []
            if args.model is not None:
                if args.report_command or args.report not in ("opencode", "auto"):
                    raise WorklogError("--model needs --report opencode or --report auto")
                if "/" not in args.model:
                    raise WorklogError(f"--model must be PROVIDER/MODEL with a forward slash, got {args.model!r} "
                                       "(`opencode models` lists them)")
            if args.report_command:
                cmd = json.loads(args.report_command)
                if not (isinstance(cmd, list) and all(isinstance(c, str) for c in cmd)):
                    raise WorklogError("--report-command must be a JSON array of strings")
            elif args.report in ("opencode", "auto"):
                cmd = _opencode_report_command(args.model)
                if cmd is None:
                    if args.report == "opencode":
                        raise WorklogError("opencode was not found on PATH")
                    warnings.append("opencode is not on PATH: 'morning' closes out changes but starts no agent. "
                                    "With DS Cowork, schedule the daily report in the app instead.")
                    cmd = []
                elif not args.model:
                    warnings.append("no --model: the scheduled `opencode run` uses the model last picked in the "
                                    "opencode TUI, and a model that cannot call tools writes nothing. Run init "
                                    "again with --model PROVIDER/MODEL (see `opencode models`).")
            elif args.report == "none":
                cmd = []
            _wl, result = Worklog.init(root, cmd)
            for w in warnings:
                log.warning("init: %s", w)
            if warnings:
                result["warnings"] = warnings
            _print(result)
            return
        wl = Worklog(root)
        if args.cmd == "scan":
            result = wl.scan()
        elif args.cmd == "close":
            result = wl.close(args.budget)
        elif args.cmd == "baseline":
            result = wl.baseline(args.budget)
        elif args.cmd == "morning":
            result = wl.morning()
        elif args.cmd == "status":
            result = wl.status()
        elif args.cmd == "changes":
            result = changes_text(wl, args.day) if args.diffs else wl.changes(args.day)
        elif args.cmd == "diff":
            d = wl.diff(args.change_id, max_lines=args.max_lines)
            result = format_diff(d) if d.get("ok") else d
        elif args.cmd == "read":
            r = wl.read_file(args.path, max_chars=args.max_chars)
            result = r["text"] if r.get("kind") == "text" else r
        elif args.cmd == "search":
            result = wl.search(args.query)
        elif args.cmd == "render":
            r = wl.render(args.kind, args.key or latest_report_key(wl, args.kind), args.format)
            result = r["content"] if r.get("ok") else r
        else:  # pragma: no cover
            raise WorklogError(f"unknown command {args.cmd}")
        if _ok(result):
            log.info("%s: ok", args.cmd)
        else:
            log.warning("%s: failed: %s", args.cmd,
                        json.dumps(result, ensure_ascii=False, default=str)[:4000])
        _print(result)
        if not _ok(result):
            sys.exit(1)
    except WorklogError as e:
        log.error("%s", e)
        if sys.stderr is not None:
            print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
