"""The `brief` command.

    python -m switchboard brief                     build today's brief now
    python -m switchboard brief status              what the last build found, as counts
    python -m switchboard brief connect canvas      save your Canvas calendar feed link
    python -m switchboard brief connect outlook     sign in to Outlook with a code
    python -m switchboard brief disconnect canvas   remove the link again (or: disconnect outlook)
    python -m switchboard brief demo                fill a test database with made-up items

`status` and `connect` print numbers and shapes and nothing from the items themselves, so their
output is safe to paste somewhere or to show to an assistant.
"""

from __future__ import annotations

import contextlib
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
            if s.get("note"):
                lines.append(f"    {s['note']}")
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


REGISTER = """Outlook needs an app registration of your own before it can sign in. It's free and takes a few minutes.

  1. Sign in at https://entra.microsoft.com with your Microsoft account. Open App registrations and
     click New registration. If it says your account needs a directory first, a free Azure account
     gives it one.
  2. Name it Switchboard and pick "Personal Microsoft accounts only". Leave the redirect URI empty.
  3. Under Authentication, set "Allow public client flows" to Yes and save.
  4. Under API permissions, add the Microsoft Graph delegated permissions Mail.Read and offline_access.
  5. Copy the Application (client) ID from the Overview page into config.local.yaml, next to config.yaml:

       brief:
         mail:
           client_id: <the id>

Then run this command again."""


def _copy_to_clipboard(text: str) -> bool:
    """Put `text` on the clipboard, where the system has a command for it. False when it couldn't."""
    import subprocess

    command = {"win32": ["clip"], "darwin": ["pbcopy"]}.get(sys.platform)
    if command is None:
        return False
    try:
        subprocess.run(command, input=text, text=True, check=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _show_code(flow: dict, open_page) -> None:
    """Show the code before anything else happens. The browser opens on top of this window, and a
    code printed next to the instructions is easy to miss once it does, so the page only opens when
    you press Enter."""
    code, page = str(flow["user_code"]), str(flow["verification_uri"])
    minutes = max(int(flow.get("expires_in") or 900) // 60, 1)
    copied = _copy_to_clipboard(code)
    print("Microsoft's sign-in page will ask for a code. Your code is:\n")
    print(f"    {code}\n")
    print(("It's on the clipboard too, so you can paste it there. " if copied else "")
          + f"It works for {minutes} minutes.")
    if page.startswith("https://"):
        with contextlib.suppress(EOFError):  # nobody at the keyboard. Open it anyway.
            input(f"Press Enter to open {page} in your browser...")
        with contextlib.suppress(Exception):
            open_page(page)
    else:
        print(f"Open {page} in your browser.")
    print("On that page, enter the code, then sign in with the Microsoft account whose mail the brief should read "
          "and let Switchboard read it.")
    print(f"Waiting for the sign-in to finish. The code again: {code}. Press Ctrl+C to stop.")


def _connect_outlook(cfg: Config) -> int:
    """Sign in to Outlook with a code, then save the sign-in to the credential store. When a saved
    sign-in still works, it is used and no code is needed, so running this again just checks the
    mailbox. What it prints is the mailbox's shape and nothing any message says."""
    import webbrowser

    from .sources import outlook

    mail = cfg.brief.mail
    if not mail.client_id:
        print(REGISTER)
        return 1
    with outlook._client() as http:
        sign_in = outlook.SignIn(mail, http)
        tokens = None
        saved = credentials.get(outlook.REFRESH_TOKEN)
        if saved:
            try:
                tokens = sign_in.refresh(saved)
            except SourceError:
                print("The saved sign-in no longer works, so here is a new code.")
            else:
                print("Already signed in, so no code is needed. To sign in as someone else, run "
                      "python -m switchboard brief disconnect outlook first.")
        if tokens is None:
            try:
                flow = sign_in.start()
            except SourceError as e:
                print(f"Couldn't start the sign-in. {e}", file=sys.stderr)
                return 1
            try:
                _show_code(flow, webbrowser.open)
                tokens = sign_in.wait(flow)
            except SourceError as e:
                print(f"Not connected. {e}", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                print("Stopped. Nothing was saved.", file=sys.stderr)
                return 1
            signed_in = "Signed in, and saved the sign-in to this machine's credential store."
        else:
            signed_in = None
        try:
            credentials.save(outlook.REFRESH_TOKEN, tokens["refresh_token"])
        except credentials.CredentialError as e:
            print(f"Signed in, but not saved. {e}", file=sys.stderr)
            return 1
        if signed_in:
            print(signed_in)
        try:
            snapshot = outlook.read(mail, http, tokens["access_token"], datetime.now().astimezone())
        except SourceError as e:
            print(f"Reading the inbox failed. {e}", file=sys.stderr)
            return 1
    print("\n".join(outlook.shape(snapshot, mail)))
    if not cfg.brief.enabled:
        print(OFF)
    return 0


def _disconnect_outlook(cfg: Config) -> int:
    from .sources import outlook

    removed = credentials.forget(outlook.REFRESH_TOKEN)
    print("Removed the Outlook sign-in from this machine." if removed else "There was no Outlook sign-in saved.")
    print("To take back the permission itself, remove Switchboard from the apps that can use your Microsoft account.")
    return 0


TARGETS = {"canvas": (_connect_canvas, _disconnect_canvas), "outlook": (_connect_outlook, _disconnect_outlook)}


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
