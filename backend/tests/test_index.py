"""Vector index: indexing, similar prompts, learning from ratings, search by meaning and memory.
Qdrant runs in local (in-process) mode, embeddings come from the fake Ollama."""

import pytest
from fastapi.testclient import TestClient

from switchboard.db import connect
from switchboard.main import create_app


@pytest.fixture
def app(cfg, fake_ollama):
    a = create_app(cfg, background_sync=False)
    with TestClient(a) as c:
        c.svc = a.state.service
        yield c


def send(client, prompt, chat=None, **kw):
    r = client.post("/api/prompt", json={"prompt": prompt, "conversation_id": chat, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def rate(client, rid, value):
    assert client.put(f"/api/requests/{rid}/rating", json={"rating": value}).status_code == 200


def test_indexes_answered_turns_only(app, fake_ollama):
    send(app, "tune postgres autovacuum settings")
    send(app, "please FAIL here", model="claude-opus")  # failed turn
    fake_ollama.down = True
    send(app, "C2 R1 something")  # falls back to Sonnet, still answered
    fake_ollama.down = False
    idx = app.svc.index
    assert idx.index_all() == 2
    st = app.get("/api/status").json()["index"]
    assert st["ok"] is True and st["indexed"] == 2 and st["pending"] == 0 and st["dim"] == 64
    assert idx.client().count(idx.collection).count == 2
    # Two texts per turn (prompt alone and prompt with answer), plus the size probe.
    assert sum(len(c["input"]) for c in fake_ollama.embed_calls) == 1 + 2 * 2


def test_changing_embedding_model_reindexes(app, cfg):
    send(app, "first prompt about rust lifetimes")
    idx = app.svc.index
    idx.index_all()
    cfg.index.embedding.model = "another-embed"
    assert app.get("/api/status").json()["index"]["pending"] == 1
    assert idx.index_all() == 1
    with connect() as conn:
        assert conn.execute("SELECT indexed_model FROM requests").fetchone()[0] == "another-embed"


def test_missing_collection_is_rebuilt(app):
    send(app, "a prompt about docker volumes")
    idx = app.svc.index
    idx.index_all()
    idx.client().delete_collection(idx.collection)
    assert idx.index_all() == 1  # recreated and refilled from SQLite


def test_unavailable_embedder_doesnt_break_chat(app, fake_ollama):
    fake_ollama.embed_down = True
    send(app, "hello there")
    assert app.svc.index.index_all() == 0
    st = app.get("/api/status").json()["index"]
    assert st["ok"] is False and "ollama pull fake-embed" in st["reason"]
    assert send(app, "still works")["status"] == "ok"


def test_similar_prompts_are_attached(app):
    first = send(app, "tune postgres autovacuum for a write heavy table")
    send(app, "bake sourdough bread at home")
    app.svc.index.index_all()
    data = send(app, "tune postgres autovacuum for a busy table")
    nbs = data["routing"]["neighbors"]
    assert nbs[0]["request_id"] == first["id"] and nbs[0]["score"] > 0.5
    assert nbs[0]["model_key"] == "qwen-moe" and "postgres" in nbs[0]["prompt"]
    # Stored on the request too, so the chat can show them later.
    assert app.get(f"/api/requests/{data['id']}").json()["neighbors"][0]["request_id"] == first["id"]


def test_bad_ratings_move_similar_prompts_up(app):
    for _ in range(2):
        r = send(app, "write a regex for iso dates with timezone offsets")
        rate(app, r["id"], -1)
    app.svc.index.index_all()
    data = send(app, "write a regex for iso dates with timezone offsets please")
    assert data["model_key"] == "claude-sonnet"
    learned = data["routing"]["learned"]
    assert learned["direction"] == "up" and learned["from"] == "qwen-moe"
    assert "moved up to Claude Sonnet" in data["route_note"]


def test_good_ratings_move_similar_prompts_down(app):
    for _ in range(2):
        r = send(app, "HARD explain python decorators with examples", model="qwen-moe")
        rate(app, r["id"], 1)
    app.svc.index.index_all()
    data = send(app, "HARD explain python decorators with examples again")
    assert data["routed_model"] == "claude-sonnet"  # the rule still picked Sonnet...
    assert data["model_key"] == "qwen-moe"         # ...but Qwen's good ratings won
    assert data["routing"]["learned"]["direction"] == "down"


def test_learning_needs_enough_similar_votes(app):
    r = send(app, "write a regex for iso dates")
    rate(app, r["id"], -1)  # one vote, min_votes is 2
    send(app, "completely unrelated bread recipe")
    app.svc.index.index_all()
    data = send(app, "write a regex for iso dates")
    assert data["model_key"] == "qwen-moe" and data["routing"]["learned"] is None


def test_learning_off_and_forced_model_ignore_ratings(app, cfg):
    for _ in range(2):
        rate(app, send(app, "summarize the french revolution briefly")["id"], -1)
    app.svc.index.index_all()
    assert send(app, "summarize the french revolution briefly", model="qwen-moe")["model_key"] == "qwen-moe"
    cfg.routing.learn.enabled = False
    assert send(app, "summarize the french revolution briefly")["model_key"] == "qwen-moe"


def test_search_by_meaning_then_words(app):
    a = send(app, "tune postgres autovacuum thresholds")["conversation_id"]
    b = send(app, "bake sourdough bread at home")["conversation_id"]
    c = send(app, "my notes mention autovacuum once")["conversation_id"]
    app.svc.index.index_all()
    res = app.get("/api/conversations?q=postgres autovacuum thresholds").json()
    assert res["search"] == "meaning and words"
    ids = [i["id"] for i in res["items"]]
    assert ids[0] == a and b not in ids
    assert res["items"][0]["match"] == "meaning"
    # Before the index is ready, search falls back to words only.
    app.svc.index.state["ok"] = None
    res = app.get("/api/conversations?q=autovacuum").json()
    assert res["search"] == "words" and set(i["id"] for i in res["items"]) == {a, c}


def test_memory_brings_in_other_chats(app, fake_ollama):
    send(app, "my homelab gpu is an rtx 5090 with 32gb vram")
    send(app, "favourite sourdough hydration ratio")
    app.svc.index.index_all()

    off = send(app, "which local models fit my homelab gpu vram")
    assert off["memory"] == []

    on = send(app, "which local models fit my homelab gpu vram", memory=True)
    assert [m["title"] for m in on["memory"]][0].startswith("my homelab gpu")
    system = [m for m in fake_ollama.chat_calls[-1]["messages"] if m["role"] == "system"][0]["content"]
    assert "<memory>" in system and "rtx 5090" in system and "sourdough" not in system

    cid = on["conversation_id"]
    conv = app.get(f"/api/conversations/{cid}").json()
    assert conv["memory"] == 1
    # Memory stays on for the chat, never pulls from the chat itself, and reaches Claude too.
    follow = send(app, "and for claude?", cid, model="claude-sonnet")
    assert "memory=1" in follow["response"]
    assert all(m["conversation_id"] != cid for m in follow["memory"])
    # It can be switched off again.
    assert app.patch(f"/api/conversations/{cid}", json={"memory": False}).json()["memory"] == 0


def test_deleting_a_chat_removes_its_points(app):
    keep = send(app, "keep this one about kubernetes")["conversation_id"]
    drop = send(app, "drop this one about terraform")["conversation_id"]
    idx = app.svc.index
    idx.index_all()
    assert app.delete(f"/api/conversations/{drop}").status_code == 204
    assert idx.client().count(idx.collection).count == 1
    assert [i["id"] for i in app.get("/api/conversations?q=terraform").json()["items"]] == []
    assert keep


def test_rebuild(app):
    send(app, "one")
    send(app, "two")
    idx = app.svc.index
    idx.index_all()
    st = app.post("/api/index/rebuild").json()
    assert st["indexed"] == 0 and st["pending"] == 2
    assert idx.index_all() == 2
