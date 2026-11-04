"""opencode registration (global and workspace) and the scheduled morning run."""

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from conftest import backdate_tracking, set_mtime
from worklog import cli
from worklog.config import Config, set_report_command
from worklog.ignore import IgnoreRules
from worklog.opencode_setup import setup_opencode
from worklog.service import strip_ansi


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def test_workspace_registration(root: Path, backups: Path):
    r = setup_opencode(root, workspace=root, python_exe="py.exe", backup_dir=backups)
    assert r["ok"] and r["scope"] == "workspace"
    cfg = root / "opencode.jsonc"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcp"]["worklog"]["command"] == ["py.exe", "-m", "worklog", "mcp", "--root", str(root.resolve())]
    assert (root / ".opencode" / "skills" / "worklog" / "SKILL.md").exists()
    assert (root / ".opencode" / "commands" / "daily-report.md").exists()
    assert "backup" not in r  # nothing to back up for a new file

    # Re-running changes nothing, so it leaves no backup behind.
    again = setup_opencode(root, workspace=root, python_exe="py.exe", backup_dir=backups)
    assert again["unchanged"] and "backup" not in again and not backups.exists()

    # An existing config with comments is kept (minus comments) and backed up OUTSIDE the work folder.
    cfg.write_text('{\n  // mine\n  "model": "a/b",\n}\n', encoding="utf-8")
    r = setup_opencode(root, workspace=root, python_exe="py.exe", backup_dir=backups)
    assert json.loads(cfg.read_text(encoding="utf-8"))["model"] == "a/b"
    assert Path(r["backup"]).parent == backups and r["warnings"]
    assert not list(root.glob("*.bak*"))

    setup_opencode(root, workspace=root, remove=True, backup_dir=backups)
    assert "worklog" not in json.loads(cfg.read_text(encoding="utf-8"))["mcp"]
    assert not (root / ".opencode").exists()  # emptied folders are removed too


def test_global_registration_and_remove_without_config(tmp_path: Path, root: Path, backups: Path, monkeypatch):
    conf = tmp_path / "oc"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(conf))
    r = setup_opencode(root, remove=True, backup_dir=backups)
    assert r["ok"] and not (conf / "opencode.json").exists()  # removing never creates a config
    r = setup_opencode(root, python_exe="py.exe", backup_dir=backups)
    assert r["scope"] == "global" and (conf / "opencode.json").exists()
    assert (conf / "skills" / "worklog" / "SKILL.md").exists()


def test_old_config_backups_are_not_tracked(root: Path):
    rules = IgnoreRules(root)
    assert rules.ignored("opencode.jsonc.bak-20261104091500", False)
    assert rules.ignored("sub/opencode.json.bak-20261104091500", False)
    assert not rules.ignored("보고서.bak-1.docx", False)


def test_report_command_with_model(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\bin\opencode.cmd")
    cmd = cli._opencode_report_command("codemate/CodeLLMPro")
    assert cmd[:4] == [r"C:\bin\opencode.cmd", "run", "--model", "codemate/CodeLLMPro"]
    assert cmd[-4:] == ["--command", "daily-report", "--auto", "auto"]
    assert "--model" not in cli._opencode_report_command()


@pytest.mark.parametrize("args", [["--report", "none", "--model", "a/b"], ["--report", "auto", "--model", "a\\b"]])
def test_init_rejects_bad_model_use(root: Path, monkeypatch, args):
    monkeypatch.setattr(cli, "_setup_logging", lambda *a: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["init", "--root", str(root), *args])
    assert e.value.code == 2
    assert not (root / ".worklog").exists()


def test_strip_ansi():
    raw = b"\x1b[0m\x1b[1;32m> build\x1b[0m \xed\x95\x9c\x1b]0;title\x07 done\x1b[2K\r\n"
    assert strip_ansi(raw) == "> build 한 done\r\n".encode("utf-8")


def _yesterday_change(root: Path, wl) -> str:
    yday = (dt.date.fromisoformat(wl.cfg.today()) - dt.timedelta(days=1)).isoformat()
    base = dt.datetime.combine(dt.date.fromisoformat(yday), dt.time(10, 0))
    backdate_tracking(wl, base)
    f = root / "a.txt"
    f.write_text("v2", encoding="utf-8")
    set_mtime(f, base + dt.timedelta(hours=3))
    return yday


def _use_command(wl, code: str) -> None:
    set_report_command(wl.ws.config_path, [sys.executable, "-c", code])
    wl.cfg = Config.load(wl.ws.config_path)


def test_morning_flags_a_report_run_that_saved_nothing(root: Path, make_wl):
    (root / "a.txt").write_text("v1", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    yday = _yesterday_change(root, wl)
    # What a model that cannot call tools does: print the tool call as text, exit 0.
    _use_command(wl, r"print('\x1b[33mworklog_daily_save(...)\x1b[0m')")
    out = wl.morning()
    run = out["report_command"]
    assert out["ok"] is False and run["returncode"] == 0 and run["ok"] is False
    assert run["missing"] == [yday] and run["written"] == []
    assert "worklog_daily_save(...)" in run["log_tail"]
    assert b"\x1b" not in Path(run["log"]).read_bytes()  # the log is plain text
    st = wl.status()
    assert st["last_report_run"]["missing"] == [yday]
    assert any("did not write the log for " + yday in h for h in st["hints"])

    # Once the day is written (by hand or the next run), the hint goes away.
    wl.topic_upsert(name="A사 제안서 작성")
    assert wl.daily_save({"date": yday, "items": [{"topic_id": "T001", "summary": "작성"}]})["ok"]
    assert not any("did not write" in h for h in wl.status()["hints"])


def test_morning_succeeds_when_the_agent_writes_the_reports(root: Path, make_wl):
    (root / "a.txt").write_text("v1", encoding="utf-8")
    wl = make_wl()
    wl.baseline()
    yday = _yesterday_change(root, wl)
    wl.topic_upsert(name="A사 제안서 작성")
    _use_command(wl, "from worklog.service import Worklog; wl = Worklog(%r); "
                     "wl.close(); wl.daily_save({'date': %r, 'items': [{'topic_id': 'T001', 'summary': '작성'}]})"
                 % (str(root), yday))
    out = wl.morning()
    assert out["ok"] is True, out["report_command"]
    assert out["report_command"]["written"] == [yday] and "log_tail" not in out["report_command"]
    assert wl.status()["last_report_run"]["ok"] is True
