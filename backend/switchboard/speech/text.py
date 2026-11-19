"""Markdown made into plain sentences, fit to be read aloud.

Code blocks are skipped, a link is read by its text, a bare address is dropped, a table is read row
by row as its cells, and headings and list items become sentences of their own. A long answer is
cut at a sentence near the limit, because ElevenLabs bills per character.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"(`{3,}|~{3,})")
_RULE = re.compile(r"(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,}|={3,}")  # also the line under a heading
_TABLE_SEPARATOR = re.compile(r"\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?")

_HEADING = re.compile(r"#{1,6}\s+(.*?)\s*#*")
_ITEM = re.compile(r"(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?(.*)")

_INLINE = [
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),  # an image, by its description
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # a link, by its text
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),  # a reference link
    (re.compile(r"<(?:https?://|mailto:)[^>]+>"), ""),  # an address in angle brackets
    # A bare address, which nobody wants read out. The punctuation after it stays.
    (re.compile(r"\b(?:https?://|www\.)[^\s<>]*[^\s<>.,;:!?'\")\]]"), ""),
    (re.compile(r"\(\s*\)"), ""),  # the brackets an address was in
    (re.compile(r"`([^`]+)`"), r"\1"),  # inline code, read as the word it is
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),  # bold
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)"), r"\1"),  # italic with stars
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),  # italic with underscores, not snake_case
    (re.compile(r"~~(.+?)~~"), r"\1"),  # struck through
    (re.compile(r"</?[A-Za-z][^>]*>"), ""),  # HTML tags
]


_LOOSE_MARK = re.compile(r"\s+([.,;:!?])")  # "or ." where an address was taken out


def inline(text: str) -> str:
    for pattern, replacement in _INLINE:
        text = pattern.sub(replacement, text)
    return _LOOSE_MARK.sub(r"\1", " ".join(text.split()))


def sentence(text: str) -> str:
    """Text ending the way a sentence does, so the voice pauses after it."""
    text = text.strip()
    if not text:
        return ""
    return text if text[-1] in ".!?:;…" else text + "."


def _is_separator(line: str) -> bool:
    """The |---|---| line under a table's header. It has a pipe, which tells it from a rule."""
    return "|" in line and _TABLE_SEPARATOR.fullmatch(line) is not None


def _cells(line: str) -> str:
    cells = [inline(c) for c in line.strip().strip("|").split("|")]
    return sentence(", ".join(c for c in cells if c))


def cut(text: str, max_chars: int) -> tuple[str, bool]:
    """At most `max_chars`, ending at a sentence when there is one in the last stretch. Returns the
    text and whether it was cut."""
    if len(text) <= max_chars:
        return text, False
    window = text[:max_chars]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", window)]
    if ends and ends[-1] >= max_chars * 0.6:
        return window[:ends[-1]], True
    space = window.rfind(" ")
    return sentence(window[:space] if space > max_chars * 0.6 else window[:-1]), True  # leaves room for the period


def speakable(markdown: str, max_chars: int) -> tuple[str, bool]:
    """The answer as plain sentences, and whether it was cut to `max_chars`."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    paragraph: list[str] = []
    fence: str | None = None
    in_table = False

    def flush() -> None:
        if paragraph:
            parts.append(sentence(inline(" ".join(paragraph))))
            paragraph.clear()

    for i, raw in enumerate(lines):
        line = raw.strip()
        opening = _FENCE.match(line)
        if fence:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence):
                fence = None
            continue
        if opening:
            flush()
            fence = opening.group(1)
            continue
        if not line:
            flush()
            in_table = False
            continue
        if _is_separator(line):
            in_table = True
            continue
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if "|" in line and (in_table or line.startswith("|") or _is_separator(next_line)):
            flush()
            parts.append(_cells(line))
            continue
        if _RULE.fullmatch(line):
            flush()
            continue
        heading = _HEADING.fullmatch(line)
        if heading:
            flush()
            parts.append(sentence(inline(heading.group(1))))
            continue
        item = _ITEM.fullmatch(line)
        if item:
            flush()
            parts.append(sentence(inline(item.group(1))))
            continue
        paragraph.append(line.lstrip("> ").strip())
    flush()
    return cut(" ".join(p for p in parts if p), max_chars)
