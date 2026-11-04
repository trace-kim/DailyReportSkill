"""Exclusion rules: built-in defaults + ``<root>/.worklogignore`` (gitignore syntax)."""

from __future__ import annotations

from pathlib import Path

import pathspec

DEFAULT_IGNORE = [
    # worklog itself and agent tooling
    ".worklog/",
    ".worklogignore",
    ".opencode/",
    ".claude/",
    ".agents/",
    "opencode.json",
    "opencode.jsonc",
    "opencode.json*.bak-*",  # config backups left by worklog 0.1.0 setup-opencode
    # version control / dev environments
    ".git/",
    ".svn/",
    ".hg/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".idea/",
    ".vscode/",
    # Office lock files and temp files
    "~$*",
    "~*.tmp",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.asd",  # Word AutoRecover
    "*.wbk",  # Word backup copy
    ".~lock.*#",
    # Windows clutter
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    "*.lnk",
    "$RECYCLE.BIN/",
    "System Volume Information/",
]


class IgnoreRules:
    def __init__(self, root: Path, extra: list[str] | None = None):
        lines = list(DEFAULT_IGNORE) + list(extra or [])
        user_file = Path(root) / ".worklogignore"
        if user_file.exists():
            lines += user_file.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        self.spec = pathspec.GitIgnoreSpec.from_lines(lines)

    def ignored(self, rel: str, is_dir: bool) -> bool:
        return self.spec.match_file(rel + "/" if is_dir else rel)
