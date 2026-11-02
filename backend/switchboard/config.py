"""Loads and validates config.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]

# Claude Code effort levels, lowest to highest.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


def clamp_effort(level: str, floor: str | None = None, ceiling: str | None = None) -> str:
    i = EFFORTS.index(level)
    if floor is not None:
        i = max(i, EFFORTS.index(floor))
    if ceiling is not None:
        i = min(i, EFFORTS.index(ceiling))
    return EFFORTS[i]


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
    # Ollama only. None turns thinking on or off from the effort level (off at low).
    # true or false pins it.
    think: bool | None = None
    options: dict = Field(default_factory=dict)  # Ollama only, e.g. {num_ctx: 16384}
    # Claude only. Highest effort this model is ever run at, unless picked by hand.
    max_effort: EffortLevel | None = None


class RuleCfg(BaseModel):
    name: str
    model: str
    task_types: list[str] | None = None
    min_complexity: int | None = None
    max_complexity: int | None = None
    min_reasoning: int | None = None
    max_reasoning: int | None = None
    effort: EffortLevel | None = None  # pin the effort instead of deriving it from reasoning
    min_effort: EffortLevel | None = None
    max_effort: EffortLevel | None = None

    def matches(self, task_type: str, complexity: int, reasoning: int) -> bool:
        if self.task_types and task_type not in self.task_types:
            return False
        if self.min_complexity is not None and complexity < self.min_complexity:
            return False
        if self.max_complexity is not None and complexity > self.max_complexity:
            return False
        if self.min_reasoning is not None and reasoning < self.min_reasoning:
            return False
        if self.max_reasoning is not None and reasoning > self.max_reasoning:
            return False
        return True


def _default_effort_map() -> dict[int, str]:
    return {1: "low", 2: "low", 3: "medium", 4: "high", 5: "xhigh"}


class EffortCfg(BaseModel):
    # Reasoning depth (1-5, rated by the classifier) -> effort level.
    # A missing level means "don't set one", so Claude Code uses your own /effort setting.
    by_reasoning: dict[int, EffortLevel] = Field(default_factory=_default_effort_map)


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
    effort: EffortCfg = Field(default_factory=EffortCfg)
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
        bad_levels = [k for k in self.routing.effort.by_reasoning if not 1 <= k <= 5]
        if bad_levels:
            problems.append(f"routing.effort.by_reasoning keys must be 1-5, got {bad_levels}")
        for rule in self.routing.rules:
            if rule.min_effort and rule.max_effort and EFFORTS.index(rule.min_effort) > EFFORTS.index(rule.max_effort):
                problems.append(f"rule '{rule.name}' has min_effort above max_effort")
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
