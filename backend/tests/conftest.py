"""Shared fixtures: a temp config, a fake Ollama (httpx MockTransport), a fake `claude` CLI and an
empty credential store for every test."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import textwrap
import zlib
from pathlib import Path

import httpx
import pytest

from switchboard import credentials
from switchboard.config import Config
from switchboard.db import init_db
from switchboard.providers.ollama import OllamaClient


class FakeKeyring:
    """Stands in for the keyring module. Like Windows Credential Manager, it refuses a value longer
    than 1,280 characters."""

    LIMIT = 1280

    def __init__(self):
        self.saved: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.saved.get((service, name))

    def set_password(self, service, name, value):
        if len(value) > self.LIMIT:
            raise OSError("The stub received bad data.")  # what Windows says, and nothing of the value
        self.saved[(service, name)] = value

    def delete_password(self, service, name):
        del self.saved[(service, name)]  # raises when there is nothing to delete, as keyring does


@pytest.fixture(autouse=True)
def vault(monkeypatch) -> FakeKeyring:
    """An empty credential store for every test, whether it asks for one or not. Without this, a
    test on a machine where a real feed link or sign-in is saved would find it and use it."""
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_store", lambda: fake)
    for name in list(os.environ):
        if name.startswith("SWITCHBOARD_") and name != "SWITCHBOARD_CONFIG":
            monkeypatch.delenv(name)  # a secret set in the environment wins over the store
    return fake


FAKE_CLAUDE = textwrap.dedent(
    """
    import json, os, sys
    args = sys.argv[1:]
    if "--version" in args:
        print("9.9.9 (Claude Code)")
        sys.exit(0)
    prompt = sys.stdin.read()
    model = args[args.index("--model") + 1]
    sp = ""
    if "--system-prompt-file" in args:
        with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as f:
            sp = f.read()
    if "FAIL" in prompt:
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}))
        sys.exit(1)
    # Report what reached the CLI. (Plain variables keep this valid on Python 3.11.)
    key_present = "ANTHROPIC_API_KEY" in os.environ
    effort = os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")
    automem_off = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    sources = args[args.index("--setting-sources") + 1] if "--setting-sources" in args else None
    history = sp.count('<turn role="user">')
    memory = sp.count("<excerpt ")
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": (f"claude[{model}] answer. key_present={key_present} system_prompt={bool(sp)} "
                   f"effort={effort} history={history} memory={memory} "
                   f"sources={sources} automem_off={automem_off} prompt={prompt!r}"),
        "session_id": "s1", "num_turns": 1, "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "output_tokens": 20,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50},
        "modelUsage": {
            "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 2, "costUSD": 0.0001},
            "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 20, "costUSD": 0.0122},
        },
    }))
    """
)


STOPWORDS = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "it", "my", "how", "do",
             "i", "what", "with", "this", "that", "be", "can", "you"}


def fake_embedding(text: str, dim: int = 64) -> list[float]:
    """Bag-of-words vector: texts that share words point the same way. Instructions are dropped."""
    if text.startswith("Instruct:") and "\nQuery: " in text:
        text = text.split("\nQuery: ", 1)[1]
    v = [0.0] * dim
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w not in STOPWORDS:
            v[zlib.crc32(w.encode()) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v] if any(v) else [1.0 / math.sqrt(dim)] * dim


class FakeOllama:
    """Classifier labels: "C<n> R<n>" in the prompt sets complexity and reasoning directly.
    Otherwise HARD -> code 4/4, EXPERT -> analysis 5/5, anything else -> chat 1/1.
    Follow-ups: WRONG in the new prompt reads as a correction, THANKS as an approval."""

    def __init__(self):
        self.loaded = ["qwen3.6:35b-a3b"]
        self.installed = ["qwen3.6:35b-a3b", "qwen3.8:27b"]
        self.down = False
        self.embed_down = False
        self.chat_calls: list[dict] = []
        self.embed_calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.99.0"})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n} for n in self.loaded]})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n} for n in self.installed]})
        if path == "/api/embed":
            if self.embed_down:
                return httpx.Response(404, json={"error": "model not found"})
            body = json.loads(request.content)
            self.embed_calls.append(body)
            return httpx.Response(200, json={"model": body["model"],
                                             "embeddings": [fake_embedding(t) for t in body["input"]]})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.chat_calls.append(body)
            user = body["messages"][-1]["content"]
            fmt = body.get("format")
            if isinstance(fmt, dict) and "action" in fmt.get("properties", {}):
                content = self.triage(user)
            elif "format" in body:
                m = re.search(r"C(\d) R(\d)", user)
                if m:
                    label = {"task_type": "code", "complexity": int(m[1]), "reasoning": int(m[2]), "reason": "set"}
                elif "EXPERT" in user:
                    label = {"task_type": "analysis", "complexity": 5, "reasoning": 5, "reason": "expert"}
                elif "HARD" in user:
                    label = {"task_type": "code", "complexity": 4, "reasoning": 4, "reason": "hard code"}
                else:
                    label = {"task_type": "chat", "complexity": 1, "reasoning": 1, "reason": "trivial"}
                new = user.split("Prompt to classify:", 1)[-1]
                label["follow_up"] = "correction" if "WRONG" in new else "approval" if "THANKS" in new else "neutral"
                content = json.dumps(label)
            else:
                content = f"<think>hmm</think>local answer from {body['model']}"
            return httpx.Response(200, json={
                "model": body["model"], "message": {"role": "assistant", "content": content},
                "prompt_eval_count": 42, "eval_count": 7, "load_duration": 2_000_000,
            })
        return httpx.Response(404, json={"error": "not found"})

    @staticmethod
    def triage(user: str) -> str:
        """Mail labels, read from markers in the email: [NOISE] nothing to do, [DO] a task, [DRAFT] a
        reply a model can draft, [JOBS] the jobs category, [DUE yyyy-mm-dd] a deadline, [LINKY] a
        summary with a link in it, [BADJSON] an answer that isn't JSON. Anything else is a reply only
        you can write."""
        email = user.split("<email>", 1)[-1]
        if "[BADJSON]" in email:
            return "Sorry, I can't sort that one."
        due = re.search(r"\[DUE (\d{4}-\d{2}-\d{2})\]", email)
        return json.dumps({
            "action": "none" if "[NOISE]" in email else "do" if "[DO]" in email else "reply",
            "owner": "model" if "[DRAFT]" in email else "you",
            "category": "jobs" if "[JOBS]" in email else "other",
            "summary": "Wants an answer, see https://phish.example/login" if "[LINKY]" in email else "Wants an answer.",
            "reason": "A test label.",
            "due": due.group(1) if due else "",
        })


@pytest.fixture
def fake_ollama(monkeypatch) -> FakeOllama:
    fake = FakeOllama()

    def _client(self, timeout):
        return httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(OllamaClient, "_client", _client)
    return fake


@pytest.fixture
def fake_claude(tmp_path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if sys.platform == "win32":
        launcher = bindir / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = bindir / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


@pytest.fixture
def cfg(tmp_path, fake_claude, monkeypatch) -> Config:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    raw = {
        "server": {"data_dir": str(tmp_path / "data")},
        "ollama": {"base_url": "http://ollama.test"},
        "claude": {"command": str(fake_claude), "logs_dir": str(tmp_path / "claude-home"),
                   "system_prompt": "be brief"},
        "chat": {"memory": {"min_score": 0.3}},
        "models": {
            "qwen-moe": {"provider": "ollama", "model": "qwen3.6:35b-a3b", "label": "Qwen MoE"},
            "qwen-dense": {"provider": "ollama", "model": "qwen3.8:27b", "label": "Qwen Dense"},
            "claude-sonnet": {"provider": "claude_cli", "model": "sonnet", "label": "Claude Sonnet"},
            "claude-opus": {"provider": "claude_cli", "model": "opus", "label": "Claude Opus"},
        },
        "routing": {
            "classifier": {"model": "qwen-moe"},
            "prefer_loaded_local": True,
            "fallback_order": ["claude-sonnet", "qwen-moe"],
            "learn": {"ladder": ["qwen-moe", "claude-sonnet", "claude-opus"], "min_similarity": 0.8, "min_votes": 2},
            "rules": [
                {"name": "expert", "min_complexity": 5, "model": "claude-opus"},
                {"name": "hard", "min_complexity": 4, "model": "claude-sonnet"},
                {"name": "deep-reasoning", "min_complexity": 3, "min_reasoning": 5, "model": "claude-sonnet"},
                {"name": "default", "model": "qwen-moe"},
            ],
        },
        "index": {"qdrant": {"url": ":memory:"}, "embedding": {"model": "fake-embed"}, "batch_size": 4},
        "pricing": {
            "reference_model": "sonnet",
            "models": {
                "opus": {"match": "opus", "input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5},
                "sonnet": {"match": "sonnet", "input": 2, "output": 10, "cache_read": 0.1, "cache_write": 2.5},
            },
        },
    }
    c = Config.model_validate(raw)
    c.data_path.mkdir(parents=True, exist_ok=True)
    c.claude_workdir.mkdir(parents=True, exist_ok=True)
    init_db(c.db_path)
    return c
