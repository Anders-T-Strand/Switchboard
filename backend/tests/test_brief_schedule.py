"""The morning schedule: when the day's build is due, how it waits for Ollama, the notification it
ends in and the setting that says when it starts. Sources are fakes and nothing here waits."""

from __future__ import annotations

import threading
from datetime import datetime, time, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from switchboard import main as server
from switchboard.brief import Brief, Found, SourceError, ready_message, store
from switchboard.config import Config, load_config

LOCAL = timezone(timedelta(hours=-6))
DAY = datetime(2026, 11, 16, tzinfo=LOCAL)


def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return DAY + timedelta(days=days, hours=hour, minutes=minute)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeSource:
    def __init__(self, name: str = "mail", error: Exception | None = None):
        self.name, self.label, self.setup = name, name.capitalize(), ""
        self.error = error

    def connected(self) -> bool:
        return True

    def collect(self, now, usage) -> list[Found]:
        if self.error:
            raise self.error
        return [Found(f"{self.name}-1", "Something")]


@pytest.fixture
def on(cfg) -> Config:
    cfg.brief.enabled = True  # build_after stays at its default, 06:30
    return cfg


# When the day's build is due ---------------------------------------------------------


def test_it_is_due_after_build_after_until_a_build_goes_through(on):
    clock = Clock(at(6, 0))
    brief = Brief(on, [FakeSource()], clock=clock)
    assert brief.due() is False  # not yet 06:30
    clock.now = at(6, 31)
    assert brief.due() is True
    brief.run("schedule")
    assert brief.due() is False  # done for the day
    clock.now = at(6, 31, days=1)
    assert brief.due() is True  # and due again the next morning


def test_started_later_it_builds_right_away(on):
    assert Brief(on, [FakeSource()], clock=Clock(at(11, 0))).due() is True  # the PC was off at 06:30


def test_a_build_in_the_small_hours_does_not_count_for_the_morning(on):
    clock = Clock(at(2, 0))
    brief = Brief(on, [FakeSource()], clock=clock)
    brief.run("manual")
    clock.now = at(7, 0)
    assert brief.due() is True


def test_a_manual_build_after_build_after_counts(on):
    clock = Clock(at(7, 0))
    brief = Brief(on, [FakeSource()], clock=clock)
    brief.run("manual")
    assert brief.due() is False


def test_a_failed_build_is_tried_again_after_a_while_and_a_partial_one_counts(on):
    clock = Clock(at(7, 0))
    mail, course = FakeSource("mail", SourceError("offline")), FakeSource("coursework", SourceError("offline"))
    brief = Brief(on, [mail, course], clock=clock)
    assert brief.run("schedule")["status"] == "error"
    clock.now = at(7, 10)
    assert brief.due() is False  # it failed ten minutes ago
    clock.now = at(7, 31)
    assert brief.due() is True
    course.error = None
    assert brief.run("schedule")["status"] == "partial"
    assert brief.due() is False  # half a brief beats trying all morning


def test_it_is_never_due_while_the_brief_is_off_or_building(cfg):
    brief = Brief(cfg, [FakeSource()], clock=Clock(at(7, 0)))
    assert brief.due() is False  # off
    cfg.brief.enabled = True
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeSource):
        def collect(self, now, usage):
            entered.set()
            assert release.wait(5)
            return super().collect(now, usage)

    brief.sources = [Slow()]
    assert brief.start("manual") is True and entered.wait(5)
    assert brief.due() is False
    release.set()
    brief.wait(5)


# The schedule in the server -------------------------------------------------------------


def test_the_schedule_waits_a_little_for_ollama_after_startup(on, fake_ollama):
    app = server.create_app(on, background_sync=False)
    svc = app.state.service
    svc.brief.sources = [FakeSource()]
    svc.brief._clock = Clock(at(8, 0))
    fake_ollama.down = True

    assert server.brief_tick(svc, started=0, now=60) is False  # just started, and Ollama may still be starting
    assert store.latest_run() is None
    assert server.brief_tick(svc, started=0, now=server.OLLAMA_WAIT_S + 1) is True  # waited long enough
    assert store.latest_run()["started_by"] == "schedule"
    assert server.brief_tick(svc, started=0, now=server.OLLAMA_WAIT_S + 61) is False  # done for today


def test_the_schedule_builds_at_once_when_ollama_is_up(on, fake_ollama):
    svc = server.create_app(on, background_sync=False).state.service
    svc.brief.sources = [FakeSource()]
    svc.brief._clock = Clock(at(8, 0))
    assert server.brief_tick(svc, started=0, now=1) is True


def test_the_server_runs_the_schedule_only_while_the_brief_is_on(cfg, fake_ollama, monkeypatch):
    monkeypatch.setattr(Brief, "due", lambda self: False)  # no build, only the thread

    def names() -> set[str]:
        return {t.name for t in threading.enumerate()}

    with TestClient(server.create_app(cfg)):
        assert "brief-schedule" not in names()
    cfg.brief.enabled = True
    with TestClient(server.create_app(cfg)):
        assert "brief-schedule" in names()
    assert "brief-schedule" not in names()  # stopped with the server


# Telling about a finished build ------------------------------------------------------------


def open_item(source: str = "mail", owner: str = "you") -> dict:
    return {"status": "open", "source": source, "owner": owner, "title": "Secret offer", "context": "Priya"}


def test_the_notification_counts_and_says_nothing_else():
    view = {"items": [open_item(), open_item(), open_item(owner="model"), open_item("coursework"),
                      open_item("coursework"), open_item("coursework"), {**open_item(), "status": "done"}]}
    message = ready_message({"status": "ok"}, view)
    assert message == "Today's brief is ready: 2 need you, 1 a model can draft and 3 coursework."
    assert "Secret" not in message and "Priya" not in message
    one = {"items": [open_item()]}
    assert ready_message({"status": "ok"}, one) == "Today's brief is ready: 1 needs you."
    assert ready_message({"status": "ok"}, {"items": []}) == "Today's brief is ready. Nothing needs you right now."
    assert ready_message({"status": "partial"}, one).endswith(
        "Part of it couldn't be read, so open Switchboard to see which.")
    assert ready_message({"status": "error"}, view) == "Today's brief couldn't be built. Open Switchboard to see why."


def test_every_build_ends_by_calling_on_built(on):
    brief = Brief(on, [FakeSource()], clock=Clock(at(7, 0)))
    seen: list[dict] = []
    brief.on_built = seen.append
    run = brief.run("schedule")
    assert seen == [run]

    def broken(run):
        raise RuntimeError("the tray icon is gone")

    brief.on_built = broken
    assert brief.run("manual")["status"] == "ok"  # a hook that fails doesn't fail the build


# The setting -------------------------------------------------------------------------------


BASE = {
    "models": {"qwen-moe": {"provider": "ollama", "model": "qwen3.6:35b-a3b"}},
    "routing": {"classifier": {"model": "qwen-moe"}, "rules": [{"name": "default", "model": "qwen-moe"}]},
    "pricing": {"reference_model": None},
}


def config_with(tmp_path, brief_yaml: str) -> Config:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, "server": {"data_dir": str(tmp_path / "data")}}), encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text(f"brief:\n  build_after: {brief_yaml}\n", encoding="utf-8")
    return load_config(path)


def test_build_after_is_a_time_of_day(tmp_path):
    assert config_with(tmp_path, '"07:15"').brief.build_time == time(7, 15)
    assert config_with(tmp_path, "7:15").brief.build_after == "07:15"  # YAML reads that as the number 435
    assert config_with(tmp_path, "06:05").brief.build_after == "06:05"
    assert config_with(tmp_path, '"6:05"').brief.build_after == "06:05"
    for bad in ('"25:00"', "noon", '"7:60"'):
        with pytest.raises(ValueError, match="build_after"):
            config_with(tmp_path, bad)
