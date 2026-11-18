"""Secrets the app needs at run time, such as sign-in tokens and private links.

They are kept in the operating system's credential store through the keyring package: Windows
Credential Manager, the macOS Keychain or the Secret Service on Linux. They never go into a config
file, and never under data/, which is the parent of the folder `claude -p` runs in.

Windows Credential Manager refuses a value longer than 1,280 characters, and a sign-in token can be
longer than that. So a long secret is kept in parts: the entry under its own name says how many,
and the parts sit next to it. Reading puts them back together.

An environment variable overrides the store, for a machine without one. Its name is SWITCHBOARD_
and the secret's name in capitals, so `canvas_feed_url` is read from SWITCHBOARD_CANVAS_FEED_URL.

Setting SWITCHBOARD_NO_CREDENTIAL_STORE turns the store off altogether. A second instance started
to try something out shares this machine's store with the real one, and this keeps it from signing
in as you and reading your real mail or coursework.
"""

from __future__ import annotations

import contextlib
import os

SERVICE = "Switchboard"
OFF_SWITCH = "SWITCHBOARD_NO_CREDENTIAL_STORE"
PART_CHARS = 1000  # comfortably under Windows' 1,280
_PARTS = "switchboard-parts:"  # what the entry of a secret kept in parts holds, followed by the count


class CredentialError(RuntimeError):
    """The credential store can't be used. The message says what to do instead."""


def env_name(name: str) -> str:
    return "SWITCHBOARD_" + name.upper()


def _store():
    """The keyring module. Imported here, so nothing else needs it installed or a store to exist."""
    if os.environ.get(OFF_SWITCH):
        raise CredentialError(f"the credential store is turned off by {OFF_SWITCH}")
    import keyring

    return keyring


def _part(name: str, i: int) -> str:
    return f"{name}.part{i}"


def _part_count(store, name: str) -> int:
    """How many parts the saved value of `name` is in. 0 when it is whole or there is none."""
    try:
        value = store.get_password(SERVICE, name)
        return int(value[len(_PARTS):]) if value and value.startswith(_PARTS) else 0
    except Exception:
        return 0


def get(name: str) -> str | None:
    """The secret called `name`, or None when there isn't one. Never raises: a machine without a
    credential store reads the same as a secret that was never saved."""
    value = os.environ.get(env_name(name))
    if value:
        return value
    try:
        store = _store()
        value = store.get_password(SERVICE, name)
        if value and value.startswith(_PARTS):
            parts = [store.get_password(SERVICE, _part(name, i)) for i in range(1, int(value[len(_PARTS):]) + 1)]
            value = None if any(p is None for p in parts) else "".join(parts)
        return value or None
    except Exception:
        return None


def save(name: str, value: str) -> None:
    try:
        store = _store()
        before = _part_count(store, name)
        if len(value) <= PART_CHARS:
            store.set_password(SERVICE, name, value)
            after = 0
        else:
            parts = [value[i:i + PART_CHARS] for i in range(0, len(value), PART_CHARS)]
            for i, part in enumerate(parts, 1):  # the parts first, so the entry never names parts that aren't there
                store.set_password(SERVICE, _part(name, i), part)
            store.set_password(SERVICE, name, f"{_PARTS}{len(parts)}")
            after = len(parts)
    except Exception as e:
        # Only the kind of failure. A store's own message could repeat the value.
        raise CredentialError(
            f"Couldn't save to this machine's credential store ({type(e).__name__}). "
            f"Set the environment variable {env_name(name)} instead."
        ) from None
    for i in range(after + 1, before + 1):  # parts a longer earlier value left behind
        with contextlib.suppress(Exception):
            store.delete_password(SERVICE, _part(name, i))


def forget(name: str) -> bool:
    """Remove a saved secret, and its parts if it has any. False when there was nothing to remove."""
    try:
        store = _store()
        count = _part_count(store, name)
        store.delete_password(SERVICE, name)
    except Exception:
        return False
    for i in range(1, count + 1):
        with contextlib.suppress(Exception):
            store.delete_password(SERVICE, _part(name, i))
    return True
