"""FastAPI app: JSON API under /api plus the built dashboard at /."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import claude_logs, stats
from .config import REPO_ROOT, Config, get_config
from .db import init_db
from .service import NotFound, Service

log = logging.getLogger("switchboard")


class PromptIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=500_000)
    model: str = "auto"
    effort: str = "auto"
    task_id: int | None = None
    system_prompt: str | None = None
    conversation_id: int | None = None  # continue this chat. Leave out to start a new one.
    memory: bool | None = None  # turn memory from other chats on or off for this chat


class PreviewIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=500_000)
    model: str = "auto"
    effort: str = "auto"
    conversation_id: int | None = None


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    memory: bool | None = None


class RatingIn(BaseModel):
    rating: int  # 1 good, -1 bad, 0 clears


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: str = ""
    status: str = "todo"


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = None
    status: str | None = None


def _sync_loop(cfg: Config, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            claude_logs.sync(cfg)
        except Exception:  # keep the loop alive no matter what a log file contains
            log.exception("Claude Code log sync failed")
        stop.wait(max(cfg.claude.sync_interval_s, 5))


def _index_loop(index, interval: int, stop: threading.Event) -> None:
    """Index new turns as they're saved, and catch up after Qdrant or Ollama was unavailable."""
    while not stop.is_set():
        try:
            index.index_all()
        except Exception:
            log.exception("vector indexing failed")
        index.wake.wait(max(interval, 5))
        index.wake.clear()


def create_app(cfg: Config | None = None, *, background_sync: bool = True) -> FastAPI:
    cfg = cfg or get_config()
    init_db(cfg.db_path)
    svc = Service(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        threads = []
        if background_sync:
            threads.append(threading.Thread(target=_sync_loop, args=(cfg, stop), daemon=True, name="cc-sync"))
            if svc.index is not None:
                threads.append(threading.Thread(target=_index_loop, args=(svc.index, cfg.index.interval_s, stop),
                                                daemon=True, name="indexer"))
            for t in threads:
                t.start()
        yield
        stop.set()
        if svc.index is not None:
            svc.index.wake.set()
        for t in threads:
            t.join(timeout=5)

    app = FastAPI(title="Switchboard", version="0.1.0", lifespan=lifespan)
    app.state.service = svc

    def not_found(e: NotFound):
        raise HTTPException(status_code=404, detail=str(e))

    # Prompts ---------------------------------------------------------------------

    @app.post("/api/prompt")
    def run_prompt(body: PromptIn):
        try:
            return svc.run_prompt(body.prompt, body.model, body.task_id, body.system_prompt,
                                  body.effort, body.conversation_id, body.memory)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound as e:
            not_found(e)

    @app.post("/api/route/preview")
    def preview(body: PreviewIn):
        try:
            return svc.preview(body.prompt, body.model, body.effort, body.conversation_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound as e:
            not_found(e)

    # Conversations ---------------------------------------------------------------

    @app.get("/api/conversations")
    def list_conversations(
        q: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        return svc.list_conversations(q, limit, offset)

    @app.get("/api/conversations/{cid}")
    def get_conversation(cid: int):
        try:
            return svc.get_conversation(cid)
        except NotFound as e:
            not_found(e)

    @app.patch("/api/conversations/{cid}")
    def update_conversation(cid: int, body: ConversationPatch):
        try:
            return svc.update_conversation(cid, body.title, body.memory)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound as e:
            not_found(e)

    @app.delete("/api/conversations/{cid}", status_code=204)
    def delete_conversation(cid: int):
        try:
            svc.delete_conversation(cid)
        except NotFound as e:
            not_found(e)

    @app.put("/api/requests/{rec_id}/rating")
    def rate(rec_id: int, body: RatingIn):
        try:
            return svc.rate(rec_id, body.rating)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound as e:
            not_found(e)

    @app.post("/api/index/rebuild")
    def rebuild_index():
        try:
            return svc.rebuild_index()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/export.jsonl")
    def export(format: str = "conversations"):
        if format not in ("conversations", "turns"):
            raise HTTPException(status_code=400, detail="format must be conversations or turns")
        name = f"switchboard-{format}-{date.today().isoformat()}.jsonl"
        return StreamingResponse(
            svc.export(format),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # Requests --------------------------------------------------------------------

    @app.get("/api/requests")
    def list_requests(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        task_id: int | None = None,
        model: str | None = None,
        status: str | None = None,
    ):
        return svc.list_requests(limit, offset, task_id, model, status)

    @app.get("/api/requests/{rec_id}")
    def get_request(rec_id: int):
        try:
            return svc.get_request(rec_id)
        except NotFound as e:
            not_found(e)

    # Tasks -----------------------------------------------------------------------

    @app.get("/api/tasks")
    def list_tasks():
        return svc.list_tasks()

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskIn):
        try:
            return svc.create_task(body.title, body.notes, body.status)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: int, body: TaskPatch):
        try:
            return svc.update_task(task_id, **body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except NotFound as e:
            not_found(e)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: int):
        try:
            svc.delete_task(task_id)
        except NotFound as e:
            not_found(e)

    # Stats and status ------------------------------------------------------------

    @app.get("/api/stats/summary")
    def stats_summary(days: int = Query(7, ge=1, le=365)):
        return stats.summary(cfg, days)

    @app.get("/api/stats/timeseries")
    def stats_timeseries(days: int = Query(14, ge=1, le=365)):
        return stats.timeseries(cfg, days)

    @app.get("/api/status")
    def status():
        return svc.status()

    @app.get("/api/config")
    def config_view():
        return svc.config_view()

    @app.post("/api/claude-code/sync")
    def sync_now():
        return claude_logs.sync(cfg)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    dist = REPO_ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    else:
        @app.get("/")
        def no_frontend():
            return {
                "message": "API is running, but the dashboard hasn't been built. "
                           "Run `npm install` and `npm run build` in frontend/, then restart.",
            }

    return app
