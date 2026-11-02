"""Reads Claude Code session logs (~/.claude/projects/**/*.jsonl) for usage stats.

Each assistant message in those files carries the model and token usage. Files are
read incrementally: the byte offset of the last complete line is stored per file, so
each sync only parses what was appended since the previous one.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .db import connect, now_iso
from .pricing import cost_usd, find_price

_lock = threading.Lock()
last_sync: dict = {"at": None, "ok": None, "files": 0, "entries": 0, "reason": None, "root": None}

UPSERT = """
INSERT INTO cc_usage (uid, ts, session_id, project, source, model,
                      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)
VALUES (:uid, :ts, :session_id, :project, :source, :model,
        :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens, :cost_usd)
ON CONFLICT(uid) DO UPDATE SET
  input_tokens = MAX(input_tokens, excluded.input_tokens),
  output_tokens = MAX(output_tokens, excluded.output_tokens),
  cache_read_tokens = MAX(cache_read_tokens, excluded.cache_read_tokens),
  cache_write_tokens = MAX(cache_write_tokens, excluded.cache_write_tokens),
  cost_usd = MAX(cost_usd, excluded.cost_usd)
"""


def norm_path(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def to_utc_iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_entry(obj: dict, router_dir: str) -> dict | None:
    """Turn one JSONL record into a usage row, or None if it isn't a billable assistant message."""
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    model = msg.get("model")
    if not usage or not model or model == "<synthetic>":
        return None
    ts = to_utc_iso(obj.get("timestamp"))
    if not ts:
        return None
    # Streaming writes one line per content block with the same message id and
    # request id, so that pair is the dedupe key.
    mid, rid = msg.get("id"), obj.get("requestId")
    uid = f"{mid}:{rid}" if (mid or rid) else obj.get("uuid")
    if not uid:
        return None
    cwd = obj.get("cwd") or ""
    return {
        "uid": uid,
        "ts": ts,
        "session_id": obj.get("sessionId"),
        "project": cwd,
        "source": "router" if cwd and norm_path(cwd) == router_dir else "interactive",
        "model": model,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def sync(cfg: Config) -> dict:
    """Read new lines from every session log. Safe to call from several threads."""
    with _lock:
        result = _sync(cfg)
        last_sync.update(result, at=now_iso())
        return dict(last_sync)


def _sync(cfg: Config) -> dict:
    root = cfg.claude_logs_root / "projects"
    if not root.is_dir():
        return {"ok": False, "files": 0, "entries": 0, "root": str(root),
                "reason": f"{root} not found. Has Claude Code been used on this machine?"}

    router_dir = norm_path(cfg.claude_workdir)
    with connect() as conn:
        known = {r["path"]: r["offset"] for r in conn.execute("SELECT path, offset FROM cc_files")}

    files = entries_total = 0
    for path in sorted(root.rglob("*.jsonl")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files += 1
        key = str(path)
        offset = known.get(key, 0)
        if size < offset:  # file was rewritten, start over (rows are deduped)
            offset = 0
        if size == offset:
            continue
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            continue
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:  # only a partial line so far
            continue

        rows = []
        for raw in chunk[: last_nl + 1].splitlines():
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            row = parse_entry(obj, router_dir)
            if row:
                row["cost_usd"] = cost_usd(
                    find_price(cfg, row["model"]),
                    row["input_tokens"], row["output_tokens"],
                    row["cache_read_tokens"], row["cache_write_tokens"],
                )
                rows.append(row)

        with connect() as conn:
            if rows:
                conn.executemany(UPSERT, rows)
            conn.execute(
                """INSERT INTO cc_files (path, offset, size, synced_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET offset = excluded.offset,
                   size = excluded.size, synced_at = excluded.synced_at""",
                (key, offset + last_nl + 1, size, now_iso()),
            )
        entries_total += len(rows)

    return {"ok": True, "files": files, "entries": entries_total, "root": str(root), "reason": None}
