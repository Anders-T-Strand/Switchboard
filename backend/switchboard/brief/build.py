"""Builds the morning brief and answers what the Today page asks about it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from ..config import Config
from . import store
from .base import Source, SourceError, Usage, stamp

log = logging.getLogger("switchboard.brief")

OFF = "The morning brief is turned off. Set brief.enabled to true in config.local.yaml."


class Busy(RuntimeError):
    """A build is already going."""


class Brief:
    """`sources` is where items come from. `create_task` makes a card on the Tasks board and
    returns it. `clock` returns the local time, and tests set it."""

    def __init__(self, cfg: Config, sources: list[Source] | None = None, *,
                 create_task: Callable[[str, str], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self.sources = list(sources or [])
        self._create_task = create_task
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()  # held for as long as a build is going
        self._thread: threading.Thread | None = None

    # Building ----------------------------------------------------------------------

    def building(self) -> bool:
        return self._lock.locked()

    def recover(self) -> None:
        """Close runs that a stopped process left open. For startup, before anything builds."""
        store.interrupt_running(stamp(self._clock()))

    def run(self, started_by: str, sources: list[Source] | None = None) -> dict:
        """Build now and return the finished run. Raises Busy when a build is already going."""
        if not self._lock.acquire(blocking=False):
            raise Busy("a brief is already being built")
        try:
            return self._build(started_by, self.sources if sources is None else sources)
        finally:
            self._lock.release()

    def start(self, started_by: str) -> bool:
        """Build in the background. False when a build is already going.

        The lock is taken here and released by the thread, so building() is true from the moment
        this returns and a page that asks right away doesn't see a gap."""
        if not self.cfg.brief.enabled:
            raise ValueError(OFF)
        if not self._lock.acquire(blocking=False):
            return False

        def work() -> None:
            try:
                self._build(started_by, self.sources)
            except Exception:
                log.exception("building the brief failed")
            finally:
                self._lock.release()

        self._thread = threading.Thread(target=work, name="brief", daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float | None = None) -> None:
        """Wait for a build that start() began."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _build(self, started_by: str, sources: list[Source]) -> dict:
        now = self._clock()
        run_id = store.start_run(now.date().isoformat(), started_by, stamp(now))
        usage = Usage()
        results: list[dict] = []
        try:
            for source in sources:
                results.append(self._collect(source, now, usage))
        except BaseException:
            store.finish_run(run_id, "error", results, usage, stamp(self._clock()),
                             error="the build stopped before it finished")
            raise
        worked = any(r["ok"] is True for r in results)
        failed = any(r["ok"] is False for r in results)
        status = "ok" if not failed else "partial" if worked else "error"
        store.finish_run(run_id, status, results, usage, stamp(self._clock()))
        return store.get_run(run_id)

    def _collect(self, source: Source, now: datetime, usage: Usage) -> dict:
        """One source's part of a build. A source that fails is reported and the others go on."""
        result = {"name": source.name, "label": source.label, "connected": True, "ok": None, "items": 0,
                  "error": None, "note": None}
        if not source.connected():
            result["connected"] = False
            return result
        before = len(usage.notes)
        try:
            found = source.collect(now, usage)
            store.save_found(source.name, found, stamp(now))
            store.mark_collected(source.name, stamp(now))  # only after it delivered. A failure hides nothing.
            result.update(ok=True, items=len(found))
        except SourceError as e:
            result.update(ok=False, error=str(e)[:300])
        except Exception as e:
            # Only the kind of failure. The message of an unexpected error can carry a private
            # address or a line of what was being read, and this ends up in the log and on the page.
            log.error("brief source %s stopped with %s", source.name, type(e).__name__)
            result.update(ok=False, error=f"stopped unexpectedly ({type(e).__name__})")
        result["note"] = " ".join(usage.notes[before:])[:300] or None
        return result

    # The Today page -------------------------------------------------------------------

    def _midnight(self) -> str:
        return stamp(self._clock().replace(hour=0, minute=0, second=0, microsecond=0))

    def view(self) -> dict:
        return {
            "enabled": self.cfg.brief.enabled,
            "day": self._clock().date().isoformat(),
            "building": self.building(),
            "run": store.latest_run(),
            "sources": [{"name": s.name, "label": s.label, "connected": s.connected(), "setup": s.setup}
                        for s in self.sources],
            "items": store.list_items(self._midnight()),
        }

    def counts(self) -> dict:
        return store.counts(self._midnight())

    def set_status(self, item_id: int, status: str) -> dict:
        return store.set_status(item_id, status, stamp(self._clock()))

    def draft(self, item_id: int) -> dict:
        """Ask the item's source for a reply and keep it on the item. Returns the item, draft and all.
        Raises ValueError for an item its source can't draft, and SourceError when drafting failed."""
        item = store.get_item(item_id)
        source = next((s for s in self.sources if s.name == item["source"]), None)
        if source is None or not hasattr(source, "draft"):
            raise ValueError("Only mail can be drafted.")
        text, model_key = source.draft(item, self._clock())
        store.save_draft(item_id, text, model_key, stamp(self._clock()))
        return store.get_item(item_id)

    def make_task(self, item_id: int) -> dict:
        """Put the item on the Tasks board, once. Returns the item, which then names its task."""
        item = store.get_item(item_id)
        if item["task_id"] is None:
            if self._create_task is None:
                raise ValueError("this brief has no task board to add to")
            notes = "\n".join(part for part in (item["context"], item["link"]) if part)
            task = self._create_task(item["title"], notes)
            store.link_task(item_id, task["id"])
        return store.get_item(item_id)
