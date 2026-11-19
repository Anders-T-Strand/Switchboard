"""Reading a Console answer aloud with ElevenLabs.

The browser asks for an answer by its request id and gets audio back as it is made. The server cleans
the answer into plain sentences (text.py), calls ElevenLabs' streaming endpoint with the key from the
credential store, and passes the audio through. The browser never sees the key.

A play is opened before any audio is sent. That is when an error can still come back as JSON, and
it is also when the time to first audio is known, since the first chunk is in hand. After that the
audio streams, and the play is recorded as ok, stopped (the browser hung up) or error (ElevenLabs
broke off).

A finished clip is kept under data/speech, named by a hash of the text, the voice, the model and the
format, so playing it again sends nothing and costs nothing. A clip that was stopped or broke off is
never kept. The one HTTP client stays open between plays and keeps its connection for a minute, so
plays close together skip the TLS handshake with ElevenLabs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx

from .. import credentials
from ..config import Config
from ..db import connect, now_iso, rows
from .text import speakable

log = logging.getLogger("switchboard.speech")

API = "https://api.elevenlabs.io/v1/text-to-speech"
OUTPUT_FORMAT = "mp3_44100_128"
API_KEY = "elevenlabs_api_key"  # the name the key is saved under in the credential store
SETUP = "python -m switchboard speech connect"
CHUNK = 32 * 1024
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SpeechError(Exception):
    """Something to tell the page before any audio was sent. The message is fit to show there."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, limits=httpx.Limits(keepalive_expiry=60.0))


def clip_key(text: str, voice_id: str, model_id: str) -> str:
    return hashlib.sha256("\n".join((model_id, voice_id, OUTPUT_FORMAT, text)).encode("utf-8")).hexdigest()


def eleven_message(body: bytes) -> str | None:
    """ElevenLabs' own words from an error body: {"detail": {"message": ...}}, a plain detail
    string, or the list a validation error carries."""
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(detail, dict):
        text = detail.get("message") or detail.get("code") or detail.get("status")
    elif isinstance(detail, list):
        text = ". ".join(str(d["msg"]).rstrip(".") for d in detail if isinstance(d, dict) and d.get("msg"))
    else:
        text = detail
    return " ".join(str(text).split())[:300] if text else None


def explain(status: int, body: bytes) -> str:
    said = eleven_message(body)
    if status == 401:
        reason = f" ({said})" if said else ""
        return f"ElevenLabs refused the saved API key{reason}. Save a new one with: {SETUP}"
    if said:
        return f"ElevenLabs: {said}"
    return f"ElevenLabs answered with HTTP {status}."


@dataclass
class Play:
    """One play, opened: the first audio is in hand. `chunks()` streams it all, keeps the clip when
    the stream ran to its end, and records how the play ended. Iterate it once."""

    speech: Speech
    play_id: int
    cached: bool
    chars: int
    cut: bool
    first_audio_ms: int
    first: bytes
    rest: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
    started: float
    clip: Path | None = None  # where the audio is kept once the stream is complete, when it is to be kept

    async def chunks(self) -> AsyncIterator[bytes]:
        sent, status, error = 0, "stopped", None  # stopped, unless it runs to the end or breaks off
        audio = bytearray() if self.clip else None  # a clip is a few MB at most, so it waits in memory
        try:
            chunk = self.first
            while chunk:
                if audio is not None:
                    audio += chunk
                sent += len(chunk)
                yield chunk
                chunk = await anext(self.rest, b"")
            status = "ok"
        except (httpx.HTTPError, OSError) as e:
            status = "error"
            error = "The saved clip couldn't be read." if self.cached else "ElevenLabs stopped sending partway."
            log.warning("read aloud broke off: %s", type(e).__name__)
        finally:
            # A stop arrives as a cancellation, or as the generator being closed, so the cleanup that
            # needs no waiting comes first.
            if status == "ok" and audio is not None and self.clip:
                self.speech.keep(self.clip, bytes(audio), self.play_id)
            self.speech.finish(self.play_id, status, sent, self.started, error)
            with anyio.CancelScope(shield=True):
                await self.close()


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            yield chunk


class Speech:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http: httpx.AsyncClient | None = None

    @property
    def folder(self) -> Path:
        return self.cfg.speech_path

    def key_saved(self) -> bool:
        return credentials.get(API_KEY) is not None

    def state(self) -> dict:
        return {"enabled": self.cfg.speech.enabled, "key_saved": self.key_saved()}

    def _http(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = _client()
        return self.http

    def warm(self) -> None:
        """Make the HTTP client before the first play, when read aloud is on. Making one loads the CA
        certificates, which took 130 to 175 ms on Windows when measured, and that would otherwise count
        against the first play's time to first audio."""
        if self.cfg.speech.enabled:
            self._http()

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    def recover(self) -> None:
        """At startup: plays a stopped process left open, and a clip it was halfway through writing."""
        with connect() as conn:
            conn.execute("UPDATE speech SET status = 'stopped' WHERE status = 'streaming'")
        if self.folder.is_dir():
            for part in self.folder.glob("*.part"):
                part.unlink(missing_ok=True)

    # The play log ------------------------------------------------------------------

    def _start(self, request_id: int, text: str, key: str | None, cached: bool) -> int:
        s = self.cfg.speech
        with connect() as conn:
            return conn.execute(
                """INSERT INTO speech (request_id, created_at, voice_id, model_id, chars, chars_sent, cached,
                                       clip_key, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'streaming')""",
                (request_id, now_iso(), s.voice_id, s.model_id, len(text), 0 if cached else len(text), int(cached),
                 key),
            ).lastrowid

    def _first_audio(self, play_id: int, started: float) -> int:
        ms = int((time.perf_counter() - started) * 1000)
        with connect() as conn:
            conn.execute("UPDATE speech SET first_audio_ms = ? WHERE id = ?", (ms, play_id))
        return ms

    def keep(self, clip: Path, audio: bytes, play_id: int) -> None:
        """Save a complete clip. It is written beside its name and then moved into place, so a clip on
        disk is always whole. When that fails, the next play of the answer just asks ElevenLabs again."""
        part = clip.with_name(f"{clip.stem}.{play_id}.part")
        try:
            clip.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(audio)
            os.replace(part, clip)
        except OSError as e:  # on Windows, also another play reading the same clip right now
            log.warning("read aloud couldn't keep a clip: %s", type(e).__name__)
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)

    def finish(self, play_id: int, status: str, sent: int, started: float, error: str | None = None) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE speech SET status = ?, bytes = ?, total_ms = ?, error = ? WHERE id = ?",
                (status, sent, int((time.perf_counter() - started) * 1000), error, play_id),
            )

    # Playing -----------------------------------------------------------------------

    def _answer(self, request_id: int) -> str:
        with connect() as conn:
            row = conn.execute("SELECT status, response FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise SpeechError(f"Answer {request_id} wasn't found.", 404)
        if row["status"] != "ok" or not (row["response"] or "").strip():
            raise SpeechError("That turn has no answer to read aloud.", 404)
        return row["response"]

    async def open(self, request_id: int) -> Play:
        """Get the first audio for an answer. Raises SpeechError, before anything is sent to the
        browser, when it can't be had."""
        started = time.perf_counter()
        s = self.cfg.speech
        if not s.enabled:
            raise SpeechError("Read aloud is turned off. Set speech.enabled to true in config.local.yaml.", 400)
        text, cut = speakable(self._answer(request_id), s.max_chars)
        if not text:
            raise SpeechError("There's nothing to read aloud here once code and web addresses are left out.", 400)
        key = clip_key(text, s.voice_id, s.model_id)
        clip = self.folder / f"{key}.mp3"

        if s.cache and clip.is_file():
            rest = _file_chunks(clip)
            first = await anext(rest, b"")
            if first:
                play_id = self._start(request_id, text, key, cached=True)
                return Play(self, play_id, True, len(text), cut, self._first_audio(play_id, started), first, rest,
                            rest.aclose, started)
            await rest.aclose()
            clip.unlink(missing_ok=True)  # an empty clip is no clip

        api_key = credentials.get(API_KEY)
        if not api_key:
            raise SpeechError(f"No ElevenLabs key is saved. Save one with: {SETUP}", 400)
        play_id = self._start(request_id, text, key if s.cache else None, cached=False)
        http = self._http()
        request = http.build_request(
            "POST", f"{API}/{s.voice_id}/stream", params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": api_key, "accept": "audio/mpeg"}, json={"text": text, "model_id": s.model_id},
        )
        try:
            response = await http.send(request, stream=True)
        except httpx.TimeoutException:
            raise self._failed(play_id, started, "ElevenLabs took too long to answer.", 504) from None
        except httpx.HTTPError:
            raise self._failed(play_id, started, "ElevenLabs couldn't be reached.", 502) from None
        if response.status_code != 200:
            body = await response.aread()
            await response.aclose()
            message = explain(response.status_code, body).replace(api_key, "(the key)")  # in case it is repeated
            raise self._failed(play_id, started, message, 502)
        rest = response.aiter_bytes()
        try:
            first = await anext(rest, b"")
        except httpx.HTTPError:
            await response.aclose()
            raise self._failed(play_id, started, "ElevenLabs stopped before sending any audio.", 502) from None
        if not first:
            await response.aclose()
            raise self._failed(play_id, started, "ElevenLabs sent no audio.", 502)
        first_audio_ms = self._first_audio(play_id, started)
        return Play(self, play_id, False, len(text), cut, first_audio_ms, first, rest, response.aclose, started,
                    clip=clip if s.cache else None)

    def _failed(self, play_id: int, started: float, message: str, status: int) -> SpeechError:
        self.finish(play_id, "error", 0, started, message)
        return SpeechError(message, status)

    # Chats that go away ------------------------------------------------------------

    def detach(self, conversation_id: int) -> list[str]:
        """Before a chat is deleted: unlink its plays from their clips, and return the clips it used.
        The plays stay, without text or a clip, for the latency numbers."""
        with connect() as conn:
            where = "request_id IN (SELECT id FROM requests WHERE conversation_id = ?)"
            keys = [r[0] for r in conn.execute(
                f"SELECT DISTINCT clip_key FROM speech WHERE clip_key IS NOT NULL AND {where}", (conversation_id,)
            )]
            conn.execute(f"UPDATE speech SET clip_key = NULL WHERE {where}", (conversation_id,))
        return keys

    def drop_unused(self, keys: list[str]) -> int:
        """Delete the clips no other answer still plays. Returns how many went."""
        dropped = 0
        with connect() as conn:
            for key in keys:
                if conn.execute("SELECT 1 FROM speech WHERE clip_key = ? LIMIT 1", (key,)).fetchone() is None:
                    clip = self.folder / f"{key}.mp3"
                    if clip.is_file():
                        clip.unlink()
                        dropped += 1
        return dropped

    # Numbers -----------------------------------------------------------------------

    def stats(self) -> dict:
        """Counts and timings, nothing from any answer."""
        with connect() as conn:
            plays = rows(conn.execute(
                "SELECT cached, status, first_audio_ms, total_ms, chars_sent FROM speech WHERE status != 'streaming'"
            ))

        def median(values: list[int]) -> int | None:
            return int(statistics.median(values)) if values else None

        answered = [p for p in plays if not p["cached"] and p["first_audio_ms"] is not None]  # ElevenLabs sent audio
        fresh = [p["first_audio_ms"] for p in answered]
        saved = [p["first_audio_ms"] for p in plays if p["cached"] and p["first_audio_ms"] is not None]
        whole = [p["total_ms"] for p in answered if p["status"] == "ok" and p["total_ms"]]
        clips = list(self.folder.glob("*.mp3")) if self.folder.is_dir() else []
        return {
            "plays": len(plays),
            "from_clips": sum(1 for p in plays if p["cached"]),
            "stopped": sum(1 for p in plays if p["status"] == "stopped"),
            "errors": sum(1 for p in plays if p["status"] == "error"),
            # What ElevenLabs charges for. A request it refused or never answered isn't counted.
            "chars_sent": sum(p["chars_sent"] for p in answered),
            "first_audio_ms": {"elevenlabs": median(fresh), "elevenlabs_plays": len(fresh),
                               "saved": median(saved), "saved_plays": len(saved)},
            "total_ms": {"elevenlabs": median(whole)},
            "clips": len(clips),
            "clip_bytes": sum(c.stat().st_size for c in clips),
            "voice_id": self.cfg.speech.voice_id,
            "model_id": self.cfg.speech.model_id,
        }
