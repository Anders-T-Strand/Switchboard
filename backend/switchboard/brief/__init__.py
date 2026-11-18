"""The morning brief: what needs doing today, gathered from sources such as mail and coursework.

Nothing here goes through the router or the chat tables. Sources hand in what they found, a local
model sorts what needs sorting, and the result lives in brief_runs and brief_items. What a brief
reads is private and nobody here wrote it, while chats are indexed and can reach Claude through
Memory or a fallback. So the two never share a table or a model call.
"""

from __future__ import annotations

from ..config import Config
from .base import Found, Source, SourceError, Usage, stamp
from .build import OFF, Brief, Busy, ready_message
from .store import ItemNotFound

__all__ = ["OFF", "Brief", "Busy", "Found", "ItemNotFound", "Source", "SourceError", "Usage", "load_sources",
           "ready_message", "stamp"]


def load_sources(cfg: Config, ollama=None) -> list[Source]:
    """Every source there is, connected or not. One that isn't connected is skipped by a build, and
    the Today page says how to connect it. `ollama` is the client mail is sorted with."""
    from ..providers import OllamaClient
    from .sources.canvas_feed import CanvasFeed
    from .sources.outlook import OutlookMail

    return [OutlookMail(cfg, ollama or OllamaClient(cfg.ollama)), CanvasFeed(cfg.brief.coursework)]
