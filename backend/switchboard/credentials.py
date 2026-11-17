"""Secrets the app needs at run time, such as sign-in tokens and private links.

They are kept in the operating system's credential store through the keyring package: Windows
Credential Manager, the macOS Keychain or the Secret Service on Linux. They never go into a config
file, and never under data/, which is the parent of the folder `claude -p` runs in.

An environment variable overrides the store, for a machine without one. Its name is SWITCHBOARD_
and the secret's name in capitals, so `canvas_feed_url` is read from SWITCHBOARD_CANVAS_FEED_URL.
"""

from __future__ import annotations

import os

SERVICE = "Switchboard"


class CredentialError(RuntimeError):
    """The credential store can't be used. The message says what to do instead."""


def env_name(name: str) -> str:
    return "SWITCHBOARD_" + name.upper()


def _store():
    """The keyring module. Imported here, so nothing else needs it installed or a store to exist."""
    import keyring

    return keyring


def get(name: str) -> str | None:
    """The secret called `name`, or None when there isn't one. Never raises: a machine without a
    credential store reads the same as a secret that was never saved."""
    value = os.environ.get(env_name(name))
    if value:
        return value
    try:
        return _store().get_password(SERVICE, name) or None
    except Exception:
        return None


def save(name: str, value: str) -> None:
    try:
        _store().set_password(SERVICE, name, value)
    except Exception as e:
        # Only the kind of failure. A store's own message could repeat the value.
        raise CredentialError(
            f"Couldn't save to this machine's credential store ({type(e).__name__}). "
            f"Set the environment variable {env_name(name)} instead."
        ) from None


def forget(name: str) -> bool:
    """Remove a saved secret. False when there was nothing to remove."""
    try:
        _store().delete_password(SERVICE, name)
        return True
    except Exception:
        return False
