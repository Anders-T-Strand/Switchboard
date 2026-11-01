"""API-equivalent cost estimates from the pricing table in config.yaml."""

from __future__ import annotations

from .config import Config, PriceCfg


def find_price(cfg: Config, model_id: str | None) -> PriceCfg | None:
    if not model_id:
        return None
    mid = model_id.lower()
    for price in cfg.pricing.models.values():
        if price.match.lower() in mid:
            return price
    return None


def cost_usd(price: PriceCfg | None, input_tokens: int, output_tokens: int,
             cache_read: int = 0, cache_write: int = 0) -> float:
    if price is None:
        return 0.0
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read * price.cache_read
        + cache_write * price.cache_write
    ) / 1_000_000


def reference_cost(cfg: Config, input_tokens: int, output_tokens: int) -> float:
    """What a local request would have cost on the reference Claude model.

    Local and Claude tokenizers differ, so this is a ballpark figure, not a bill.
    """
    ref = cfg.pricing.models.get(cfg.pricing.reference_model or "")
    return cost_usd(ref, input_tokens, output_tokens)
