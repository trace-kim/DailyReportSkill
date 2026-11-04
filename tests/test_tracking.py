import datetime as dt
import os
from pathlib import Path

from conftest import backdate_tracking, make_docx, set_mtime
from worklog.config import Config
from worklog.extract import ExtractResult


def _today(wl):
    return wl.cfg.today()


def _kinds(wl, day=None):
    return [(c["kind"], c["path"]) for c in wl.changes(day or _today(wl))["changes"]]


def test_initial_scan_records_inventory_without_events(root: Path, make_wl):
    (root / "a.txt").write_text("hello", encoding="utf-8")
    make_docx(root / "docs" / "b.docx", ["본문"])
    wl = make_wl()
    st = wl.status()
    assert st["tracked_files"] == 2
    assert st["baseline_remaining"] == 2
    assert st["pending_close"]["changed"] == 0
    assert wl.changes(_today(wl))["total"] == 0


def test_ignore_rules(root: Path, make_wl):
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("x")
    (root / "~$보고서.docx").write_bytes(b"lock")
    (root / "keep.txt").write_text("keep")
    (root / "private").mkdir()
    (root / "private" / "diary.txt").write_text("secret")
    (root / ".worklogignore").write_text("private/\n", encoding="utf-8")
    wl = make_wl()
    paths = [r["path"] for r in wl.con.execute("SELECT path FROM files")]
    assert paths == ["keep.txt"]


def test_modify_after_baseline_gives_diff(root: Path, make_wl):
    f = root / "메모.txt"
    f.write_text("첫 줄\n둘째 줄\n", encoding="utf-8")
    wl = make_wl()
    assert wl.baseline()["processed"] == 1
    f.write_text("첫 줄\n둘째 줄 수정\n셋째 줄\n", encoding="utf-8")
    res = wl.close()
    assert res["changes"] == 1 and res["remaining"] == 0
    (change,) = wl.changes(_today(wl))["changes"]
    assert change["kind"] == "modified" and change["lines"] == "+2 -1"
    d = wl.diff(change["id"])
    assert "+둘째 줄 수정" in d["diff"] and "-둘째 줄" in d["diff"] and "+셋째 줄" in d["diff"]
    # The stored text is encrypted on Windows (DPAPI) and not readable as plain text.
    blob = wl.ws.text_blob("메모.txt").read_bytes()
    assert "셋째".encode("utf-8") not in blob


def test_new_docx_is_added_with_content(root: Path, make_wl):
    wl = make_wl()
    make_docx(root / "프로젝트" / "제안서.docx", ["추진 일정"], [["단계", "기간"], ["착수", "11월"]])
    wl.close()
    (change,) = wl.changes(_today(wl))["changes"]
    assert change["kind"] == "added" and change["reader"] == "text/direct"
    d = wl.diff(change["id"])
    assert "+추진 일정" in d["diff"] and "+착수 | 11월" in d["diff"]
    assert "new file" in d["note"]


def test_resave_without_text_change(root: Path, make_wl):
    f = root / "notes.csv"
    f.write_bytes("이름,금액\n인건비,1350\n".encode("cp949"))
    wl = make_wl()
    wl.baseline()
    f.write_bytes("이름,금액\n인건비,1350\n".encode("utf-8"))  # same text, different bytes
    wl.close()
    assert _kinds(wl) == [("resaved", "notes.csv")]


def test_touch_only_is_not_a_change(root: Path, make_wl):
    f = root / "a.txt"
    f.write_text("same", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    set_mtime(f, dt.datetime.now() + dt.timedelta(seconds=5))
    res = wl.close()
    assert res["changes"] == 0


def test_rename_and_delete(root: Path, make_wl):
    (root / "old").mkdir()
    (root / "old" / "a.txt").write_text("내용 A", encoding="utf-8")
    (root / "b.txt").write_text("내용 B\n두 번째", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    (root / "new").mkdir()
    os.replace(root / "old" / "a.txt", root / "new" / "a.txt")
    (root / "b.txt").unlink()
    wl.close()
    kinds = {c["kind"]: c for c in wl.changes(_today(wl))["changes"]}
    assert kinds["renamed"]["path"] == "new/a.txt" and kinds["renamed"]["old_path"] == "old/a.txt"
    assert kinds["deleted"]["path"] == "b.txt"
    d = wl.diff(kinds["deleted"]["id"])
    assert "-내용 B" in d["diff"]
    # the renamed file keeps its text, so its next change is a real diff
    (root / "new" / "a.txt").write_text("내용 A 수정", encoding="utf-8")
    wl.close()
    mod = [c for c in wl.changes(_today(wl))["changes"] if c["kind"] == "modified"]
    assert mod and not mod[0].get("no_baseline")


def test_unreadable_folder_is_not_reported_as_deleted(root: Path, make_wl, monkeypatch):
    (root / "locked").mkdir()
    (root / "locked" / "a.txt").write_text("내용", encoding="utf-8")
    (root / "b.txt").write_text("other", encoding="utf-8")
    wl = make_wl()
    wl.baseline()

    import worklog.scanner as scanner

    real_scandir = os.scandir

    def flaky_scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(scanner.os, "scandir", flaky_scandir)
    res = wl.scan()
    assert res["deleted"] == 0 and res["unreadable_folders"] == ["locked"]
    wl.close()
    assert _kinds(wl) == []
    monkeypatch.undo()
    # once the folder is readable again nothing has changed
    assert wl.scan()["deleted"] == 0


def test_move_before_baseline_is_detected_by_size_and_mtime(root: Path, make_wl):
    (root / "a.txt").write_text("never extracted", encoding="utf-8")
    wl = make_wl()  # no baseline
    (root / "sub").mkdir()
    os.replace(root / "a.txt", root / "sub" / "a.txt")
    wl.close()
    assert _kinds(wl) == [("renamed", "sub/a.txt")]
    assert wl.baseline_remaining() == 1  # still waiting for its baseline


def test_change_before_baseline_is_flagged(root: Path, make_wl):
    f = root / "a.txt"
    f.write_text("v1", encoding="utf-8")
    wl = make_wl()
    f.write_text("v2 longer", encoding="utf-8")
    wl.close()
    (change,) = wl.changes(_today(wl))["changes"]
    assert change["kind"] == "modified" and change["no_baseline"] is True
    assert "no earlier version" in wl.diff(change["id"])["note"]


def test_copied_file_with_old_mtime_counts_today(root: Path, make_wl):
    wl = make_wl()
    f = root / "archive.txt"
    f.write_text("old document", encoding="utf-8")
    set_mtime(f, dt.datetime(2019, 3, 1, 10, 0))
    wl.close()
    assert _kinds(wl) == [("added", "archive.txt")]


def test_change_yesterday_is_attributed_to_yesterday(root: Path, make_wl):
    f = root / "a.txt"
    f.write_text("v1", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    # "yesterday" in logical days, so the test also passes between 00:00 and 04:00
    yday = (dt.date.fromisoformat(wl.cfg.today()) - dt.timedelta(days=1)).isoformat()
    yesterday_10 = dt.datetime.combine(dt.date.fromisoformat(yday), dt.time(10, 0))
    backdate_tracking(wl, yesterday_10)
    f.write_text("v2 changed", encoding="utf-8")
    set_mtime(f, yesterday_10 + dt.timedelta(hours=5))
    wl.close()
    assert _kinds(wl, yday) == [("modified", "a.txt")]
    assert [d["day"] for d in wl.status()["days_needing_report"]] == [yday]


def test_day_boundary():
    cfg = Config()  # boundary 04:00
    assert cfg.logical_day(dt.datetime(2026, 11, 5, 2, 30).timestamp()) == "2026-11-04"
    assert cfg.logical_day(dt.datetime(2026, 11, 5, 4, 0).timestamp()) == "2026-11-05"
    assert cfg.day_end("2026-11-04") == dt.datetime(2026, 11, 5, 4, 0).timestamp()


def test_budget_leaves_remaining_for_next_call(root: Path, make_wl):
    wl = make_wl()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
    # A zero budget still processes one file per call, so the agent always makes progress.
    first = wl.close(budget_seconds=0)
    assert first["budget_hit"] and first["remaining"] == 2 and first["changes"] == 1
    assert wl.status()["pending_close"]["changed"] == 2
    second = wl.close()
    assert second["remaining"] == 0 and second["changes"] == 2


class _FlakyExtractor:
    """Fails the first time (like a DRM client that is not ready), then works."""

    calls = 0

    def __init__(self, cfg):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def extract(self, path, method=None):
        _FlakyExtractor.calls += 1
        if _FlakyExtractor.calls == 1:
            return ExtractResult("failed", method="metadata", error="com: timed out")
        return ExtractResult("text", text="복구된 본문", method="com")


def test_failed_extraction_is_retried_and_fills_the_record(root: Path, make_wl, monkeypatch):
    import worklog.closeout as closeout

    wl = make_wl()
    monkeypatch.setattr(closeout, "Extractor", _FlakyExtractor)
    make_docx(root / "drm.docx", ["x"])
    wl.close()
    (change,) = wl.changes(_today(wl))["changes"]
    assert change["error"] == "com: timed out"
    assert wl.status()["pending_close"]["retry_failed"] == 1
    res = wl.close()
    assert res["revised_earlier_changes"] == 1
    (change,) = wl.changes(_today(wl))["changes"]
    assert "error" not in change and change["has_diff"]
    assert "+복구된 본문" in wl.diff(change["id"])["diff"]


def test_read_file_does_not_overwrite_the_baseline(root: Path, make_wl):
    f = root / "a.txt"
    f.write_text("before", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    f.write_text("after edit", encoding="utf-8")
    r = wl.read_file("a.txt")
    assert r["text"] == "after edit" and r["source"].startswith("fresh")
    wl.close()
    (change,) = wl.changes(_today(wl))["changes"]
    d = wl.diff(change["id"])["diff"]
    assert "-before" in d and "+after edit" in d


def test_read_file_rejects_outside_paths(root: Path, make_wl):
    wl = make_wl()
    assert wl.read_file("../outside.txt")["ok"] is False
    assert wl.read_file(".worklog/config.toml")["ok"] is False


def test_related_and_search(root: Path, make_wl):
    make_docx(root / "A사" / "제안서_v1.docx", ["A사 사업 제안 개요"])
    make_docx(root / "A사" / "예산.docx", ["인건비 산정"])
    make_docx(root / "기타" / "제안서_최종.docx", ["다른 제안"])
    wl = make_wl()
    wl.baseline()
    rel = wl.related("A사/제안서_v1.docx")
    assert [s["path"] for s in rel["same_folder"]] == ["A사/예산.docx"]
    assert rel["similar_names"][0]["path"] == "기타/제안서_최종.docx"
    found = wl.search("인건비")
    assert [h["path"] for h in found["content_matches"]] == ["A사/예산.docx"]
    ov = wl.overview()
    assert {f["folder"] for f in ov["folders"]} == {"A사", "기타"}
