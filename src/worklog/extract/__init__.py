"""Extractor registry: direct parsing first, Office COM fallback, metadata otherwise.

Each file type maps to an ordered chain of readers. Adding a format (e.g. HWP
later) means adding one function to ``DIRECT`` or ``COM_APPS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from .com import OfficeCom, com_available
from .common import normalize_text
from .image import IMAGE_EXTENSIONS, image_meta
from .ooxml import NoTextLayer, docx_text, pdf_text, pptx_text, xlsx_text
from .textfile import TEXT_EXTENSIONS, read_text_file

DIRECT = {
    ".docx": docx_text, ".docm": docx_text,
    ".xlsx": xlsx_text, ".xlsm": xlsx_text,
    ".pptx": pptx_text, ".pptm": pptx_text,
    ".pdf": pdf_text,
}

COM_APPS = {
    ".docx": "word", ".docm": "word", ".doc": "word", ".dot": "word", ".dotx": "word",
    ".rtf": "word", ".odt": "word",
    ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel", ".xlsb": "excel", ".xltx": "excel",
    ".ods": "excel",
    ".pptx": "powerpoint", ".pptm": "powerpoint", ".ppt": "powerpoint", ".ppsx": "powerpoint",
    ".pps": "powerpoint", ".odp": "powerpoint",
}


@dataclass
class ExtractResult:
    kind: str  # text | image | binary | failed
    text: str | None = None
    method: str = "metadata"  # direct | com | metadata
    error: str | None = None
    meta: dict = field(default_factory=dict)


def _err(e: BaseException) -> str:
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


class Extractor:
    """Use as a context manager so hidden Office instances are always closed."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._com: OfficeCom | None = None
        self.text_exts = TEXT_EXTENSIONS | set(cfg.extra_text_extensions)
        self.skip = set(cfg.skip_extensions)

    def __enter__(self) -> "Extractor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def com(self) -> OfficeCom:
        if self._com is None:
            self._com = OfficeCom(timeout_sec=self.cfg.com_timeout_sec, pdf_via_word=self.cfg.com_pdf_via_word)
        return self._com

    def close(self) -> None:
        if self._com is not None:
            self._com.shutdown()
            self._com = None

    def category(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in self.skip:
            return "binary"
        if ext in IMAGE_EXTENSIONS:
            return "image"
        if ext in self.text_exts or ext in DIRECT or ext in COM_APPS:
            return "text"
        return "binary"

    def extract(self, path: Path, method: str | None = None) -> ExtractResult:
        """Extract text. ``method`` forces 'direct' or 'com' (used by doctor)."""
        path = Path(path)
        ext = path.suffix.lower()
        try:
            size = path.stat().st_size
        except OSError as e:
            return ExtractResult("failed", error=f"stat failed: {_err(e)}")
        meta: dict = {"size": size}
        if ext in self.skip:
            return ExtractResult("binary", meta={**meta, "reason": "skipped by config"})
        if size > self.cfg.max_file_mb * 1024 * 1024:
            return ExtractResult("binary", meta={**meta, "reason": "larger than max_file_mb"})
        if ext in IMAGE_EXTENSIONS:
            return ExtractResult("image", meta={**meta, **image_meta(path)})
        if ext in self.text_exts:
            try:
                return self._text(read_text_file(path), "direct", meta)
            except Exception as e:
                return ExtractResult("failed", error=_err(e), meta=meta)

        direct = DIRECT.get(ext)
        app = COM_APPS.get(ext)
        if ext == ".pdf" and self.cfg.com_pdf_via_word:
            app = "word"
        if direct is None and app is None:
            return ExtractResult("binary", meta=meta)

        errors: list[str] = []
        if direct is not None and method in (None, "direct"):
            try:
                return self._text(direct(path), "direct", meta)
            except NoTextLayer as e:
                return ExtractResult("failed", error=str(e), meta=meta)
            except Exception as e:
                errors.append(f"direct: {_err(e)}")
        if app is not None and method in (None, "com"):
            if not self.cfg.com_enabled:
                errors.append("com: disabled in config")
            elif not com_available():
                errors.append("com: Office automation (pywin32) not available")
            else:
                try:
                    return self._text(self.com.extract(app, path), "com", meta)
                except Exception as e:
                    errors.append(f"com: {_err(e)}")
        return ExtractResult("failed", error="; ".join(errors) or "no extractor", meta=meta)

    def _text(self, text: str, method: str, meta: dict) -> ExtractResult:
        text = normalize_text(text or "")
        if len(text) > self.cfg.max_text_chars:
            text = text[: self.cfg.max_text_chars] + "\n…(텍스트 길이 제한으로 이하 생략)"
            meta = {**meta, "truncated": True}
        return ExtractResult("text", text=text, method=method, meta=meta)


__all__ = ["Extractor", "ExtractResult", "DIRECT", "COM_APPS", "IMAGE_EXTENSIONS", "TEXT_EXTENSIONS"]
