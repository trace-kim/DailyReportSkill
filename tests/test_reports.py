import datetime as dt
from pathlib import Path

from conftest import backdate_tracking, set_mtime

PARAGRAPH = ("10월 28일 착수한 A사 제안서 초안 작성을 이어서 진행하였습니다. 요구사항을 반영하여 인건비 단가를 "
             "1,200만 원에서 1,350만 원으로 조정하였습니다. 다음 주 중 내부 검토를 진행할 예정입니다.")


def _yesterday_change(root: Path, wl, name="a.txt", text="v2"):
    """Record a change attributed to (logical) yesterday and return that day."""
    f = root / name
    yday = (dt.date.fromisoformat(wl.cfg.today()) - dt.timedelta(days=1)).isoformat()
    base = dt.datetime.combine(dt.date.fromisoformat(yday), dt.time(10, 0))
    backdate_tracking(wl, base)
    f.write_text(text, encoding="utf-8")
    set_mtime(f, base + dt.timedelta(hours=3))
    wl.close()
    return yday


def test_topics(make_wl):
    wl = make_wl()
    r = wl.topic_upsert(name="A사 제안서 작성", paths=["프로젝트\\A사"])
    assert r["ok"] and r["created"] and r["topic"]["id"] == "T001"
    assert r["topic"]["paths"] == ["프로젝트/A사"]
    assert wl.topic_upsert(name="A사 제안서 작성")["ok"] is False  # duplicate name
    assert wl.topic_upsert(topic_id="T001", status="nonsense")["ok"] is False
    assert wl.topic_upsert(topic_id="T001", status="reviewing")["topic"]["status"] == "reviewing"
    assert wl.topic_upsert(name="교육 자료 준비")["topic"]["id"] == "T002"


def test_daily_save_validation_and_render(root: Path, make_wl):
    (root / "a.txt").write_text("v1", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    day = _yesterday_change(root, wl)
    bad = wl.daily_save({"date": day, "items": [{"topic_id": "T009", "summary": "x"}]})
    assert bad["ok"] is False and "unknown topic" in bad["errors"][0]
    wl.topic_upsert(name="A사 제안서 작성")
    ok = wl.daily_save({
        "date": day,
        "items": [{"topic_id": "T001", "summary": "제안서 3장 신규 작성", "details": ["추진 일정 2쪽"],
                   "files": ["a.txt"], "status": "in_progress", "confidence": "high"}],
        "excluded": [{"summary": "다운로드 폴더 정리", "reason": "보고 대상 아님"}],
    })
    assert ok["ok"], ok
    md = Path(ok["path"]).read_text(encoding="utf-8")
    weekday = "월화수목금토일"[dt.date.fromisoformat(day).weekday()]
    assert md.startswith(f"# {day}({weekday}) 업무 일지")
    assert "## [T001] A사 제안서 작성 · 진행중" in md
    assert "- 확신도: 높음" in md and "## 보고 제외 변경" in md
    assert wl.status()["days_needing_report"] == []


def test_report_goes_stale_when_new_changes_arrive(root: Path, make_wl):
    (root / "a.txt").write_text("v1", encoding="utf-8")
    (root / "b.txt").write_text("b1", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    day = _yesterday_change(root, wl)
    wl.topic_upsert(name="작업")
    assert wl.daily_save({"date": day, "items": [{"topic_id": "T001", "summary": "a 수정"}]})["ok"]
    assert wl.status()["days_needing_report"] == []
    _yesterday_change(root, wl, name="b.txt", text="b2 longer")
    (entry,) = wl.status()["days_needing_report"]
    assert entry["day"] == day and entry["reason"].startswith("stale")


def test_weekly_save(root: Path, make_wl):
    (root / "a.txt").write_text("v1", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    day = _yesterday_change(root, wl)
    wl.topic_upsert(name="A사 제안서 작성")
    wl.topic_upsert(name="예산안 검토")
    wl.daily_save({"date": day, "items": [
        {"topic_id": "T001", "summary": "제안서 작성", "files": ["a.txt"]},
        {"topic_id": "T002", "summary": "예산안 검토"},
    ]})
    bad = wl.weekly_save({"period_from": day, "period_to": day, "topics": [
        {"topic_id": "T001", "subject": "A사 제안서 작성", "status": "in_progress",
         "paragraph": "- 제안서 작성 진행함.\n단가 조정함."},
        {"topic_id": "T002", "subject": "예산안 검토", "status": "reviewing", "paragraph": PARAGRAPH},
    ]})
    assert bad["ok"] is False
    joined = " ".join(bad["errors"])
    assert "line breaks" in joined and "bullet" in joined and "개조식" in joined and "repeat" in joined

    good = wl.weekly_save({"period_from": day, "period_to": day, "topics": [
        {"topic_ids": ["T001"], "subject": "A사 제안서 작성", "status": "in_progress", "paragraph": PARAGRAPH},
        {"topic_ids": ["T002"], "subject": "예산안", "status": "reviewing",
         "paragraph": "2027년 예산안 초안을 검토하였습니다. 부서별 요청 금액의 적정성을 확인 중입니다."},
    ]})
    assert good["ok"], good
    d = dt.date.fromisoformat(day).strftime("%Y.%m.%d")
    text = Path(good["path"]).read_text(encoding="utf-8")
    assert text == (
        f"주간 업무 보고 ({d} ~ {d})\n\n"
        f"1. A사 제안서 작성을 진행중입니다.\n{PARAGRAPH}\n\n"
        "2. 예산안을 검토중입니다.\n2027년 예산안 초안을 검토하였습니다. 부서별 요청 금액의 적정성을 확인 중입니다.\n"
    )
    saved = wl.report_get("weekly", good["key"])["report"]
    assert saved["topics"][0]["evidence"] == {"days": [day], "files": ["a.txt"]}
    assert wl.topics.get("T002")["status"] == "reviewing"
    assert wl.topics.get("T001")["last_reported"] == day
    nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    assert wl.status()["weekly"]["next_period_from"] == nxt
    assert wl.render("weekly", good["key"], "md")["content"].startswith("# 주간 업무 보고")


def test_notes_and_folder_map(make_wl):
    wl = make_wl()
    assert wl.note_add("A사 담당자와 일정 협의 미팅")["ok"]
    assert "일정 협의" in wl.notes_get(wl.cfg.today())["notes"]
    assert wl.folder_map_get()["exists"] is False
    assert wl.folder_map_save("- 프로젝트/A사/ : A사 제안 건")["ok"]
    assert "A사 제안 건" in wl.folder_map_get()["content"]
