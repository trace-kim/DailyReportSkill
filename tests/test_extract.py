from pathlib import Path

import pytest

from conftest import make_docx, make_pdf, make_pptx, make_xlsx
from worklog.config import Config
from worklog.extract import Extractor
from worklog.extract.textfile import decode_bytes


def _ex():
    cfg = Config()
    cfg.com_enabled = False
    return Extractor(cfg)


def test_docx_paragraphs_and_table(tmp_path: Path):
    p = make_docx(tmp_path / "보고서.docx", ["제목: 추진 일정", "1단계 착수"], [["항목", "금액"], ["인건비", "1,350"]])
    r = _ex().extract(p)
    assert r.kind == "text" and r.method == "direct"
    assert "제목: 추진 일정" in r.text
    assert "인건비 | 1,350" in r.text


def test_xlsx_rows(tmp_path: Path):
    p = make_xlsx(tmp_path / "예산.xlsx", {"예산": [["항목", "단가", "수량"], ["인건비", 1350, 12], ["경비", 2.5, None]]})
    r = _ex().extract(p)
    assert r.kind == "text"
    assert "[시트] 예산" in r.text
    assert "인건비 | 1350 | 12" in r.text
    assert "경비 | 2.5" in r.text


def test_pptx_slides_and_notes(tmp_path: Path):
    p = make_pptx(tmp_path / "발표.pptx", [["개요", "추진 배경", "기대 효과"], ["일정"]], notes=["발표 메모"])
    r = _ex().extract(p)
    assert r.kind == "text"
    assert "[슬라이드 1]" in r.text and "추진 배경" in r.text and "기대 효과" in r.text
    assert "[노트] 발표 메모" in r.text
    assert "[슬라이드 2]" in r.text


def test_pdf(tmp_path: Path):
    p = make_pdf(tmp_path / "doc.pdf", ["Quarterly report", "Revenue 1350"])
    r = _ex().extract(p)
    assert r.kind == "text" and "Revenue 1350" in r.text


def test_cp949_text(tmp_path: Path):
    p = tmp_path / "메모.txt"
    p.write_bytes("회의 메모: 예산 검토".encode("cp949"))
    r = _ex().extract(p)
    assert r.kind == "text" and r.text == "회의 메모: 예산 검토"


def test_drm_like_docx_fails_without_com(tmp_path: Path):
    p = tmp_path / "secret.docx"
    p.write_bytes(b"DRMONE\x00\x01" + bytes(range(256)) * 20)
    r = _ex().extract(p)
    assert r.kind == "failed"
    assert "DRM" in r.error and "com: disabled" in r.error


def test_binary_and_image(tmp_path: Path):
    from PIL import Image

    img = tmp_path / "chart.png"
    Image.new("RGB", (40, 30)).save(img)
    r = _ex().extract(img)
    assert r.kind == "image" and r.meta["width"] == 40 and r.meta["height"] == 30
    z = tmp_path / "archive.zip"
    z.write_bytes(b"PK\x03\x04")
    assert _ex().extract(z).kind == "binary"


def test_decode_rejects_binary():
    assert decode_bytes(b"\x00\x01\x02binary") is None
    assert decode_bytes("한글".encode("utf-8")) == "한글"


# --- Office COM logic with fake application objects (no Office needed) ----
class _FakeDoc:
    def __init__(self, text: str):
        self.Content = type("Content", (), {"Text": text})()
        self.Tables = type("Tables", (), {"Count": 0})()
        self.closed = False

    def Close(self, SaveChanges=0):
        self.closed = True


class _FakeWord:
    def __init__(self, text: str):
        self.doc = _FakeDoc(text)
        self.Documents = type("Documents", (), {"Open": lambda _self, **kw: self.doc})()


def test_word_raw_pdf_source_is_a_failure():
    from worklog.extract.com import ComError, looks_like_pdf_source, word_text

    raw = "%PDF-1.4\r%\xe2\xe3\r1 0 obj\r<< /Filter /Standard >>\rendobj\r2 0 obj\rendobj\rstartxref\r123\r%%EOF"
    assert looks_like_pdf_source(raw) and looks_like_pdf_source("\ufeff \n" + raw)
    assert not looks_like_pdf_source("PDF 변환 기능 검토 보고서\n%PDF-1.4 규격 비교")
    app = _FakeWord(raw)
    with pytest.raises(ComError, match="raw PDF source"):
        word_text(app, "locked.pdf")
    assert app.doc.closed
    assert word_text(_FakeWord("추진 일정\r1단계 착수\r"), "a.pdf") == "추진 일정\n1단계 착수"


class _FakePowerPoint:
    """The user's PowerPoint: alerts on (ppAlertsAll=2), macros by UI setting (1)."""

    def __init__(self):
        self.DisplayAlerts = 2
        self.AutomationSecurity = 1


def test_shared_powerpoint_settings_last_only_for_the_job(monkeypatch):
    from worklog.extract import com

    app, seen = _FakePowerPoint(), []

    def fake_extract(a, path):
        seen.append((a.DisplayAlerts, a.AutomationSecurity))
        if path == "bad.pptx":
            raise RuntimeError("open failed")
        return "text"

    monkeypatch.setitem(com._EXTRACT, "powerpoint", fake_extract)
    settings = com.app_settings("powerpoint", shared=True)
    assert com.run_job("powerpoint", app, "a.pptx", settings) == "text"
    assert seen == [(1, 3)]  # alerts off, macros force-disabled while reading
    assert (app.DisplayAlerts, app.AutomationSecurity) == (2, 1)  # the user's values are back
    with pytest.raises(RuntimeError):
        com.run_job("powerpoint", app, "bad.pptx", settings)
    assert (app.DisplayAlerts, app.AutomationSecurity) == (2, 1)  # also after a failure


def test_shared_instances_keep_their_windows():
    from worklog.extract.com import app_settings

    names = {k for k, _ in app_settings("word", shared=True)}
    assert "Visible" not in names and "ScreenUpdating" not in names and "DisplayAlerts" in names
    assert {k for k, _ in app_settings("excel", shared=False)} >= {"Visible", "EnableEvents"}


def test_drm_pdf_is_not_sent_to_word(tmp_path: Path, monkeypatch):
    import worklog.extract as extract_mod

    calls = []

    class FakeCom:
        def extract(self, app, path, timeout=None):
            calls.append((app, Path(path).name))
            return "본문"

        def shutdown(self, wait=30):
            pass

    monkeypatch.setattr(extract_mod, "com_available", lambda: True)
    cfg = Config()
    cfg.com_enabled = True
    ex = Extractor(cfg)
    ex._com = FakeCom()
    drm = tmp_path / "보안.pdf"
    drm.write_bytes(b"<## DRM Header ##>" + bytes(range(256)) * 8)  # what Python sees of a DRM PDF
    r = ex.extract(drm)
    assert r.kind == "failed" and "DRM" in r.error and calls == []
    # doctor forces Word, so a DRM product whose Word *can* convert PDFs would still show up
    assert ex.extract(drm, method="com").kind == "text" and calls == [("word", "보안.pdf")]
    # a real but damaged PDF still gets the Word fallback
    bad = tmp_path / "damaged.pdf"
    bad.write_bytes(b"%PDF-1.4\n" + bytes(range(256)) * 8)
    assert ex.extract(bad).method == "com" and calls[-1] == ("word", "damaged.pdf")
