"""Made-up items, for working on the Today page without anyone's real mail or coursework.

    python -m switchboard brief demo

Every name, course and message here is invented. The command refuses to write into the default
data folder, so these can't end up next to real items.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from .base import Found, Usage, stamp


class DemoSource:
    def __init__(self, name: str, label: str, make: Callable[[datetime], list[Found]]):
        self.name = name
        self.label = label
        self._make = make

    def connected(self) -> bool:
        return True

    def collect(self, now: datetime, usage: Usage) -> list[Found]:
        return self._make(now)


def _mail(now: datetime) -> list[Found]:
    def ago(**delta) -> str:
        return stamp(now - timedelta(**delta))

    return [
        Found("demo-mail-1", "Interview times for next week", context="Dana Whitfield, Northwind Labs",
              summary="Asks for three time slots next week for a 45 minute technical interview.",
              category="recruiter", owner="model", reason="A reply with your availability can be drafted.",
              received_at=ago(hours=2), link="https://example.com/mail/1"),
        Found("demo-mail-2", "Offer letter needs your signature", context="Priya Raman, Fabrikam",
              summary="The offer expires on Friday. Signing it and the start date are your call.",
              category="recruiter", owner="you", reason="A decision only you can make.",
              due_at=(now + timedelta(days=3)).date().isoformat(), received_at=ago(days=1),
              link="https://example.com/mail/2"),
        Found("demo-mail-3", "Can you send the lease renewal form?", context="Marta Olsen",
              summary="Wants the signed renewal form back before the end of the month.",
              category="personal", owner="you", reason="Needs a document from you.",
              received_at=ago(hours=20), link="https://example.com/mail/3"),
        Found("demo-mail-4", "Study group on Thursday?", context="Leo Park",
              summary="Asks whether Thursday at six works for the algorithms study group.",
              category="school", owner="model", reason="A short yes or no can be drafted.",
              received_at=ago(hours=5), link="https://example.com/mail/4"),
    ]


def _coursework(now: datetime) -> list[Found]:
    def day(offset: int) -> str:
        return (now + timedelta(days=offset)).date().isoformat()

    five_pm = (now + timedelta(days=3)).replace(hour=17, minute=0, second=0, microsecond=0)
    return [
        Found("demo-course-1", "Project proposal", context="SE 4350", category="assignment", owner="you",
              due_at=day(-1), link="https://example.com/courses/3"),
        Found("demo-course-2", "Problem set 6", context="ALGO 3110", category="assignment", owner="you",
              due_at=day(1), link="https://example.com/courses/1"),
        Found("demo-course-3", "Lab report 4", context="PHYS 2220", category="assignment", owner="you",
              due_at=stamp(five_pm), link="https://example.com/courses/2"),
        Found("demo-course-4", "Reading response 9", context="HIST 2610", category="assignment", owner="you",
              due_at=day(6), link="https://example.com/courses/4"),
    ]


def sources() -> list[DemoSource]:
    return [DemoSource("mail", "Mail (made up)", _mail), DemoSource("coursework", "Coursework (made up)", _coursework)]
