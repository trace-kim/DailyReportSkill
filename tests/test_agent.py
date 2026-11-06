"""Agent mode (MCP): every call returns in time, long work moves to the background, nothing queues."""

import datetime as dt
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from conftest import set_mtime, write_config
from worklog.closeout import close_waiting
from worklog.config import Config
from worklog.extract import Extractor
from worklog.service import Worklog, WorklogBusy, same_folder, task_root
from worklog.storage import FileLock, JobMarker


def _agent(wl: Worklog) -> Worklog:
    wl.agent_mode = True
    wl.lock_wait = 0.3
    return wl


def _scans(wl: Worklog) -> int:
    return wl.con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]


def _age_scans(wl: Worklog, seconds: float = 300) -> None:
    wl.con.execute("UPDATE scans SET taken_at = taken_at - ?", (seconds,))
    wl.con.commit()


def test_status_before_init_starts_nothing(root: Path):
    write_config(root)
    wl = _agent(Worklog(root, create=True))
    st = wl.status()
    assert st["initialized"] is False and any("call init" in h for h in st["hints"])
    assert _scans(wl) == 0
    assert wl.close(30)["ok"] is False  # close does not start tracking behind the user's back
    r = wl.start_tracking(wait=10)
    assert r["initialized"] and not r["already_initialized"] and _scans(wl) == 1
    assert wl.start_tracking(wait=10)["already_initialized"] is True and _scans(wl) == 1
    wl.close_db()


def test_close_loop_walks_the_folder_once(root: Path, make_wl):
    wl = _agent(make_wl())
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
    _age_scans(wl)
    first = wl.close(budget_seconds=0)  # one file per call
    assert first["remaining"] == 2 and not first["scan"].get("reused")
    n = _scans(wl)
    _age_scans(wl, 120)  # older than the plain reuse window: only the continuation rule keeps it
    second = wl.close(budget_seconds=0)
    third = wl.close(budget_seconds=0)
    assert second["scan"]["reused"] and third["scan"]["reused"] and _scans(wl) == n
    assert (second["remaining"], third["remaining"]) == (1, 0)


def test_slow_scan_runs_in_the_background(root: Path, make_wl, monkeypatch):
    import worklog.service as service

    wl = _agent(make_wl())
    (root / "a.txt").write_text("new", encoding="utf-8")
    _age_scans(wl)
    gate = threading.Event()
    real = service.scan_folder

    def slow_scan(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "scan_folder", slow_scan)
    monkeypatch.setattr(service, "STATUS_SCAN_WAIT", 0.2)
    t0 = time.monotonic()
    busy = wl.close(budget_seconds=1)
    assert busy["ok"] is False and busy["busy"] and busy["scanning"]
    st = wl.status()  # answers with the previous scan's data instead of waiting
    assert st["scan_running"] is not None and time.monotonic() - t0 < 5
    gate.set()
    wl._scan_job.done.wait(10)
    done = wl.close(budget_seconds=5)
    assert done["ok"] and done["by_kind"] == {"added": 1}


def test_a_call_answers_busy_instead_of_queueing(root: Path, make_wl, monkeypatch):
    import worklog.context as context

    wl = _agent(make_wl())
    release = threading.Event()
    monkeypatch.setattr(context, "search", lambda *a, **k: release.wait(10) and {"ok": True})
    holder = threading.Thread(target=wl.search, args=("x",))
    holder.start()
    time.sleep(0.1)
    try:
        with pytest.raises(WorklogBusy, match="'search'"):
            wl.status()
    finally:
        release.set()
        holder.join()
    assert wl.status()["initialized"]  # free again


def test_close_hashes_files_within_the_budget(root: Path, make_wl, monkeypatch):
    import worklog.closeout as closeout

    wl = make_wl()
    for i in range(20):
        (root / f"f{i:02d}.txt").write_text(f"file {i}", encoding="utf-8")
    hashed = []
    real = closeout.sha256_file
    monkeypatch.setattr(closeout, "sha256_file", lambda p: hashed.append(p) or real(p))
    res = wl.close(budget_seconds=0)
    assert len(hashed) == 1 and res["remaining"] == 19


def test_new_file_of_the_same_size_is_not_a_rename(root: Path, make_wl):
    (root / "a.txt").write_text("AAAA", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    (root / "a.txt").unlink()
    (root / "b.txt").write_text("BBBB", encoding="utf-8")
    wl.close()
    kinds = sorted((c["kind"], c["path"]) for c in wl.changes(wl.cfg.today())["changes"])
    assert kinds == [("added", "b.txt"), ("deleted", "a.txt")]


def test_baseline_skips_files_untouched_for_long(root: Path, make_wl):
    old = root / "2019 보고서.txt"
    old.write_text("old", encoding="utf-8")
    set_mtime(old, dt.datetime.now() - dt.timedelta(days=800))
    (root / "recent.txt").write_text("recent", encoding="utf-8")
    wl = make_wl()
    assert wl.baseline_remaining() == 1 and wl.baseline_skipped_old() == 1
    assert wl.baseline()["processed"] == 1 and wl.baseline_remaining() == 0


def test_baseline_pauses_for_a_waiting_close(root: Path, make_wl):
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
    wl = make_wl()
    marker = wl.ws.locks_dir / "close.wanted"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    res = wl.baseline()
    assert res["processed"] == 1 and res["paused_for_close"] and res["remaining"] == 2


def test_close_asks_a_running_baseline_to_pause(root: Path, make_wl):
    wl = make_wl()
    held = threading.Event()
    seen = []

    def long_baseline():  # holds the shared lock like a baseline, pauses once asked
        lock = FileLock(wl.ws.lock_path("close"))
        lock.acquire()
        held.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not close_waiting(wl.ws):
            time.sleep(0.05)
        seen.append(close_waiting(wl.ws))
        lock.release()

    t = threading.Thread(target=long_baseline)
    t.start()
    held.wait(5)
    res = wl.close()
    t.join()
    assert seen == [True] and res["ok"] is True
    assert not close_waiting(wl.ws)  # the marker is gone again


def test_background_baseline_resumes_after_a_close(root: Path, make_wl, monkeypatch):
    import worklog.closeout as closeout

    for i in range(3):
        (root / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
    wl = make_wl()
    first_file = threading.Event()
    real = closeout.sha256_file

    def slow_in_baseline(p):
        if threading.current_thread().name == "baseline":
            first_file.set()
            time.sleep(0.3)  # long enough for the close-out to ask for the lock mid-file
        return real(p)

    monkeypatch.setattr(closeout, "sha256_file", slow_in_baseline)
    out = {}

    def background():  # its own connection, like the spawned process
        other = Worklog(root)
        out["res"] = other.baseline(budget_seconds=30)
        other.close_db()

    t = threading.Thread(target=background, name="baseline")
    t.start()
    assert first_file.wait(10)
    (root / "new.txt").write_text("new", encoding="utf-8")
    closed = wl.close()
    t.join(30)
    assert closed["ok"] and closed["by_kind"] == {"added": 1}
    res = out["res"]
    assert res["resumed_after_close"] == 1 and res["processed"] == 3 and res["remaining"] == 0, res
    assert "paused_for_close" not in res


def test_job_marker(root: Path, make_wl):
    wl = make_wl()
    assert JobMarker.probe(wl.ws, "baseline") is None
    m = JobMarker(wl.ws, "baseline")
    m.begin(budget_seconds=60)
    info = JobMarker.probe(wl.ws, "baseline")
    assert info["budget_seconds"] == 60 and wl.jobs_state()["baseline"]["running"]
    m.end()
    assert JobMarker.probe(wl.ws, "baseline") is None
    m.info_path.write_text("{}", encoding="utf-8")  # left behind by a process that died
    assert JobMarker.probe(wl.ws, "baseline") is None


def test_baseline_and_doctor_run_in_background_processes(root: Path, make_wl):
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "b.txt").write_text("world", encoding="utf-8")
    wl = _agent(make_wl())
    started = wl.start_baseline(minutes=1)
    assert started["started"] and started["remaining"] == 2
    assert wl.jobs_state()["baseline"]["running"]  # at once, before the process sets its marker
    assert wl.start_baseline(minutes=1)["running"]  # no second process
    assert wl._procs["baseline"][0].wait(60) == 0
    assert wl.baseline_remaining() == 0
    assert wl.start_baseline(minutes=1)["remaining"] == 0  # nothing left: nothing started

    res = wl.doctor_job(wait=90)
    assert res["ok"] and res["verdicts"] and res["checked_at"], res
    assert wl.doctor_job()["checked_at"] == res["checked_at"]  # the stored result, no new run


def test_extractor_deadline_caps_office_waits(tmp_path: Path, monkeypatch):
    import worklog.extract as extract_mod

    seen = {}

    class FakeCom:
        def extract(self, app, path, timeout=None):
            seen["timeout"] = timeout
            return "본문"

        def shutdown(self, wait=30):
            seen["shutdown_wait"] = wait

    monkeypatch.setattr(extract_mod, "com_available", lambda: True)
    cfg = Config()
    cfg.com_enabled = True
    doc = tmp_path / "옛 문서.doc"
    doc.write_bytes(b"legacy")
    with Extractor(cfg, deadline=time.monotonic() + 10) as ex:
        ex._com = FakeCom()
        assert ex.extract(doc).method == "com"
    assert 8 < seen["timeout"] <= 10 and seen["shutdown_wait"] == Extractor.SHUTDOWN_WAIT_LIMITED
    with Extractor(cfg) as ex:  # CLI: no cap
        ex._com = FakeCom()
        ex.extract(doc)
    assert seen["timeout"] is None and seen["shutdown_wait"] == 30


def test_office_failures_are_remembered_across_calls():
    from worklog.extract.com import ComUnavailable, OfficeCom

    memory: dict = {}
    OfficeCom(memory=memory)._mark_unavailable("excel", "Excel.Application did not start", conclusive=False)
    assert memory == {}  # a wait cut short by the caller's time limit proves nothing
    OfficeCom(memory=memory)._mark_unavailable("word", "Word.Application did not start within 45s")
    with pytest.raises(ComUnavailable, match="tries Office again"):
        OfficeCom(memory=memory)._worker("word")


def test_scheduled_task_arguments():
    assert task_root('scan --root "D:\\업무 폴더"') == "D:\\업무 폴더"
    assert task_root("morning --root D:\\Work") == "D:\\Work"
    assert task_root("scan") is None
    assert same_folder("C:\\Windows\\", "c:\\windows")


def test_old_index_is_migrated(tmp_path: Path):
    from worklog.db import connect

    path = tmp_path / "index.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE scans(id INTEGER PRIMARY KEY, taken_at REAL NOT NULL, n_files INTEGER, n_events INTEGER)")
    old.execute("INSERT INTO scans(taken_at, n_files, n_events) VALUES(1, 2, 3)")
    old.commit()
    old.close()
    con = connect(path)
    assert "duration" in {r[1] for r in con.execute("PRAGMA table_info(scans)")}
    assert con.execute("SELECT n_files FROM scans").fetchone()[0] == 2
    con.close()
