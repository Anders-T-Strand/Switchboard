"""Loads and validates config.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: str = "data"


class OllamaCfg(BaseModel):
    base_url: str = "http://127.0.0.1:11434"
    timeout_s: float = 600
    keep_alive: str = "30m"


class ClaudeCfg(BaseModel):
    command: str = "claude"
    timeout_s: float = 300
    max_turns: int = 3
    use_subscription: bool = True
    prompt_via: Literal["stdin", "arg"] = "stdin"
    disallowed_tools: list[str] = Field(default_factory=lambda: ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"])
    extra_args: list[str] = Field(default_factory=list)
    system_prompt: str | None = None
    logs_dir: str | None = None
    sync_interval_s: int = 60


class ModelCfg(BaseModel):
    provider: Literal["ollama", "claude_cli"]
    model: str
    label: str | None = None
    think: bool | None = None  # Ollama only. None leaves Ollama's default alone.
    options: dict = Field(default_factory=dict)  # Ollama only, e.g. {num_ctx: 16384}


class RuleCfg(BaseModel):
    name: str
    model: str
    task_types: list[str] | None = None
    min_complexity: int | None = None
    max_complexity: int | None = None

    def matches(self, task_type: str, complexity: int) -> bool:
        if self.task_types and task_type not in self.task_types:
            return False
        if self.min_complexity is not None and complexity < self.min_complexity:
            return False
        if self.max_complexity is not None and complexity > self.max_complexity:
            return False
        return True


class ClassifierCfg(BaseModel):
    model: str
    use_loaded_model: bool = True
    think: bool | None = False
    timeout_s: float = 60
    max_prompt_chars: int = 4000


class RoutingCfg(BaseModel):
    classifier: ClassifierCfg
    prefer_loaded_local: bool = True
    fallback_order: list[str] = Field(default_factory=list)
    rules: list[RuleCfg]


class PriceCfg(BaseModel):
    match: str
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


class PricingCfg(BaseModel):
    reference_model: str | None = "sonnet"
    models: dict[str, PriceCfg] = Field(default_factory=dict)


class Config(BaseModel):
    server: ServerCfg = Field(default_factory=ServerCfg)
    ollama: OllamaCfg = Field(default_factory=OllamaCfg)
    claude: ClaudeCfg = Field(default_factory=ClaudeCfg)
    models: dict[str, ModelCfg]
    routing: RoutingCfg
    pricing: PricingCfg = Field(default_factory=PricingCfg)

    @model_validator(mode="after")
    def _check_references(self) -> "Config":
        known = set(self.models)
        problems = []
        for rule in self.routing.rules:
            if rule.model not in known:
                problems.append(f"rule '{rule.name}' uses unknown model '{rule.model}'")
        for key in self.routing.fallback_order:
            if key not in known:
                problems.append(f"fallback_order has unknown model '{key}'")
        clf = self.models.get(self.routing.classifier.model)
        if clf is None:
            problems.append(f"classifier model '{self.routing.classifier.model}' is not defined")
        elif clf.provider != "ollama":
            problems.append("classifier model must be a local (ollama) model")
        if not self.routing.rules:
            problems.append("routing.rules is empty")
        if self.pricing.reference_model and self.pricing.reference_model not in self.pricing.models:
            problems.append(f"pricing.reference_model '{self.pricing.reference_model}' is not in pricing.models")
        if problems:
            raise ValueError("config.yaml problems:\n  - " + "\n  - ".join(problems))
        return self

    # Derived paths -------------------------------------------------------

    @property
    def data_path(self) -> Path:
        p = Path(self.server.data_dir)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def db_path(self) -> Path:
        return self.data_path / "switchboard.db"

    @property
    def claude_workdir(self) -> Path:
        return self.data_path / "claude-workdir"

    @property
    def claude_logs_root(self) -> Path:
        if self.claude.logs_dir:
            return Path(self.claude.logs_dir).expanduser()
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".claude"

    def label(self, key: str | None) -> str | None:
        if key is None:
            return None
        m = self.models.get(key)
        return (m.label or key) if m else key


def config_path() -> Path:
    env = os.environ.get("SWITCHBOARD_CONFIG")
    return Path(env).expanduser() if env else REPO_ROOT / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = Config.model_validate(raw)
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    cfg.claude_workdir.mkdir(parents=True, exist_ok=True)
    return cfg


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
