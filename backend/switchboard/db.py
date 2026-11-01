"""SQLite storage. One short-lived connection per operation keeps things thread-safe."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'done')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  prompt TEXT NOT NULL,
  system_prompt TEXT,
  response TEXT,
  status TEXT NOT NULL,              -- ok | error
  error TEXT,
  route_mode TEXT NOT NULL,          -- auto | forced
  task_type TEXT,
  complexity INTEGER,
  classifier_source TEXT,            -- model | heuristic
  classifier_model TEXT,
  classifier_reason TEXT,
  classifier_ms INTEGER,
  rule TEXT,
  routed_model TEXT,                 -- model key the router picked
  model_key TEXT,                    -- model key that actually answered
  provider TEXT,
  provider_model TEXT,
  route_note TEXT,
  fell_back INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER,
  load_ms INTEGER,
  cost_usd REAL NOT NULL DEFAULT 0,      -- API-equivalent cost of what ran
  avoided_usd REAL NOT NULL DEFAULT 0    -- reference-model cost avoided by running locally
);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);
CREATE INDEX IF NOT EXISTS idx_requests_task ON requests(task_id);

CREATE TABLE IF NOT EXISTS cc_usage (
  uid TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  session_id TEXT,
  project TEXT,
  source TEXT NOT NULL,              -- interactive | router
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cc_ts ON cc_usage(ts);

CREATE TABLE IF NOT EXISTS cc_files (
  path TEXT PRIMARY KEY,
  offset INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT
);
"""

_db_path: Path | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_db(path: Path) -> None:
    global _db_path
    _db_path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        raise RuntimeError("init_db() has not been called")
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]
