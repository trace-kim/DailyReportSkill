"""Daily work logs and weekly reports: validation, storage (JSON = source of truth) and rendering."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .korean import build_title, check_paragraph
from .storage import atomic_write_text

if TYPE_CHECKING:  # pragma: no cover
    from .service import Worklog

SCHEMA = 1
_WEEKDAYS = "월화수목금토일"
_CONFIDENCE = {"high": "높음", "medium": "보통", "low": "낮음"}
_PHRASE_TAIL = re.compile(r"(하였습니다|했습니다|입니다|합니다)$")


def _iso_day(value: Any, field: str, errors: list[str]) -> str | None:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        errors.append(f"{field}: expected a date like 2026-11-04, got {value!r}")
        return None


def _str_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{field}: expected a list of strings")
        return []
    return [v.strip() for v in value if v.strip()]


def _single_line(value: Any, field: str, errors: list[str], required: bool = True) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors.append(f"{field}: required")
        return ""
    if not isinstance(value, str):
        errors.append(f"{field}: expected a string")
        return ""
    v = " ".join(value.split())
    return v


def parse_report(report: Any) -> dict:
    if isinstance(report, str):
        report = json.loads(report)
    if not isinstance(report, dict):
        raise ValueError("report must be a JSON object")
    return report


class Reports:
    def __init__(self, wl: "Worklog"):
        self.wl = wl

    @property
    def daily_dir(self) -> Path:
        return self.wl.ws.reports_dir / "daily"

    @property
    def weekly_dir(self) -> Path:
        return self.wl.ws.reports_dir / "weekly"

    def status_label(self, status: str | None) -> str:
        phrase = self.wl.cfg.status_phrases.get(status or "", "")
        return _PHRASE_TAIL.sub("", phrase) if phrase else (status or "")

    # ==================================================================
    # daily
    # ==================================================================
    def validate_daily(self, rep: dict) -> tuple[dict, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        topics = self.wl.topics.all()
        statuses = self.wl.cfg.status_phrases
        day = _iso_day(rep.get("date"), "date", errors)
        items_in = rep.get("items") or []
        if not isinstance(items_in, list):
            errors.append("items: expected a list")
            items_in = []
        items = []
        seen_topics: set[str] = set()
        for i, it in enumerate(items_in):
            f = f"items[{i}]"
            if not isinstance(it, dict):
                errors.append(f"{f}: expected an object")
                continue
            tid = str(it.get("topic_id") or "").strip()
            if not tid:
                errors.append(f"{f}.topic_id: required (create topics with topic_upsert)")
            elif tid not in topics:
                errors.append(f"{f}.topic_id: unknown topic {tid!r}; create it with topic_upsert first")
            elif tid in seen_topics:
                errors.append(f"{f}.topic_id: {tid} appears twice; merge the items of one topic into one item")
            seen_topics.add(tid)
            status = it.get("status")
            if status is not None and status not in statuses:
                errors.append(f"{f}.status: unknown status {status!r}; use one of {list(statuses)}")
            conf = str(it.get("confidence") or "medium")
            if conf not in _CONFIDENCE:
                errors.append(f"{f}.confidence: use high, medium or low")
            items.append({
                "topic_id": tid,
                "topic_name": topics.get(tid, {}).get("name", ""),
                "summary": _single_line(it.get("summary"), f"{f}.summary", errors),
                "details": _str_list(it.get("details"), f"{f}.details", errors),
                "files": _str_list(it.get("files"), f"{f}.files", errors),
                "context": _single_line(it.get("context"), f"{f}.context", errors, required=False),
                "status": status or topics.get(tid, {}).get("status"),
                "confidence": conf,
            })
        excluded_in = rep.get("excluded") or []
        excluded = []
        if not isinstance(excluded_in, list):
            errors.append("excluded: expected a list")
            excluded_in = []
        for i, ex in enumerate(excluded_in):
            f = f"excluded[{i}]"
            if isinstance(ex, str):
                ex = {"summary": ex}
            if not isinstance(ex, dict):
                errors.append(f"{f}: expected an object")
                continue
            excluded.append({
                "summary": _single_line(ex.get("summary"), f"{f}.summary", errors),
                "files": _str_list(ex.get("files"), f"{f}.files", errors),
                "reason": _single_line(ex.get("reason"), f"{f}.reason", errors, required=False),
            })
        no_work = bool(rep.get("no_work", False))
        if not items and not excluded and not no_work:
            errors.append('empty report: add items, excluded entries, or set "no_work": true')
        norm = {"schema": SCHEMA, "kind": "daily", "date": day, "items": items, "excluded": excluded,
                "no_work": no_work and not items}
        return norm, errors, warnings

    def save_daily(self, report: Any) -> dict:
        try:
            rep = parse_report(report)
        except (ValueError, json.JSONDecodeError) as e:
            return {"ok": False, "errors": [str(e)]}
        norm, errors, warnings = self.validate_daily(rep)
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}
        day = norm["date"]
        rows = self.wl.con.execute("SELECT id, path, old_path FROM changes WHERE day=?", (day,)).fetchall()
        changed_paths = {r["path"] for r in rows} | {r["old_path"] for r in rows if r["old_path"]}
        mentioned = {p for it in norm["items"] for p in it["files"]} | {p for ex in norm["excluded"] for p in ex["files"]}
        unknown = sorted(p for p in mentioned if p not in changed_paths)
        if unknown:
            warnings.append(f"files not among {day}'s recorded changes (fine if intended): {unknown[:5]}")
        now = time.time()
        norm["generated_at"] = dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")
        norm["generated_ts"] = now
        norm["covered_change_ids"] = sorted(r["id"] for r in rows)
        path = self.daily_dir / f"{day}.json"
        atomic_write_text(path, json.dumps(norm, ensure_ascii=False, indent=2))
        md_path = path.with_suffix(".md")
        atomic_write_text(md_path, self.render_daily(norm))
        return {"ok": True, "path": str(md_path), "json": str(path), "warnings": warnings}

    def get_daily(self, day: str) -> dict | None:
        path = self.daily_dir / f"{day}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_daily(self, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        out = []
        if not self.daily_dir.exists():
            return out
        for p in sorted(self.daily_dir.glob("*.json")):
            day = p.stem
            if (date_from and day < date_from) or (date_to and day > date_to):
                continue
            try:
                rep = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "date": day,
                "items": [{"topic_id": it["topic_id"], "topic_name": it.get("topic_name", ""),
                           "summary": it["summary"], "status": it.get("status")} for it in rep.get("items", [])],
                "no_work": rep.get("no_work", False),
            })
        return out

    def render_daily(self, rep: dict) -> str:
        d = dt.date.fromisoformat(rep["date"])
        lines = [f"# {d.isoformat()}({_WEEKDAYS[d.weekday()]}) 업무 일지", ""]
        if rep.get("no_work") and not rep.get("items"):
            lines += ["기록된 업무 변경 없음", ""]
        for it in rep.get("items", []):
            label = self.status_label(it.get("status"))
            head = f"## [{it['topic_id']}] {it.get('topic_name') or ''}".rstrip()
            lines.append(f"{head} · {label}" if label else head)
            lines.append(it["summary"])
            for detail in it.get("details", []):
                lines.append(f"- {detail}")
            if it.get("files"):
                lines.append("- 관련 파일: " + ", ".join(f"`{p}`" for p in it["files"]))
            if it.get("context"):
                lines.append(f"- 맥락: {it['context']}")
            lines.append(f"- 확신도: {_CONFIDENCE.get(it.get('confidence', 'medium'), '보통')}")
            lines.append("")
        if rep.get("excluded"):
            lines.append("## 보고 제외 변경")
            for ex in rep["excluded"]:
                extra = f" ({ex['reason']})" if ex.get("reason") else ""
                files = (" — " + ", ".join(f"`{p}`" for p in ex["files"][:5])) if ex.get("files") else ""
                lines.append(f"- {ex['summary']}{extra}{files}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # ==================================================================
    # weekly
    # ==================================================================
    def validate_weekly(self, rep: dict) -> tuple[dict, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        topics = self.wl.topics.all()
        phrases = self.wl.cfg.status_phrases
        p_from = _iso_day(rep.get("period_from"), "period_from", errors)
        p_to = _iso_day(rep.get("period_to"), "period_to", errors)
        if p_from and p_to and p_from > p_to:
            errors.append("period_from is after period_to")
        entries_in = rep.get("topics")
        if not isinstance(entries_in, list) or not entries_in:
            errors.append("topics: a non-empty list is required")
            entries_in = []
        entries = []
        used: set[str] = set()
        for i, t in enumerate(entries_in):
            f = f"topics[{i}]"
            if not isinstance(t, dict):
                errors.append(f"{f}: expected an object")
                continue
            ids = t.get("topic_ids")
            if ids is None and t.get("topic_id"):
                ids = [t["topic_id"]]
            ids = _str_list(ids, f"{f}.topic_ids", errors)
            if not ids:
                errors.append(f"{f}: topic_id (or topic_ids when merging topics) is required")
            for tid in ids:
                if tid not in topics:
                    errors.append(f"{f}: unknown topic {tid!r}")
                elif tid in used:
                    errors.append(f"{f}: topic {tid} is used by more than one entry")
                used.add(tid)
            status = t.get("status")
            if status not in phrases:
                errors.append(f"{f}.status: use one of {list(phrases)}")
            particle = t.get("particle")
            subject = _single_line(t.get("subject"), f"{f}.subject", errors)
            title = None
            if subject and status in phrases:
                title, terrs = build_title(subject, phrases[status], particle)
                errors += [f"{f}.subject: {e}" for e in terrs]
            paragraph = t.get("paragraph")
            perrs, pwarns = check_paragraph(paragraph if isinstance(paragraph, str) else "")
            errors += [f"{f}.paragraph: {e}" for e in perrs]
            warnings += [f"{f}.paragraph: {w}" for w in pwarns]
            entries.append({
                "topic_ids": ids,
                "subject": subject,
                "status": status,
                "particle": particle,
                "title": title,
                "paragraph": paragraph.strip() if isinstance(paragraph, str) else "",
            })
        norm = {"schema": SCHEMA, "kind": "weekly", "period_from": p_from, "period_to": p_to,
                "title": self.wl.cfg.weekly_title, "author": self.wl.cfg.author, "topics": entries}
        return norm, errors, warnings

    def save_weekly(self, report: Any) -> dict:
        try:
            rep = parse_report(report)
        except (ValueError, json.JSONDecodeError) as e:
            return {"ok": False, "errors": [str(e)]}
        norm, errors, warnings = self.validate_weekly(rep)
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}
        dailies = [self.get_daily(d["date"]) for d in self.list_daily(norm["period_from"], norm["period_to"])]
        if not dailies:
            warnings.append("no daily reports exist in this period")
        for entry in norm["topics"]:
            days, files = [], []
            for rep_d in dailies:
                for it in (rep_d or {}).get("items", []):
                    if it["topic_id"] in entry["topic_ids"]:
                        days.append(rep_d["date"])
                        files += [p for p in it.get("files", []) if p not in files]
            entry["evidence"] = {"days": sorted(set(days)), "files": files[:20]}
            if not days:
                warnings.append(f"{entry['topic_ids']}: no daily report in the period mentions this topic")
        now = time.time()
        norm["generated_at"] = dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")
        key = f"{norm['period_from']}_{norm['period_to']}"
        path = self.weekly_dir / f"{key}.json"
        atomic_write_text(path, json.dumps(norm, ensure_ascii=False, indent=2))
        txt_path = path.with_suffix(".txt")
        text = self.render_weekly_text(norm)
        atomic_write_text(txt_path, text)
        self.wl.topics.mark_reported(
            {tid: e["status"] for e in norm["topics"] for tid in e["topic_ids"]}, norm["period_to"]
        )
        return {"ok": True, "key": key, "path": str(txt_path), "json": str(path), "text": text,
                "warnings": warnings}

    def get_weekly(self, key: str) -> dict | None:
        path = self.weekly_dir / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_weekly(self) -> list[dict]:
        out = []
        if not self.weekly_dir.exists():
            return out
        for p in sorted(self.weekly_dir.glob("*.json")):
            try:
                rep = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"key": p.stem, "period_from": rep.get("period_from"), "period_to": rep.get("period_to"),
                        "topics": [e.get("title") for e in rep.get("topics", [])],
                        "generated_at": rep.get("generated_at")})
        return out

    @staticmethod
    def _period(rep: dict) -> str:
        a = dt.date.fromisoformat(rep["period_from"]).strftime("%Y.%m.%d")
        b = dt.date.fromisoformat(rep["period_to"]).strftime("%Y.%m.%d")
        return f"{a} ~ {b}"

    def render_weekly_text(self, rep: dict) -> str:
        lines = [f"{rep.get('title') or self.wl.cfg.weekly_title} ({self._period(rep)})"]
        if rep.get("author"):
            lines.append(f"작성자: {rep['author']}")
        lines.append("")
        for i, e in enumerate(rep["topics"], 1):
            lines.append(f"{i}. {e['title']}")
            lines.append(e["paragraph"])
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def render_weekly_md(self, rep: dict) -> str:
        lines = [f"# {rep.get('title') or self.wl.cfg.weekly_title} ({self._period(rep)})", ""]
        if rep.get("author"):
            lines += [f"작성자: {rep['author']}", ""]
        for i, e in enumerate(rep["topics"], 1):
            lines += [f"## {i}. {e['title']}", "", e["paragraph"], ""]
        return "\n".join(lines).rstrip() + "\n"

    def weekly_state(self, today: str) -> dict:
        weeklies = [w for w in self.list_weekly() if w.get("period_to")]
        last = max(weeklies, key=lambda w: w["period_to"]) if weeklies else None
        days = [d["date"] for d in self.list_daily()]
        if last:
            next_from = (dt.date.fromisoformat(last["period_to"]) + dt.timedelta(days=1)).isoformat()
        else:
            next_from = days[0] if days else today
        pending = [d for d in days if d >= next_from]
        state = {
            "last_period": {"from": last["period_from"], "to": last["period_to"]} if last else None,
            "next_period_from": next_from,
            "suggested_period_to": pending[-1] if pending else None,
            "daily_reports_in_next_period": len(pending),
        }
        if last:
            state["days_since_last_weekly"] = (
                dt.date.fromisoformat(today) - dt.date.fromisoformat(last["period_to"])
            ).days
        return state

    # ==================================================================
    def render(self, kind: str, key: str, fmt: str = "text") -> dict:
        if kind == "daily":
            rep = self.get_daily(key)
            if rep is None:
                return {"ok": False, "errors": [f"no daily report for {key}"]}
            if fmt not in ("md", "text"):
                return {"ok": False, "errors": ["daily reports render as 'md' (or 'text', same output)"]}
            return {"ok": True, "format": "md", "content": self.render_daily(rep)}
        if kind == "weekly":
            rep = self.get_weekly(key)
            if rep is None:
                return {"ok": False, "errors": [f"no weekly report {key!r}; keys look like 2026-11-02_2026-11-13"]}
            if fmt == "text":
                return {"ok": True, "format": "text", "content": self.render_weekly_text(rep)}
            if fmt == "md":
                return {"ok": True, "format": "md", "content": self.render_weekly_md(rep)}
            return {"ok": False, "errors": ["supported formats: text, md (email/Confluence renderers come later)"]}
        return {"ok": False, "errors": ["kind must be 'daily' or 'weekly'"]}
