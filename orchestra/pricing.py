"""Per-token prices for estimating spend from token counts (the one place prices live).

Anthropic first-party API rates in USD per million tokens. Cache writes are billed by their
time-to-live: 5-minute writes at 1.25x the input price, 1-hour writes at 2x. The opus and
sonnet rows match the costs the Claude Code CLI itself recorded for wave 1 to the cent.
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float, float]] = {  # model -> (input, output, cache read)
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULT = {"5m": 1.25, "1h": 2.0}


def price_of(model: str) -> tuple[float, float, float] | None:
    """Exact model id first, then the current model of the same family; None if unknown."""
    m = (model or "").lower()
    if m in PRICES:
        return PRICES[m]
    return next((p for k, p in PRICES.items() if k.split("-")[1] in m), None)


def cost_usd(model: str, inp: int = 0, out: int = 0, cache_read: int = 0,
             write_5m: int = 0, write_1h: int = 0) -> float | None:
    p = price_of(model)
    if p is None:
        return None
    i, o, r = p
    return (inp * i + out * o + cache_read * r
            + write_5m * i * CACHE_WRITE_MULT["5m"] + write_1h * i * CACHE_WRITE_MULT["1h"]) / 1e6
