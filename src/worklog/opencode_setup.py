"""Register worklog with opencode: MCP server entry, skill and slash commands.

Two scopes:

* global: the opencode config dir (``~/.config/opencode`` or ``$OPENCODE_CONFIG_DIR``).
* workspace: one project folder, as ``<ws>/opencode.jsonc`` plus ``<ws>/.opencode/skills`` and
  ``<ws>/.opencode/commands``. DS Cowork reads only this scope: its bundled opencode uses a
  config dir of its own (``~/.openwork/...``) that the app rewrites on every start.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sys
from importlib import resources
from pathlib import Path

SKILL_NAME = "worklog"
COMMANDS = ("daily-report.md", "weekly-report.md")
SCHEMA_URL = "https://opencode.ai/config.json"


def opencode_config_dir() -> Path:
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "opencode"


def default_backup_dir() -> Path:
    """Where backups of rewritten opencode configs go.

    Outside every work folder on purpose: a backup next to a workspace config
    would be tracked as a work file.
    """
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "worklog" / "backups"


def asset_text(*parts: str) -> str:
    return resources.files("worklog").joinpath("assets", *parts).read_text(encoding="utf-8")


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments (outside strings) and trailing commas."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def python_for_mcp() -> str:
    """The console interpreter of the current environment (pythonw has no usable stdio)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _asset_paths(dest: Path) -> tuple[Path, list[Path]]:
    return dest / "skills" / SKILL_NAME / "SKILL.md", [dest / "commands" / name for name in COMMANDS]


def _write_assets(dest: Path, skill: bool = True, commands: bool = True) -> list[str]:
    skill_path, cmd_paths = _asset_paths(dest)
    jobs = ([(skill_path, ("skill", SKILL_NAME, "SKILL.md"))] if skill else []) + (
        [(p, ("commands", name)) for name, p in zip(COMMANDS, cmd_paths)] if commands else [])
    written = []
    for path, parts in jobs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(asset_text(*parts), encoding="utf-8")
        written.append(str(path))
    return written


def export_assets(dest: Path) -> list[str]:
    return _write_assets(dest)


def _backup(config_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    label = config_path.parent.name or "root"
    backup = backup_dir / f"{label}_{config_path.name}.{dt.datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(config_path, backup)
    return backup


def setup_opencode(
    root: Path,
    config_path: Path | None = None,
    python_exe: str | None = None,
    install_skill: bool = True,
    install_commands: bool = True,
    remove: bool = False,
    timeout_ms: int = 120_000,
    workspace: Path | None = None,
    backup_dir: Path | None = None,
) -> dict:
    """Add (or with ``remove`` take out) the MCP entry, the skill and the commands.

    ``workspace`` switches from the global config dir to that folder. The config
    is only rewritten when its content changes, and a backup of the old file
    goes to ``backup_dir`` (default: :func:`default_backup_dir`).
    """
    if workspace is not None:
        config_dir = Path(workspace).expanduser().resolve()
        assets_dir, default_name = config_dir / ".opencode", "opencode.jsonc"
    else:
        config_dir = assets_dir = opencode_config_dir()
        default_name = "opencode.json"
    if config_path is None:
        config_path = next((p for p in (config_dir / "opencode.jsonc", config_dir / "opencode.json") if p.exists()),
                           config_dir / default_name)
    result: dict = {"scope": "workspace" if workspace is not None else "global",
                    "config": str(config_path), "warnings": []}

    raw: str | None = None
    data: dict = {}
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = json.loads(strip_jsonc(raw))
            result["warnings"].append("the config had comments; they were removed (a backup was kept)")
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} does not contain a JSON object")

    if not remove:
        mcp = data.setdefault("mcp", {})
        mcp["worklog"] = {
            "type": "local",
            "command": [python_exe or python_for_mcp(), "-m", "worklog", "mcp", "--root", str(Path(root).resolve())],
            "enabled": True,
            "timeout": int(timeout_ms),
        }
        result["mcp"] = mcp["worklog"]
        if "$schema" not in data:
            data = {"$schema": SCHEMA_URL, **data}
    elif isinstance(data.get("mcp"), dict):
        data["mcp"].pop("worklog", None)
    if raw is not None or not remove:  # removing from a missing config: nothing to write
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if text == raw:
            result["unchanged"] = True
        else:
            if raw is not None:
                result["backup"] = str(_backup(config_path, backup_dir or default_backup_dir()))
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(text, encoding="utf-8")

    files: list[str] = []
    if remove:
        skill_path, cmd_paths = _asset_paths(assets_dir)
        for p in [skill_path, *cmd_paths]:
            if p.exists():
                p.unlink()
                files.append(f"removed {p}")
        prune = [skill_path.parent]
        if workspace is not None:  # also drop the .opencode folders if nothing else uses them
            prune += [skill_path.parent.parent, cmd_paths[0].parent, assets_dir]
        for d in prune:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    else:
        files = _write_assets(assets_dir, install_skill, install_commands)
        if workspace is not None:
            result["next"] = ("Reload the workspace in DS Cowork (or restart opencode in that folder) "
                              "so it loads the worklog MCP server, skill and commands.")
        elif (Path.home() / ".openwork").is_dir():
            result["warnings"].append(
                "DS Cowork is installed: it does not read this global config (its bundled opencode uses "
                "~/.openwork/... and rewrites it on every start). For DS Cowork, run setup-opencode "
                "--workspace in the folder it opens as its workspace.")
    result["files"] = files
    result["ok"] = True
    return result
