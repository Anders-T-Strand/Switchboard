"""The morning brief: the local config overlay, the credential store, building and storing items,
the API and the command. Sources are fakes. Nothing here reads mail, coursework or a real
credential store, and nothing reaches a Switchboard that happens to be running on this machine."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from switchboard import credentials
from switchboard.__main__ import build_parser, main
from switchboard.brief import Brief, Busy, Found, SourceError, stamp, store
from switchboard.brief import cli as brief_cli
from switchboard.config import load_config, merge
from switchboard.db import connect
from switchboard.main import create_app

MORNING = datetime(2026, 11, 16, 8, 0, tzinfo=timezone(timedelta(hours=-6)))
REAL_STORE = credentials._store  # taken at import, before conftest.py puts a fake in its place for each test


class Clock:
    def __init__(self, now: datetime = MORNING):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def forward(self, **delta) -> None:
        self.now += timedelta(**delta)


class FakeSource:
    def __init__(self, name: str = "mail", found: list[Found] | None = None, *, connected: bool = True,
                 error: Exception | None = None):
        self.name = name
        self.label = name.capitalize()
        self.setup = f"connect {name}"
        self.found = list(found or [])
        self.is_connected = connected
        self.error = error
        self.calls = 0

    def connected(self) -> bool:
        return self.is_connected

    def collect(self, now, usage) -> list[Found]:
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.found)


def found(n: int, **fields) -> Found:
    return Found(**{"external_id": f"id-{n}", "title": f"Item {n}", **fields})


def item_rows() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM brief_items ORDER BY id")]


# The local config ----------------------------------------------------------------

BASE = {
    "models": {"qwen-moe": {"provider": "ollama", "model": "qwen3.6:35b-a3b"}},
    "routing": {"classifier": {"model": "qwen-moe"}, "fallback_order": ["qwen-moe"],
                "rules": [{"name": "default", "model": "qwen-moe"}]},
    "pricing": {"reference_model": None},
}


def write_config(tmp_path, local: dict | str | None = None):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, "server": {"data_dir": str(tmp_path / "data")}}), encoding="utf-8")
    if local is not None:
        text = local if isinstance(local, str) else yaml.safe_dump(local)
        (tmp_path / "config.local.yaml").write_text(text, encoding="utf-8")
    return path


def test_merge_goes_key_by_key_and_replaces_lists():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    assert merge(base, {"a": {"y": [3]}, "c": 2}) == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # what was passed in is left alone


def test_local_config_is_laid_over_the_tracked_one(tmp_path):
    cfg = load_config(write_config(tmp_path, {"brief": {"enabled": True}, "server": {"port": 9001}}))
    assert cfg.brief.enabled is True and cfg.server.port == 9001
    assert cfg.data_path == tmp_path / "data"  # a setting the local file doesn't name is kept
    assert list(cfg.models) == ["qwen-moe"]


def test_the_brief_is_off_without_a_local_config(tmp_path):
    assert load_config(write_config(tmp_path)).brief.enabled is False


def test_local_config_has_to_be_a_mapping(tmp_path):
    with pytest.raises(ValueError, match="config.local.yaml has to be a mapping"):
        load_config(write_config(tmp_path, "- just\n- a list\n"))


# Credentials. `vault` is the fake store that conftest.py gives every test. ---------------


def test_credentials_round_trip(vault):
    assert credentials.get("canvas_feed_url") is None
    credentials.save("canvas_feed_url", "https://example.com/feed.ics")
    assert vault.saved == {("Switchboard", "canvas_feed_url"): "https://example.com/feed.ics"}
    assert credentials.get("canvas_feed_url") == "https://example.com/feed.ics"
    assert credentials.forget("canvas_feed_url") is True
    assert credentials.forget("canvas_feed_url") is False
    assert credentials.get("canvas_feed_url") is None


def test_an_environment_variable_wins_over_the_store(vault, monkeypatch):
    credentials.save("outlook_refresh_token", "from the store")
    monkeypatch.setenv("SWITCHBOARD_OUTLOOK_REFRESH_TOKEN", "from the environment")
    assert credentials.get("outlook_refresh_token") == "from the environment"


def test_a_machine_without_a_store_reads_as_nothing_saved(monkeypatch):
    def no_store():
        raise ImportError("No module named 'keyring'")

    monkeypatch.setattr(credentials, "_store", no_store)
    assert credentials.get("canvas_feed_url") is None
    assert credentials.forget("canvas_feed_url") is False
    with pytest.raises(credentials.CredentialError) as failure:
        credentials.save("canvas_feed_url", "https://example.com/private-feed.ics")
    assert "SWITCHBOARD_CANVAS_FEED_URL" in str(failure.value)  # it says what to do instead
    assert "private-feed" not in str(failure.value)  # and never repeats the secret


def test_the_store_can_be_turned_off_for_an_instance_that_must_not_sign_in_as_you(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_NO_CREDENTIAL_STORE", "1")
    monkeypatch.setattr(credentials, "_store", REAL_STORE)  # the real lookup, which the switch stops short
    assert credentials.get("canvas_feed_url") is None
    with pytest.raises(credentials.CredentialError):
        credentials.save("canvas_feed_url", "https://example.com/feed.ics")
    monkeypatch.setenv("SWITCHBOARD_CANVAS_FEED_URL", "https://example.com/made-up.ics")
    assert credentials.get("canvas_feed_url") == "https://example.com/made-up.ics"  # the override still works


# Building ------------------------------------------------------------------------


def test_build_saves_what_the_sources_found(cfg):
    mail = FakeSource("mail", [found(1, context="Dana", summary="Asks for times", owner="model",
                                     received_at=stamp(MORNING - timedelta(hours=1)),
                                     link="https://example.com/1")])
    course = FakeSource("coursework", [found(2, context="ALGO 3110", due_at="2026-11-17")])
    run = Brief(cfg, [mail, course], clock=Clock()).run("manual")

    assert (run["status"], run["day"], run["started_by"]) == ("ok", "2026-11-16", "manual")
    assert [(s["name"], s["ok"], s["items"]) for s in run["sources"]] == [("mail", True, 1), ("coursework", True, 1)]
    assert [(r["source"], r["external_id"], r["owner"], r["status"]) for r in item_rows()] == [
        ("mail", "id-1", "model", "open"), ("coursework", "id-2", "you", "open")]


def test_a_rebuild_updates_items_and_never_doubles_them(cfg):
    source = FakeSource("mail", [found(1, summary="first reading")])
    brief = Brief(cfg, [source], clock=Clock())
    brief.run("manual")
    source.found = [found(1, title="Item 1, edited", summary="second reading")]
    brief.run("manual")

    (row,) = item_rows()
    assert (row["title"], row["summary"]) == ("Item 1, edited", "second reading")


def test_a_build_that_did_not_judge_an_item_keeps_the_earlier_judgment(cfg):
    source = FakeSource("mail", [found(1, summary="Asks for times", category="recruiter", owner="model",
                                       reason="Can be drafted")])
    brief = Brief(cfg, [source], clock=Clock())
    brief.run("manual")
    source.found = [found(1)]  # the same mail on a morning when the local model was down
    brief.run("manual")

    (row,) = item_rows()
    assert (row["summary"], row["category"], row["owner"], row["reason"]) == (
        "Asks for times", "recruiter", "model", "Can be drafted")


def test_a_closed_item_stays_closed_until_there_is_news(cfg):
    clock = Clock()
    mail = FakeSource("mail", [found(1, received_at=stamp(MORNING - timedelta(hours=2)))])
    course = FakeSource("coursework", [found(2, due_at="2026-11-17")])
    brief = Brief(cfg, [mail, course], clock=clock)
    brief.run("manual")
    for row in item_rows():
        brief.set_status(row["id"], "done")

    clock.forward(hours=1)
    brief.run("manual")
    assert [r["status"] for r in item_rows()] == ["done", "done"]

    clock.forward(hours=1)
    mail.found = [found(1, received_at=stamp(clock.now - timedelta(minutes=5)))]  # a new message in the thread
    brief.run("manual")
    assert [r["status"] for r in item_rows()] == ["open", "done"]


def test_a_source_that_knows_an_item_is_handled_closes_it(cfg):
    source = FakeSource("mail", [found(1)])
    brief = Brief(cfg, [source], clock=Clock())
    brief.run("manual")
    replied = stamp(MORNING - timedelta(minutes=30))
    # The first got its reply. The second was answered before any build had listed it.
    source.found = [found(1, done=True, done_at=replied), found(2, done=True)]
    brief.run("manual")

    # Closed when the reply went out, and the one that was never on the list isn't stored at all.
    assert [(r["external_id"], r["status"], r["status_at"]) for r in item_rows()] == [("id-1", "done", replied)]


def test_a_failing_source_is_reported_and_the_others_go_on(cfg, caplog):
    private = "https://school.example/feeds/user_PRIVATE.ics"
    sources = [
        FakeSource("mail", error=SourceError("Sign in to Outlook again.")),
        FakeSource("coursework", error=RuntimeError(f"404 for url {private}")),
        FakeSource("board", [found(1)]),
    ]
    with caplog.at_level(logging.INFO):
        run = Brief(cfg, sources, clock=Clock()).run("manual")

    assert run["status"] == "partial"
    by_name = {s["name"]: s for s in run["sources"]}
    assert by_name["mail"]["error"] == "Sign in to Outlook again."
    assert by_name["coursework"]["error"] == "stopped unexpectedly (RuntimeError)"
    assert by_name["board"]["ok"] is True and len(item_rows()) == 1
    # What an unexpected error says can be private. It reaches neither the saved run nor the log.
    with connect() as conn:
        saved = conn.execute("SELECT sources FROM brief_runs").fetchone()[0]
    assert "PRIVATE" not in saved and "PRIVATE" not in caplog.text


def test_a_build_where_every_source_fails_is_an_error(cfg):
    run = Brief(cfg, [FakeSource("mail", error=SourceError("offline"))], clock=Clock()).run("manual")
    assert run["status"] == "error"


def test_a_source_that_is_not_connected_is_skipped(cfg):
    source = FakeSource("mail", [found(1)], connected=False)
    run = Brief(cfg, [source], clock=Clock()).run("manual")

    assert run["status"] == "ok" and source.calls == 0 and item_rows() == []
    assert (run["sources"][0]["connected"], run["sources"][0]["ok"]) == (False, None)


def test_what_a_source_says_is_cut_to_size_and_only_https_links_are_kept(cfg):
    source = FakeSource("mail", [
        found(1, title="x" * 1000, summary="y" * 1000, link="javascript:alert(1)", owner="root"),
        found(2, link="http://example.com/not-secure"),
        found(3, title="", link="https://example.com/fine"),
    ])
    Brief(cfg, [source], clock=Clock()).run("manual")

    first, second, third = item_rows()
    assert (len(first["title"]), len(first["summary"]), first["owner"]) == (300, 400, "you")
    assert (first["link"], second["link"], third["link"]) == (None, None, "https://example.com/fine")
    assert third["title"] == "(no title)"


def test_only_one_build_at_a_time(cfg):
    cfg.brief.enabled = True
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeSource):
        def collect(self, now, usage):
            entered.set()
            assert release.wait(5)
            return super().collect(now, usage)

    brief = Brief(cfg, [Slow("mail", [found(1)])], clock=Clock())
    assert brief.start("manual") is True
    assert brief.building() is True  # from the moment start() returns, so a page that asks at once sees it
    assert entered.wait(5)
    assert brief.start("manual") is False
    with pytest.raises(Busy):
        brief.run("cli")
    assert brief.view()["run"]["status"] == "running"

    release.set()
    brief.wait(5)
    assert brief.building() is False
    assert brief.view()["run"]["status"] == "ok" and len(item_rows()) == 1


def test_start_needs_the_brief_turned_on(cfg):
    with pytest.raises(ValueError, match="turned off"):
        Brief(cfg, [FakeSource()]).start("manual")


def test_a_build_leaves_the_chat_tables_alone(cfg):
    """Everything in requests becomes a chat, gets indexed and can reach Claude through Memory.
    What the brief reads must never land there."""
    Brief(cfg, [FakeSource("mail", [found(1, summary="private")])], clock=Clock()).run("manual")
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_what_the_local_model_spent_is_saved_with_the_run(cfg):
    class Sorting(FakeSource):
        def collect(self, now, usage):
            usage.add("qwen-moe", 120, 30, 900)
            usage.add("qwen-moe", 80, 20, 600)
            return super().collect(now, usage)

    run = Brief(cfg, [Sorting("mail", [found(1)])], clock=Clock()).run("manual")
    assert (run["model_key"], run["input_tokens"], run["output_tokens"], run["model_ms"]) == ("qwen-moe", 200, 50, 1500)


def test_recover_closes_a_run_that_was_cut_off(cfg):
    store.start_run("2026-11-15", "schedule", stamp(MORNING - timedelta(days=1)))
    brief = Brief(cfg, [], clock=Clock())
    assert brief.view()["run"]["status"] == "running"
    brief.recover()

    run = brief.view()["run"]
    assert run["status"] == "error" and "stopped" in run["error"]


def test_view_puts_the_soonest_due_first_and_keeps_todays_closed_items(cfg):
    clock = Clock()
    source = FakeSource("coursework", [found(1, due_at="2026-11-20"), found(2, due_at="2026-11-17"), found(3),
                                       found(4, due_at="2026-11-18")])
    brief = Brief(cfg, [source], clock=clock)
    brief.run("manual")
    ids = {r["external_id"]: r["id"] for r in item_rows()}
    brief.set_status(ids["id-4"], "dismissed")

    view = brief.view()
    assert view["day"] == "2026-11-16" and view["building"] is False
    assert [(i["external_id"], i["status"]) for i in view["items"]] == [
        ("id-2", "open"), ("id-1", "open"), ("id-3", "open"), ("id-4", "dismissed")]
    assert brief.counts() == {"open": 3, "open_by_source": {"coursework": 3}, "closed": 1}

    clock.forward(days=1)  # the next day it no longer shows
    assert [i["external_id"] for i in brief.view()["items"]] == ["id-2", "id-1", "id-3"]
    assert brief.counts()["closed"] == 0


def test_an_item_its_source_stopped_listing_is_not_shown_and_comes_back_if_listed_again(cfg):
    clock = Clock()
    source = FakeSource("coursework", [found(1), found(2)])
    brief = Brief(cfg, [source], clock=clock)
    brief.run("manual")

    clock.forward(hours=1)
    source.found = [found(2)]  # the first one has passed, such as coursework well after its due date
    brief.run("manual")
    assert [i["external_id"] for i in brief.view()["items"]] == ["id-2"]
    assert brief.counts() == {"open": 1, "open_by_source": {"coursework": 1}, "closed": 0}

    clock.forward(hours=1)
    source.found = [found(1), found(2)]  # its due date was moved, so the source lists it again
    brief.run("manual")
    assert [i["external_id"] for i in brief.view()["items"]] == ["id-1", "id-2"]


def test_a_source_that_fails_hides_nothing(cfg):
    clock = Clock()
    source = FakeSource("coursework", [found(1), found(2)])
    brief = Brief(cfg, [source], clock=clock)
    brief.run("manual")

    clock.forward(hours=1)
    source.error = SourceError("offline")
    assert brief.run("manual")["status"] == "error"
    assert [i["external_id"] for i in brief.view()["items"]] == ["id-1", "id-2"]


def test_an_item_goes_on_the_board_once(cfg):
    made = []

    def create_task(title: str, notes: str) -> dict:
        made.append((title, notes))
        ts = stamp(MORNING)
        with connect() as conn:
            task_id = conn.execute(
                "INSERT INTO tasks (title, notes, status, created_at, updated_at) VALUES (?, ?, 'todo', ?, ?)",
                (title, notes, ts, ts),
            ).lastrowid
        return {"id": task_id}

    source = FakeSource("mail", [found(1, context="Dana", link="https://example.com/1")])
    brief = Brief(cfg, [source], create_task=create_task, clock=Clock())
    brief.run("manual")
    item_id = item_rows()[0]["id"]
    first = brief.make_task(item_id)
    again = brief.make_task(item_id)

    assert made == [("Item 1", "Dana\nhttps://example.com/1")]
    assert first["task_id"] == again["task_id"] and first["task_title"] == "Item 1"
    with pytest.raises(store.ItemNotFound):
        brief.make_task(999)


# API -----------------------------------------------------------------------------


@pytest.fixture
def client(cfg, fake_ollama):
    app = create_app(cfg, background_sync=False)
    with TestClient(app) as c:
        yield c


def test_api_brief_is_off_until_it_is_turned_on(client):
    view = client.get("/api/brief").json()
    assert (view["enabled"], view["building"], view["run"], view["items"]) == (False, False, None, [])
    # Every source is listed, with the command that connects it, so the page can say what to do.
    assert view["sources"] == [
        {"name": "mail", "label": "Outlook", "connected": False,
         "setup": "python -m switchboard brief connect outlook"},
        {"name": "coursework", "label": "Canvas", "connected": False,
         "setup": "python -m switchboard brief connect canvas"},
    ]
    refused = client.post("/api/brief/run")
    assert refused.status_code == 400 and "turned off" in refused.json()["detail"]
    assert client.get("/api/config").json()["brief"]["enabled"] is False


def test_api_builds_the_brief_and_changes_items(client):
    svc = client.app.state.service
    svc.cfg.brief.enabled = True
    svc.brief.sources = [FakeSource("mail", [found(1, context="Dana", link="https://example.com/1")])]

    assert client.post("/api/brief/run").status_code == 202
    svc.brief.wait(5)
    view = client.get("/api/brief").json()
    assert (view["building"], view["run"]["status"], view["run"]["started_by"]) == (False, "ok", "manual")
    assert view["sources"] == [{"name": "mail", "label": "Mail", "connected": True, "setup": "connect mail"}]
    (item,) = view["items"]
    assert (item["title"], item["status"], item["task_id"]) == ("Item 1", "open", None)

    done = client.patch(f"/api/brief/items/{item['id']}", json={"status": "done"}).json()
    assert done["status"] == "done" and done["status_at"]
    assert client.patch(f"/api/brief/items/{item['id']}", json={"status": "later"}).status_code == 400
    assert client.patch("/api/brief/items/999", json={"status": "done"}).status_code == 404

    linked = client.post(f"/api/brief/items/{item['id']}/task").json()
    again = client.post(f"/api/brief/items/{item['id']}/task").json()
    (task,) = client.get("/api/tasks").json()
    assert (task["title"], task["notes"]) == ("Item 1", "Dana\nhttps://example.com/1")
    assert linked["task_id"] == again["task_id"] == task["id"] and linked["task_title"] == "Item 1"
    assert client.post("/api/brief/items/999/task").status_code == 404


# The command -----------------------------------------------------------------------


@pytest.fixture
def no_server(monkeypatch):
    """The command asks a running Switchboard to do the work when it finds one. A test must never
    find the real one on this machine."""
    monkeypatch.setattr(brief_cli.Server, "find", classmethod(lambda cls, cfg: None))


def command(action: str):
    return build_parser().parse_args(["brief", action])


def test_brief_arguments(capsys):
    parser = build_parser()
    assert parser.parse_args(["brief"]).action == "build"
    assert parser.parse_args(["brief", "status"]).action == "status"
    assert parser.parse_args(["brief", "demo"]).action == "demo"
    with pytest.raises(SystemExit):
        parser.parse_args(["brief", "send"])
    capsys.readouterr()  # argparse printed its usage. Keep it out of the test output.


def test_main_hands_brief_to_its_command(cfg, no_server, monkeypatch, capsys):
    monkeypatch.setattr("switchboard.config.get_config", lambda: cfg)
    assert main(["brief", "status"]) == 0
    assert "Last build: never" in capsys.readouterr().out


def test_demo_fills_a_database_of_its_own(cfg, no_server, capsys):
    assert brief_cli.run(command("demo"), cfg) == 0
    out = capsys.readouterr().out
    assert "started by demo" in out and "Open: 8 (coursework 4, mail 4)" in out
    assert "Interview" not in out and "Dana" not in out  # numbers, never the items themselves
    assert len(item_rows()) == 8


def test_demo_refuses_the_default_data_folder(cfg, no_server, monkeypatch, capsys):
    monkeypatch.setattr(brief_cli, "REPO_ROOT", cfg.data_path.parent)  # so this test's folder is "the default"
    assert brief_cli.run(command("demo"), cfg) == 1
    assert "won't touch the default data folder" in capsys.readouterr().err
    assert item_rows() == []


def test_status_prints_counts_and_nothing_from_the_items(cfg, no_server, capsys):
    source = FakeSource("mail", [found(1, title="Offer letter", context="Priya", summary="Expires Friday")])
    Brief(cfg, [source]).run("manual")
    assert brief_cli.run(command("status"), cfg) == 0

    out = capsys.readouterr().out
    assert "Mail: 1 found" in out and "Open: 1 (mail 1)" in out and "Closed today: 0" in out
    for private in ("Offer", "Priya", "Friday"):
        assert private not in out


def test_build_needs_the_brief_turned_on(cfg, no_server, capsys):
    assert brief_cli.run(command("build"), cfg) == 1
    assert "turned off" in capsys.readouterr().err
    assert store.latest_run() is None


def test_build_runs_in_this_process_when_no_server_is_up(cfg, no_server, capsys):
    cfg.brief.enabled = True
    assert brief_cli.run(command("build"), cfg) == 0
    assert "started by cli" in capsys.readouterr().out
    assert store.latest_run()["status"] == "ok"


def test_build_goes_through_a_running_server(cfg, monkeypatch, capsys):
    cfg.brief.enabled = True
    run = {"started_at": stamp(MORNING), "status": "ok", "started_by": "manual", "sources": [], "error": None}
    going = {"enabled": True, "building": True, "run": None, "items": []}
    finished = {"enabled": True, "building": False, "run": run,
                "items": [{"status": "open", "source": "mail"}, {"status": "done", "source": "mail"}]}
    answers = iter([going, going, finished])
    calls = []

    class FakeServer:
        def call(self, method: str, path: str) -> dict:
            calls.append((method, path))
            return next(answers)

    monkeypatch.setattr(brief_cli.Server, "find", classmethod(lambda cls, cfg: FakeServer()))
    monkeypatch.setattr(brief_cli.time, "sleep", lambda seconds: None)
    assert brief_cli.run(command("build"), cfg) == 0

    assert calls == [("POST", "/api/brief/run"), ("GET", "/api/brief"), ("GET", "/api/brief")]
    out = capsys.readouterr().out
    assert "Open: 1 (mail 1)" in out and "Closed today: 1" in out
    assert store.latest_run() is None  # the server built it. This process built nothing.


def test_a_server_that_refuses_is_reported(cfg, monkeypatch, capsys):
    class Refusing:
        def call(self, method: str, path: str) -> dict:
            raise brief_cli.ServerError("The morning brief is turned off.")

    monkeypatch.setattr(brief_cli.Server, "find", classmethod(lambda cls, cfg: Refusing()))
    assert brief_cli.run(command("status"), cfg) == 1
    assert "Error: The morning brief is turned off." in capsys.readouterr().err


def test_the_server_is_found_by_its_health_check(cfg, monkeypatch):
    asked = []

    def healthy(url, timeout):
        asked.append(url)
        return httpx.Response(200, json={"ok": True})

    def refused(url, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", healthy)
    assert brief_cli.Server.find(cfg).base == "http://127.0.0.1:8787"
    assert asked == ["http://127.0.0.1:8787/api/health"]
    monkeypatch.setattr(httpx, "get", refused)
    assert brief_cli.Server.find(cfg) is None
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, text="<html>another program</html>"))
    assert brief_cli.Server.find(cfg) is None


def test_a_server_error_carries_the_servers_own_words(monkeypatch):
    def refuse(method, url, timeout):
        return httpx.Response(400, json={"detail": "The morning brief is turned off."})

    monkeypatch.setattr(httpx, "request", refuse)
    with pytest.raises(brief_cli.ServerError, match="turned off"):
        brief_cli.Server("http://127.0.0.1:1").call("POST", "/api/brief/run")


def test_a_server_started_before_the_brief_existed_is_told_apart(monkeypatch):
    def not_found(method, url, timeout):
        return httpx.Response(404, json={"detail": "Not Found"})

    monkeypatch.setattr(httpx, "request", not_found)
    with pytest.raises(brief_cli.ServerError, match="older than this command. Restart it"):
        brief_cli.Server("http://127.0.0.1:1").call("GET", "/api/brief")
