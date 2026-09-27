"""Read numbers out of Kalshi API objects regardless of which field generation they use.

Kalshi is migrating from integer-cent fields (``yes_bid``, ``volume``) to
string fields in dollars or fixed-point (``yes_bid_dollars``, ``volume_fp``).
Markets on the demo exchange already return only the new ones. Everything in
the tool goes through these helpers so either shape works.
"""

from __future__ import annotations

import math


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price_cents(obj: dict, field: str) -> float | None:
    """Price in cents (may be fractional), from ``field`` or ``field_dollars``."""
    v = _num(obj.get(field))
    if v is not None:
        return v
    d = _num(obj.get(field + "_dollars"))
    return d * 100 if d is not None else None


def volume(m: dict) -> float:
    for key in ("volume", "volume_fp", "volume_24h", "volume_24h_fp"):
        v = _num(m.get(key))
        if v is not None:
            return v
    return 0.0


def spread_cents(m: dict) -> float | None:
    yb, ya = price_cents(m, "yes_bid"), price_cents(m, "yes_ask")
    if yb is None or ya is None:
        return None
    return ya - yb


def mid_cents(m: dict) -> float | None:
    yb, ya = price_cents(m, "yes_bid"), price_cents(m, "yes_ask")
    if yb is None or ya is None:
        last = price_cents(m, "last_price")
        return last
    return (yb + ya) / 2


def orderbook_levels(book: dict, side: str) -> list[tuple[float, float]]:
    """Resting bids for ``side`` as (price_cents, quantity), whichever field shape is present."""
    out: list[tuple[float, float]] = []
    for level in book.get(side) or []:
        p, q = _num(level[0]), _num(level[1])
        if p is not None and q is not None:
            out.append((p, q))
    if out:
        return out
    for level in book.get(side + "_dollars") or []:
        p, q = _num(level[0]), _num(level[1])
        if p is not None and q is not None:
            out.append((p * 100, q))
    return out


def ceil_cents(x: float) -> int:
    """Round a fractional-cent price up to a whole cent, for a buy limit that must cross the ask."""
    return int(math.ceil(x - 1e-9))


def funded_exchange_indexes(balance: dict) -> set[int] | None:
    """Exchange shards that hold cash, from a balance response; None if the API doesn't report shards."""
    breakdown = balance.get("balance_breakdown")
    if not breakdown:
        return None
    return {int(b["exchange_index"]) for b in breakdown if _num(b.get("balance")) and float(b["balance"]) > 0}


def count(obj: dict, field: str) -> float:
    """A contract count from ``field`` or ``field_fp``."""
    v = _num(obj.get(field))
    if v is None:
        v = _num(obj.get(field + "_fp"))
    return v or 0.0


def position_contracts(p: dict) -> int:
    """Signed net position of a market_positions entry: +YES, -NO."""
    return int(round(count(p, "position")))


def exposure_cents(p: dict) -> float:
    v = price_cents(p, "market_exposure")
    return abs(v) if v is not None else 0.0
