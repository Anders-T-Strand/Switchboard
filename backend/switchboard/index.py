"""Vector index of chat turns in Qdrant.

SQLite stays the record. Every answered turn is embedded through Ollama and stored in Qdrant as one
point with two named vectors:

  prompt  the user's message alone. Used to find similar past prompts for the router.
  turn    the message and the answer together. Used for chat search and memory.

The point id is the request id, so re-indexing overwrites in place. `requests.indexed_model` records
which embedding model a turn was indexed with. Changing the model re-indexes everything, and a
missing or resized collection is rebuilt from SQLite.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .config import Config
from .db import connect, rows
from .providers import OllamaClient, ProviderError

log = logging.getLogger("switchboard.index")

PROMPT_INSTRUCTION = "Given a user's prompt, retrieve earlier prompts that ask for the same kind of thing"
SEARCH_INSTRUCTION = "Given a search query, retrieve earlier chat turns about it"
MEMORY_INSTRUCTION = "Given a new message, retrieve earlier chat turns with information that helps answer it"
VECTORS = ("prompt", "turn")


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class TurnIndex:
    def __init__(self, cfg: Config, ollama: OllamaClient, client=None):
        self.cfg = cfg
        self.icfg = cfg.index
        self.ollama = ollama
        self._client = client
        self._lock = threading.RLock()
        self.wake = threading.Event()  # set after a turn is saved so the indexer runs right away
        self.state: dict = {"ok": None, "reason": None, "dim": None, "checked_at": None}
        self._probed: tuple[str, int] | None = None  # (embedding model, vector size) already measured

    # Plumbing ------------------------------------------------------------------

    @property
    def collection(self) -> str:
        return self.icfg.qdrant.collection

    @property
    def model(self) -> str:
        return self.icfg.embedding.model

    def ready(self) -> bool:
        """True once the indexer has reached Qdrant and Ollama successfully."""
        return self.state["ok"] is True and self.state["dim"] is not None

    def client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            q = self.icfg.qdrant
            if q.path:
                self._client = QdrantClient(path=str(self.cfg.qdrant_path))
            elif q.url == ":memory:":
                self._client = QdrantClient(":memory:")
            else:
                self._client = QdrantClient(url=q.url, api_key=q.api_key, timeout=int(q.timeout_s))
        return self._client

    def where(self) -> str:
        q = self.icfg.qdrant
        return f"embedded at {self.cfg.qdrant_path}" if q.path else (q.url or "")

    def _ok(self) -> None:
        self.state.update(ok=True, reason=None, checked_at=datetime.now(timezone.utc).isoformat())

    def _fail(self, e: Exception) -> None:
        reason = str(e) or e.__class__.__name__
        if "Connection refused" in reason or "ConnectError" in reason or "connect" in reason.lower():
            reason = f"Qdrant isn't reachable at {self.where()}. Is it running?"
        if self.state["reason"] != reason:
            log.warning("vector index unavailable: %s", reason)
        self.state.update(ok=False, reason=reason[:300], checked_at=datetime.now(timezone.utc).isoformat())

    def _embed(self, texts: list[str]) -> list[list[float]]:
        e = self.icfg.embedding
        return self.ollama.embed(e.model, texts, timeout=e.timeout_s, keep_alive=e.keep_alive)

    def _query_text(self, text: str, instruction: str) -> str:
        text = text[: self.icfg.embedding.max_chars]
        return f"Instruct: {instruction}\nQuery: {text}" if self.icfg.embedding.instructions else text

    def _reset_sqlite(self) -> None:
        with connect() as conn:
            conn.execute("UPDATE requests SET indexed_model = NULL WHERE indexed_model IS NOT NULL")

    def _ensure_collection(self) -> int:
        """Make sure the collection exists with the embedding model's vector size. Returns the size."""
        from qdrant_client import models

        if self._probed and self._probed[0] == self.model:
            dim = self._probed[1]
        else:
            dim = len(self._embed(["dimension probe"])[0])
            self._probed = (self.model, dim)
        c = self.client()
        if c.collection_exists(self.collection):
            vectors = c.get_collection(self.collection).config.params.vectors
            sizes = {k: v.size for k, v in vectors.items()} if isinstance(vectors, dict) else {}
            if sizes == {name: dim for name in VECTORS}:
                self.state["dim"] = dim
                return dim
            log.info("embedding size changed to %s, rebuilding the collection", dim)
            c.delete_collection(self.collection)
        c.create_collection(
            self.collection,
            vectors_config={name: models.VectorParams(size=dim, distance=models.Distance.COSINE) for name in VECTORS},
        )
        q = self.icfg.qdrant
        if not (q.path or q.url == ":memory:"):  # payload indexes only exist on a Qdrant server
            for field, schema in (("conversation_id", models.PayloadSchemaType.INTEGER),
                                  ("model_key", models.PayloadSchemaType.KEYWORD)):
                try:
                    c.create_payload_index(self.collection, field_name=field, field_schema=schema)
                except Exception:
                    pass
        self._reset_sqlite()  # the collection was empty, so everything needs indexing again
        self.state["dim"] = dim
        return dim

    # Indexing ------------------------------------------------------------------

    def index_pending(self) -> int:
        """Embed and upsert one batch of turns that aren't indexed with the current model.
        Returns how many were indexed. Never raises: problems end up in self.state."""
        if not self.icfg.enabled:
            return 0
        from qdrant_client import models

        with self._lock:
            try:
                self._ensure_collection()
                with connect() as conn:
                    batch = rows(conn.execute(
                        """SELECT r.id, r.conversation_id, r.turn, r.created_at, r.prompt, r.response,
                                  r.model_key, r.provider, r.effort, r.task_type, r.complexity, r.reasoning
                           FROM requests r
                           WHERE r.status = 'ok' AND r.response IS NOT NULL
                             AND (r.indexed_model IS NULL OR r.indexed_model != ?)
                           ORDER BY r.id LIMIT ?""",
                        (self.model, self.icfg.batch_size),
                    ))
                if not batch:
                    self._ok()
                    return 0
                n = self.icfg.embedding.max_chars
                texts = [b["prompt"][:n] for b in batch] + [f"{b['prompt']}\n\n{b['response']}"[:n] for b in batch]
                vectors = self._embed(texts)
                points = [
                    models.PointStruct(
                        id=b["id"],
                        vector={"prompt": vectors[i], "turn": vectors[len(batch) + i]},
                        payload={
                            "conversation_id": b["conversation_id"], "turn": b["turn"],
                            "created_at": b["created_at"], "model_key": b["model_key"],
                            "provider": b["provider"], "effort": b["effort"], "task_type": b["task_type"],
                            "complexity": b["complexity"], "reasoning": b["reasoning"],
                            "prompt": _clip(b["prompt"], 300), "embed_model": self.model,
                        },
                    )
                    for i, b in enumerate(batch)
                ]
                self.client().upsert(self.collection, points=points, wait=True)
                with connect() as conn:
                    conn.executemany("UPDATE requests SET indexed_model = ? WHERE id = ?",
                                     [(self.model, b["id"]) for b in batch])
                self._ok()
                return len(batch)
            except (ProviderError, Exception) as e:  # noqa: B014 - any failure just pauses indexing
                self._fail(e)
                return 0

    def index_all(self) -> int:
        total = 0
        while (n := self.index_pending()) > 0:
            total += n
        return total

    def rebuild(self) -> None:
        """Drop the collection and index everything again on the next run."""
        with self._lock:
            try:
                if self.client().collection_exists(self.collection):
                    self.client().delete_collection(self.collection)
            except Exception as e:
                self._fail(e)
            self._reset_sqlite()
            self.state["dim"] = None
            self._probed = None
        self.wake.set()

    def delete_conversation(self, cid: int) -> None:
        if not self.ready():
            return  # leftover points are skipped at query time because their rows are gone
        from qdrant_client import models

        try:
            self.client().delete(
                self.collection,
                points_selector=models.FilterSelector(filter=models.Filter(must=[
                    models.FieldCondition(key="conversation_id", match=models.MatchValue(value=cid)),
                ])),
            )
        except Exception as e:
            self._fail(e)

    # Queries -------------------------------------------------------------------

    def _query(self, text: str, instruction: str, using: str, limit: int,
               exclude_conversation: int | None = None, threshold: float | None = None) -> list[tuple[int, float]]:
        if not self.ready():
            return []
        from qdrant_client import models

        flt = None
        if exclude_conversation is not None:
            flt = models.Filter(must_not=[
                models.FieldCondition(key="conversation_id", match=models.MatchValue(value=exclude_conversation)),
            ])
        try:
            vector = self._embed([self._query_text(text, instruction)])[0]
            res = self.client().query_points(
                self.collection, query=vector, using=using, limit=limit,
                query_filter=flt, score_threshold=threshold, with_payload=False,
            )
        except Exception as e:
            self._fail(e)
            return []
        return [(int(p.id), float(p.score)) for p in res.points]

    @staticmethod
    def _rows_by_id(ids: list[int], columns: str) -> dict[int, dict]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with connect() as conn:
            found = rows(conn.execute(
                f"""SELECT r.id, {columns}, c.title FROM requests r
                    LEFT JOIN conversations c ON c.id = r.conversation_id
                    WHERE r.id IN ({marks}) AND r.status = 'ok'""",
                ids,
            ))
        return {r["id"]: r for r in found}

    def neighbors(self, prompt: str, limit: int) -> list[dict]:
        """Most similar past prompts, with the model that answered and the current rating."""
        hits = self._query(prompt, PROMPT_INSTRUCTION, "prompt", limit)
        found = self._rows_by_id([i for i, _ in hits],
                                 "r.conversation_id, r.turn, r.prompt, r.model_key, r.effort, r.rating, r.created_at")
        out = []
        for rid, score in hits:
            r = found.get(rid)
            if r:
                out.append({
                    "request_id": rid, "score": round(score, 4), "conversation_id": r["conversation_id"],
                    "turn": r["turn"], "title": r["title"], "prompt": _clip(r["prompt"], 160),
                    "model_key": r["model_key"], "effort": r["effort"], "rating": r["rating"],
                    "created_at": r["created_at"],
                })
        return out

    def search_conversations(self, query: str, limit: int = 60) -> list[tuple[int, float]]:
        """Chats ranked by how well their best turn matches the query's meaning."""
        hits = self._query(query, SEARCH_INSTRUCTION, "turn", limit, threshold=self.icfg.search_min_score)
        found = self._rows_by_id([i for i, _ in hits], "r.conversation_id")
        best: dict[int, float] = {}
        for rid, score in hits:
            r = found.get(rid)
            if r and r["conversation_id"] is not None and score > best.get(r["conversation_id"], -1):
                best[r["conversation_id"]] = score
        return sorted(best.items(), key=lambda kv: -kv[1])

    def memory(self, message: str, exclude_conversation: int | None) -> list[dict]:
        """Turns from other chats that look relevant to a new message, best first."""
        m = self.cfg.chat.memory
        hits = self._query(message, MEMORY_INSTRUCTION, "turn", m.max_items,
                           exclude_conversation=exclude_conversation, threshold=m.min_score)
        found = self._rows_by_id([i for i, _ in hits], "r.conversation_id, r.turn, r.prompt, r.response, r.created_at")
        return [found[rid] | {"score": round(score, 4)} for rid, score in hits if rid in found]

    # Status --------------------------------------------------------------------

    def status(self) -> dict:
        with connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE status = 'ok' AND response IS NOT NULL"
            ).fetchone()[0]
            indexed = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE status = 'ok' AND response IS NOT NULL AND indexed_model = ?",
                (self.model,),
            ).fetchone()[0]
        return {
            "enabled": self.icfg.enabled,
            "ok": self.state["ok"],
            "reason": self.state["reason"],
            "checked_at": self.state["checked_at"],
            "where": self.where(),
            "collection": self.collection,
            "embed_model": self.model,
            "dim": self.state["dim"],
            "turns": total,
            "indexed": indexed,
            "pending": total - indexed,
        }


def build_memory_block(items: list[dict], max_chars: int) -> str | None:
    """System prompt text carrying excerpts from other chats."""
    if not items:
        return None
    per_item = max(400, max_chars // len(items))
    parts = [
        "Below are excerpts from the user's earlier, separate chats. They were picked automatically "
        "because they look related to the new message. Use them only if they help, say so when you rely "
        "on one, and don't treat them as part of this conversation.",
        "<memory>",
    ]
    for it in items:
        date = (it.get("created_at") or "")[:10]
        user = _clip(it["prompt"], min(600, per_item // 3))
        reply = _clip(it["response"], max(200, per_item - len(user)))
        title = (it.get("title") or "").replace('"', "'")
        parts.append(f'<excerpt chat="{title}" date="{date}">\nUser: {user}\nAssistant: {reply}\n</excerpt>')
    parts.append("</memory>")
    return "\n".join(parts)
