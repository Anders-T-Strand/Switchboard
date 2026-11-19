"""Read aloud: Console answers spoken by ElevenLabs, streamed through the server."""

from __future__ import annotations

from .player import API_KEY, SETUP, Play, Speech, SpeechError, clip_key
from .text import speakable

__all__ = ["API_KEY", "SETUP", "Play", "Speech", "SpeechError", "clip_key", "speakable"]
