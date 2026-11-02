"""Glue between the API, the router and the database."""

from __future__ import annotations

import json
from typing import Iterator

from .config import Config
from .db import connect, make_title, now_iso, rows
from .index import TurnIndex, build_memory_block
from .pricing import cost_usd, find_price, reference_cost
from .providers import ClaudeCLI, OllamaClient, ProviderError
from .router import Router, Turn

TASK_STATUSES = ("todo", "doing", "done")


class NotFound(Exception):
    pass


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ollama = OllamaClient(cfg.ollama)
        self.claude = ClaudeCLI(cfg.claude, cfg.claude_workdir)
        self.index = TurnIndex(cfg, self.ollama) if cfg.index.enabled else None
        self.router = Router(cfg, self.ollama, self.claude, self.index)

    # Prompts -------------------------------------------------------------------

    def _plan(self, prompt: str, model: str, effort: str, context: Turn | None = None):
        try:
            return self.router.plan(prompt, model, effort, context)
        except KeyError as e:
            raise ValueError(str(e).strip("'\"")) from None

    def _history(self, conversation_id: int) -> list[Turn]:
        """Answered turns of a conversation, oldest first. Failed turns are left out."""
        with connect() as conn:
            if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                raise NotFound(f"chat {conversation_id} not found")
            found = conn.execute(
                """SELECT prompt, response FROM requests
                   WHERE conversation_id = ? AND status = 'ok' AND response IS NOT NULL
                   ORDER BY turn, id""",
                (conversation_id,),
            ).fetchall()
        return [(r["prompt"], r["response"]) for r in found]

    def preview(self, prompt: str, model: str = "auto", effort: str = "auto",
                conversation_id: int | None = None) -> dict:
        history = self._history(conversation_id) if conversation_id is not None else []
        return self._plan(prompt, model, effort, history[-1] if history else None).to_dict(self.cfg)

    def run_prompt(self, prompt: str, model: str = "auto", task_id: int | None = None,
                   system_prompt: str | None = None, effort: str = "auto",
                   conversation_id: int | None = None, memory: bool | None = None) -> dict:
        """Route and answer one message. Without a conversation_id a new chat is started,
        so every prompt can be picked up again later."""
        if task_id is not None:
            with connect() as conn:
                if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                    raise ValueError(f"task {task_id} does not exist")
        history = self._history(conversation_id) if conversation_id is not None else []
        plan = self._plan(prompt, model, effort, history[-1] if history else None)
        if conversation_id is None:
            conversation_id = self._create_conversation(prompt, bool(memory))
        elif memory is not None:
            self._set_memory(conversation_id, memory)

        # Memory: relevant turns from other chats go along in the system prompt.
        memory_items: list[dict] = []
        if self.index is not None and self._memory_on(conversation_id):
            # A short follow-up says little on its own, so search with the previous message too.
            query = f"{history[-1][0]}\n{prompt}" if history else prompt
            memory_items = self.index.memory(query, exclude_conversation=conversation_id)
        extra_context = build_memory_block(memory_items, self.cfg.chat.memory.max_chars)

        c = plan.classification
        rec: dict = {
            "created_at": now_iso(),
            "task_id": task_id,
            "conversation_id": conversation_id,
            "context_turns": None,
            "neighbors": json.dumps(plan.neighbors) if plan.neighbors else None,
            "memory_ids": json.dumps([
                {"request_id": m["id"], "conversation_id": m["conversation_id"], "turn": m["turn"],
                 "title": m["title"], "score": m["score"]}
                for m in memory_items
            ]) if memory_items else None,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "route_mode": plan.mode,
            "task_type": c.task_type if c else None,
            "complexity": c.complexity if c else None,
            "reasoning": c.reasoning if c else None,
            "effort": plan.effort,
            "effort_source": plan.effort_source,
            "think": None if plan.think is None else int(plan.think),
            "classifier_source": c.source if c else None,
            "classifier_model": c.model if c else None,
            "classifier_reason": c.reason if c else None,
            "classifier_ms": c.ms if c else None,
            "rule": plan.rule,
            "routed_model": plan.routed,
            "response": None,
            "status": "ok",
            "error": None,
            "model_key": None,
            "provider": None,
            "provider_model": None,
            "fell_back": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "latency_ms": None,
            "load_ms": None,
            "cost_usd": 0.0,
            "avoided_usd": 0.0,
        }
        notes = list(plan.notes)

        try:
            comp, used, errors = self.router.execute(plan, prompt, system_prompt, history, extra_context)
        except ProviderError as e:
            rec["status"] = "error"
            rec["error"] = str(e)
        else:
            m = self.cfg.models[used]
            if used != plan.chosen:
                rec["fell_back"] = 1
                notes.append(f"fell back to {self.cfg.label(used)} after: {' | '.join(errors)}")
            dropped = comp.extra.get("context_dropped") or 0
            if dropped:
                notes.append(f"left out the oldest {dropped} {'turn' if dropped == 1 else 'turns'} to fit the context")
            rec.update(
                response=comp.text,
                model_key=used,
                provider=m.provider,
                provider_model=comp.provider_model,
                input_tokens=comp.input_tokens,
                output_tokens=comp.output_tokens,
                cache_read_tokens=comp.cache_read_tokens,
                cache_write_tokens=comp.cache_write_tokens,
                latency_ms=comp.latency_ms,
                load_ms=comp.load_ms,
                effort=comp.extra.get("effort"),
                think=None if comp.extra.get("think") is None else int(comp.extra["think"]),
                context_turns=comp.extra.get("context_turns"),
            )
            if m.provider == "claude_cli":
                if comp.cost_usd is not None:
                    rec["cost_usd"] = comp.cost_usd
                else:
                    price = find_price(self.cfg, comp.provider_model) or find_price(self.cfg, m.model)
                    rec["cost_usd"] = cost_usd(price, comp.input_tokens, comp.output_tokens,
                                               comp.cache_read_tokens, comp.cache_write_tokens)
            else:
                rec["avoided_usd"] = reference_cost(self.cfg, comp.input_tokens, comp.output_tokens)
        rec["route_note"] = "; ".join(notes) or None

        with connect() as conn:
            rec["turn"] = conn.execute(
                "SELECT COALESCE(MAX(turn), 0) + 1 FROM requests WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()[0]
            cols = ", ".join(rec)
            marks = ", ".join(f":{k}" for k in rec)
            rec_id = conn.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", rec).lastrowid
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (rec["created_at"], conversation_id))
        if self.index is not None and rec["status"] == "ok":
            self.index.wake.set()  # index the new turn now instead of on the next timer
        return self.get_request(rec_id) | {"routing": plan.to_dict(self.cfg)}

    def rate(self, rec_id: int, rating: int) -> dict:
        if rating not in (-1, 0, 1):
            raise ValueError("rating must be -1, 0 or 1")
        with connect() as conn:
            cur = conn.execute("UPDATE requests SET rating = ? WHERE id = ?", (rating or None, rec_id))
            if cur.rowcount == 0:
                raise NotFound(f"request {rec_id} not found")
        return self.get_request(rec_id)

    # Conversations -------------------------------------------------------------

    def _create_conversation(self, first_prompt: str, memory: bool = False) -> int:
        ts = now_iso()
        with connect() as conn:
            return conn.execute(
                "INSERT INTO conversations (title, memory, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (make_title(first_prompt), int(memory), ts, ts),
            ).lastrowid

    def _set_memory(self, cid: int, on: bool) -> None:
        with connect() as conn:
            if conn.execute("UPDATE conversations SET memory = ? WHERE id = ?", (int(on), cid)).rowcount == 0:
                raise NotFound(f"chat {cid} not found")

    def _memory_on(self, cid: int) -> bool:
        with connect() as conn:
            row = conn.execute("SELECT memory FROM conversations WHERE id = ?", (cid,)).fetchone()
        return bool(row and row["memory"])

    _SUMMARY = """SELECT c.id, c.title, c.memory, c.created_at, c.updated_at,
                    (SELECT COUNT(*) FROM requests r WHERE r.conversation_id = c.id) AS turns,
                    (SELECT r.model_key FROM requests r WHERE r.conversation_id = c.id
                       ORDER BY r.turn DESC, r.id DESC LIMIT 1) AS last_model,
                    (SELECT r.status FROM requests r WHERE r.conversation_id = c.id
                       ORDER BY r.turn DESC, r.id DESC LIMIT 1) AS last_status
                  FROM conversations c"""

    def list_conversations(self, q: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Newest first. With a query: chats that match by meaning (vector index) come first,
        then chats whose title or messages contain the words."""
        q = (q or "").strip()
        if not q:
            with connect() as conn:
                total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                items = rows(conn.execute(
                    f"{self._SUMMARY} ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?", (limit, offset)
                ))
            return {"total": total, "items": items, "search": None}

        by_meaning: list[int] = []
        searched_meaning = self.index is not None and self.index.ready()
        if searched_meaning:
            by_meaning = [cid for cid, _ in self.index.search_conversations(q)]
        like = _like(q)
        with connect() as conn:
            by_words = [r[0] for r in conn.execute(
                """SELECT c.id FROM conversations c
                   WHERE c.title LIKE ? ESCAPE '\\' OR EXISTS (
                     SELECT 1 FROM requests x WHERE x.conversation_id = c.id
                     AND (x.prompt LIKE ? ESCAPE '\\' OR x.response LIKE ? ESCAPE '\\'))
                   ORDER BY c.updated_at DESC, c.id DESC""",
                (like, like, like),
            )]
        meaning_set = set(by_meaning)
        ordered = by_meaning + [cid for cid in by_words if cid not in meaning_set]
        page = ordered[offset: offset + limit]
        found: dict[int, dict] = {}
        if page:
            with connect() as conn:
                for r in rows(conn.execute(
                    f"{self._SUMMARY} WHERE c.id IN ({','.join('?' * len(page))})", page
                )):
                    found[r["id"]] = r | {"match": "meaning" if r["id"] in meaning_set else "words"}
        return {
            "total": len(ordered),
            "items": [found[cid] for cid in page if cid in found],
            "search": "meaning and words" if searched_meaning else "words",
        }

    def get_conversation(self, cid: int) -> dict:
        with connect() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
            if not conv:
                raise NotFound(f"chat {cid} not found")
            messages = rows(conn.execute(
                """SELECT r.*, t.title AS task_title FROM requests r
                   LEFT JOIN tasks t ON t.id = r.task_id
                   WHERE r.conversation_id = ? ORDER BY r.turn, r.id""",
                (cid,),
            ))
        return dict(conv) | {"messages": [self._decorate(m) for m in messages]}

    def update_conversation(self, cid: int, title: str | None = None, memory: bool | None = None) -> dict:
        if title is not None:
            title = " ".join(title.split())
            if not title:
                raise ValueError("title can't be empty")
            with connect() as conn:
                if conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title[:200], cid)).rowcount == 0:
                    raise NotFound(f"chat {cid} not found")
        if memory is not None:
            self._set_memory(cid, memory)
        return self.get_conversation(cid)

    def rename_conversation(self, cid: int, title: str) -> dict:
        return self.update_conversation(cid, title=title)

    def delete_conversation(self, cid: int) -> None:
        """Deletes the chat and its turns, so they also leave the request log and stats."""
        with connect() as conn:
            if conn.execute("DELETE FROM conversations WHERE id = ?", (cid,)).rowcount == 0:
                raise NotFound(f"chat {cid} not found")
        if self.index is not None:
            self.index.delete_conversation(cid)

    # Export --------------------------------------------------------------------

    EXPORT_FIELDS = (
        "turn", "created_at", "prompt", "response", "status", "error", "task_type", "complexity",
        "reasoning", "classifier_source", "rule", "routed_model", "model_key", "provider",
        "provider_model", "effort", "effort_source", "think", "fell_back", "context_turns",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "latency_ms",
        "cost_usd", "rating",
    )

    def export(self, fmt: str = "conversations") -> Iterator[str]:
        """JSON Lines. "conversations" is one chat per line with its turns. "turns" is one turn per
        line with the routing decision, the outcome and the previous turn, for training a router."""
        if fmt not in ("conversations", "turns"):
            raise ValueError("format must be conversations or turns")
        with connect() as conn:
            convs = rows(conn.execute("SELECT * FROM conversations ORDER BY id"))
            turns = rows(conn.execute(
                f"SELECT conversation_id, {', '.join(self.EXPORT_FIELDS)} FROM requests "
                "WHERE conversation_id IS NOT NULL ORDER BY conversation_id, turn, id"
            ))
        by_conv: dict[int, list[dict]] = {}
        for t in turns:
            by_conv.setdefault(t["conversation_id"], []).append(t)

        for conv in convs:
            items = by_conv.get(conv["id"], [])
            if fmt == "conversations":
                msgs = [{k: v for k, v in t.items() if k != "conversation_id"} for t in items]
                yield json.dumps(conv | {"turns": msgs}, ensure_ascii=False) + "\n"
                continue
            prev_ok: dict | None = None
            for t in items:
                yield json.dumps(t | {
                    "conversation_title": conv["title"],
                    "previous_prompt": prev_ok["prompt"] if prev_ok else None,
                    "previous_model": prev_ok["model_key"] if prev_ok else None,
                }, ensure_ascii=False) + "\n"
                if t["status"] == "ok":
                    prev_ok = t

    # Requests ------------------------------------------------------------------

    def _decorate(self, r: dict) -> dict:
        r["model_label"] = self.cfg.label(r.get("model_key"))
        r["routed_label"] = self.cfg.label(r.get("routed_model"))
        for col, key in (("neighbors", "neighbors"), ("memory_ids", "memory")):
            if col in r:
                raw = r.pop(col)
                try:
                    r[key] = json.loads(raw) if raw else []
                except ValueError:
                    r[key] = []
        return r

    def list_requests(self, limit: int = 50, offset: int = 0, task_id: int | None = None,
                      model: str | None = None, status: str | None = None) -> dict:
        where, params = [], []
        if task_id is not None:
            where.append("r.task_id = ?")
            params.append(task_id)
        if model:
            where.append("r.model_key = ?")
            params.append(model)
        if status:
            where.append("r.status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM requests r {clause}", params).fetchone()[0]
            items = rows(conn.execute(
                f"""SELECT r.id, r.created_at, r.task_id, t.title AS task_title,
                           r.conversation_id, r.turn, c.title AS conversation_title, r.rating,
                           substr(r.prompt, 1, 200) AS prompt_preview, r.status, r.error, r.route_mode,
                           r.task_type, r.complexity, r.reasoning, r.effort, r.effort_source, r.think,
                           r.classifier_source, r.rule, r.routed_model,
                           r.model_key, r.provider, r.fell_back, r.route_note, r.input_tokens,
                           r.output_tokens, r.cache_read_tokens, r.cache_write_tokens,
                           r.latency_ms, r.cost_usd, r.avoided_usd
                    FROM requests r
                    LEFT JOIN tasks t ON t.id = r.task_id
                    LEFT JOIN conversations c ON c.id = r.conversation_id
                    {clause} ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ))
        return {"total": total, "items": [self._decorate(i) for i in items]}

    def get_request(self, rec_id: int) -> dict:
        with connect() as conn:
            row = conn.execute(
                """SELECT r.*, t.title AS task_title, c.title AS conversation_title FROM requests r
                   LEFT JOIN tasks t ON t.id = r.task_id
                   LEFT JOIN conversations c ON c.id = r.conversation_id
                   WHERE r.id = ?""",
                (rec_id,),
            ).fetchone()
        if not row:
            raise NotFound(f"request {rec_id} not found")
        return self._decorate(dict(row))

    # Tasks ---------------------------------------------------------------------

    def list_tasks(self) -> list[dict]:
        with connect() as conn:
            return rows(conn.execute(
                """SELECT t.*, COUNT(r.id) AS requests,
                          COALESCE(SUM(r.input_tokens + r.output_tokens), 0) AS tokens,
                          COALESCE(SUM(r.cost_usd), 0) AS cost_usd,
                          MAX(r.created_at) AS last_request_at
                   FROM tasks t LEFT JOIN requests r ON r.task_id = t.id
                   GROUP BY t.id
                   ORDER BY CASE t.status WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END,
                            t.updated_at DESC"""
            ))

    def get_task(self, task_id: int) -> dict:
        for t in self.list_tasks():
            if t["id"] == task_id:
                return t
        raise NotFound(f"task {task_id} not found")

    def create_task(self, title: str, notes: str = "", status: str = "todo") -> dict:
        if status not in TASK_STATUSES:
            raise ValueError(f"status must be one of {', '.join(TASK_STATUSES)}")
        ts = now_iso()
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, notes, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (title.strip(), notes, status, ts, ts),
            )
            task_id = cur.lastrowid
        return self.get_task(task_id)

    def update_task(self, task_id: int, **changes) -> dict:
        changes = {k: v for k, v in changes.items() if v is not None}
        if "status" in changes and changes["status"] not in TASK_STATUSES:
            raise ValueError(f"status must be one of {', '.join(TASK_STATUSES)}")
        if "title" in changes:
            changes["title"] = changes["title"].strip()
        if changes:
            changes["updated_at"] = now_iso()
            sets = ", ".join(f"{k} = :{k}" for k in changes)
            with connect() as conn:
                cur = conn.execute(f"UPDATE tasks SET {sets} WHERE id = :id", {**changes, "id": task_id})
                if cur.rowcount == 0:
                    raise NotFound(f"task {task_id} not found")
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> None:
        with connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id} not found")

    # Status --------------------------------------------------------------------

    def status(self) -> dict:
        from . import claude_logs  # local import keeps module load order simple

        ollama_version = self.ollama.version()
        loaded = self.ollama.loaded_models() if ollama_version else None
        installed = self.ollama.installed_models() if ollama_version else None
        claude_path = self.claude.resolve()
        claude_version = self.claude.version() if claude_path else None

        from .providers.ollama import normalize_name

        models = []
        for key, m in self.cfg.models.items():
            if m.provider == "ollama":
                name = normalize_name(m.model)
                available = installed is not None and name in installed
                is_loaded = loaded is not None and name in loaded
            else:
                available = claude_path is not None
                is_loaded = None
            models.append({
                "key": key, "label": m.label or key, "provider": m.provider, "model": m.model,
                "available": available, "loaded": is_loaded,
            })

        return {
            "ollama": {
                "reachable": ollama_version is not None,
                "base_url": self.cfg.ollama.base_url,
                "version": ollama_version,
                "loaded": loaded or [],
                "installed": installed or [],
            },
            "claude": {
                "found": claude_path is not None,
                "path": claude_path,
                "version": claude_version,
                "use_subscription": self.cfg.claude.use_subscription,
                "logs_root": str(self.cfg.claude_logs_root),
            },
            "sync": dict(claude_logs.last_sync),
            "models": models,
            "index": self.index.status() if self.index is not None else {"enabled": False},
        }

    def rebuild_index(self) -> dict:
        if self.index is None:
            raise ValueError("the vector index is turned off in config.yaml (index.enabled)")
        self.index.rebuild()
        return self.index.status()

    def config_view(self) -> dict:
        from .config import EFFORTS

        return {
            "models": {k: m.model_dump() for k, m in self.cfg.models.items()},
            "routing": self.cfg.routing.model_dump(),
            "pricing": self.cfg.pricing.model_dump(),
            "efforts": list(EFFORTS),
            "index": self.cfg.index.model_dump(exclude={"qdrant": {"api_key"}}),
            "memory": self.cfg.chat.memory.model_dump(),
        }
