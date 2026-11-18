"""The desktop shell, minus the window and the tray icon themselves. Nothing here needs a display,
and pywebview and pystray are never imported: fakes stand in for the window and the icon."""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections import namedtuple
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI

from switchboard import desktop, startup
from switchboard.__main__ import build_parser, main
from switchboard.config import REPO_ROOT
from switchboard.main import create_app

URL = "http://127.0.0.1:8787"
LIFESPAN_THREADS = {"cc-sync", "indexer"}


class FakeWindow:
    def __init__(self):
        self.calls: list[str] = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def restore(self):
        self.calls.append("restore")

    def destroy(self):
        self.calls.append("destroy")


class FakeIcon:
    def __init__(self):
        self.notes: list[tuple] = []
        self.menu_updates = 0

    def notify(self, message, title=None):
        self.notes.append((title, message))

    def update_menu(self):
        self.menu_updates += 1


class FakeBrief:
    """Stands in for the server's brief, for the tray's sake."""

    def __init__(self, started: bool = True, error: Exception | None = None):
        self.started_by: list[str] = []
        self._started, self._error = started, error

    def start(self, started_by: str) -> bool:
        self.started_by.append(started_by)
        if self._error:
            raise self._error
        return self._started

    def view(self) -> dict:
        return {"items": [{"status": "open", "source": "mail", "owner": "you", "title": "Secret offer"}]}


class FakePystray:
    """Just enough of pystray to build the menu with."""

    class Menu:
        SEPARATOR = "---"

    @staticmethod
    def MenuItem(text, action, **options):
        return (text, action, options)


def shell_with_window() -> desktop.Shell:
    shell = desktop.Shell(URL)
    shell.window = FakeWindow()
    return shell


def serve(app) -> tuple[str, desktop.ServerThread]:
    """Start a server thread on a free port and wait until it answers."""
    sock = desktop.claim_port("127.0.0.1", 0)
    port = sock.getsockname()[1]
    url = desktop.local_url("127.0.0.1", port)
    server = desktop.ServerThread(app, sock, "127.0.0.1", port)
    server.start()
    assert server.wait_ready(url, timeout=20)
    return url, server


def free_port() -> int:
    sock = desktop.claim_port("127.0.0.1", 0)
    port = sock.getsockname()[1]
    sock.close()
    return port


def alive_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.is_alive()}


@pytest.fixture
def running_app(cfg, fake_ollama):
    """What `app` runs: the real FastAPI app with the show route in front, minus the GUI."""
    shell = shell_with_window()
    app = create_app(cfg)
    app.add_middleware(desktop.ShowRoute, show=shell.show)
    url, server = serve(app)
    yield url, shell
    assert server.stop()


@pytest.fixture
def no_file_log(monkeypatch):
    monkeypatch.setattr(desktop, "setup_logging", lambda path: None)


# Arguments -----------------------------------------------------------------------


def test_app_arguments():
    args = build_parser().parse_args(["app"])
    assert (args.cmd, args.host, args.port, args.hidden, args.devtools) == ("app", None, None, False, False)
    args = build_parser().parse_args(["app", "--host", "0.0.0.0", "--port", "9000", "--hidden", "--devtools"])
    assert (args.host, args.port, args.hidden, args.devtools) == ("0.0.0.0", 9000, True, True)


def test_serve_arguments_are_unchanged():
    parser = build_parser()
    assert vars(parser.parse_args([])) == {"open": False, "cmd": None}
    assert vars(parser.parse_args(["--open"])) == {"open": True, "cmd": None}
    assert vars(parser.parse_args(["serve", "--port", "9000", "--open"])) == {
        "open": True, "cmd": "serve", "host": None, "port": 9000}


def test_main_hands_app_to_the_desktop_module(monkeypatch):
    seen = []

    def fake_run(args):
        seen.append(args)
        return 7

    def no_config():
        raise AssertionError("the app loads its own config, so it can report a bad one in a dialog")

    monkeypatch.setattr(desktop, "run", fake_run)
    monkeypatch.setattr("switchboard.config.get_config", no_config)
    assert main(["app", "--hidden"]) == 7
    assert seen[0].hidden is True


def test_gui_packages_load_only_inside_the_app():
    code = ("import sys; import switchboard.__main__, switchboard.desktop; "
            "sys.exit(int(any(m in sys.modules for m in ('webview', 'pystray', 'PIL'))))")
    backend = Path(__file__).resolve().parents[1]
    assert subprocess.run([sys.executable, "-c", code], cwd=backend).returncode == 0


def test_local_url():
    assert desktop.local_url("127.0.0.1", 8787) == URL
    assert desktop.local_url("0.0.0.0", 8787) == URL  # every interface: reach it on loopback
    assert desktop.local_url("::1", 9000) == "http://[::1]:9000"


def test_window_size_fits_the_smallest_screen():
    screen = namedtuple("Screen", "width height")
    assert desktop.window_size([]) == desktop.WINDOW_SIZE
    assert desktop.window_size([screen(3440, 1440)]) == desktop.WINDOW_SIZE
    assert desktop.window_size([screen(3440, 1440), screen(1366, 768)]) == (1229, 691)
    assert desktop.window_size([screen(800, 600)]) == desktop.WINDOW_MIN_SIZE


# One instance per port -----------------------------------------------------------


def test_claim_port_is_exclusive():
    first = desktop.claim_port("127.0.0.1", 0)
    port = first.getsockname()[1]
    try:
        assert desktop.claim_port("127.0.0.1", port) is None
    finally:
        first.close()
    again = desktop.claim_port("127.0.0.1", port)
    assert again is not None
    again.close()


def test_claim_port_sees_another_programs_listener():
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    try:
        assert desktop.claim_port("127.0.0.1", other.getsockname()[1]) is None
    finally:
        other.close()


def test_second_launch_brings_the_window_forward(running_app):
    url, shell = running_app
    assert desktop.hand_over(url) == "app"
    assert "show" in shell.window.calls


def test_hidden_second_launch_leaves_the_window_alone(running_app):
    url, shell = running_app
    assert desktop.hand_over(url, show=False) == "server"
    assert shell.window.calls == []


def test_hand_over_to_a_server_without_a_window(cfg, fake_ollama):
    url, server = serve(create_app(cfg, background_sync=False))  # what `serve` runs: no show route
    try:
        assert desktop.hand_over(url) == "server"
    finally:
        assert server.stop()


def test_hand_over_when_another_web_server_has_the_port():
    class NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(404)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    other = http.server.HTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert desktop.hand_over(f"http://127.0.0.1:{other.server_address[1]}", timeout=5) == "other"
    finally:
        other.shutdown()
        other.server_close()


@pytest.mark.parametrize("greeting", [b"", b"SSH-2.0-OpenSSH_9.6\r\n"], ids=["hangs up", "talks another protocol"])
def test_hand_over_when_something_that_is_not_http_has_the_port(greeting):
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()

    def answer():
        while True:
            try:
                conn = other.accept()[0]
            except OSError:
                return
            with conn:
                if greeting:
                    conn.recv(4096)  # take the request first, or closing resets the connection
                    conn.sendall(greeting)

    threading.Thread(target=answer, daemon=True).start()
    try:
        assert desktop.hand_over(f"http://127.0.0.1:{other.getsockname()[1]}", timeout=5) == "other"
    finally:
        other.close()


def test_second_launch_outcomes(monkeypatch):
    alerts, opened, found = [], [], ["app"]
    monkeypatch.setattr(desktop, "alert", alerts.append)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    monkeypatch.setattr(desktop, "hand_over", lambda url, show=True: found[0])

    assert desktop.second_launch(URL, 8787, hidden=False) == 0
    assert not alerts and not opened

    found[0] = "server"  # started with `serve`: explain, and open the browser instead
    assert desktop.second_launch(URL, 8787, hidden=False) == 0
    assert opened == [URL] and "already running" in alerts[0]
    assert desktop.second_launch(URL, 8787, hidden=True) == 0  # a hidden start stays quiet
    assert len(alerts) == 1 and len(opened) == 1

    found[0] = "other"
    assert desktop.second_launch(URL, 8787, hidden=False) == 1
    assert "8787" in alerts[-1]


# The show route ------------------------------------------------------------------


def post_show(url: str, headers: dict) -> tuple[int, dict | None]:
    request = urllib.request.Request(url + desktop.SHOW_PATH, data=b"", method="POST", headers=headers)
    try:
        with desktop._http.open(request, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_show_route_needs_the_header(running_app):
    url, shell = running_app
    assert post_show(url, {})[0] == 403  # what a web page could send
    assert shell.window.calls == []
    assert post_show(url, {desktop.SHOW_HEADER: "1"}) == (200, {"ok": True, "app": "Switchboard"})
    assert "show" in shell.window.calls
    with pytest.raises(urllib.error.HTTPError) as e:
        desktop._http.open(url + desktop.SHOW_PATH, timeout=10)
    assert e.value.code == 405


def test_show_route_leaves_the_api_alone(running_app):
    url, _ = running_app
    assert desktop._fetch_json(url + "/api/health", 10) == {"ok": True}
    assert desktop._fetch_json(url + "/api/tasks", 10) is None  # a list, so the API answered as itself


def test_show_route_is_for_this_machine_only():
    shown, sent = [], []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": desktop.SHOW_PATH, "client": ("192.168.1.20", 50000),
             "headers": [(desktop.SHOW_HEADER.encode(), b"1")]}
    asyncio.run(desktop.ShowRoute(None, lambda: shown.append(1))(scope, receive, send))
    assert sent[0]["status"] == 403 and not shown


# Server thread -------------------------------------------------------------------


def test_server_thread_starts_and_stops_with_the_lifespan(cfg, fake_ollama):
    url, server = serve(create_app(cfg))
    port = int(url.rsplit(":", 1)[1])
    assert desktop._fetch_json(url + "/api/health", 10) == {"ok": True}
    assert LIFESPAN_THREADS | {"server"} <= alive_threads()

    assert server.stop()
    assert not (LIFESPAN_THREADS | {"server"}) & alive_threads()
    again = desktop.claim_port("127.0.0.1", port)  # the port is free for the next start
    assert again is not None
    again.close()
    assert server.stop()  # stopping twice is fine


def test_wait_ready_gives_up_when_the_server_dies():
    @asynccontextmanager
    async def broken(app):
        raise RuntimeError("the database is locked")
        yield

    sock = desktop.claim_port("127.0.0.1", 0)
    port = sock.getsockname()[1]
    server = desktop.ServerThread(FastAPI(lifespan=broken), sock, "127.0.0.1", port)
    server.start()
    assert server.wait_ready(desktop.local_url("127.0.0.1", port), timeout=20) is False
    assert not server.thread.is_alive()


def test_stop_reports_a_request_that_outlasts_it():
    started, release = threading.Event(), threading.Event()
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/slow")
    def slow():  # a model that is still answering when Quit is clicked
        started.set()
        release.wait(30)
        return {"done": True}

    url, server = serve(app)
    caller = threading.Thread(target=lambda: desktop._fetch_json(url + "/slow", 30), daemon=True)
    caller.start()
    assert started.wait(10)
    try:
        assert server.stop(timeout=0.5) is False  # run() exits the process at this point
    finally:
        release.set()
    server.thread.join(15)
    assert not server.thread.is_alive()


# Window and tray -----------------------------------------------------------------


def test_closing_the_window_hides_it():
    shell = shell_with_window()
    assert shell.on_closing() is False  # False cancels the close
    assert shell.window.calls == ["hide"]


def test_a_close_that_is_not_the_users_goes_through():
    shell = shell_with_window()
    assert shell.on_closing(by_user=False) is True  # Windows shutting down, Task Manager
    assert shell.window.calls == []
    assert shell.on_closing() is False  # and the X button still only hides afterwards


def test_quit_closes_the_window_for_real():
    shell = shell_with_window()
    shell.quit()
    assert shell.window.calls == ["destroy"]
    assert shell.on_closing() is True
    shell.show()  # a second launch that arrives while quitting
    assert shell.window.calls == ["destroy"]


def test_show_brings_the_window_back():
    shell = shell_with_window()
    shell.on_closing()
    shell.show()
    assert shell.window.calls[:2] == ["hide", "show"]


def test_shell_before_the_window_exists():
    shell = desktop.Shell(URL)
    shell.show()  # a second launch can arrive while the first is still starting
    shell.notify("nothing to show it on yet")
    shell.quit()
    assert shell.quitting


def test_open_in_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    desktop.Shell(URL).open_in_browser()
    assert opened == [URL]


def test_notify_goes_to_the_tray_icon():
    shell = desktop.Shell(URL)
    shell.icon = FakeIcon()
    shell.notify("3 things need you today")
    shell.notify("Reply to the recruiter", title="Morning brief")
    assert shell.icon.notes == [("Switchboard", "3 things need you today"), ("Morning brief", "Reply to the recruiter")]


# The morning brief from the tray ---------------------------------------------------


def test_build_the_brief_from_the_tray():
    shell = desktop.Shell(URL, brief=FakeBrief())
    shell.icon = FakeIcon()
    shell.build_brief()
    assert shell.brief.started_by == ["tray"] and shell.icon.notes == []  # the notification comes when it is done

    shell.brief = FakeBrief(started=False)
    shell.build_brief()
    assert shell.icon.notes == [("Switchboard", "The brief is already being built.")]
    shell.brief = FakeBrief(error=ValueError("The morning brief is turned off."))
    shell.build_brief()
    assert shell.icon.notes[-1] == ("Switchboard", "The morning brief is turned off.")


def test_only_builds_nobody_is_watching_get_a_notification():
    shell = desktop.Shell(URL, brief=FakeBrief())
    shell.icon = FakeIcon()
    shell.brief_built({"status": "ok", "started_by": "manual"})  # built from the page, which you are looking at
    assert shell.icon.notes == []
    for started_by in ("schedule", "tray"):
        shell.brief_built({"status": "ok", "started_by": started_by})
    assert shell.icon.notes == [("Switchboard", "Today's brief is ready: 1 needs you.")] * 2
    assert all("Secret" not in message for _, message in shell.icon.notes)


def test_the_tray_menu(cfg, monkeypatch):
    def labels(items) -> list[str]:
        return [item if item == "---" else item[0] for item in items]

    monkeypatch.setattr(startup, "supported", lambda: True)
    cfg.brief.enabled = True
    shell = desktop.Shell(URL, brief=FakeBrief())
    items = desktop.tray_menu(FakePystray, shell, cfg)
    assert labels(items) == ["Open", "Open in browser", "---", "Build the brief now", "Start with Windows", "---",
                             "Quit"]
    assert items[0][2] == {"default": True}
    monkeypatch.setattr(startup, "enabled", lambda: True)
    assert items[4][2]["checked"](None) is True  # the check mark follows the shortcut

    cfg.brief.enabled = False
    monkeypatch.setattr(startup, "supported", lambda: False)
    assert labels(desktop.tray_menu(FakePystray, shell, cfg)) == ["Open", "Open in browser", "---", "Quit"]


def test_start_with_windows_from_the_tray(monkeypatch):
    state = {"on": False}

    def toggle() -> bool:
        state["on"] = not state["on"]
        return state["on"]

    monkeypatch.setattr(startup, "toggle", toggle)
    shell = desktop.Shell(URL)
    shell.icon = FakeIcon()
    shell.toggle_startup()
    shell.toggle_startup()
    assert [message for _, message in shell.icon.notes] == [
        "Switchboard will start in the tray when you sign in.", "Switchboard won't start when you sign in anymore."]
    assert shell.icon.menu_updates == 2

    def broken() -> bool:
        raise OSError("access denied to C:\\Users\\someone")

    monkeypatch.setattr(startup, "toggle", broken)
    shell.toggle_startup()
    assert shell.icon.notes[-1] == ("Switchboard", "Couldn't change Start with Windows (OSError).")


def test_start_with_windows_makes_and_removes_a_shortcut(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(startup, "supported", lambda: True)
    ran = []

    def fake_powershell(args, **options):
        ran.append((args, options["env"]))
        Path(options["env"]["SB_LINK"]).write_text("a shortcut", encoding="utf-8")

    monkeypatch.setattr(startup.subprocess, "run", fake_powershell)
    assert startup.enabled() is False
    assert startup.toggle() is True

    ((args, env),) = ran
    assert args[0] == "powershell" and "CreateShortcut($env:SB_LINK)" in args[-1]
    assert Path(env["SB_LINK"]) == startup.startup_folder() / "Switchboard.lnk"
    assert startup.startup_folder() == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    assert env["SB_TARGET"].endswith("powershell.exe")
    assert "-WindowStyle Hidden" in env["SB_ARGS"] and env["SB_ARGS"].endswith('start.ps1" app --hidden')
    assert env["SB_DIR"] == str(REPO_ROOT) and env["SB_ICON"].endswith("switchboard.ico,0")
    assert startup.toggle() is False
    assert not startup.shortcut_path().exists()


def test_start_with_windows_is_for_windows_only(monkeypatch):
    monkeypatch.setattr(startup, "supported", lambda: False)
    assert startup.enabled() is False
    with pytest.raises(OSError, match="only set up for Windows"):
        startup.enable()


# The command ---------------------------------------------------------------------


def test_run_serves_until_the_window_is_gone(cfg, fake_ollama, monkeypatch, no_file_log):
    seen = {}

    def fake_gui(cfg_, shell, *, hidden, devtools):
        shell.window = FakeWindow()
        seen["health"] = desktop._fetch_json(shell.url + "/api/health", 10)
        seen["threads"] = alive_threads()
        seen["second launch"] = desktop.hand_over(shell.url)
        seen["window"] = list(shell.window.calls)
        seen["flags"] = (hidden, devtools)

    monkeypatch.setattr(desktop, "get_config", lambda: cfg)
    monkeypatch.setattr(desktop, "run_gui", fake_gui)
    port = free_port()
    assert desktop.run(build_parser().parse_args(["app", "--port", str(port), "--hidden"])) == 0

    assert seen["health"] == {"ok": True}
    assert LIFESPAN_THREADS | {"server"} <= seen["threads"]
    assert seen["second launch"] == "app" and "show" in seen["window"]
    assert seen["flags"] == (True, False)
    assert not (LIFESPAN_THREADS | {"server"}) & alive_threads()  # Quit stopped all of it
    again = desktop.claim_port("127.0.0.1", port)
    assert again is not None
    again.close()


def test_run_hands_over_instead_of_starting_twice(running_app, cfg, monkeypatch, no_file_log):
    url, shell = running_app
    monkeypatch.setattr(desktop, "get_config", lambda: cfg)
    monkeypatch.setattr(desktop, "run_gui", lambda *a, **k: pytest.fail("a second window was opened"))
    args = build_parser().parse_args(["app", "--port", url.rsplit(":", 1)[1]])
    assert desktop.run(args) == 0
    assert "show" in shell.window.calls


def test_run_reports_a_config_that_does_not_load(monkeypatch):
    def broken():
        raise ValueError("config.yaml problems:\n  - routing.rules is empty")

    alerts = []
    monkeypatch.setattr(desktop, "get_config", broken)
    monkeypatch.setattr(desktop, "alert", alerts.append)
    assert desktop.run(build_parser().parse_args(["app"])) == 1
    assert "routing.rules is empty" in alerts[0]


def test_run_reports_a_server_that_does_not_start(cfg, fake_ollama, monkeypatch, no_file_log):
    alerts = []
    monkeypatch.setattr(desktop, "get_config", lambda: cfg)
    monkeypatch.setattr(desktop, "alert", alerts.append)
    monkeypatch.setattr(desktop.ServerThread, "wait_ready", lambda self, url, timeout=30.0: False)
    monkeypatch.setattr(desktop, "run_gui", lambda *a, **k: pytest.fail("a window opened without a server"))
    assert desktop.run(build_parser().parse_args(["app", "--port", str(free_port())])) == 1
    assert "didn't start" in alerts[0] and "switchboard.log" in alerts[0]
    assert "server" not in alive_threads()


def test_run_frees_the_port_when_the_app_cannot_be_built(cfg, monkeypatch, no_file_log):
    def broken(cfg):
        raise RuntimeError("database disk image is malformed")

    alerts = []
    monkeypatch.setattr(desktop, "get_config", lambda: cfg)
    monkeypatch.setattr(desktop, "alert", alerts.append)
    monkeypatch.setattr("switchboard.main.create_app", broken)
    port = free_port()
    assert desktop.run(build_parser().parse_args(["app", "--port", str(port)])) == 1
    assert "malformed" in alerts[0]
    again = desktop.claim_port("127.0.0.1", port)  # not left listening behind the error dialog
    assert again is not None
    again.close()


def test_alert_prints_when_there_is_a_console(capsys):
    desktop.alert("Port 8787 is in use by another program")
    assert "Port 8787" in capsys.readouterr().err


# Log file ------------------------------------------------------------------------


@pytest.fixture
def clean_logging():
    """setup_logging changes the root logger. Put it back."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.NOTSET)


def test_log_goes_to_a_file_when_there_is_no_console(tmp_path, monkeypatch, clean_logging):
    monkeypatch.setattr(sys, "stdout", None)  # what pythonw gives a program
    monkeypatch.setattr(sys, "stderr", None)
    path = tmp_path / "data" / "logs" / "switchboard.log"
    desktop.setup_logging(path)
    assert not path.exists()  # opened by the first line, so a second launch doesn't hold it

    logging.getLogger("switchboard").warning("vector index unavailable")
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    logging.getLogger("httpx").info("HTTP Request: GET /api/ps")
    print("a stray print")
    sys.stderr.write("Traceback (most recent call last):\n  boom\n")

    text = path.read_text(encoding="utf-8")
    assert "WARNING switchboard: vector index unavailable" in text
    assert "INFO uvicorn.error: Application startup complete." in text
    assert "INFO stdout: a stray print" in text
    assert "ERROR stderr: Traceback (most recent call last):" in text and "ERROR stderr:   boom" in text
    assert "HTTP Request" not in text  # one per status check on Ollama, so left out


def test_log_also_prints_when_there_is_a_console(tmp_path, capsys, clean_logging):
    stderr = sys.stderr
    path = tmp_path / "switchboard.log"
    desktop.setup_logging(path)
    logging.getLogger("switchboard.desktop").info("Switchboard on %s", URL)
    assert sys.stderr is stderr
    assert f"Switchboard on {URL}" in capsys.readouterr().err
    assert f"Switchboard on {URL}" in path.read_text(encoding="utf-8")


def test_log_writer_waits_for_whole_lines():
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("test.logwriter")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(Collect())
    writer = desktop.LogWriter(logger, logging.INFO)
    writer.write("half a ")
    assert records == []
    writer.write("line\n\nand the start")
    assert records == ["half a line"]  # the blank line is dropped
    writer.write(" of another\n")
    writer.flush()
    assert records == ["half a line", "and the start of another"]
    assert writer.isatty() is False
