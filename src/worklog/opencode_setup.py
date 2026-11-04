"""Register worklog with opencode: MCP server entry, skill and slash commands."""

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


def opencode_config_dir() -> Path:
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".config" / "opencode"


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


def export_assets(dest: Path) -> list[str]:
    written = []
    skill = dest / "skills" / SKILL_NAME / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(asset_text("skill", SKILL_NAME, "SKILL.md"), encoding="utf-8")
    written.append(str(skill))
    for name in COMMANDS:
        p = dest / "commands" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(asset_text("commands", name), encoding="utf-8")
        written.append(str(p))
    return written


def setup_opencode(
    root: Path,
    config_path: Path | None = None,
    python_exe: str | None = None,
    install_skill: bool = True,
    install_commands: bool = True,
    remove: bool = False,
    timeout_ms: int = 120_000,
) -> dict:
    base = opencode_config_dir()
    if config_path is None:
        config_path = next(
            (p for p in (base / "opencode.jsonc", base / "opencode.json") if p.exists()), base / "opencode.json"
        )
    result: dict = {"config": str(config_path), "warnings": []}
    data: dict = {}
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = json.loads(strip_jsonc(raw))
            result["warnings"].append("the config had comments; they were removed (a backup was kept)")
        backup = config_path.with_name(f"{config_path.name}.bak-{dt.datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(config_path, backup)
        result["backup"] = str(backup)
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} does not contain a JSON object")

    mcp = data.setdefault("mcp", {})
    if remove:
        mcp.pop("worklog", None)
    else:
        mcp["worklog"] = {
            "type": "local",
            "command": [python_exe or python_for_mcp(), "-m", "worklog", "mcp", "--root", str(Path(root).resolve())],
            "enabled": True,
            "timeout": int(timeout_ms),
        }
        result["mcp"] = mcp["worklog"]
    if "$schema" not in data:
        data = {"$schema": "https://opencode.ai/config.json", **data}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    files: list[str] = []
    skill_path = base / "skills" / SKILL_NAME / "SKILL.md"
    cmd_paths = [base / "commands" / name for name in COMMANDS]
    if remove:
        for p in [skill_path, *cmd_paths]:
            if p.exists():
                p.unlink()
                files.append(f"removed {p}")
        if skill_path.parent.exists() and not any(skill_path.parent.iterdir()):
            skill_path.parent.rmdir()
    else:
        if install_skill:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(asset_text("skill", SKILL_NAME, "SKILL.md"), encoding="utf-8")
            files.append(str(skill_path))
        if install_commands:
            for name, p in zip(COMMANDS, cmd_paths):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(asset_text("commands", name), encoding="utf-8")
                files.append(str(p))
    result["files"] = files
    result["ok"] = True
    return result
