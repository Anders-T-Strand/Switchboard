from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    """A model call failed in a way the router can fall back from."""


@dataclass
class Completion:
    text: str
    provider_model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    load_ms: int | None = None
    cost_usd: float | None = None  # set when the provider reports its own cost
    extra: dict = field(default_factory=dict)
