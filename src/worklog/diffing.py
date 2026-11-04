"""Line diffs of extracted text."""

from __future__ import annotations

import difflib


def make_diff(old: str, new: str, path: str, context: int = 2) -> tuple[str, int, int]:
    """Unified diff of two texts -> (diff text, lines added, lines removed)."""
    a = old.splitlines() if old else []
    b = new.splitlines() if new else []
    lines = list(
        difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=context)
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return "\n".join(lines), added, removed
