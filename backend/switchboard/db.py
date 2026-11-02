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

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
  turn INTEGER,                      -- position in the conversation, from 1
  context_turns INTEGER,             -- earlier turns sent along with this prompt
  rating INTEGER,                    -- 1 good, -1 bad, NULL not rated
  prompt TEXT NOT NULL,
  system_prompt TEXT,
  response TEXT,
  status TEXT NOT NULL,              -- ok | error
  error TEXT,
  route_mode TEXT NOT NULL,          -- auto | forced
  task_type TEXT,
  complexity INTEGER,
  reasoning INTEGER,
  effort TEXT,                       -- low | medium | high | xhigh | max, NULL = Claude Code's own setting
  effort_source TEXT,                -- forced | rule | reasoning
  think INTEGER,                     -- local models: thinking on (1) or off (0)
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
CREATE INDEX IF NOT EXISTS idx_requests_conversation ON requests(conversation_id, turn);

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

# Columns added after the first release. init_db adds any that an existing database lacks.
ADDED_COLUMNS = {
    "requests": [
        ("reasoning", "INTEGER"),
        ("effort", "TEXT"),
        ("effort_source", "TEXT"),
        ("think", "INTEGER"),
        ("conversation_id", "INTEGER REFERENCES conversations(id) ON DELETE CASCADE"),
        ("turn", "INTEGER"),
        ("context_turns", "INTEGER"),
        ("rating", "INTEGER"),
    ],
}


def make_title(prompt: str, limit: int = 70) -> str:
    """A conversation title from its first prompt: first line, cut at a word boundary."""
    line = " ".join(prompt.strip().split())
    if len(line) <= limit:
        return line or "New chat"
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut if len(cut) > limit // 2 else line[:limit]).rstrip(",.:;-") + "…"

_db_path: Path | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_db(path: Path) -> None:
    global _db_path
    _db_path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        for table, columns in ADDED_COLUMNS.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if existing:  # table exists from an older version
                for name, sql_type in columns:
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        conn.executescript(SCHEMA)
        _adopt_loose_requests(conn)


def _adopt_loose_requests(conn: sqlite3.Connection) -> None:
    """Requests from before conversations existed each become a one-turn conversation,
    so they show up in the chat list and can be continued."""
    loose = conn.execute(
        "SELECT id, created_at, prompt FROM requests WHERE conversation_id IS NULL ORDER BY id"
    ).fetchall()
    for r in loose:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (make_title(r["prompt"]), r["created_at"], r["created_at"]),
        )
        conn.execute("UPDATE requests SET conversation_id = ?, turn = 1 WHERE id = ?", (cur.lastrowid, r["id"]))


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
