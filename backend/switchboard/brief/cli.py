"""The `brief` command.

    python -m switchboard brief                     build today's brief now
    python -m switchboard brief status              what the last build found, as counts
    python -m switchboard brief connect canvas      save your Canvas calendar feed link
    python -m switchboard brief disconnect canvas   remove it again
    python -m switchboard brief demo                fill a test database with made-up items

`status` and `connect` print numbers and shapes and nothing from the items themselves, so their
output is safe to paste somewhere or to show to an assistant.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from .. import credentials
from ..config import REPO_ROOT, Config
from ..db import init_db
from . import demo, load_sources
from .base import SourceError
from .build import OFF, Brief

BUILD_TIMEOUT_S = 15 * 60


class ServerError(RuntimeError):
    pass


class Server:
    """The running Switchboard server. A build goes through it when there is one, so two processes
    never build at the same time."""

    def __init__(self, base: str):
        self.base = base

    @classmethod
    def find(cls, cfg: Config) -> Server | None:
        import httpx

        host = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
        base = f"http://{host}:{cfg.server.port}"
        try:
            if httpx.get(f"{base}/api/health", timeout=2).json().get("ok") is True:
                return cls(base)
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def call(self, method: str, path: str) -> dict:
        import httpx

        try:
            r = httpx.request(method, self.base + path, timeout=10)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ServerError(f"The server at {self.base} didn't answer ({type(e).__name__}).") from None
        if r.status_code == 404:
            # Only brief routes are asked for here, so a Switchboard without them was started before this code.
            raise ServerError(f"The Switchboard running at {self.base} is older than this command. Restart it.")
        if r.status_code >= 400:
            raise ServerError(str(data.get("detail", data)) if isinstance(data, dict) else f"HTTP {r.status_code}")
        return data


def _local(ts: str | None) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M") if ts else "never"


def summary(view: dict, counts: dict) -> str:
    """The state of the brief in a few lines. Counts only."""
    lines = [f"Morning brief: {'on' if view['enabled'] else 'off'}"]
    run = view["run"]
    if run is None:
        lines.append("Last build: never")
    else:
        lines.append(f"Last build: {_local(run['started_at'])}, {run['status']}, started by {run['started_by']}")
        for s in run["sources"]:
            if not s["connected"]:
                outcome = "not connected"
            elif s["ok"]:
                outcome = f"{s['items']} found"
            else:
                outcome = f"failed, {s['error']}"
            lines.append(f"  {s['label']}: {outcome}")
        if run["error"]:
            lines.append(f"  {run['error']}")
    by_source = ", ".join(f"{name} {n}" for name, n in counts["open_by_source"].items())
    lines.append(f"Open: {counts['open']}" + (f" ({by_source})" if by_source else ""))
    lines.append(f"Closed today: {counts['closed']}")
    return "\n".join(lines)


def counts_of(view: dict) -> dict:
    """The numbers store.counts gives, worked out from a view the server sent."""
    by_source: dict[str, int] = {}
    closed = 0
    for item in view["items"]:
        if item["status"] == "open":
            by_source[item["source"]] = by_source.get(item["source"], 0) + 1
        else:
            closed += 1
    return {"open": sum(by_source.values()), "open_by_source": dict(sorted(by_source.items())), "closed": closed}


def _local_brief(cfg: Config) -> Brief:
    init_db(cfg.db_path)
    return Brief(cfg, load_sources(cfg))


def _build(cfg: Config) -> int:
    if not cfg.brief.enabled:
        print(OFF, file=sys.stderr)
        return 1
    server = Server.find(cfg)
    if server is None:
        brief = _local_brief(cfg)
        brief.recover()
        brief.run("cli")
        view, counts = brief.view(), brief.counts()
    else:
        view = server.call("POST", "/api/brief/run")
        deadline = time.monotonic() + BUILD_TIMEOUT_S
        while view["building"] and time.monotonic() < deadline:
            time.sleep(1)
            view = server.call("GET", "/api/brief")
        if view["building"]:
            print("The brief is still being built. Look again with: python -m switchboard brief status",
                  file=sys.stderr)
            return 1
        counts = counts_of(view)
    print(summary(view, counts))
    return 0 if view["run"] and view["run"]["status"] != "error" else 1


def _status(cfg: Config) -> int:
    server = Server.find(cfg)
    if server is None:
        brief = _local_brief(cfg)
        view, counts = brief.view(), brief.counts()
    else:
        view = server.call("GET", "/api/brief")
        counts = counts_of(view)
    print(summary(view, counts))
    return 0


def _demo(cfg: Config) -> int:
    if cfg.data_path.resolve() == (REPO_ROOT / "data").resolve():
        print("brief demo writes made-up items, so it won't touch the default data folder.\n"
              "Point SWITCHBOARD_CONFIG at a copy of config.yaml that sets server.data_dir to a folder "
              "of its own, and another server.port.", file=sys.stderr)
        return 1
    brief = _local_brief(cfg)
    brief.recover()
    brief.run("demo", demo.sources())
    print(summary(brief.view(), brief.counts()))
    return 0


def _connect_canvas(cfg: Config) -> int:
    """Ask for the feed link without showing it, check that it really is a calendar, then save it
    to the credential store. What it prints is the feed's shape and nothing it says."""
    import getpass

    import httpx

    from .sources import canvas_feed, ical

    print("In Canvas, open Calendar, click Calendar Feed and copy the link.")
    url = getpass.getpass("Paste it here (it won't be shown): ").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        host = httpx.URL(url).host if url.lower().startswith("https://") else ""
    except Exception:
        host = ""
    if not host:
        print("Not saved. That isn't an https link.", file=sys.stderr)
        return 1

    feed = canvas_feed.CanvasFeed(cfg.brief.coursework)
    now = datetime.now().astimezone()
    try:
        events = ical.events(feed.fetch(url), now.tzinfo)
        credentials.save(canvas_feed.FEED_URL, url)
    except (SourceError, credentials.CredentialError) as e:
        print(f"Not saved. {e}", file=sys.stderr)
        return 1
    print("Saved the link to this machine's credential store.")
    print("\n".join(canvas_feed.shape(events, host)))
    print(f"The brief would list {len(feed.select(events, now, host))} of them right now.")
    if not cfg.brief.enabled:
        print(OFF)
    return 0


def _disconnect_canvas(cfg: Config) -> int:
    from .sources import canvas_feed

    removed = credentials.forget(canvas_feed.FEED_URL)
    print("Removed the Canvas feed link." if removed else "There was no Canvas feed link saved.")
    return 0


TARGETS = {"canvas": (_connect_canvas, _disconnect_canvas)}


def _target(args, cfg: Config) -> int:
    if args.target not in TARGETS:
        print(f"brief {args.action} needs to know what: {', '.join(TARGETS)}", file=sys.stderr)
        return 2
    connect, disconnect = TARGETS[args.target]
    return (connect if args.action == "connect" else disconnect)(cfg)


def run(args, cfg: Config) -> int:
    if args.action in ("connect", "disconnect"):
        return _target(args, cfg)
    try:
        return {"build": _build, "status": _status, "demo": _demo}[args.action](cfg)
    except ServerError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
