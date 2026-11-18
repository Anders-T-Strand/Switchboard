"""Starting Switchboard when you sign in to Windows.

A shortcut in your Startup folder runs the launcher with `app --hidden`, so Switchboard starts in the
tray with no window, and the morning brief is built without you opening anything. The launcher does
its usual checks first, such as rebuilding the dashboard after a `git pull`.

Turned on and off from the tray menu. Nothing else here touches Windows settings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT

SHORTCUT = "Switchboard.lnk"
_CREATE_NO_WINDOW = 0x08000000

# Written for WScript.Shell, which makes .lnk files. Every value comes in through the environment,
# so no path ever has to be quoted inside the script.
_MAKE_SHORTCUT = (
    "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SB_LINK)\n"
    "$link.TargetPath = $env:SB_TARGET\n"
    "$link.Arguments = $env:SB_ARGS\n"
    "$link.WorkingDirectory = $env:SB_DIR\n"
    "$link.IconLocation = $env:SB_ICON\n"
    "$link.Description = 'Switchboard, in the tray'\n"
    "$link.WindowStyle = 7\n"  # minimized, so the launcher's own window doesn't flash up
    "$link.Save()\n"
)


def supported() -> bool:
    return sys.platform == "win32"


def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_folder() / SHORTCUT


def enabled() -> bool:
    return supported() and shortcut_path().is_file()


def shortcut_fields() -> dict[str, str]:
    """What the shortcut runs: PowerShell, hidden, on start.ps1 with `app --hidden`."""
    system = os.environ.get("SYSTEMROOT", r"C:\Windows")  # Windows reads environment names in any case
    launcher = REPO_ROOT / "start.ps1"
    return {
        "SB_LINK": str(shortcut_path()),
        "SB_TARGET": str(Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
        "SB_ARGS": f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{launcher}" app --hidden',
        "SB_DIR": str(REPO_ROOT),
        "SB_ICON": f"{REPO_ROOT / 'switchboard.ico'},0",
    }


def enable() -> None:
    """Make the shortcut. Raises OSError or CalledProcessError when Windows won't."""
    if not supported():
        raise OSError("starting with the system is only set up for Windows")
    startup_folder().mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _MAKE_SHORTCUT],
        env={**os.environ, **shortcut_fields()}, check=True, capture_output=True, timeout=30,
        creationflags=_CREATE_NO_WINDOW,
    )


def disable() -> None:
    shortcut_path().unlink(missing_ok=True)


def toggle() -> bool:
    """Turn it on when it is off and off when it is on. Returns whether it is on now."""
    if enabled():
        disable()
    else:
        enable()
    return enabled()
