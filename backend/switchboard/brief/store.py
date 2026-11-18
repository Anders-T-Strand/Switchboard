"""SQLite access for the morning brief: a row per build in brief_runs and a row per item in brief_items."""

from __future__ import annotations

import json
from dataclasses import asdict

from ..db import connect, rows
from ..router import clip
from .base import Found, Usage

STATUSES = ("open", "done", "dismissed")
OWNERS = ("you", "model")

# What a source can say about an item. Sources read text nobody here wrote, so every field is cut
# to a sane length before it is stored.
_LIMITS = {"title": 300, "context": 200, "summary": 400, "category": 40, "reason": 300}


class ItemNotFound(LookupError):
    pass


# Runs ---------------------------------------------------------------------------


def start_run(day: str, started_by: str, ts: str) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO brief_runs (day, started_at, status, started_by) VALUES (?, ?, 'running', ?)",
            (day, ts, started_by),
        ).lastrowid


def finish_run(run_id: int, status: str, sources: list[dict], usage: Usage, ts: str,
               error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE brief_runs SET finished_at = ?, status = ?, sources = ?, model_key = ?,
                      input_tokens = ?, output_tokens = ?, model_ms = ?, error = ?
               WHERE id = ?""",
            (ts, status, json.dumps(sources), usage.model_key, usage.input_tokens, usage.output_tokens,
             usage.model_ms, error, run_id),
        )


def _run(row) -> dict | None:
    if row is None:
        return None
    run = dict(row)
    try:
        run["sources"] = json.loads(run["sources"]) if run["sources"] else []
    except ValueError:
        run["sources"] = []
    return run


def get_run(run_id: int) -> dict | None:
    with connect() as conn:
        return _run(conn.execute("SELECT * FROM brief_runs WHERE id = ?", (run_id,)).fetchone())


def runs_since(ts: str) -> list[dict]:
    """Runs started at or after `ts`, oldest first."""
    with connect() as conn:
        return [_run(r) for r in conn.execute("SELECT * FROM brief_runs WHERE started_at >= ? ORDER BY id", (ts,))]


def latest_run() -> dict | None:
    with connect() as conn:
        return _run(conn.execute("SELECT * FROM brief_runs ORDER BY id DESC LIMIT 1").fetchone())


def interrupt_running(ts: str) -> int:
    """Close runs that a stopped process left open. Returns how many there were."""
    with connect() as conn:
        return conn.execute(
            "UPDATE brief_runs SET status = 'error', finished_at = ?, error = ? WHERE status = 'running'",
            (ts, "Switchboard stopped while this was being built"),
        ).rowcount


# Items --------------------------------------------------------------------------


def save_found(source: str, found: list[Found], ts: str) -> None:
    """Add what a source found, or update the items it already knew about.

    An item keeps its status from one build to the next, with two exceptions. News that arrived
    after it was closed, such as a new message in a mail thread, opens it again. And a source that
    knows an item is handled closes it. Something that was already handled before it was ever
    listed is not stored at all, so a reply sent last week doesn't turn up as closed today."""
    with connect() as conn:
        for f in found:
            values = asdict(f)
            done, done_at = values.pop("done"), values.pop("done_at") or ts
            for field, limit in _LIMITS.items():
                if values[field]:
                    values[field] = clip(str(values[field]), limit)
            values["title"] = values["title"] or "(no title)"
            if values["owner"] not in OWNERS:
                values["owner"] = None
            if not (values["link"] or "").startswith("https://"):
                values["link"] = None

            known = conn.execute(
                "SELECT id, status, status_at, received_at FROM brief_items WHERE source = ? AND external_id = ?",
                (source, f.external_id),
            ).fetchone()
            if known is None:
                if done:
                    continue
                values["owner"] = values["owner"] or "you"
                values.update(source=source, status="open", status_at=None, first_seen_at=ts, last_seen_at=ts)
                cols = ", ".join(values)
                marks = ", ".join(f":{k}" for k in values)
                conn.execute(f"INSERT INTO brief_items ({cols}) VALUES ({marks})", values)
                continue

            status, status_at = known["status"], known["status_at"]
            if done:
                if status == "open":
                    status, status_at = "done", done_at
            elif status != "open" and f.received_at and status_at and f.received_at > status_at:
                status, status_at = "open", ts
            values.pop("external_id")
            for judged in ("summary", "category", "owner", "reason"):
                if values[judged] is None:
                    values.pop(judged)  # not judged this time, so what an earlier build said stays
            values.update(status=status, status_at=status_at, last_seen_at=ts)
            if f.received_at and known["received_at"] and f.received_at > known["received_at"]:
                values.update(draft=None, draft_model=None, drafted_at=None)  # it answered an older message
            sets = ", ".join(f"{k} = :{k}" for k in values)
            conn.execute(f"UPDATE brief_items SET {sets} WHERE id = :id", {**values, "id": known["id"]})


# What the local model said about each message ------------------------------------------


def judgments(message_ids: list[str]) -> dict[str, dict]:
    """Earlier judgments of these messages, by message id."""
    if not message_ids:
        return {}
    found: dict[str, dict] = {}
    with connect() as conn:
        for i in range(0, len(message_ids), 500):  # SQLite caps how many values one query can take
            chunk = message_ids[i:i + 500]
            for r in conn.execute(
                f"SELECT * FROM brief_triage WHERE message_id IN ({','.join('?' * len(chunk))})", chunk
            ):
                found[r["message_id"]] = dict(r)
    return found


def save_judgment(message_id: str, judgment: dict, model_key: str, ts: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO brief_triage (message_id, action, owner, category, summary, reason, due_at, model_key,
                                         judged_at)
               VALUES (:message_id, :action, :owner, :category, :summary, :reason, :due_at, :model_key, :judged_at)
               ON CONFLICT(message_id) DO UPDATE SET action = excluded.action, owner = excluded.owner,
                 category = excluded.category, summary = excluded.summary, reason = excluded.reason,
                 due_at = excluded.due_at, model_key = excluded.model_key, judged_at = excluded.judged_at""",
            {**judgment, "message_id": message_id, "model_key": model_key, "judged_at": ts},
        )


def prune_judgments(before: str) -> int:
    """Forget judgments made before `before`. Their messages are long out of the lookback window."""
    with connect() as conn:
        return conn.execute("DELETE FROM brief_triage WHERE judged_at < ?", (before,)).rowcount


def mark_collected(source: str, ts: str) -> None:
    """Note that a source delivered at `ts`. What it listed then is what is current for it."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO brief_sources (name, collected_at) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET collected_at = excluded.collected_at""",
            (source, ts),
        )


_ITEM = """SELECT i.*, t.title AS task_title FROM brief_items i LEFT JOIN tasks t ON t.id = i.task_id"""

# An open item is current while its source still lists it. One the source left out the last time it
# delivered has passed, such as coursework well past its due date, and is no longer shown. It stays
# in the table, so it comes back if the source lists it again, and it keeps a status the user gave it.
# A source that has never delivered hides nothing.
_SEEN = "LEFT JOIN brief_sources s ON s.name = i.source"
_CURRENT = "i.status = 'open' AND (s.collected_at IS NULL OR i.last_seen_at >= s.collected_at)"


def list_items(closed_since: str) -> list[dict]:
    """What is open and current, then what was closed since `closed_since`, so today's progress
    shows. Open items come soonest due first, and the ones without a date by the latest news."""
    with connect() as conn:
        return rows(conn.execute(
            f"""{_ITEM} {_SEEN} WHERE ({_CURRENT}) OR (i.status != 'open' AND i.status_at >= ?)
                ORDER BY i.status != 'open', i.due_at IS NULL, i.due_at, i.received_at DESC, i.id""",
            (closed_since,),
        ))


def get_item(item_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(f"{_ITEM} WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        raise ItemNotFound(f"brief item {item_id} not found")
    return dict(row)


def set_status(item_id: int, status: str, ts: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with connect() as conn:
        changed = conn.execute(
            "UPDATE brief_items SET status = ?, status_at = ? WHERE id = ?", (status, ts, item_id)
        ).rowcount
    if changed == 0:
        raise ItemNotFound(f"brief item {item_id} not found")
    return get_item(item_id)


def save_draft(item_id: int, text: str, model_key: str, ts: str) -> None:
    with connect() as conn:
        changed = conn.execute(
            "UPDATE brief_items SET draft = ?, draft_model = ?, drafted_at = ? WHERE id = ?",
            (text, model_key, ts, item_id),
        ).rowcount
    if changed == 0:
        raise ItemNotFound(f"brief item {item_id} not found")


def link_task(item_id: int, task_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE brief_items SET task_id = ? WHERE id = ?", (task_id, item_id))


def counts(closed_since: str) -> dict:
    """How many items are open, by source, and how many were closed since `closed_since`. Numbers
    only, so it is safe to print where the items themselves are not."""
    with connect() as conn:
        by_source = {r["source"]: r["n"] for r in conn.execute(
            f"""SELECT i.source, COUNT(*) AS n FROM brief_items i {_SEEN}
                WHERE {_CURRENT} GROUP BY i.source ORDER BY i.source"""
        )}
        closed = conn.execute(
            "SELECT COUNT(*) FROM brief_items WHERE status != 'open' AND status_at >= ?", (closed_since,)
        ).fetchone()[0]
    return {"open": sum(by_source.values()), "open_by_source": by_source, "closed": closed}
