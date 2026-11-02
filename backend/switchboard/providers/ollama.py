"""Ollama HTTP client (https://github.com/ollama/ollama/blob/main/docs/api.md)."""

from __future__ import annotations

import re
import time

import httpx

from ..config import OllamaCfg
from .base import Completion, ProviderError

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def normalize_name(name: str) -> str:
    """Ollama reports untagged models as 'name:latest'."""
    return name if ":" in name else f"{name}:latest"


class OllamaClient:
    def __init__(self, cfg: OllamaCfg):
        self.cfg = cfg

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self.cfg.base_url.rstrip("/"), timeout=timeout)

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        think: bool | None = None,
        fmt: dict | str | None = None,
        options: dict | None = None,
        timeout: float | None = None,
    ) -> Completion:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
        }
        if think is not None:
            payload["think"] = think
        if fmt is not None:
            payload["format"] = fmt
        if options:
            payload["options"] = options

        t0 = time.perf_counter()
        resp = self._post_chat(payload, timeout or self.cfg.timeout_s)
        if resp.status_code == 400 and "think" in payload and "think" in resp.text.lower():
            # Model doesn't support the think flag. Retry without it.
            payload.pop("think")
            resp = self._post_chat(payload, timeout or self.cfg.timeout_s)
        if resp.status_code != 200:
            raise ProviderError(f"Ollama returned HTTP {resp.status_code} for {model}: {resp.text[:300]}")

        data = resp.json()
        if data.get("error"):
            raise ProviderError(f"Ollama error for {model}: {data['error']}")
        text = _THINK_BLOCK.sub("", (data.get("message") or {}).get("content", "")).strip()
        load_ns = data.get("load_duration") or 0
        return Completion(
            text=text,
            provider_model=data.get("model", model),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            load_ms=int(load_ns / 1_000_000) if load_ns else None,
        )

    def embed(self, model: str, inputs: list[str], *, timeout: float = 60,
              keep_alive: str | None = None) -> list[list[float]]:
        """Embeddings for a batch of texts (POST /api/embed)."""
        payload = {"model": model, "input": inputs, "truncate": True,
                   "keep_alive": keep_alive or self.cfg.keep_alive}
        try:
            with self._client(timeout) as client:
                r = client.post("/api/embed", json=payload)
        except httpx.TimeoutException as e:
            raise ProviderError(f"Ollama embedding timed out after {timeout:.0f}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama unreachable at {self.cfg.base_url}: {e}") from e
        if r.status_code == 404:
            raise ProviderError(f"embedding model {model} isn't pulled. Run: ollama pull {model}")
        if r.status_code != 200:
            raise ProviderError(f"Ollama embed returned HTTP {r.status_code}: {r.text[:200]}")
        vectors = r.json().get("embeddings") or []
        if len(vectors) != len(inputs):
            raise ProviderError(f"Ollama returned {len(vectors)} embeddings for {len(inputs)} inputs")
        return vectors

    def _post_chat(self, payload: dict, timeout: float) -> httpx.Response:
        try:
            with self._client(timeout) as client:
                return client.post("/api/chat", json=payload)
        except httpx.TimeoutException as e:
            raise ProviderError(f"Ollama timed out after {timeout:.0f}s ({payload['model']})") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama unreachable at {self.cfg.base_url}: {e}") from e

    def _get(self, path: str) -> dict | None:
        try:
            with self._client(5) as client:
                r = client.get(path)
                return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def version(self) -> str | None:
        data = self._get("/api/version")
        return data.get("version") if data else None

    def loaded_models(self) -> list[str] | None:
        """Models currently in memory, or None when Ollama can't be reached."""
        data = self._get("/api/ps")
        if data is None:
            return None
        return [normalize_name(m.get("name") or m.get("model", "")) for m in data.get("models", [])]

    def installed_models(self) -> list[str] | None:
        data = self._get("/api/tags")
        if data is None:
            return None
        return [normalize_name(m.get("name") or m.get("model", "")) for m in data.get("models", [])]
