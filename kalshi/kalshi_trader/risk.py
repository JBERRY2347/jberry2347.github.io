"""Client-side risk checks and a small persistent ledger of daily spend."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .client import KalshiClient, OrderRequest
from .config import RiskLimits


class RiskViolation(Exception):
    pass


class DailyLedger:
    """Tracks worst-case cost of orders placed today (UTC) in a JSON file."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def spent_today(self, env: str) -> int:
        data = self._load()
        day = data.get(env, {})
        return int(day.get("spent_cents", 0)) if day.get("date") == self._today() else 0

    def record(self, env: str, cents: int) -> None:
        data = self._load()
        today = self._today()
        day = data.get(env, {})
        if day.get("date") != today:
            day = {"date": today, "spent_cents": 0}
        day["spent_cents"] = int(day["spent_cents"]) + cents
        data[env] = day
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))


def current_position(positions: dict, ticker: str) -> int:
    """Net contracts held in a market: positive = long yes, negative = long no."""
    for p in positions.get("market_positions", []):
        if p.get("ticker") == ticker:
            return int(p.get("position", 0))
    return 0


def check_order(order: OrderRequest, limits: RiskLimits, client: KalshiClient, ledger: DailyLedger, env: str) -> list[str]:
    """Return a list of human-readable reasons the order violates the limits.

    An empty list means the order is within limits. Makes up to two API calls
    (positions and resting orders) to evaluate position and order-count caps.
    """
    problems: list[str] = []
    cost = order.max_cost_cents()

    if order.price_cents is not None:
        if order.price_cents < limits.min_price_cents:
            problems.append(f"price {order.price_cents}c is below min_price_cents={limits.min_price_cents}")
        if order.price_cents > limits.max_price_cents:
            problems.append(f"price {order.price_cents}c is above max_price_cents={limits.max_price_cents}")

    if limits.max_order_cost_cents and cost > limits.max_order_cost_cents:
        problems.append(f"worst-case cost ${cost/100:.2f} exceeds max_order_cost ${limits.max_order_cost_cents/100:.2f}")

    if limits.max_daily_spend_cents:
        spent = ledger.spent_today(env)
        if spent + cost > limits.max_daily_spend_cents:
            problems.append(
                f"daily spend would reach ${(spent+cost)/100:.2f}, over max_daily_spend ${limits.max_daily_spend_cents/100:.2f}"
            )

    if limits.max_position_contracts:
        pos = current_position(client.positions(), order.ticker)
        delta = order.count if (order.action == "buy") == (order.side == "yes") else -order.count
        if abs(pos + delta) > limits.max_position_contracts:
            problems.append(
                f"position would be {pos + delta:+d} contracts, over max_position_contracts={limits.max_position_contracts}"
            )

    if limits.max_open_orders:
        resting = len(client.orders(status="resting"))
        if resting >= limits.max_open_orders:
            problems.append(f"{resting} resting orders already, at max_open_orders={limits.max_open_orders}")

    return problems
