"""A simple fair-value strategy driven by a plan file you write.

The bot does not predict anything. You supply, per market, the probability you
believe the YES outcome has. On each pass it looks at the live order book and:

  * buys YES when the best YES ask is at least ``edge`` cents below your
    estimate, and
  * buys NO when the best NO ask is at least ``edge`` cents below
    (100 - estimate),

up to ``max_contracts`` per market, always as limit orders at the quoted ask
so you never pay more than the price you saw. Every order still goes through
the risk checks in :mod:`kalshi_trader.risk`.

Plan file format (JSON)::

    {
      "edge_cents": 5,
      "max_contracts": 10,
      "markets": {
        "KXHIGHNY-25SEP28-B70": {"yes_prob": 0.62},
        "SOME-OTHER-TICKER":     {"yes_prob": 0.15, "edge_cents": 8, "max_contracts": 5}
      }
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .client import KalshiClient, OrderRequest
from .fields import ceil_cents, orderbook_levels
from .risk import current_position

log = logging.getLogger("kalshi.strategy")


@dataclass(frozen=True)
class MarketPlan:
    ticker: str
    yes_prob: float
    edge_cents: int
    max_contracts: int

    @property
    def fair_yes_cents(self) -> int:
        return round(self.yes_prob * 100)


def load_plan(path: str | Path) -> list[MarketPlan]:
    raw = json.loads(Path(path).read_text())
    default_edge = int(raw.get("edge_cents", 5))
    default_max = int(raw.get("max_contracts", 10))
    plans = []
    for ticker, spec in raw.get("markets", {}).items():
        prob = float(spec["yes_prob"])
        if not 0.0 < prob < 1.0:
            raise ValueError(f"{ticker}: yes_prob must be strictly between 0 and 1")
        plans.append(MarketPlan(
            ticker=ticker,
            yes_prob=prob,
            edge_cents=int(spec.get("edge_cents", default_edge)),
            max_contracts=int(spec.get("max_contracts", default_max)),
        ))
    if not plans:
        raise ValueError("plan has no markets")
    return plans


def best_ask(orderbook: dict, side: str) -> int | None:
    """Best price to BUY ``side`` right now, in whole cents, or None if no liquidity.

    Kalshi's order book lists resting bids for each side as [price, quantity].
    A resting NO bid at price p is an offer to sell YES at 100 - p, so the best
    YES ask is 100 minus the highest NO bid, and vice versa. Sub-cent prices are
    rounded up so a limit order at the returned price still crosses the ask.
    """
    other = "no" if side == "yes" else "yes"
    levels = orderbook_levels(orderbook, other)
    if not levels:
        return None
    highest_bid = max(price for price, _ in levels)
    ask = ceil_cents(100 - highest_bid)
    return ask if 1 <= ask <= 99 else None


def decide(plan: MarketPlan, orderbook: dict, position: int) -> OrderRequest | None:
    """Return the order the plan calls for given the book and current position.

    ``position`` is signed: positive = long YES, negative = long NO.
    """
    yes_ask = best_ask(orderbook, "yes")
    no_ask = best_ask(orderbook, "no")
    fair_yes = plan.fair_yes_cents
    fair_no = 100 - fair_yes

    candidates: list[tuple[int, str, int]] = []  # (edge, side, ask)
    if yes_ask is not None and fair_yes - yes_ask >= plan.edge_cents:
        candidates.append((fair_yes - yes_ask, "yes", yes_ask))
    if no_ask is not None and fair_no - no_ask >= plan.edge_cents:
        candidates.append((fair_no - no_ask, "no", no_ask))
    if not candidates:
        return None

    _, side, ask = max(candidates)
    held = position if side == "yes" else -position
    room = plan.max_contracts - max(held, 0)
    if room <= 0:
        log.info("%s: %s edge available but already at max_contracts (%d)", plan.ticker, side, plan.max_contracts)
        return None
    return OrderRequest(ticker=plan.ticker, action="buy", side=side, count=room, price_cents=ask)


def run_once(client: KalshiClient, plans: list[MarketPlan], place) -> list[dict]:
    """Evaluate every market in the plan once. ``place(order)`` submits an order
    (after risk checks) and returns the API response or None if it was refused.
    """
    positions = client.positions()
    results = []
    for plan in plans:
        try:
            book = client.orderbook(plan.ticker)
        except Exception as exc:  # network / 404 on a closed market
            log.warning("%s: could not fetch order book: %s", plan.ticker, exc)
            continue
        pos = current_position(positions, plan.ticker)
        order = decide(plan, book, pos)
        if order is None:
            log.info("%s: no trade (fair yes %dc, yes ask %s, no ask %s, position %+d)",
                     plan.ticker, plan.fair_yes_cents, best_ask(book, "yes"), best_ask(book, "no"), pos)
            continue
        resp = place(order)
        if resp is not None:
            results.append(resp)
    return results
