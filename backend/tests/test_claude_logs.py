import json

from switchboard import claude_logs
from switchboard.db import connect


def entry(mid, rid, model="claude-sonnet-5-5", out=10, cwd="/home/me/proj", ts="2026-10-31T12:00:00.000Z", session="s1"):
    return {
        "type": "assistant", "timestamp": ts, "sessionId": session, "cwd": cwd, "requestId": rid,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": 100, "output_tokens": out,
            "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200,
        }},
    }


def write_lines(path, objs, trailing_partial: str = ""):
    with open(path, "a", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")
        f.write(trailing_partial)


def usage_rows():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM cc_usage ORDER BY uid")]


def test_missing_logs_dir(cfg):
    result = claude_logs.sync(cfg)
    assert result["ok"] is False and "not found" in result["reason"]


def test_sync_dedupes_and_reads_incrementally(cfg):
    proj = cfg.claude_logs_root / "projects" / "-home-me-proj"
    proj.mkdir(parents=True)
    log = proj / "abc.jsonl"
    write_lines(log, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        entry("m1", "r1", out=5),
        entry("m1", "r1", out=12),  # same message streamed again with a larger count
        entry("m2", "r2", model="<synthetic>"),
        entry("m3", "r3", model="claude-opus-5-5", cwd=str(cfg.claude_workdir)),
    ], trailing_partial='{"type": "assistant", "trunc')

    first = claude_logs.sync(cfg)
    assert first["ok"] and first["files"] == 1
    rows = usage_rows()
    assert [r["uid"] for r in rows] == ["m1:r1", "m3:r3"]
    m1 = rows[0]
    assert m1["output_tokens"] == 12
    assert m1["source"] == "interactive"
    assert rows[1]["source"] == "router"
    # 100 * 2 + 12 * 10 + 1000 * 0.1 + 200 * 2.5 = 920 per million
    assert abs(m1["cost_usd"] - 920 / 1_000_000) < 1e-12

    # Finish the partial line plus a new entry. Only new bytes get parsed.
    with open(log, "a", encoding="utf-8") as f:
        f.write('"}\n')
    write_lines(log, [entry("m4", "r4", ts="2026-10-31T13:00:00Z")])
    claude_logs.sync(cfg)
    assert [r["uid"] for r in usage_rows()] == ["m1:r1", "m3:r3", "m4:r4"]

    # Nothing new: nothing changes.
    again = claude_logs.sync(cfg)
    assert again["entries"] == 0


def test_rewritten_file_starts_over(cfg):
    proj = cfg.claude_logs_root / "projects" / "p"
    proj.mkdir(parents=True)
    log = proj / "x.jsonl"
    write_lines(log, [entry("a", "1"), entry("b", "2")])
    claude_logs.sync(cfg)
    log.write_text(json.dumps(entry("c", "3")) + "\n", encoding="utf-8")
    claude_logs.sync(cfg)
    assert {r["uid"] for r in usage_rows()} == {"a:1", "b:2", "c:3"}
