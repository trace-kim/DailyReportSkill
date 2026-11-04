import datetime as dt
from pathlib import Path

import pytest
from conftest import set_mtime
from worklog.cli import changes_text, latest_report_key
from worklog.doctor import pick_samples
from worklog.service import WorklogError


def test_doctor_samples_newest_files_of_each_type(tmp_path: Path):
    folder = tmp_path / "업무"
    (folder / "sub").mkdir(parents=True)
    now = dt.datetime.now()
    for i, name in enumerate(["a.docx", "b.docx", "sub/c.docx", "d.docx"]):
        p = folder / name
        p.write_bytes(b"x")
        set_mtime(p, now - dt.timedelta(days=10 - i))  # a oldest, d newest
    (folder / "예산.xlsx").write_bytes(b"x")
    (folder / "[최종] 보고.pptx").write_bytes(b"x")
    (folder / "notes.txt").write_text("not an Office file")
    (folder / "~$lock.docx").write_bytes(b"lock")  # Office lock file: ignored
    (folder / ".worklog").mkdir()
    (folder / ".worklog" / "z.docx").write_bytes(b"x")  # worklog's own folder: ignored

    files, info = pick_samples([str(folder)], per_type=3)
    names = sorted(p.name for p in files)
    assert names == ["[최종] 보고.pptx", "b.docx", "c.docx", "d.docx", "예산.xlsx"]
    assert info["left_out"] == {".docx": 1}

    # explicit files (even with [ ] in the name), wildcards, and missing paths
    files, info = pick_samples([str(folder / "[최종] 보고.pptx"), str(folder / "*.xlsx"),
                                str(folder / "없음.docx")])
    assert [p.name for p in files] == ["[최종] 보고.pptx", "예산.xlsx"]
    assert info["not_found"] == [str(folder / "없음.docx")]

    files, _ = pick_samples([str(folder)], per_type=0)
    assert len(files) == 6


def test_changes_text_prints_every_diff(root: Path, make_wl):
    (root / "메모.txt").write_text("첫 줄\n", encoding="utf-8")
    (root / "old.txt").write_text("지울 내용", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    (root / "메모.txt").write_text("첫 줄\n추가한 줄\n", encoding="utf-8")
    (root / "old.txt").unlink()
    wl.close()
    text = changes_text(wl)
    assert "2 change(s)" in text
    assert "modified · 메모.txt" in text and "+추가한 줄" in text
    assert "deleted · old.txt" in text and "-지울 내용" in text


def test_changes_text_without_changes(root: Path, make_wl):
    wl = make_wl()
    assert "No changes recorded" in changes_text(wl)


def test_latest_report_key(root: Path, make_wl):
    wl = make_wl()
    with pytest.raises(WorklogError):
        latest_report_key(wl, "daily")
    wl.topic_upsert(name="A사 제안서 작성")
    today = dt.date.fromisoformat(wl.cfg.today())
    for day in (today - dt.timedelta(days=1), today):
        assert wl.daily_save({"date": day.isoformat(), "items": [{"topic_id": "T001", "summary": "작성"}]})["ok"]
    assert latest_report_key(wl, "daily") == today.isoformat()
    with pytest.raises(WorklogError):
        latest_report_key(wl, "weekly")
