"""Plain-text files with encoding detection (UTF-8 / UTF-16 / CP949)."""

from __future__ import annotations

import codecs
from pathlib import Path

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log", ".json", ".jsonl",
    ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".properties",
    ".sql", ".py", ".ipynb", ".r", ".m", ".js", ".ts", ".tsx", ".jsx", ".java",
    ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".swift", ".scala", ".sh", ".bat", ".cmd", ".ps1", ".vbs", ".html", ".htm",
    ".css", ".scss", ".tex", ".srt",
}


def _looks_like_text(text: str) -> bool:
    sample = text[:20000]
    if not sample:
        return True
    bad = 0
    for ch in sample:
        o = ord(ch)
        if (o < 32 and ch not in "\t\n\r\f\v") or 0xE000 <= o <= 0xF8FF:
            bad += 1
    return bad / len(sample) < 0.01


def decode_bytes(data: bytes) -> str | None:
    """Decode text bytes; None when the data does not look like text.

    Korean Windows files are frequently CP949 (EUC-KR superset) rather than
    UTF-8, so CP949 is tried second. DRM-encrypted files fail both.
    """
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:65536]:
        return None
    for enc in ("utf-8", "cp949"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        return text if _looks_like_text(text) else None
    return None


def read_text_file(path: Path) -> str:
    text = decode_bytes(Path(path).read_bytes())
    if text is None:
        raise ValueError("not readable as text (binary or DRM-encrypted)")
    return text
