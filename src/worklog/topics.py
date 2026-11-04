"""Work topics with stable IDs (``.worklog/topics.json``, editable by hand)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable

from .storage import atomic_write_text


class TopicStore:
    def __init__(self, path: Path, statuses: Iterable[str]):
        self.path = path
        self.statuses = list(statuses)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "topics": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("next_id", 1)
        data.setdefault("topics", {})
        return data

    def _save(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def all(self) -> dict[str, dict]:
        return self._load()["topics"]

    def get(self, topic_id: str) -> dict | None:
        return self.all().get(topic_id)

    def exists(self, topic_id: str) -> bool:
        return topic_id in self.all()

    def list(self, status: str | None = None) -> list[dict]:
        out = []
        for tid, t in sorted(self.all().items()):
            if status and t.get("status") != status:
                continue
            out.append({"id": tid, **t})
        return out

    def upsert(
        self,
        topic_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        paths: list[str] | None = None,
        aliases: list[str] | None = None,
        today: str | None = None,
    ) -> dict:
        today = today or dt.date.today().isoformat()
        data = self._load()
        topics = data["topics"]
        if status is not None and status not in self.statuses:
            return {"ok": False, "errors": [f"unknown status {status!r}; use one of {self.statuses}"]}
        created = False
        if topic_id:
            if topic_id not in topics:
                return {"ok": False, "errors": [f"unknown topic id {topic_id!r}; omit topic_id to create one"]}
            t = topics[topic_id]
        else:
            if not name or not name.strip():
                return {"ok": False, "errors": ["name is required to create a topic"]}
            dup = [tid for tid, t in topics.items() if t.get("name", "").strip() == name.strip()]
            if dup:
                return {"ok": False, "errors": [f"a topic named {name!r} already exists: {dup[0]}"]}
            topic_id = f"T{int(data['next_id']):03d}"
            data["next_id"] = int(data["next_id"]) + 1
            t = {"name": name.strip(), "status": "in_progress", "created": today, "paths": [], "aliases": []}
            topics[topic_id] = t
            created = True
        if name is not None and name.strip():
            t["name"] = name.strip()
        if description is not None:
            t["description"] = description.strip()
        if status is not None:
            t["status"] = status
        if paths is not None:
            t["paths"] = sorted({p.strip().replace("\\", "/") for p in paths if p and p.strip()})
        if aliases is not None:
            t["aliases"] = sorted({a.strip() for a in aliases if a and a.strip()})
        t["updated"] = today
        self._save(data)
        return {"ok": True, "created": created, "topic": {"id": topic_id, **t}}

    def mark_reported(self, updates: dict[str, str], reported_on: str) -> None:
        """Weekly save: record each topic's reported status and date."""
        data = self._load()
        for tid, status in updates.items():
            t = data["topics"].get(tid)
            if t is None:
                continue
            t["status"] = status
            t["last_reported"] = reported_on
            t["updated"] = reported_on
        self._save(data)
