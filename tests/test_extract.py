from pathlib import Path

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
