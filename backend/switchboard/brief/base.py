"""What a source of the morning brief hands in, and how it reports trouble."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


def stamp(moment: datetime) -> str:
    """A UTC timestamp in the form the database uses, so that two of them compare as text."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


@dataclass
class Found:
    """One thing a source says needs attention."""

    external_id: str  # the source's own id for it. The same thing must get the same id on every build.
    title: str
    context: str | None = None  # who it is from, or which course
    # The next four are a judgment about the item. None means "not judged this time", and keeps
    # what an earlier build said, so a build without the local model doesn't wipe its work.
    summary: str | None = None
    category: str | None = None
    owner: str | None = None  # you | model: who can take the next step
    reason: str | None = None
    due_at: str | None = None  # a date (YYYY-MM-DD) or stamp()
    received_at: str | None = None  # stamp() of the latest news about it, such as a new message
    link: str | None = None  # https only, and built by the source, never copied out of what it read
    done: bool = False  # the source knows it is handled, such as a mail that got its reply


@dataclass
class Usage:
    """What the local model spent on one build."""

    model_key: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model_ms: int = 0

    def add(self, model_key: str, input_tokens: int, output_tokens: int, ms: int) -> None:
        self.model_key = model_key
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.model_ms += ms


class SourceError(Exception):
    """A source couldn't deliver. The message is shown on the Today page and saved with the run,
    so it has to be safe to show: no addresses, no tokens and nothing from what was read."""


class Source(Protocol):
    name: str  # goes into brief_items.source: mail | coursework
    label: str  # what the Today page calls it

    def connected(self) -> bool:
        """Whether the source has what it needs to collect, such as a saved sign-in."""

    def collect(self, now: datetime, usage: Usage) -> list[Found]:
        """Everything that needs attention right now. Raises SourceError when it can't tell."""
