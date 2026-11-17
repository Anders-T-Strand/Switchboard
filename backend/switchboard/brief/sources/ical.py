"""Just enough of iCalendar (RFC 5545) to read the events out of a calendar feed.

A feed is somebody else's text. It is read here and never acted on: no recurrence rules, no alarms
and no attachments, only the few properties a to-do list needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

MAX_EVENTS = 5000


@dataclass
class Event:
    uid: str
    summary: str
    when: datetime | date | None  # a date for an all-day event, otherwise an aware datetime. None if unreadable.
    url: str | None = None
    properties: tuple[str, ...] = ()  # the names of the properties it had, which say nothing of their text


def unfold(text: str) -> list[str]:
    """The content lines of a file. A line that starts with a space or a tab continues the one before."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _split(text: str, separator: str) -> list[str]:
    """Split on a separator, except where it sits inside double quotes."""
    parts, start, quoted = [], 0, False
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif ch == separator and not quoted:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def split_line(line: str) -> tuple[str, dict[str, str], str]:
    """A content line as (NAME, parameters, value). `DTSTART;TZID=Europe/Oslo:20261119T120000` is
    ("DTSTART", {"TZID": "Europe/Oslo"}, "20261119T120000"). The value starts at the first colon
    outside quotes, since a quoted parameter may hold one."""
    head, *rest = _split(line, ":")
    name, *raw_params = _split(head, ";")
    params = {}
    for raw in raw_params:
        key, _, value = raw.partition("=")
        params[key.strip().upper()] = value.strip().strip('"')
    return name.strip().upper(), params, ":".join(rest)


def unescape(value: str) -> str:
    """Text as it was meant: `\\n` is a line break, and a backslash before anything else means that character."""
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            i += 1
            out.append("\n" if value[i] in "nN" else value[i])
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _zone(tzid: str | None, local: tzinfo) -> tzinfo:
    if tzid:
        try:
            return ZoneInfo(tzid)
        except Exception:
            pass  # no data for that zone on this machine. Its own zone is the best guess left.
    return local


def parse_when(params: dict[str, str], value: str, local: tzinfo) -> datetime | date | None:
    """A DTSTART value. A bare date stays a date. A time is made aware: UTC when it ends in Z, the
    named zone when it has a TZID, and otherwise `local`, the zone of the machine reading the feed."""
    value = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
            return datetime.strptime(value[:8], "%Y%m%d").date()
        if value[-1:] in ("Z", "z"):
            return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=_zone(params.get("TZID"), local))
    except ValueError:
        return None


def events(text: str, local: tzinfo) -> list[Event]:
    """Every event in a calendar that has an id and a title. What is nested in an event, such as an
    alarm, is skipped, so its properties are never mistaken for the event's own."""
    found: list[Event] = []
    current: dict | None = None
    nested = 0
    for line in unfold(text):
        name, params, value = split_line(line)
        if name == "BEGIN":
            if current is None:
                if value.strip().upper() == "VEVENT":
                    current = {"names": set()}
            else:
                nested += 1
        elif name == "END":
            if current is not None and nested:
                nested -= 1
            elif current is not None and value.strip().upper() == "VEVENT":
                if current.get("UID") and current.get("SUMMARY"):
                    found.append(Event(current["UID"], current["SUMMARY"], current.get("DTSTART"),
                                       current.get("URL") or None, tuple(sorted(current["names"]))))
                    if len(found) >= MAX_EVENTS:
                        break
                current = None
        elif current is not None and not nested:
            current["names"].add(name)
            if name in ("UID", "SUMMARY", "URL"):
                current.setdefault(name, unescape(value).strip())
            elif name == "DTSTART":
                current.setdefault(name, parse_when(params, value, local))
    return found
