import json
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from switchboard.main import create_app


@pytest.fixture
def client(cfg, fake_ollama):
    app = create_app(cfg, background_sync=False)
    with TestClient(app) as c:
        yield c


def test_prompt_routes_locally(client):
    r = client.post("/api/prompt", json={"prompt": "hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["model_key"] == "qwen-moe" and data["provider"] == "ollama"
    assert data["response"] == "local answer from qwen3.6:35b-a3b"
    assert data["routing"]["classification"]["complexity"] == 1
    assert data["cost_usd"] == 0
    assert data["avoided_usd"] == pytest.approx((42 * 2 + 7 * 10) / 1_000_000)


def test_hard_prompt_goes_to_claude(client):
    data = client.post("/api/prompt", json={"prompt": "HARD bug"}).json()
    assert data["model_key"] == "claude-sonnet"
    assert data["provider_model"] == "claude-sonnet-5-5"
    assert data["cost_usd"] == pytest.approx(0.0123)  # reported by the CLI
    assert data["cache_read_tokens"] == 100


def test_fallback_is_recorded(client, fake_ollama):
    fake_ollama.loaded = []
    fake_ollama.down = True  # classifier and local model both unavailable
    data = client.post("/api/prompt", json={"prompt": "hi"}).json()
    assert data["status"] == "ok"
    assert data["routing"]["classification"]["source"] == "heuristic"
    assert data["model_key"] == "claude-sonnet"
    assert data["fell_back"] == 1
    assert "fell back" in data["route_note"]


def test_total_failure_is_logged_as_error(client, fake_ollama):
    fake_ollama.down = True
    data = client.post("/api/prompt", json={"prompt": "please FAIL", "model": "claude-opus"}).json()
    assert data["status"] == "error" and "boom" in data["error"]
    listing = client.get("/api/requests?status=error").json()
    assert listing["total"] == 1


def test_forced_unknown_model_is_400(client):
    r = client.post("/api/prompt", json={"prompt": "x", "model": "gpt-9"})
    assert r.status_code == 400 and "unknown model" in r.json()["detail"]


def test_preview_does_not_store(client):
    r = client.post("/api/route/preview", json={"prompt": "EXPERT"})
    assert r.json()["chosen"] == "claude-opus"
    assert client.get("/api/requests").json()["total"] == 0


def test_tasks_crud_and_aggregates(client):
    t = client.post("/api/tasks", json={"title": "Write README"}).json()
    assert t["status"] == "todo" and t["requests"] == 0
    client.post("/api/prompt", json={"prompt": "hello", "task_id": t["id"]})
    client.post("/api/prompt", json={"prompt": "HARD", "task_id": t["id"]})
    t = client.patch(f"/api/tasks/{t['id']}", json={"status": "doing"}).json()
    assert t["status"] == "doing" and t["requests"] == 2 and t["tokens"] > 0
    assert client.patch(f"/api/tasks/{t['id']}", json={"status": "nope"}).status_code == 400
    assert client.post("/api/prompt", json={"prompt": "x", "task_id": 999}).status_code == 400
    filtered = client.get(f"/api/requests?task_id={t['id']}").json()
    assert filtered["total"] == 2 and filtered["items"][0]["task_title"] == "Write README"
    assert client.delete(f"/api/tasks/{t['id']}").status_code == 204
    assert client.get("/api/tasks").json() == []
    # Requests survive their task being deleted.
    assert client.get("/api/requests").json()["total"] == 2


def test_stats(client, cfg):
    client.post("/api/prompt", json={"prompt": "hello"})
    client.post("/api/prompt", json={"prompt": "HARD"})
    client.post("/api/prompt", json={"prompt": "EXPERT"})

    proj = cfg.claude_logs_root / "projects" / "p"
    proj.mkdir(parents=True)
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    (proj / "s.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": now, "sessionId": "s9", "cwd": "/x", "requestId": "r",
        "message": {"id": "m", "model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 1000}},
    }) + "\n", encoding="utf-8")
    assert client.post("/api/claude-code/sync").json()["entries"] == 1

    s = client.get("/api/stats/summary?days=7").json()
    assert s["requests"]["total"] == 3
    assert s["requests"]["local"] == 1 and s["requests"]["claude"] == 2
    assert s["requests"]["local_share"] == pytest.approx(1 / 3)
    assert s["complexity"] == [1, 0, 0, 1, 1]
    assert s["reasoning"] == [1, 0, 0, 1, 1]
    assert {m["model_key"] for m in s["by_model"]} == {"qwen-moe", "claude-sonnet", "claude-opus"}
    cc = s["claude_code"]["interactive"]
    assert cc["sessions"] == 1 and cc["cost_usd"] == pytest.approx(0.024)
    assert s["claude_code"]["by_model"][0]["model"] == "opus"

    ts = client.get("/api/stats/timeseries?days=3").json()
    assert len(ts["dates"]) == 3 and len(ts["router_tokens"]) == 3
    today = ts["router_requests"][-1]
    assert today["local"] == 1 and today["claude"] == 2
    assert ts["cc_cost"][-1]["opus"] == pytest.approx(0.024)


def test_status_and_config(client):
    s = client.get("/api/status").json()
    assert s["ollama"]["reachable"] and s["ollama"]["version"] == "0.99.0"
    assert s["claude"]["found"] and s["claude"]["version"].startswith("9.9.9")
    models = {m["key"]: m for m in s["models"]}
    assert models["qwen-moe"]["loaded"] is True and models["qwen-dense"]["loaded"] is False
    assert models["qwen-dense"]["available"] is True
    c = client.get("/api/config").json()
    assert [r["name"] for r in c["routing"]["rules"]] == ["expert", "hard", "deep-reasoning", "default"]
    assert c["efforts"] == ["low", "medium", "high", "xhigh", "max"]


def test_effort_is_recorded(client):
    data = client.post("/api/prompt", json={"prompt": "C5 R1 polish this toast"}).json()
    assert data["model_key"] == "claude-opus"
    assert (data["effort"], data["effort_source"], data["reasoning"]) == ("low", "reasoning", 1)
    assert "effort=low" in data["response"]

    data = client.post("/api/prompt", json={"prompt": "C2 R1 hi"}).json()
    assert data["provider"] == "ollama" and data["think"] == 0

    data = client.post("/api/prompt", json={"prompt": "anything", "model": "claude-sonnet", "effort": "max"}).json()
    assert (data["effort"], data["effort_source"]) == ("max", "forced")

    assert client.post("/api/prompt", json={"prompt": "x", "effort": "turbo"}).status_code == 400

    row = client.get("/api/requests").json()["items"][0]
    assert row["effort"] == "max" and row["reasoning"] is None  # both forced, classifier skipped

    s = client.get("/api/stats/summary?days=7").json()
    by = {m["model_key"]: m for m in s["by_model"]}
    assert by["claude-opus"]["efforts"] == {"low": 1}
    assert by["qwen-moe"]["think_known"] == 1 and by["qwen-moe"]["thinking"] == 0


def test_preview_with_effort(client):
    p = client.post("/api/route/preview", json={"prompt": "C4 R5"}).json()
    assert p["chosen"] == "claude-sonnet" and p["effort"] == "xhigh"


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    from switchboard.db import SCHEMA, connect, init_db

    # The first release's schema: today's minus the columns added since.
    added = ("reasoning INTEGER", "effort TEXT", "effort_source TEXT", "think INTEGER")
    v1 = "\n".join(line for line in SCHEMA.splitlines() if not line.strip().startswith(added))
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(v1)
    assert "effort" not in {r[1] for r in old.execute("PRAGMA table_info(requests)")}
    old.execute("INSERT INTO requests (created_at, prompt, status, route_mode) VALUES ('x', 'old', 'ok', 'auto')")
    old.commit()
    old.close()
    init_db(path)
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
        assert {"reasoning", "effort", "effort_source", "think"} <= cols
        assert conn.execute("SELECT prompt FROM requests").fetchone()[0] == "old"
