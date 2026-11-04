"""Korean report-style rules: 을/를 particle choice, 존댓말 보고체 checks, titles."""

from __future__ import annotations

import re

_HANGUL_FIRST, _HANGUL_LAST = 0xAC00, 0xD7A3
_CLOSERS = {")": "(", "]": "[", "}": "{", "」": "「", "』": "『", ">": "<", "）": "（", "》": "《", "〉": "〈"}
_QUOTES = "'\"‘’“”`"


def has_final_consonant(ch: str) -> bool | None:
    """True if a Hangul syllable has a 받침, None for non-Hangul characters.

    Precomposed syllables are laid out as 0xAC00 + (initial*21 + medial)*28 + final,
    where final == 0 means "no 받침".
    """
    if len(ch) != 1:
        return None
    code = ord(ch)
    if _HANGUL_FIRST <= code <= _HANGUL_LAST:
        return (code - _HANGUL_FIRST) % 28 != 0
    return None


def _strip_tail(s: str) -> str:
    """Drop trailing quotes and bracketed asides: the particle follows the word before them."""
    s = s.rstrip()
    while s:
        if s[-1] in _QUOTES:
            s = s[:-1].rstrip()
            continue
        if s[-1] in _CLOSERS:
            idx = s.rfind(_CLOSERS[s[-1]])
            if idx > 0:
                s = s[:idx].rstrip()
                continue
        break
    return s


def object_particle(phrase: str) -> str | None:
    """'을' or '를' for a noun phrase; None when it ends in a non-Hangul character."""
    core = _strip_tail(phrase)
    if not core:
        return None
    final = has_final_consonant(core[-1])
    if final is None:
        return None
    return "을" if final else "를"


# --------------------------------------------------------------------------
# paragraph style
# --------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_BULLET = re.compile(r"^\s*(?:[-*•·▪■□○●◦※]|\d+[.)]\s|[가나다라마바사아자차카타파하][.)]\s|[①-⑳])")
_MARKDOWN = re.compile(r"\*\*|__|`|^\s*#", re.M)
_POLITE_END = re.compile(r"니다[\"'”’)\]」』]*\.$")
_CASUAL_HINT = re.compile(r"(함|음|임|됨|했다|한다|이다|였다|해요|어요|아요|예요|에요)[.]?$")


def split_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(paragraph.strip()) if s.strip()]


def check_paragraph(paragraph: str) -> tuple[list[str], list[str]]:
    """Validate one weekly-report paragraph. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(paragraph, str) or not paragraph.strip():
        return ["paragraph is empty"], warnings
    p = paragraph.strip()
    if "\n" in p or "\r" in p:
        errors.append("must be exactly one paragraph: remove line breaks")
    if _BULLET.match(p):
        errors.append("must be prose, not a list: remove the leading bullet/number")
    if _MARKDOWN.search(p):
        errors.append("plain text only: remove markdown (**, __, `, #)")
    sentences = split_sentences(p.replace("\r", " ").replace("\n", " "))
    for i, s in enumerate(sentences, 1):
        if not _POLITE_END.search(s):
            tail = s[-30:]
            hint = " (개조식/반말 어미)" if _CASUAL_HINT.search(s) else ""
            errors.append(
                f"sentence {i} must end in 존댓말 보고체 '~니다.' "
                f"(e.g. ~하였습니다., ~예정입니다.){hint}: ...{tail!r}"
            )
    if len(sentences) > 8:
        warnings.append(f"{len(sentences)} sentences: consider tightening to 3-6")
    if len(p) > 1200:
        warnings.append(f"{len(p)} characters: consider tightening")
    return errors, warnings


# --------------------------------------------------------------------------
# weekly topic titles
# --------------------------------------------------------------------------
_PHRASE_STEM = re.compile(r"^([가-힣]+?)(?:중입니다|하였습니다|했습니다|합니다|입니다)$")


def build_title(subject: str, phrase: str, particle: str | None = None) -> tuple[str | None, list[str]]:
    """'{subject}을/를 {phrase}.' -> (title, errors)."""
    errors: list[str] = []
    s = " ".join(str(subject or "").split()).rstrip(".")
    phrase = str(phrase or "").strip().rstrip(".")
    if not s:
        return None, ["subject is empty"]
    if not phrase:
        return None, ["status phrase is empty"]
    if len(s) > 1 and s[-1] in "을를" and object_particle(s[:-1]) == s[-1]:
        errors.append(f"subject {s!r} already ends with the particle '{s[-1]}': the tool adds it")
    m = _PHRASE_STEM.match(phrase)
    if m and _strip_tail(s).endswith(m.group(1)):
        stem = m.group(1)
        errors.append(
            f"subject {s!r} ends with '{stem}' and the status phrase is '{phrase}', so the title "
            f"would repeat '{stem}'. Use only the object (e.g. '예산안' + '검토중입니다')."
        )
    p = particle or object_particle(s)
    if p is None:
        errors.append(
            f"cannot choose 을/를 automatically for {s!r} (ends with a non-Hangul character): "
            'set "particle" to "을" or "를"'
        )
    elif p not in ("을", "를"):
        errors.append('particle must be "을" or "를"')
    if errors:
        return None, errors
    return f"{s}{p} {phrase}.", []
