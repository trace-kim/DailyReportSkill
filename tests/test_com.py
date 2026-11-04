"""Office COM extraction. Needs Word/Excel/PowerPoint: run with WORKLOG_TEST_COM=1."""

import faulthandler
import os
import sys
from pathlib import Path

import pytest

from conftest import make_docx, make_pdf, make_pptx, make_xlsx
from worklog.config import Config
from worklog.extract import Extractor

pytestmark = [
    pytest.mark.com,
    pytest.mark.skipif(os.environ.get("WORKLOG_TEST_COM") != "1", reason="set WORKLOG_TEST_COM=1 to run"),
]


@pytest.fixture(scope="module", autouse=True)
def _quiet_faulthandler():
    """Keep pytest's faulthandler from printing handled COM exceptions.

    Releasing the last reference to an Office instance that has already quit raises
    0x800706BE (RPC call failed), which the RPC runtime handles itself. On Windows,
    faulthandler prints every such exception as "Windows fatal exception" with a
    traceback, although nothing crashed. Set up before `ex`, torn down after it.
    """
    was_enabled = faulthandler.is_enabled()
    faulthandler.disable()
    yield
    if was_enabled:
        faulthandler.enable(file=sys.__stderr__)


@pytest.fixture(scope="module")
def ex():
    cfg = Config()
    cfg.com_timeout_sec = 120
    with Extractor(cfg) as e:
        yield e


def test_word_via_com(ex, tmp_path: Path):
    p = make_docx(tmp_path / "보고서.docx", ["추진 일정", "1단계 착수"], [["항목", "금액"], ["인건비", "1,350"]])
    r = ex.extract(p, method="com")
    assert r.kind == "text", r.error
    assert r.method == "com"
    assert "추진 일정" in r.text and "1단계 착수" in r.text
    assert "인건비 | 1,350" in r.text  # table rows survive as one line each


def test_excel_via_com(ex, tmp_path: Path):
    p = make_xlsx(tmp_path / "예산.xlsx", {"예산": [["항목", "단가"], ["인건비", 1350]], "요약": [["합계", 1350]]})
    r = ex.extract(p, method="com")
    assert r.kind == "text", r.error
    assert "[시트] 예산" in r.text and "인건비 | 1350" in r.text and "[시트] 요약" in r.text


def test_powerpoint_via_com(ex, tmp_path: Path):
    p = make_pptx(tmp_path / "발표.pptx", [["개요", "추진 배경"]], notes=["발표 메모"])
    r = ex.extract(p, method="com")
    assert r.kind == "text", r.error
    assert "[슬라이드 1]" in r.text and "추진 배경" in r.text and "[노트] 발표 메모" in r.text


def test_pdf_via_word(ex, tmp_path: Path):
    p = make_pdf(tmp_path / "doc.pdf", ["Quarterly report", "Revenue 1350"])
    r = ex.extract(p, method="com")
    assert r.kind == "text", r.error
    assert "Revenue 1350" in r.text


def test_pdf_word_cannot_convert_is_a_failure(ex, tmp_path: Path):
    # Like a DRM-protected PDF: the bytes Word's PDF converter checks are not a PDF, so Word
    # loads the file as plain text and returns the PDF syntax. That must not count as text.
    plain = make_pdf(tmp_path / "plain.pdf", ["Revenue 1350"])
    p = tmp_path / "drm-like.pdf"
    p.write_bytes(b"<## DRM Header ##>" + bytes(range(256)) * 2 + plain.read_bytes())
    r = ex.extract(p, method="com")  # forced, as doctor does
    assert r.kind == "failed" and "raw PDF source" in r.error, (r.text or "")[:200]


def test_password_pdf_via_word_fails(ex, tmp_path: Path):
    from pypdf import PdfWriter

    w = PdfWriter(clone_from=str(make_pdf(tmp_path / "plain.pdf", ["Revenue 1350"])))
    w.encrypt("secret", algorithm="AES-256")
    p = tmp_path / "locked.pdf"
    w.write(str(p))
    r = ex.extract(p, method="com")
    assert r.kind == "failed", (r.text or "")[:200]


def test_com_failure_is_reported(ex, tmp_path: Path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not really a document" * 50)
    r = ex.extract(p)
    assert r.kind == "failed" and "direct:" in r.error and "com:" in r.error
