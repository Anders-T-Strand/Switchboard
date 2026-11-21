"""The `speech` command.

    python -m switchboard speech connect   save your ElevenLabs API key, at a hidden prompt
    python -m switchboard speech forget    remove it again
    python -m switchboard speech status    plays, characters and timings, as counts

Nothing here prints the key, and `status` prints nothing from any answer.
"""

from __future__ import annotations

import sys

from .. import credentials
from ..config import Config
from ..db import init_db
from .player import API_KEY, Speech


def _seconds(ms: int | None) -> str:
    return "none yet" if ms is None else f"{ms / 1000:.2f} s"


def _connect(cfg: Config) -> int:
    import getpass

    print("Create an API key at https://elevenlabs.io/app/api/api-keys. It only needs access to Text to Speech.")
    key = getpass.getpass("Paste it here (it won't be shown): ").strip()
    if not key or any(c.isspace() for c in key) or len(key) > 200:
        print("Not saved. That doesn't look like an API key.", file=sys.stderr)
        return 1
    try:
        credentials.save(API_KEY, key)
    except credentials.CredentialError as e:
        print(f"Not saved. {e}", file=sys.stderr)
        return 1
    print("Saved the key to this machine's credential store.")
    if not cfg.speech.enabled:
        print("Read aloud is turned off. Set speech.enabled to true in config.local.yaml, then restart Switchboard.")
    return 0


def _forget(cfg: Config) -> int:
    print("Removed the ElevenLabs key." if credentials.forget(API_KEY) else "There was no ElevenLabs key saved.")
    return 0


def summary(state: dict, stats: dict) -> str:
    """The state of read aloud in a few lines. Counts and timings only."""
    first = stats["first_audio_ms"]
    size = stats["clip_bytes"] / 1_000_000
    return "\n".join([
        f"Read aloud: {'on' if state['enabled'] else 'off'}, key {'saved' if state['key_saved'] else 'not saved'}",
        f"Voice {stats['voice_id']}, model {stats['model_id']}",
        f"Plays: {stats['plays']} ({stats['from_clips']} from saved clips, {stats['stopped']} stopped, "
        f"{stats['errors']} failed)",
        f"Characters sent to ElevenLabs, in plays it answered: {stats['chars_sent']:,}",
        f"First audio, median: {_seconds(first['elevenlabs'])} from ElevenLabs over {first['elevenlabs_plays']} plays, "
        f"{_seconds(first['saved'])} from saved clips",
        f"Whole clip from ElevenLabs, median: {_seconds(stats['total_ms']['elevenlabs'])}",
        f"Saved clips: {stats['clips']} ({size:.1f} MB)",
    ])


def _status(cfg: Config) -> int:
    init_db(cfg.db_path)
    speech = Speech(cfg)
    print(summary(speech.state(), speech.stats()))
    return 0


def run(args, cfg: Config) -> int:
    return {"connect": _connect, "forget": _forget, "status": _status}[args.action](cfg)
