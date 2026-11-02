"""Conversations: history reaching each provider, context-aware routing, ratings and export."""

import json

import pytest
from fastapi.testclient import TestClient

from switchboard.main import create_app
from switchboard.providers import ClaudeCLI, OllamaClient
from switchboard.router import Router, trim_history


@pytest.fixture
def client(cfg, fake_ollama):
    with TestClient(create_app(cfg, background_sync=False)) as c:
        yield c


def send(client, prompt, chat=None, **kw):
    r = client.post("/api/prompt", json={"prompt": prompt, "conversation_id": chat, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_every_prompt_starts_or_continues_a_chat(client):
    first = send(client, "hello there, what is a monad")
    cid = first["conversation_id"]
    assert first["turn"] == 1 and first["context_turns"] == 0
    assert first["conversation_title"] == "hello there, what is a monad"

    second = send(client, "and why?", cid)
    assert second["conversation_id"] == cid and second["turn"] == 2 and second["context_turns"] == 1

    conv = client.get(f"/api/conversations/{cid}").json()
    assert [m["turn"] for m in conv["messages"]] == [1, 2]
    assert conv["messages"][0]["response"].startswith("local answer")


def test_ollama_gets_history_as_messages(client, fake_ollama):
    cid = send(client, "first question")["conversation_id"]
    send(client, "follow up", cid)
    answer_call = [c for c in fake_ollama.chat_calls if "format" not in c][-1]
    roles = [m["role"] for m in answer_call["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert answer_call["messages"][0]["content"] == "first question"
    assert answer_call["messages"][-1]["content"] == "follow up"


def test_claude_gets_history_as_transcript(client):
    cid = send(client, "first question")["conversation_id"]  # answered locally
    data = send(client, "now go deeper", cid, model="claude-sonnet")
    assert "history=1" in data["response"]  # the local turn went along in the system prompt
    assert data["context_turns"] == 1


def test_classifier_sees_previous_exchange(client, fake_ollama):
    cid = send(client, "C5 R5 design a consensus protocol")["conversation_id"]
    send(client, "why?", cid)
    clf = [c for c in fake_ollama.chat_calls if "format" in c][-1]
    content = clf["messages"][-1]["content"]
    assert "Earlier in the conversation" in content and "consensus protocol" in content
    # The fake classifier reads C5 R5 from that context, so the follow-up stays on Opus.
    assert client.get(f"/api/conversations/{cid}").json()["messages"][-1]["model_key"] == "claude-opus"


def test_failed_turns_stay_out_of_history(client, fake_ollama):
    cid = send(client, "first")["conversation_id"]
    bad = send(client, "please FAIL", cid, model="claude-opus")
    assert bad["status"] == "error" and bad["turn"] == 2
    ok = send(client, "third", cid)
    assert ok["turn"] == 3 and ok["context_turns"] == 1


def test_unknown_chat_is_404(client):
    assert client.post("/api/prompt", json={"prompt": "x", "conversation_id": 999}).status_code == 404
    assert client.get("/api/conversations/999").status_code == 404


def test_trim_history_keeps_newest():
    history = [("a" * 10, "b" * 10), ("c" * 10, "d" * 10), ("e" * 10, "f" * 10)]
    kept, dropped = trim_history(history, 45)
    assert kept == history[1:] and dropped == 1
    huge = [("q" * 50, "z" * 5000)]
    kept, dropped = trim_history(huge, 1000)
    assert dropped == 0 and len(kept[0][0]) + len(kept[0][1]) <= 1001


def test_history_budget_applies(cfg, fake_ollama):
    cfg.chat.history_chars["ollama"] = 30
    r = Router(cfg, OllamaClient(cfg.ollama), ClaudeCLI(cfg.claude, cfg.claude_workdir))
    history = [("one" * 5, "uno" * 5), ("two" * 5, "dos" * 5)]
    comp = r.run_model("qwen-moe", "three", None, None, history)
    assert comp.extra["context_turns"] == 1 and comp.extra["context_dropped"] == 1


def test_list_search_rename_delete(client):
    a = send(client, "how do I tune Postgres autovacuum")["conversation_id"]
    b = send(client, "best way to learn Rust")["conversation_id"]
    send(client, "what about lifetimes", b)

    listing = client.get("/api/conversations").json()
    assert [c["id"] for c in listing["items"]] == [b, a]  # most recently active first
    assert listing["items"][0]["turns"] == 2 and listing["items"][0]["last_model"] == "qwen-moe"

    assert [c["id"] for c in client.get("/api/conversations?q=postgres").json()["items"]] == [a]
    assert [c["id"] for c in client.get("/api/conversations?q=lifetimes").json()["items"]] == [b]  # matches a turn
    assert client.get("/api/conversations?q=100%").json()["total"] == 0  # % is literal

    renamed = client.patch(f"/api/conversations/{a}", json={"title": "  Postgres   tuning "}).json()
    assert renamed["title"] == "Postgres tuning"

    assert client.delete(f"/api/conversations/{b}").status_code == 204
    assert client.get("/api/requests").json()["total"] == 1  # its turns went with it


def test_ratings(client):
    msg = send(client, "rate me")
    rated = client.put(f"/api/requests/{msg['id']}/rating", json={"rating": -1}).json()
    assert rated["rating"] == -1
    assert client.put(f"/api/requests/{msg['id']}/rating", json={"rating": 0}).json()["rating"] is None
    assert client.put(f"/api/requests/{msg['id']}/rating", json={"rating": 5}).status_code == 400
    client.put(f"/api/requests/{msg['id']}/rating", json={"rating": 1})
    by = {m["model_key"]: m for m in client.get("/api/stats/summary").json()["by_model"]}
    assert by["qwen-moe"]["rated_good"] == 1


def test_export(client):
    cid = send(client, "first question")["conversation_id"]
    second = send(client, "HARD follow up", cid)
    client.put(f"/api/requests/{second['id']}/rating", json={"rating": 1})

    r = client.get("/api/export.jsonl")
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    convs = [json.loads(line) for line in r.text.splitlines()]
    assert len(convs) == 1 and [t["turn"] for t in convs[0]["turns"]] == [1, 2]

    turns = [json.loads(line) for line in client.get("/api/export.jsonl?format=turns").text.splitlines()]
    assert turns[1]["previous_prompt"] == "first question" and turns[1]["previous_model"] == "qwen-moe"
    assert turns[1]["rating"] == 1 and turns[1]["model_key"] == "claude-sonnet"
    assert client.get("/api/export.jsonl?format=csv").status_code == 400


def test_old_requests_become_chats(tmp_path):
    import sqlite3

    from switchboard.db import SCHEMA, connect, init_db

    added = ("reasoning INTEGER", "effort TEXT", "effort_source TEXT", "think INTEGER",
             "conversation_id INTEGER", "turn INTEGER", "context_turns INTEGER", "rating INTEGER")
    skip = ("CREATE TABLE IF NOT EXISTS conversations", "CREATE INDEX IF NOT EXISTS idx_conversations",
            "CREATE INDEX IF NOT EXISTS idx_requests_conversation")
    lines, in_conv = [], False
    for line in SCHEMA.splitlines():
        if line.startswith("CREATE TABLE IF NOT EXISTS conversations"):
            in_conv = True
        if in_conv:
            in_conv = not line.startswith(");")
            continue
        if line.strip().startswith(added) or line.startswith(skip):
            continue
        lines.append(line)
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path)
    old.executescript("\n".join(lines))
    for p in ("first old prompt", "second old prompt"):
        old.execute("INSERT INTO requests (created_at, prompt, status, route_mode) VALUES ('2026-10-30T10:00:00+00:00', ?, 'ok', 'auto')", (p,))
    old.commit()
    old.close()

    init_db(path)
    init_db(path)  # running it again changes nothing
    with connect() as conn:
        convs = conn.execute("SELECT id, title, created_at FROM conversations ORDER BY id").fetchall()
        assert [c["title"] for c in convs] == ["first old prompt", "second old prompt"]
        assert convs[0]["created_at"] == "2026-10-30T10:00:00+00:00"
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE conversation_id IS NULL OR turn != 1").fetchone()[0] == 0
        # Deleting a migrated chat still cascades to its request.
        conn.execute("DELETE FROM conversations WHERE id = ?", (convs[0]["id"],))
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_make_title():
    from switchboard.db import make_title

    assert make_title("  short   one ") == "short one"
    long = make_title("Design a distributed event pipeline with exactly-once delivery across three regions please")
    assert long.endswith("…") and len(long) <= 71 and not long[:-1].endswith(" ")
