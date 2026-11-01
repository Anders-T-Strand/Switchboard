"""Glue between the API, the router and the database."""

from __future__ import annotations

from .config import Config
from .db import connect, now_iso, rows
from .pricing import cost_usd, find_price, reference_cost
from .providers import ClaudeCLI, OllamaClient, ProviderError
from .router import Router

TASK_STATUSES = ("todo", "doing", "done")


class NotFound(Exception):
    pass


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ollama = OllamaClient(cfg.ollama)
        self.claude = ClaudeCLI(cfg.claude, cfg.claude_workdir)
        self.router = Router(cfg, self.ollama, self.claude)

    # Prompts -------------------------------------------------------------------

    def preview(self, prompt: str, model: str = "auto") -> dict:
        try:
            plan = self.router.plan(prompt, model)
        except KeyError as e:
            raise ValueError(str(e).strip("'\"")) from None
        return plan.to_dict(self.cfg)

    def run_prompt(self, prompt: str, model: str = "auto", task_id: int | None = None,
                   system_prompt: str | None = None) -> dict:
        if task_id is not None:
            with connect() as conn:
                if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                    raise ValueError(f"task {task_id} does not exist")
        try:
            plan = self.router.plan(prompt, model)
        except KeyError as e:
            raise ValueError(str(e).strip("'\"")) from None

        c = plan.classification
        rec: dict = {
            "created_at": now_iso(),
            "task_id": task_id,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "route_mode": plan.mode,
            "task_type": c.task_type if c else None,
            "complexity": c.complexity if c else None,
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
            comp, used, errors = self.router.execute(plan, prompt, system_prompt)
        except ProviderError as e:
            rec["status"] = "error"
            rec["error"] = str(e)
        else:
            m = self.cfg.models[used]
            if used != plan.chosen:
                rec["fell_back"] = 1
                notes.append(f"fell back to {self.cfg.label(used)} after: {' | '.join(errors)}")
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

        cols = ", ".join(rec)
        marks = ", ".join(f":{k}" for k in rec)
        with connect() as conn:
            cur = conn.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", rec)
            rec_id = cur.lastrowid
        return self.get_request(rec_id) | {"routing": plan.to_dict(self.cfg)}

    # Requests ------------------------------------------------------------------

    def _decorate(self, r: dict) -> dict:
        r["model_label"] = self.cfg.label(r.get("model_key"))
        r["routed_label"] = self.cfg.label(r.get("routed_model"))
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
                           substr(r.prompt, 1, 200) AS prompt_preview, r.status, r.error, r.route_mode,
                           r.task_type, r.complexity, r.classifier_source, r.rule, r.routed_model,
                           r.model_key, r.provider, r.fell_back, r.route_note, r.input_tokens,
                           r.output_tokens, r.cache_read_tokens, r.cache_write_tokens,
                           r.latency_ms, r.cost_usd, r.avoided_usd
                    FROM requests r LEFT JOIN tasks t ON t.id = r.task_id
                    {clause} ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ))
        return {"total": total, "items": [self._decorate(i) for i in items]}

    def get_request(self, rec_id: int) -> dict:
        with connect() as conn:
            row = conn.execute(
                """SELECT r.*, t.title AS task_title FROM requests r
                   LEFT JOIN tasks t ON t.id = r.task_id WHERE r.id = ?""",
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
        }

    def config_view(self) -> dict:
        return {
            "models": {k: m.model_dump() for k, m in self.cfg.models.items()},
            "routing": self.cfg.routing.model_dump(),
            "pricing": self.cfg.pricing.model_dump(),
        }
