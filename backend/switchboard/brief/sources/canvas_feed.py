"""Coursework from a Canvas calendar feed.

Canvas gives every user a private calendar link (Calendar, then Calendar Feed). It lists the
assignments of all their courses with due dates. It can't say whether something was handed in, so
coursework is ticked off by hand on the Today page, and drops off by itself a couple of days after
it was due.

The link works like a password. It is kept in the credential store and is never logged, printed
or made part of an error message.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, time, timedelta

import httpx

from ... import credentials
from ...config import BriefCourseworkCfg
from ..base import Found, SourceError, Usage, stamp
from . import ical

FEED_URL = "canvas_feed_url"  # the name the link is saved under
SETUP = "python -m switchboard brief connect canvas"
TIMEOUT_S = 20
MAX_CHARS = 5_000_000

_TITLE = re.compile(r"^(?P<title>.*\S)\s*\[(?P<course>[^\[\]]+)\]\s*$", re.DOTALL)


def split_title(summary: str) -> tuple[str, str | None]:
    """Canvas writes an event as "Problem set 6 [ALGO 3110]": the title, then the course."""
    match = _TITLE.match(summary.strip())
    if match:
        return match["title"].strip(), match["course"].strip()
    return summary.strip(), None


def own_link(url: str | None, host: str) -> str | None:
    """The event's link, but only when it is https and goes to the same Canvas the feed came from."""
    if not url:
        return None
    try:
        parsed = httpx.URL(url)
    except Exception:
        return None
    return url if parsed.scheme == "https" and parsed.host.lower() == host.lower() else None


def is_assignment(event: ical.Event) -> bool:
    return "assignment" in event.uid.lower()


class CanvasFeed:
    name = "coursework"
    label = "Canvas"
    setup = SETUP

    def __init__(self, cfg: BriefCourseworkCfg):
        self.cfg = cfg

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)

    def connected(self) -> bool:
        return credentials.get(FEED_URL) is not None

    def fetch(self, url: str) -> str:
        """The text of the feed. Every failure is a SourceError that doesn't repeat the link, and
        httpx's own error is dropped for the same reason."""
        try:
            with self._client() as client:
                r = client.get(url, headers={"Accept": "text/calendar"})
        except httpx.TimeoutException:
            raise SourceError("Canvas took too long to answer.") from None
        except httpx.HTTPError:
            raise SourceError("Canvas couldn't be reached.") from None
        if r.status_code in (401, 403, 404):
            raise SourceError(f"Canvas refused the feed link. If you reset it, connect again with: {SETUP}")
        if r.status_code != 200:
            raise SourceError(f"Canvas answered with HTTP {r.status_code}.")
        text = r.text[:MAX_CHARS]
        if "BEGIN:VCALENDAR" not in text[:2000].upper():
            raise SourceError("The feed link didn't return a calendar.")
        return text

    def select(self, events: list[ical.Event], now: datetime, host: str) -> list[Found]:
        """The events that belong on today's list: assignments due within the next `lookahead_days`
        and ones that were due in the last `overdue_days`, soonest first."""
        earliest = now - timedelta(days=self.cfg.overdue_days)
        latest = now + timedelta(days=self.cfg.lookahead_days)
        picked: list[tuple[datetime, Found]] = []
        for event in events:
            assignment = is_assignment(event)
            if event.when is None or not (assignment or self.cfg.include_events):
                continue
            if isinstance(event.when, datetime):
                due, due_at = event.when, stamp(event.when)
            else:  # a date with no time is due until that day ends
                due = datetime.combine(event.when, time(23, 59, 59), tzinfo=now.tzinfo)
                due_at = event.when.isoformat()
            if not earliest <= due <= latest:
                continue
            title, course = split_title(event.summary)
            picked.append((due, Found(
                external_id=event.uid, title=title, context=course,
                category="assignment" if assignment else "event", owner="you",
                due_at=due_at, link=own_link(event.url, host),
            )))
        return [found for _, found in sorted(picked, key=lambda pair: pair[0])]

    def collect(self, now: datetime, usage: Usage) -> list[Found]:
        url = credentials.get(FEED_URL)
        if not url:
            raise SourceError(f"Canvas isn't connected. Run: {SETUP}")
        return self.select(ical.events(self.fetch(url), now.tzinfo), now, httpx.URL(url).host)


def shape(events: list[ical.Event], host: str) -> list[str]:
    """What a feed looks like, in lines that hold nothing it says: how many events there are, how
    their ids are built, what kind of dates they carry. Safe to print and to pass on."""
    ids = Counter(re.sub(r"\d+", "<n>", event.uid.split("@")[0])[:40] for event in events)
    all_day = sum(1 for e in events if e.when is not None and not isinstance(e.when, datetime))
    timed = sum(1 for e in events if isinstance(e.when, datetime))
    with_course = sum(1 for e in events if split_title(e.summary)[1])
    linked = sum(1 for e in events if own_link(e.url, host))
    names = sorted({name for e in events for name in e.properties})
    return [
        f"The feed has {len(events)} events, {sum(1 for e in events if is_assignment(e))} of them assignments.",
        "  ids: " + (", ".join(f"{form} ({n})" for form, n in ids.most_common(6)) or "none"),
        f"  dates: {all_day} all day, {timed} with a time, {len(events) - all_day - timed} unreadable",
        f"  titles that end in [course]: {with_course}",
        f"  links back to the same Canvas: {linked}",
        "  properties: " + (", ".join(names) or "none"),
    ]
