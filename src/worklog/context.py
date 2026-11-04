"""Context helpers for the agent: folder overview, related files, search, notes, folder map."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import atomic_write_text

if TYPE_CHECKING:  # pragma: no cover
    from .service import Worklog

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_STOP = {
    "최종", "최최종", "수정", "수정본", "사본", "복사본", "백업", "임시", "초안", "검토", "회신", "작성",
    "copy", "final", "draft", "new", "old", "backup", "temp", "rev", "ver", "version", "the", "and",
}
_VERSIONISH = re.compile(r"^(v|ver|rev|r)?\d+([._]\d+)*$|^\d{4,8}$")


def _tokens(path: str) -> set[str]:
    stem = Path(path).stem.lower()
    return {t for t in _TOKEN.findall(stem) if len(t) >= 2 and t not in _STOP and not _VERSIONISH.match(t)}


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).date().isoformat()


def overview(wl: "Worklog", path: str = "", depth: int = 2, limit: int = 150) -> dict:
    prefix = (wl.ws.to_rel(path) + "/") if path else ""
    depth = max(1, int(depth))
    groups: dict[str, dict] = {}
    total = 0
    for r in wl.con.execute("SELECT path, mtime FROM files WHERE present=1"):
        p = r["path"]
        if prefix and not p.startswith(prefix):
            continue
        total += 1
        dirs = p[len(prefix):].split("/")[:-1]
        key = "/".join(dirs[:depth])
        g = groups.setdefault(key, {"files": 0, "latest": 0.0, "exts": Counter()})
        g["files"] += 1
        g["latest"] = max(g["latest"], r["mtime"])
        g["exts"][Path(p).suffix.lower() or "(none)"] += 1
    folders = [
        {"folder": (prefix + key) if key else (prefix.rstrip("/") or "."),
         "files": g["files"], "latest_change": _day(g["latest"]),
         "top_types": dict(g["exts"].most_common(4))}
        for key, g in sorted(groups.items())
    ]
    return {"base": prefix.rstrip("/") or ".", "depth": depth, "total_files": total,
            "folders": folders[:limit], "truncated": len(folders) > limit}


def related(wl: "Worklog", path: str, limit: int = 10) -> dict:
    rel = wl.ws.to_rel(path)
    row = wl.file_row(rel)
    if row is not None:
        rel = row["path"]
    present = [(r["path"], r["mtime"]) for r in wl.con.execute("SELECT path, mtime FROM files WHERE present=1")]
    folder = _parent(rel)
    siblings = sorted(((p, m) for p, m in present if _parent(p) == folder and p != rel), key=lambda x: -x[1])
    toks = _tokens(rel)
    scored = []
    if toks:
        for p, m in present:
            if p == rel:
                continue
            s = len(toks & _tokens(p))
            if s:
                scored.append((s, m, p))
        scored.sort(key=lambda x: (-x[0], -x[1]))
    co = wl.con.execute(
        """
        SELECT e2.path AS path, SUM(1.0 / s.n_events) AS score, COUNT(DISTINCT e2.scan_id) AS times
        FROM scan_events e1
        JOIN scan_events e2 ON e2.scan_id = e1.scan_id AND e2.path != e1.path
        JOIN scans s ON s.id = e1.scan_id
        WHERE e1.path = ? AND e1.kind IN ('added', 'modified') AND e2.kind IN ('added', 'modified')
        GROUP BY e2.path ORDER BY score DESC LIMIT ?
        """,
        (rel, limit),
    ).fetchall()
    mentions = []
    for d in reversed(wl.reports.list_daily()):
        rep = wl.reports.get_daily(d["date"]) or {}
        for it in rep.get("items", []):
            if rel in it.get("files", []):
                mentions.append({"date": d["date"], "topic_id": it["topic_id"], "summary": it["summary"]})
        if len(mentions) >= limit:
            break
    topics = [
        {"id": t["id"], "name": t.get("name"), "status": t.get("status")}
        for t in wl.topics.list()
        if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in t.get("paths", []))
    ]
    return {
        "path": rel,
        "folder": folder or ".",
        "same_folder": [{"path": p, "modified": _day(m)} for p, m in siblings[:15]],
        "similar_names": [{"path": p, "shared_words": s, "modified": _day(m)} for s, m, p in scored[:limit]],
        "changed_together": [{"path": r["path"], "times": r["times"]} for r in co],
        "mentioned_in_reports": mentions[:limit],
        "topics_for_path": topics,
    }


def search(wl: "Worklog", query: str, limit: int = 20, path_prefix: str | None = None,
           time_budget: float = 20.0) -> dict:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "errors": ["query is empty"]}
    ql = q.lower()
    prefix = (wl.ws.to_rel(path_prefix) + "/") if path_prefix else ""
    names = [r["path"] for r in wl.con.execute("SELECT path FROM files WHERE present=1")
             if ql in r["path"].lower() and r["path"].startswith(prefix)]
    hits = []
    searched = 0
    t0 = time.monotonic()
    complete = True
    for r in wl.con.execute("SELECT path FROM files WHERE present=1 AND has_text=1 ORDER BY mtime DESC"):
        if prefix and not r["path"].startswith(prefix):
            continue
        if time.monotonic() - t0 > time_budget:
            complete = False
            break
        try:
            text = wl.vault.read(wl.ws.text_blob(r["path"]))
        except Exception:
            continue
        searched += 1
        if not text:
            continue
        low = text.lower()
        idx = low.find(ql)
        if idx < 0:
            continue
        snippet = text[max(0, idx - 80): idx + len(q) + 80].replace("\n", " ")
        hits.append({"path": r["path"], "matches": low.count(ql), "snippet": snippet})
    hits.sort(key=lambda h: -h["matches"])
    return {"ok": True, "query": q, "filename_matches": names[:limit], "content_matches": hits[:limit],
            "searched_files": searched, "complete": complete}


def note_add(wl: "Worklog", text: str, day: str | None = None) -> dict:
    if not text or not text.strip():
        return {"ok": False, "errors": ["text is empty"]}
    day = day or wl.cfg.today()
    try:
        day = dt.date.fromisoformat(day).isoformat()
    except ValueError:
        return {"ok": False, "errors": [f"invalid day {day!r}"]}
    path = wl.ws.notes_dir / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H:%M")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {' '.join(text.split())}\n")
    return {"ok": True, "day": day, "path": str(path)}


def notes_get(wl: "Worklog", day: str) -> dict:
    path = wl.ws.notes_dir / f"{day}.md"
    return {"day": day, "notes": path.read_text(encoding="utf-8") if path.exists() else ""}


def folder_map_get(wl: "Worklog") -> dict:
    p = wl.ws.folder_map_path
    return {"content": p.read_text(encoding="utf-8") if p.exists() else "",
            "exists": p.exists(),
            "hint": "" if p.exists() else "No folder map yet: build one from overview() and save it."}


def folder_map_save(wl: "Worklog", content: str) -> dict:
    if not content or not content.strip():
        return {"ok": False, "errors": ["content is empty"]}
    if len(content) > 20000:
        return {"ok": False, "errors": ["keep the folder map under 20,000 characters (one short line per folder)"]}
    atomic_write_text(wl.ws.folder_map_path, content.strip() + "\n")
    return {"ok": True, "path": str(wl.ws.folder_map_path)}


def media_description(wl: "Worklog", raw_hash: str | None) -> str | None:
    if not raw_hash:
        return None
    p = wl.ws.media_dir / f"{raw_hash}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("description")
    except (OSError, json.JSONDecodeError):
        return None
