"""Read aloud: the Markdown cleanup, the stream from ElevenLabs, saved clips, the play log and the
`speech` command. ElevenLabs is an httpx MockTransport and the credential store is the fake from
conftest.py, so no test sends anything anywhere or reads a real key."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from switchboard import credentials
from switchboard.__main__ import build_parser
from switchboard.config import Config, ConfigError, SpeechCfg, load_config
from switchboard.db import connect, now_iso, rows
from switchboard.main import create_app
from switchboard.speech import API_KEY, Speech, clip_key, player, speakable
from switchboard.speech import cli as speech_cli

VOICE = "JBFqnCBsd6RMkjVDRZzb"
MODEL = "eleven_flash_v2_5"
KEY = "sk_madeup_0123456789abcdef0123456789abcdef"  # not a real key
AUDIO = [b"ID3-made-up-audio-1-", b"made-up-audio-2-", b"made-up-audio-3"]
WHOLE = b"".join(AUDIO)
ANSWER = "## Plan\n\nRead the **answer** aloud. See [the docs](https://docs.example/tts).\n\n```bash\nmake clean\n```\n"
SPOKEN = "Plan. Read the answer aloud. See the docs."


class FakeElevenLabs:
    """ElevenLabs' streaming endpoint, set up by each test. It keeps every request it gets."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.error: object = b""  # the body of an answer other than 200: JSON, or bytes as they are
        self.chunks = list(AUDIO)
        self.break_after: int | None = None  # chunks sent before the connection drops
        self.hang_after: int | None = None  # chunks sent before it goes quiet for good
        self.raises: type[httpx.HTTPError] | None = None  # raised in place of any answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.raises is not None:
            raise self.raises("made-up failure", request=request)
        if self.status != 200:
            body = self.error if isinstance(self.error, bytes) else json.dumps(self.error).encode()
            return httpx.Response(self.status, content=body, headers={"content-type": "application/json"})
        return httpx.Response(200, content=self._audio(), headers={"content-type": "audio/mpeg"})

    async def _audio(self):
        for i, chunk in enumerate(self.chunks):
            if i == self.break_after:
                raise httpx.ReadError("connection reset")
            if i == self.hang_after:
                await asyncio.Event().wait()
            yield chunk


@pytest.fixture
def eleven(monkeypatch) -> FakeElevenLabs:
    fake = FakeElevenLabs()
    monkeypatch.setattr(player, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def on(cfg) -> Config:
    """Read aloud turned on, the way config.local.yaml turns it on."""
    cfg.speech.enabled = True
    return cfg


@pytest.fixture
def client(cfg, fake_ollama):
    with TestClient(create_app(cfg, background_sync=False)) as c:
        yield c


def answer(text: str, status: str = "ok") -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO requests (created_at, prompt, response, status, route_mode) VALUES (?, ?, ?, ?, 'auto')",
            (now_iso(), "a made-up question", text, status),
        ).lastrowid


def plays() -> list[dict]:
    with connect() as conn:
        return rows(conn.execute("SELECT * FROM speech ORDER BY id"))


def files(folder: Path) -> list[str]:
    return sorted(f.name for f in folder.glob("*"))


def speech_url(request_id: int) -> str:
    return f"/api/requests/{request_id}/speech"


# Markdown into sentences --------------------------------------------------------------------------


def test_markdown_becomes_plain_sentences():
    markdown = "\n".join([
        "# Setting it up ##",
        "",
        "Install it with `pip install switchboard`, then **run** it. See [the docs](https://example.com/docs) "
        "or https://example.com.",
        "Visit www.example.org, or (https://example.net/a_b) for more.",
        "",
        "```python",
        'print("never read")',
        "```",
        "",
        "| Model | Speed |",
        "|---|:---:|",
        "| *Flash* | fast |",
        "| Multilingual | slower |",
        "",
        "- First thing",
        "- [x] Done thing",
        "1. Numbered",
        "",
        "> A quote here",
        "",
        "---",
        "Snake_case_names stay, and _this_ is italic. An ![chart of speed](c.png) image.",
        "",
        "Title",
        "=====",
    ])
    assert speakable(markdown, 2500) == (
        "Setting it up. Install it with pip install switchboard, then run it. See the docs or. "
        "Visit, or for more. Model, Speed. Flash, fast. Multilingual, slower. First thing. Done thing. "
        "Numbered. A quote here. Snake_case_names stay, and this is italic. An chart of speed image. Title.",
        False,
    )


def test_code_is_never_read_even_inside_a_longer_fence():
    assert speakable("```python\nprint(1)\n```", 2500) == ("", False)
    assert speakable("````md\n```python\nx = 1\n```\nstill code\n````\nAfter it.", 2500) == ("After it.", False)
    assert speakable("~~~\ncode\n~~~\nAnd text", 2500) == ("And text.", False)
    assert speakable("Text first.\n```\nnever closed", 2500) == ("Text first.", False)


def test_a_long_answer_is_cut_at_a_sentence_near_the_limit():
    whole = " ".join(f"This is sentence number {i}." for i in range(200))
    text, cut = speakable(whole, 500)
    assert cut and whole.startswith(text) and text.endswith(".")
    assert 300 <= len(text) <= 500

    text, cut = speakable("word " * 200, 100)  # no sentence ends in reach, so at a space
    assert cut and len(text) <= 100 and text.endswith("word.")

    text, cut = speakable("x" * 300, 100)  # not even a space
    assert cut and len(text) == 100 and text.endswith(".")

    assert speakable("Short and sweet", 100) == ("Short and sweet.", False)


# Streaming --------------------------------------------------------------------------------------


def test_an_answer_streams_from_elevenlabs_and_the_clip_is_kept(on, eleven, client):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    with connect() as conn:
        turns = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    r = client.get(speech_url(rid))

    assert r.status_code == 200 and r.content == WHOLE
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["cache-control"] == "no-store"
    assert (r.headers["x-speech-cached"], r.headers["x-speech-cut"]) == ("0", "0")
    assert r.headers["x-speech-chars"] == str(len(SPOKEN))

    (call,) = eleven.calls
    assert call.method == "POST"
    assert str(call.url) == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}/stream?output_format=mp3_44100_128"
    assert call.headers["xi-api-key"] == KEY and call.headers["accept"] == "audio/mpeg"
    assert json.loads(call.content) == {"text": SPOKEN, "model_id": MODEL}

    (p,) = plays()
    assert (p["status"], p["request_id"], p["cached"], p["error"]) == ("ok", rid, 0, None)
    assert p["chars"] == p["chars_sent"] == len(SPOKEN) and p["bytes"] == len(WHOLE)
    assert p["first_audio_ms"] == int(r.headers["x-speech-first-audio-ms"]) >= 0
    assert p["total_ms"] >= p["first_audio_ms"]
    assert (p["voice_id"], p["model_id"]) == (VOICE, MODEL)
    assert p["clip_key"] == clip_key(SPOKEN, VOICE, MODEL)
    assert files(on.speech_path) == [f"{p['clip_key']}.mp3"]  # and no part file left behind
    assert (on.speech_path / f"{p['clip_key']}.mp3").read_bytes() == WHOLE
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == turns  # a play is not a turn


def test_a_long_answer_is_cut_before_it_is_sent(on, eleven, client):
    on.speech.max_chars = 200
    credentials.save(API_KEY, KEY)
    rid = answer(" ".join(f"Sentence number {i} is here." for i in range(50)))
    r = client.get(speech_url(rid))
    assert r.status_code == 200 and r.headers["x-speech-cut"] == "1"
    sent = json.loads(eleven.calls[0].content)["text"]
    assert len(sent) <= 200 and sent.endswith("is here.")


def test_playing_it_again_comes_from_the_saved_clip(on, eleven, client):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    first = client.get(speech_url(rid))
    credentials.forget(API_KEY)  # a saved clip needs no key, since nothing is sent
    again = client.get(speech_url(rid))

    assert again.status_code == 200 and again.content == first.content == WHOLE
    assert again.headers["x-speech-cached"] == "1" and len(eleven.calls) == 1
    fresh, saved = plays()
    assert (saved["cached"], saved["chars_sent"], saved["status"]) == (1, 0, "ok")
    assert saved["chars"] == len(SPOKEN) and saved["first_audio_ms"] is not None
    assert saved["clip_key"] == fresh["clip_key"]


def test_another_voice_or_model_is_another_clip(on, eleven, client):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    client.get(speech_url(rid))
    on.speech.voice_id = "AnotherVoice01"
    assert client.get(speech_url(rid)).headers["x-speech-cached"] == "0"
    on.speech.model_id = "eleven_multilingual_v2"
    assert client.get(speech_url(rid)).headers["x-speech-cached"] == "0"

    assert len(eleven.calls) == 3
    assert eleven.calls[1].url.path == "/v1/text-to-speech/AnotherVoice01/stream"
    assert json.loads(eleven.calls[2].content)["model_id"] == "eleven_multilingual_v2"
    assert len(files(on.speech_path)) == 3


def test_with_saving_turned_off_every_play_asks_elevenlabs(on, eleven, client):
    on.speech.cache = False
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    assert client.get(speech_url(rid)).content == client.get(speech_url(rid)).content == WHOLE
    assert len(eleven.calls) == 2
    assert files(on.speech_path) == []
    assert [p["clip_key"] for p in plays()] == [None, None]


# Errors before any audio ------------------------------------------------------------------------


def test_read_aloud_turned_off(cfg, eleven, client):
    credentials.save(API_KEY, KEY)
    r = client.get(speech_url(answer(ANSWER)))
    assert r.status_code == 400
    assert r.json() == {"detail": "Read aloud is turned off. Set speech.enabled to true in config.local.yaml."}
    assert eleven.calls == [] and plays() == []


def test_no_key_saved(on, eleven, client):
    r = client.get(speech_url(answer(ANSWER)))
    assert r.status_code == 400
    assert r.json() == {"detail": "No ElevenLabs key is saved. Save one with: python -m switchboard speech connect"}
    assert eleven.calls == [] and plays() == []


def test_answers_there_is_nothing_to_read_in(on, eleven, client):
    credentials.save(API_KEY, KEY)
    assert client.get(speech_url(999)).json() == {"detail": "Answer 999 wasn't found."}
    r = client.get(speech_url(answer("", status="error")))
    assert r.status_code == 404 and r.json() == {"detail": "That turn has no answer to read aloud."}
    r = client.get(speech_url(answer("```python\nprint(1)\n```\n\nhttps://example.com")))
    assert r.status_code == 400
    assert r.json() == {"detail": "There's nothing to read aloud here once code and web addresses are left out."}
    assert eleven.calls == [] and plays() == []


SETUP = "python -m switchboard speech connect"


@pytest.mark.parametrize("status, body, message", [
    (401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}},
     f"ElevenLabs refused the saved API key (Invalid API key). Save a new one with: {SETUP}"),
    (401, b"", f"ElevenLabs refused the saved API key. Save a new one with: {SETUP}"),
    (400, {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}},
     "ElevenLabs: This request exceeds your quota."),
    (422, {"detail": [{"loc": ["body", "text"], "msg": "Field required", "type": "missing"},
                      {"loc": ["body", "model_id"], "msg": "Input should be a string.", "type": "string_type"}]},
     "ElevenLabs: Field required. Input should be a string"),
    (429, {"detail": "Too many requests"}, "ElevenLabs: Too many requests"),
    (500, b"<html>oops</html>", "ElevenLabs answered with HTTP 500."),
])
def test_what_elevenlabs_says_when_it_refuses(on, eleven, client, status, body, message):
    credentials.save(API_KEY, KEY)
    eleven.status, eleven.error = status, body
    r = client.get(speech_url(answer(ANSWER)))
    assert r.status_code == 502 and r.json() == {"detail": message}
    (p,) = plays()
    assert (p["status"], p["error"], p["first_audio_ms"], p["bytes"]) == ("error", message, None, 0)
    assert files(on.speech_path) == []
    assert client.get("/api/speech/stats").json()["chars_sent"] == 0  # nothing ElevenLabs charges for


@pytest.mark.parametrize("failure, status, message", [
    (httpx.ConnectError, 502, "ElevenLabs couldn't be reached."),
    (httpx.ConnectTimeout, 504, "ElevenLabs took too long to answer."),
    (httpx.ReadTimeout, 504, "ElevenLabs took too long to answer."),
])
def test_elevenlabs_out_of_reach(on, eleven, client, failure, status, message):
    credentials.save(API_KEY, KEY)
    eleven.raises = failure
    r = client.get(speech_url(answer(ANSWER)))
    assert r.status_code == status and r.json() == {"detail": message}
    assert [(p["status"], p["error"]) for p in plays()] == [("error", message)]


def test_a_stream_with_no_audio_in_it(on, eleven, client):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    eleven.break_after = 0
    r = client.get(speech_url(rid))
    assert r.status_code == 502 and r.json() == {"detail": "ElevenLabs stopped before sending any audio."}
    eleven.break_after, eleven.chunks = None, []
    r = client.get(speech_url(rid))
    assert r.status_code == 502 and r.json() == {"detail": "ElevenLabs sent no audio."}
    assert [p["status"] for p in plays()] == ["error", "error"]
    assert files(on.speech_path) == []


# Plays that end early ---------------------------------------------------------------------------


def test_a_stream_that_breaks_off_is_recorded_and_keeps_no_clip(on, eleven, client):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    eleven.break_after = 2
    r = client.get(speech_url(rid))
    assert r.status_code == 200 and r.content == AUDIO[0] + AUDIO[1]  # what came, and then the end

    (p,) = plays()
    assert (p["status"], p["error"]) == ("error", "ElevenLabs stopped sending partway.")
    assert p["bytes"] == len(AUDIO[0] + AUDIO[1])
    assert files(on.speech_path) == []

    eleven.break_after = None  # so the next play asks again, and that one is kept
    assert client.get(speech_url(rid)).headers["x-speech-cached"] == "0"
    assert len(eleven.calls) == 2 and len(files(on.speech_path)) == 1


def test_stopping_partway_keeps_no_clip(on, eleven):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)

    async def hear_one_chunk() -> bytes:
        speech = Speech(on)
        play = await speech.open(rid)
        stream = play.chunks()
        first = await anext(stream)
        await stream.aclose()  # how the server lets go of a play when the browser hangs up
        await speech.aclose()
        return first

    assert asyncio.run(hear_one_chunk()) == AUDIO[0]
    (p,) = plays()
    assert (p["status"], p["bytes"], p["error"]) == ("stopped", len(AUDIO[0]), None)
    assert files(on.speech_path) == []


def test_stopping_while_elevenlabs_is_still_sending(on, eleven):
    credentials.save(API_KEY, KEY)
    eleven.hang_after = 1
    rid = answer(ANSWER)

    async def stop_while_waiting() -> list[bytes]:
        speech = Speech(on)
        play = await speech.open(rid)
        heard: list[bytes] = []

        async def listen():
            async for chunk in play.chunks():
                heard.append(chunk)

        task = asyncio.create_task(listen())
        while not heard:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # waiting on ElevenLabs for the next chunk, which never comes
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await speech.aclose()
        return heard

    assert asyncio.run(stop_while_waiting()) == [AUDIO[0]]
    (p,) = plays()
    assert (p["status"], p["bytes"]) == ("stopped", len(AUDIO[0]))
    assert files(on.speech_path) == []


def test_startup_closes_plays_left_open_and_drops_half_written_clips(on):
    with connect() as conn:
        conn.execute(
            "INSERT INTO speech (created_at, voice_id, model_id, chars, chars_sent, status) VALUES (?, ?, ?, 1, 1, ?)",
            (now_iso(), VOICE, MODEL, "streaming"),
        )
    on.speech_path.mkdir(parents=True)
    (on.speech_path / "abc.7.part").write_bytes(b"half")
    (on.speech_path / "def.mp3").write_bytes(b"whole")
    Speech(on).recover()
    assert [p["status"] for p in plays()] == ["stopped"]
    assert files(on.speech_path) == ["def.mp3"]


# Chats that go away -----------------------------------------------------------------------------


def test_deleting_a_chat_deletes_its_clips_but_not_one_another_chat_plays(on, eleven, client):
    credentials.save(API_KEY, KEY)
    a = client.post("/api/prompt", json={"prompt": "the first chat"}).json()
    b = client.post("/api/prompt", json={"prompt": "the second chat"}).json()
    assert a["response"] == b["response"]  # the same words, so the same clip
    assert client.get(speech_url(a["id"])).headers["x-speech-cached"] == "0"
    assert client.get(speech_url(b["id"])).headers["x-speech-cached"] == "1"
    (clip,) = on.speech_path.glob("*.mp3")

    assert client.delete(f"/api/conversations/{a['conversation_id']}").status_code == 204
    assert clip.is_file()  # the second chat still plays it

    assert client.delete(f"/api/conversations/{b['conversation_id']}").status_code == 204
    assert not clip.exists()
    left = plays()
    assert len(left) == 2  # the timings stay, with no answer and no clip
    assert all(p["request_id"] is None and p["clip_key"] is None for p in left)


# The key ----------------------------------------------------------------------------------------


def test_the_key_goes_to_elevenlabs_and_nowhere_else(on, eleven, client, caplog):
    caplog.set_level(logging.DEBUG)
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    seen = [client.get(speech_url(rid)), client.get(speech_url(rid))]  # played, then from the clip
    eleven.status = 401
    eleven.error = {"detail": {"status": "invalid_api_key", "message": f"Invalid API key {KEY}"}}
    seen.append(client.get(speech_url(answer("Another answer."))))  # refused, repeating the key
    seen += [client.get("/api/speech/stats"), client.get("/api/status"), client.get(f"/api/requests/{rid}")]

    assert len(eleven.calls) == 2
    assert all(c.url.host == "api.elevenlabs.io" and c.headers["xi-api-key"] == KEY for c in eleven.calls)
    assert seen[2].json()["detail"] == (
        f"ElevenLabs refused the saved API key (Invalid API key (the key)). Save a new one with: {SETUP}"
    )
    for r in seen:
        assert KEY not in r.text and not any(KEY in value for value in r.headers.values())
    with connect() as conn:
        assert KEY not in "\n".join(conn.iterdump())
    assert KEY not in caplog.text
    assert not any(KEY.encode() in clip.read_bytes() for clip in on.speech_path.glob("*"))


def test_status_says_whether_read_aloud_is_on_and_a_key_is_saved(cfg, client):
    assert client.get("/api/status").json()["speech"] == {"enabled": False, "key_saved": False}
    cfg.speech.enabled = True
    credentials.save(API_KEY, KEY)
    r = client.get("/api/status")
    assert r.json()["speech"] == {"enabled": True, "key_saved": True}
    assert KEY not in r.text


def test_the_http_client_is_made_at_startup_when_read_aloud_is_on(cfg, fake_ollama, eleven):
    """Making it loads the CA certificates, which would otherwise count against the first play."""
    app = create_app(cfg, background_sync=False)
    with TestClient(app):
        assert app.state.service.speech.http is None  # off, so nothing is made
    cfg.speech.enabled = True
    app = create_app(cfg, background_sync=False)
    with TestClient(app):
        assert app.state.service.speech.http is not None
    assert app.state.service.speech.http is None  # closed on the way out
    assert eleven.calls == []  # making it sends nothing


# Numbers ----------------------------------------------------------------------------------------


def test_the_numbers_are_counts_and_medians(on, client):
    with connect() as conn:
        for cached, status, first, total, sent in [
            (0, "ok", 300, 2000, 100), (0, "ok", 100, 1000, 100), (0, "stopped", 200, 500, 100),
            (1, "ok", 10, 50, 0), (1, "ok", 30, 70, 0),
            (0, "error", None, 20, 100),  # refused before any audio, so its characters don't count
            (0, "streaming", None, None, 100),  # still playing, so not counted yet
        ]:
            conn.execute(
                """INSERT INTO speech (created_at, voice_id, model_id, chars, chars_sent, cached, status,
                                       first_audio_ms, total_ms) VALUES (?, ?, ?, 100, ?, ?, ?, ?, ?)""",
                (now_iso(), VOICE, MODEL, sent, cached, status, first, total),
            )
    on.speech_path.mkdir(parents=True)
    (on.speech_path / "a.mp3").write_bytes(b"x" * 1000)
    (on.speech_path / "b.mp3").write_bytes(b"x" * 500)

    assert client.get("/api/speech/stats").json() == {
        "plays": 6, "from_clips": 2, "stopped": 1, "errors": 1, "chars_sent": 300,
        "first_audio_ms": {"elevenlabs": 200, "elevenlabs_plays": 3, "saved": 20, "saved_plays": 2},
        "total_ms": {"elevenlabs": 1500},
        "clips": 2, "clip_bytes": 1500, "voice_id": VOICE, "model_id": MODEL,
    }


def test_the_play_log_holds_no_text(on, eleven, client):
    credentials.save(API_KEY, KEY)
    client.get(speech_url(answer(ANSWER)))
    (p,) = plays()
    assert not any(isinstance(v, str) and ("Read the" in v or "Plan" in v) for v in p.values())


# The speech command -----------------------------------------------------------------------------


def command(*words: str):
    return build_parser().parse_args(["speech", *words])


def test_connect_saves_the_key_from_a_hidden_prompt(cfg, monkeypatch, capsys):
    prompts: list[str] = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or f"  {KEY}\n")
    assert speech_cli.run(command("connect"), cfg) == 0
    assert credentials.get(API_KEY) == KEY
    assert len(prompts) == 1 and "won't be shown" in prompts[0]
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert "Saved the key" in out.out and "turned off" in out.out  # read aloud is off in this config


@pytest.mark.parametrize("pasted", ["", "   ", "two words", "x" * 201])
def test_connect_refuses_what_is_not_a_key(cfg, monkeypatch, capsys, pasted):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": pasted)
    assert speech_cli.run(command("connect"), cfg) == 1
    assert credentials.get(API_KEY) is None
    assert "Not saved" in capsys.readouterr().err


def test_forget_removes_the_key(cfg, capsys):
    credentials.save(API_KEY, KEY)
    assert speech_cli.run(command("forget"), cfg) == 0
    assert credentials.get(API_KEY) is None
    assert speech_cli.run(command("forget"), cfg) == 0
    out = capsys.readouterr().out
    assert "Removed the ElevenLabs key." in out and "There was no ElevenLabs key saved." in out


def test_status_prints_counts_and_timings_only(on, eleven, client, capsys):
    credentials.save(API_KEY, KEY)
    rid = answer(ANSWER)
    client.get(speech_url(rid))
    client.get(speech_url(rid))
    assert command().action == "status"  # the default
    assert speech_cli.run(command(), on) == 0
    out = capsys.readouterr().out
    assert "Read aloud: on, key saved" in out
    assert f"Voice {VOICE}, model {MODEL}" in out
    assert "Plays: 2 (1 from saved clips, 0 stopped, 0 failed)" in out
    assert f"Characters sent to ElevenLabs, in plays it answered: {len(SPOKEN)}" in out
    assert "Saved clips: 1" in out
    assert KEY not in out and "Read the answer" not in out


# Config -----------------------------------------------------------------------------------------


def test_read_aloud_is_off_until_turned_on_and_its_ids_are_checked():
    s = SpeechCfg()
    assert (s.enabled, s.voice_id, s.model_id, s.max_chars, s.cache) == (False, VOICE, MODEL, 2500, True)
    for bad in ({"voice_id": "../../v1/user"}, {"model_id": "a b"}, {"max_chars": 99}, {"max_chars": 40001}):
        with pytest.raises(ValidationError):
            SpeechCfg(**bad)


def test_the_tracked_config_keeps_read_aloud_off():
    raw = yaml.safe_load((Path(__file__).parents[2] / "config.yaml").read_text(encoding="utf-8"))
    assert raw["speech"] == {"enabled": False, "voice_id": VOICE, "model_id": MODEL, "max_chars": 2500, "cache": True}
    assert Config.model_validate(raw).speech.enabled is False


def test_an_unknown_key_under_speech_stops_the_load(tmp_path):
    base = {"server": {"data_dir": str(tmp_path / "data")},
            "models": {"qwen-moe": {"provider": "ollama", "model": "qwen3.6:35b-a3b"}},
            "routing": {"classifier": {"model": "qwen-moe"}, "rules": [{"name": "default", "model": "qwen-moe"}]},
            "pricing": {"reference_model": None}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    local = tmp_path / "config.local.yaml"
    for settings, message in [
        ({"speech": {"enabled": True, "voice": "made-up"}}, "In config.local.yaml, speech.voice isn't a setting."),
        ({"voice_id": "made-up"}, "In config.local.yaml, voice_id isn't a setting at the top level. "
                                  "It goes under speech, as speech.voice_id."),
    ]:
        local.write_text(yaml.safe_dump(settings), encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            load_config(path)
        assert str(caught.value) == message
