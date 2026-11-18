"""Switchboard as a desktop app: the same server and dashboard, in a window of their own.

    python -m switchboard app

The server runs in a background thread and a native window (pywebview) shows the dashboard in place
of a browser tab. A tray icon (pystray) has Open, Open in browser and Quit. Closing the window only
hides it, so the server keeps answering `python -m switchboard ask`. Quit stops everything.

One copy runs per port. A second launch finds the port taken, asks the first one to bring its window
forward and exits.

pywebview and pystray are imported inside the functions that use them, the way uvicorn and
qdrant_client are elsewhere. This module and its tests need neither a display nor the packages in
requirements-desktop.txt.

The tray menu can also build the morning brief now, and turn on starting with Windows, which runs
the app with `--hidden`: in the tray, with no window (startup.py). A build nobody is watching, from
the schedule or the tray, ends in a notification from the tray icon, with counts and nothing else.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import startup
from .brief import ready_message
from .config import REPO_ROOT, Config, get_config

log = logging.getLogger("switchboard.desktop")

APP_NAME = "Switchboard"
# Windows files a window under the program that made it, which here is pythonw.exe, unless the
# process names itself with an AppUserModelID. Notifications will have to use the same id.
APP_ID = "Switchboard.Desktop"
ICON_PATH = REPO_ROOT / "switchboard.ico"

WINDOW_SIZE = (1280, 860)
WINDOW_MIN_SIZE = (900, 600)  # the dashboard moves its sidebar to the top below 820 px
PAGE_COLOR = "#1d1915"  # --page in styles.css. Shown until the dashboard has loaded, and the title bar.
INK_COLOR = "#f1e9dc"  # --ink, the title bar's text

SHOW_PATH = "/api/desktop/show"
SHOW_HEADER = "x-switchboard-desktop"
LOOPBACK = ("127.0.0.1", "::1")

GRACE_S = 5  # how long Quit waits for a request that is still being answered before dropping it
STOP_TIMEOUT_S = 20  # GRACE_S, plus the lifespan joining its two threads for up to 5 s each

# Requests to our own server must not go through a system proxy.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_url(host: str, port: int) -> str:
    """Where this machine reaches the server. A server on every interface is reached on loopback."""
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def window_size(screens: Iterable = ()) -> tuple[int, int]:
    """The default window size, shrunk to fit the smallest screen."""
    width, height = WINDOW_SIZE
    for screen in screens:
        width = min(width, int(screen.width * 0.9))
        height = min(height, int(screen.height * 0.9))
    return max(width, WINDOW_MIN_SIZE[0]), max(height, WINDOW_MIN_SIZE[1])


# Log file ------------------------------------------------------------------------


def log_file(cfg: Config) -> Path:
    return cfg.data_path / "logs" / "switchboard.log"


class LogWriter:
    """Stands in for sys.stdout and sys.stderr when there is no console. pythonw sets both to None,
    so print() does nothing and code that calls sys.stderr.write() crashes. This sends each line to
    the log instead."""

    encoding = "utf-8"

    def __init__(self, logger: logging.Logger, level: int):
        self._logger = logger
        self._level = level
        self._local = threading.local()  # a buffer per thread, so lines from two threads don't mix

    def write(self, text: str) -> int:
        state = self._local
        if getattr(state, "busy", False):
            return len(text)  # logging is reporting its own failure here. Passing it on would loop.
        *lines, state.pending = (getattr(state, "pending", "") + text).split("\n")
        state.busy = True
        try:
            for line in lines:
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        finally:
            state.busy = False
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def setup_logging(path: Path) -> None:
    """Send the server log to a file. With a console it also prints there as usual."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # delay: the file is opened by the first line written. A second launch that only brings the
    # running app forward writes none, so it never holds the file while the first one rotates it.
    file_handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8", delay=True)
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # or every status check on Ollama is a line
    if sys.stderr is None:
        sys.stderr = LogWriter(logging.getLogger("stderr"), logging.ERROR)
    else:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    if sys.stdout is None:
        sys.stdout = LogWriter(logging.getLogger("stdout"), logging.INFO)


def alert(text: str) -> None:
    """Tell the user about a problem. Without a console that takes a dialog."""
    if sys.platform == "win32" and (sys.stderr is None or isinstance(sys.stderr, LogWriter)):
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x30)  # MB_ICONWARNING
    else:
        print(text, file=sys.stderr)


# One instance per port -----------------------------------------------------------


def claim_port(host: str, port: int) -> socket.socket | None:
    """Bind the server's port and start listening on it. Returns None when it is already taken,
    which is how a second launch finds out that Switchboard is running.

    Listening right away means a second launch that arrives while this one is still starting waits
    in the queue for its answer instead of being refused."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # Without this another program could bind the same port with SO_REUSEADDR.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError as e:
        sock.close()
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            return None
        raise
    return sock


def _fetch_json(request: str | urllib.request.Request, timeout: float) -> dict | None:
    """The JSON object a request returns. None for an HTTP error status or a body that isn't one.
    Raises OSError when nothing answers, and http.client.HTTPException when the answer isn't HTTP."""
    try:
        with _http.open(request, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError:
        return None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def hand_over(url: str, *, show: bool = True, timeout: float = 15.0) -> str:
    """Find out what has the port, and ask it to bring its window forward if it is this app.

    Returns "app" for a running desktop app, "server" for Switchboard without a window (started
    with `serve`, or a desktop app that wasn't asked to show itself) and "other" for anything else."""
    try:
        if show:
            _allow_foreground()
            request = urllib.request.Request(url + SHOW_PATH, data=b"", method="POST", headers={SHOW_HEADER: "1"})
            if (_fetch_json(request, timeout) or {}).get("app") == APP_NAME:
                return "app"
        # No such route, or not asked to show. The health check says whether it is Switchboard at all.
        return "server" if (_fetch_json(url + "/api/health", timeout) or {}).get("ok") is True else "other"
    except (OSError, http.client.HTTPException):  # nothing answers, or it doesn't speak HTTP
        return "other"


def second_launch(url: str, port: int, *, hidden: bool) -> int:
    """The port is taken. Hand over to whatever has it and return the exit code."""
    found = hand_over(url, show=not hidden)
    if found == "app" or (found == "server" and hidden):
        return 0
    if found == "server":
        alert(f"Switchboard is already running on {url}, started from a terminal, so it has no window to "
              "bring forward.\n\nIt opens in your browser instead. For the app window, stop that server first.")
        webbrowser.open(url)
        return 0
    alert(f"Port {port} is in use by another program, so Switchboard can't start.\n\n"
          "Close that program, or change server.port in config.yaml.")
    return 1


class ShowRoute:
    """ASGI middleware that answers POST /api/desktop/show by calling `show`. Everything else goes
    to the app unchanged, so the route only exists while the desktop app is what's running.

    Bringing a window forward is harmless, but a web page or another device on the network has no
    business doing it. The header keeps browsers out, since a page can't send a custom header to
    another origin without a preflight this server never approves. The address check keeps other
    devices out when the server listens beyond loopback."""

    def __init__(self, app, show: Callable[[], None]):
        self.app = app
        self.show = show

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != SHOW_PATH:
            await self.app(scope, receive, send)
            return
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse

        client = (scope.get("client") or ("", 0))[0]
        headers = {name.decode("latin-1") for name, _ in scope["headers"]}
        if scope["method"] != "POST":
            response = JSONResponse({"detail": "Method Not Allowed"}, status_code=405, headers={"Allow": "POST"})
        elif client not in LOOPBACK or SHOW_HEADER not in headers:
            response = JSONResponse({"detail": "Forbidden"}, status_code=403)
        else:
            # Off the event loop: showing a window waits for the GUI thread.
            await run_in_threadpool(self.show)
            response = JSONResponse({"ok": True, "app": APP_NAME})
        await response(scope, receive, send)


# Server thread -------------------------------------------------------------------


class ServerThread:
    """uvicorn in a background thread, on a socket that is already bound."""

    def __init__(self, app, sock: socket.socket, host: str, port: int):
        import uvicorn

        # The log goes to a file, so leave the handlers to setup_logging and leave out the access
        # lines. The dashboard polls every few seconds and would fill the file with them.
        config = uvicorn.Config(app, host=host, port=port, log_level="info", log_config=None, access_log=False,
                                timeout_graceful_shutdown=GRACE_S)
        self.server = uvicorn.Server(config)
        self._sock = sock
        self.thread = threading.Thread(target=self._run, name="server", daemon=True)

    def _run(self) -> None:
        try:
            # Off the main thread uvicorn installs no signal handlers, so stop() is the way out.
            self.server.run(sockets=[self._sock])
        except SystemExit:
            log.error("the server could not start")  # uvicorn has logged why
        except Exception:
            log.exception("the server stopped with an error")
        finally:
            self._sock.close()

    def start(self) -> None:
        self.thread.start()

    def wait_ready(self, url: str, timeout: float = 30.0) -> bool:
        """Wait for /api/health to answer. False when the server died or took too long."""
        deadline = time.monotonic() + timeout
        while self.thread.is_alive() and time.monotonic() < deadline:
            try:
                with _http.open(url + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return True
            except (OSError, http.client.HTTPException):
                time.sleep(0.1)
        return False

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Shut down the way Ctrl+C does for `serve`: let open requests finish, then run the app's
        lifespan shutdown, which stops its background threads. A request still running after GRACE_S
        is dropped. False if all of that took longer than `timeout`."""
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout)
        return not self.thread.is_alive()


# Window and tray -----------------------------------------------------------------


class Shell:
    """The window and the tray icon. `window` is a pywebview window and `icon` a pystray icon. Both
    are filled in once they exist, and the tests put fakes there."""

    def __init__(self, url: str, brief=None):
        self.url = url
        self.window = None
        self.icon = None
        self.quitting = False
        self.brief = brief  # the server's morning brief, or None

    def on_closing(self, by_user: bool = True) -> bool:
        """The window is about to close. Returns False to cancel that.

        The X button hides the window and leaves the server running. Quit closes it for real, and
        so does anything that isn't the user closing it, such as Windows shutting down."""
        if self.quitting or not by_user:
            return True
        self.window.hide()
        return False

    def show(self) -> None:
        """Bring the window back from the tray, or forward from behind other windows."""
        if self.window is None or self.quitting:
            return
        self.window.show()
        hwnd = _hwnd(self.window)
        if hwnd:
            _to_front(hwnd)
        elif sys.platform != "win32":
            self.window.restore()

    def open_in_browser(self) -> None:
        webbrowser.open(self.url)

    def quit(self) -> None:
        """Close the window for real. That ends the GUI loop, and run() then stops the server."""
        self.quitting = True
        if self.window is not None:
            self.window.destroy()

    def notify(self, message: str, title: str = APP_NAME) -> None:
        """Show a desktop notification from the tray icon. A build that ends before the icon exists,
        in the first second or two, goes without one."""
        if self.icon is not None:
            self.icon.notify(message, title)

    def build_brief(self) -> None:
        """Tray menu: build the brief now. A notification says when it is ready."""
        if self.brief is None:
            return
        try:
            started = self.brief.start("tray")
        except ValueError as e:  # the brief is turned off
            self.notify(str(e))
            return
        if not started:
            self.notify("The brief is already being built.")

    def brief_built(self, run: dict) -> None:
        """Called after every build. One nobody is watching, from the schedule or the tray, gets a
        notification. It holds counts only: what is in the brief stays in Switchboard."""
        if self.brief is not None and run["started_by"] in ("schedule", "tray"):
            self.notify(ready_message(run, self.brief.view()))

    def toggle_startup(self) -> None:
        """Tray menu: start in the tray when you sign in to Windows, or stop doing that."""
        try:
            on = startup.toggle()
        except Exception as e:
            log.error("changing Start with Windows failed: %s", type(e).__name__)
            self.notify(f"Couldn't change Start with Windows ({type(e).__name__}).")
            return
        self.notify("Switchboard will start in the tray when you sign in." if on
                    else "Switchboard won't start when you sign in anymore.")
        if self.icon is not None and hasattr(self.icon, "update_menu"):
            self.icon.update_menu()  # so the check mark follows


def _hwnd(window) -> int | None:
    """The window's Win32 handle, or None off Windows and before the window exists."""
    if sys.platform != "win32":
        return None
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        return None


def _to_front(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    for fn in (user32.IsIconic, user32.SetForegroundWindow):
        fn.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: back to the size it was minimized from
    user32.SetForegroundWindow(hwnd)


def _allow_foreground() -> None:
    """Windows only lets a program take the foreground when the user just started it or clicked
    it. A second launch has that right and the running app doesn't, so pass it on."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY


def _taskbar_identity(hwnd: int, *, clear: bool = False) -> None:
    """Windows: tell the taskbar what this window is. Its button and right-click menu then say
    Switchboard with the logo, and Pin to taskbar pins the launcher. Left alone they say Python,
    even with the app id and the window icon set, and the pin is a bare pythonw.exe that starts
    nothing.

    These are properties on the window, and Windows wants them removed before it closes."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                    ("data4", wintypes.BYTE * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):  # enough of it for an empty value or a string
        _fields_ = [("vt", wintypes.USHORT), ("reserved", wintypes.USHORT * 3), ("text", wintypes.LPCWSTR),
                    ("unused", ctypes.c_void_p)]

    def guid(text: str) -> GUID:
        g = GUID()
        ctypes.OleDLL("ole32").CLSIDFromString(text, ctypes.byref(g))
        return g

    launcher = REPO_ROOT / "switchboard.cmd"
    values = {
        5: APP_ID,                                                         # System.AppUserModel.ID
        3: f"{ICON_PATH},0",                                               # .RelaunchIconResource
        2: f'{os.environ.get("COMSPEC", "cmd.exe")} /c ""{launcher}""',  # .RelaunchCommand
        4: APP_NAME,                                                       # .RelaunchDisplayNameResource
    }
    get_store = ctypes.OleDLL("shell32").SHGetPropertyStoreForWindow
    get_store.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    # IPropertyStore, called through its vtable: Release is slot 2, SetValue 6 and Commit 7.
    set_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
        6, "SetValue")
    commit = ctypes.WINFUNCTYPE(ctypes.HRESULT)(7, "Commit")
    release = ctypes.WINFUNCTYPE(wintypes.ULONG)(2, "Release")

    store = ctypes.c_void_p()
    get_store(hwnd, ctypes.byref(guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")), ctypes.byref(store))
    try:
        app_model = guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
        for pid, text in values.items():
            value = PROPVARIANT(0) if clear else PROPVARIANT(31, text=text)  # VT_EMPTY, VT_LPWSTR
            set_value(store, ctypes.byref(PROPERTYKEY(app_model, pid)), ctypes.byref(value))
        commit(store)
    finally:
        release(store)


def _title_bar(hwnd: int) -> None:
    """Windows: a title bar in the dashboard's colors. The dashboard is dark whatever the Windows
    theme is, and under a light theme the default title bar is a white strip on top of it.
    The two colors need Windows 11. Windows 10 ignores them and gets its own dark title bar."""
    import ctypes
    from ctypes import wintypes

    def colorref(color: str) -> int:  # "#rrggbb" to 0x00bbggrr
        return int(color[5:7] + color[3:5] + color[1:3], 16)

    set_attribute = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
    set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    # 20 is dark mode, which is what makes the caption buttons light. 35 and 36 are the caption
    # and its text.
    for attribute, value in ((20, 1), (35, colorref(PAGE_COLOR)), (36, colorref(INK_COLOR))):
        set_attribute(hwnd, attribute, ctypes.byref(ctypes.c_int(value)), 4)


def _on_form_created(shell: Shell) -> None:
    """Windows, when the native window exists and before it shows."""
    from Microsoft.Win32 import SystemEvents
    from System.Windows.Forms import CloseReason

    hwnd = _hwnd(shell.window)

    def on_form_closing(sender, args) -> None:
        # Decided here and not in pywebview's closing event, because only this says why the window
        # is closing. Cancelling a close that Windows asked for would block shutdown.
        if not shell.on_closing(by_user=args.CloseReason == CloseReason.UserClosing):
            args.Cancel = True
        elif hwnd:
            with contextlib.suppress(Exception):
                _taskbar_identity(hwnd, clear=True)

    def on_preference_changed(sender, args) -> None:
        # pywebview resets the title bar to the Windows theme on every settings change. It
        # subscribed first, so this runs after it.
        with contextlib.suppress(Exception):
            _title_bar(hwnd)

    shell.window.native.FormClosing += on_form_closing
    for step in (_taskbar_identity, _title_bar):  # cosmetic, so a failure is logged and the app goes on
        try:
            step(hwnd)
        except Exception:
            log.exception("%s failed", step.__name__)
    SystemEvents.UserPreferenceChanged += on_preference_changed


def _tray_image():
    from PIL import Image

    try:
        return Image.open(ICON_PATH)
    except OSError:
        return Image.new("RGB", (64, 64), PAGE_COLOR)  # no icon file. The tray still needs a picture.


def tray_menu(pystray, shell: Shell, cfg: Config) -> list:
    """The tray icon's menu. `pystray` is passed in, so a test can hand over a stand-in for it."""
    items = [
        pystray.MenuItem("Open", shell.show, default=True),  # default: a click on the icon does this
        pystray.MenuItem("Open in browser", shell.open_in_browser),
    ]
    more = []
    if cfg.brief.enabled and shell.brief is not None:
        more.append(pystray.MenuItem("Build the brief now", shell.build_brief))
    if startup.supported():
        more.append(pystray.MenuItem("Start with Windows", shell.toggle_startup,
                                     checked=lambda item: startup.enabled()))
    if more:
        items += [pystray.Menu.SEPARATOR, *more]
    return items + [pystray.Menu.SEPARATOR, pystray.MenuItem("Quit", shell.quit)]


def run_gui(cfg: Config, shell: Shell, *, hidden: bool = False, devtools: bool = False) -> None:
    """Create the window and the tray icon, then block until Quit. Main thread only."""
    import pystray
    import webview

    logging.getLogger("pywebview").handlers.clear()  # it prints on its own. The root logger has it now.
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    webview.settings["ALLOW_DOWNLOADS"] = True  # the chat export links

    try:
        width, height = window_size(webview.screens)
    except Exception:
        width, height = WINDOW_SIZE
    window = webview.create_window(APP_NAME, shell.url, width=width, height=height, min_size=WINDOW_MIN_SIZE,
                                   hidden=hidden, background_color=PAGE_COLOR, text_select=True)
    shell.window = window
    if sys.platform == "win32":
        window.events.before_show += lambda: _on_form_created(shell)
    else:
        window.events.closing += shell.on_closing
    window.events.shown += lambda: log.info("window ready (%s)", webview.renderer)

    icon = pystray.Icon("switchboard", _tray_image(), APP_NAME, menu=pystray.Menu(*tray_menu(pystray, shell, cfg)))
    shell.icon = icon
    tray_ready = threading.Event()

    def show_icon(icon) -> None:
        icon.visible = True
        tray_ready.set()

    icon.run_detached(show_icon)
    try:
        # Not private mode: the dashboard keeps a setting or two in localStorage.
        webview.start(private_mode=False, storage_path=str(cfg.data_path / "webview"), icon=str(ICON_PATH),
                      debug=devtools)
    finally:
        tray_ready.wait(5)  # stop() does nothing to an icon that hasn't started, and it would then stay
        icon.stop()


# The command ---------------------------------------------------------------------


def run(args) -> int:
    """`python -m switchboard app`. Loads the config itself because there may be no console to
    print a traceback to, so every failure has to end in a dialog."""
    try:
        cfg = get_config()
    except Exception as e:
        alert(f"Switchboard can't start because its config didn't load.\n\n{e}")
        return 1
    try:
        setup_logging(log_file(cfg))
        return _run(cfg, args)
    except Exception as e:
        log.exception("the desktop app stopped with an error")
        alert(f"Switchboard stopped with an error.\n\n{e}\n\nThe log is at {log_file(cfg)}")
        return 1


def _run(cfg: Config, args) -> int:
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    url = local_url(host, port)

    sock = claim_port(host, port)
    if sock is None:
        return second_launch(url, port, hidden=args.hidden)

    try:
        from .main import create_app

        shell = Shell(url)
        app = create_app(cfg)
        shell.brief = app.state.service.brief
        shell.brief.on_built = shell.brief_built  # the schedule's builds announce themselves from the tray
        app.add_middleware(ShowRoute, show=shell.show)
        server = ServerThread(app, sock, host, port)
    except Exception:
        sock.close()  # or a second launch would wait on a port that nothing will ever answer
        raise
    log.info("Switchboard on %s", url)
    server.start()
    try:
        if not server.wait_ready(url):
            raise RuntimeError("The server didn't start.")
        run_gui(cfg, shell, hidden=args.hidden, devtools=args.devtools)
    finally:
        stopped = server.stop()
    if not stopped:
        # A request that outlasts the grace period is dropped and doesn't get here, so the shutdown
        # itself is stuck. The window and the tray icon are gone. Don't leave a process behind that
        # nobody can see.
        log.error("the server did not stop within %s s, exiting anyway", STOP_TIMEOUT_S)
        logging.shutdown()
        os._exit(1)
    return 0
