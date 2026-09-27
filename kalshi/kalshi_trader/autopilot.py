"""Autopilot: pick markets, research them with Claude, trade the edge.

Each pass:

1. Pull open markets and keep the ones that are liquid, close soon enough to
   be researchable but not so soon that the outcome is already known, and are
   not excluded by the config.
2. For the top ``max_research_per_pass`` by volume that have no fresh
   estimate, ask :class:`kalshi_trader.research.Researcher` for a probability.
3. Turn every fresh, tradeable estimate into a :class:`MarketPlan` and hand it
   to the same fair-value logic manual plans use, with the same risk checks.
4. Before any order, enforce two account-level caps that need no local state:
   total exposure and a cash floor. Those protect you even when the daily
   ledger file is lost (for example between GitHub Actions runs).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .client import KalshiClient, OrderRequest
from .fields import count, exposure_cents, funded_exchange_indexes, mid_cents, price_cents, spread_cents, volume
from .research import Estimate, ResearchCache, ResearchFailed, Researcher, journal
from .risk import current_position
from .strategy import MarketPlan, best_ask, decide

log = logging.getLogger("kalshi.autopilot")

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class AutopilotSettings:
    # market selection
    min_volume: int = 500                 # contracts traded so far
    max_spread_cents: int = 10            # yes_ask - yes_bid
    min_hours_to_close: float = 6.0       # skip markets about to resolve
    max_days_to_close: float = 21.0       # skip far-future markets
    min_price_cents: int = 5              # skip near-certain markets
    max_price_cents: int = 95
    include_series: list[str] = field(default_factory=list)   # empty = all
    exclude_series: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)  # matched against title, case-insensitive
    # research
    model: str = "claude-opus-5"
    max_research_per_pass: int = 5
    max_searches_per_market: int = 8
    research_ttl_hours: float = 12.0
    min_confidence: str = "medium"
    # trading
    edge_cents: int = 10                  # required gap between estimate and ask
    max_contracts: int = 10               # per market
    max_total_exposure_cents: int = 20_000   # $200 across all positions and resting orders
    min_balance_cents: int = 5_000           # stop buying below $50 cash

    @classmethod
    def from_mapping(cls, m: dict, env: str | None = None) -> "AutopilotSettings":
        """Build settings from the ``[autopilot]`` table, then apply ``[autopilot.<env>]`` overrides."""
        merged = {k: v for k, v in m.items() if k in cls.__dataclass_fields__}
        if env and isinstance(m.get(env), dict):
            merged.update({k: v for k, v in m[env].items() if k in cls.__dataclass_fields__})
        return cls(**merged)


def _error_chain(exc: BaseException) -> str:
    """'OuterError: msg <- CauseError: msg', so connection errors say what actually failed."""
    parts, cur = [], exc
    while cur is not None and len(parts) < 6:
        parts.append(f"{type(cur).__name__}: {str(cur)[:200]}")
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def rejection_reason(m: dict, cfg: AutopilotSettings, now: datetime, shards: set[int] | None = None) -> str | None:
    """Why this market is not a candidate, or None if it is one.

    ``shards`` is the set of exchange indexes that hold cash; a market on another
    shard can't be paid for, so it is skipped.
    """
    if m.get("status") not in (None, "open", "active"):
        return f"status={m.get('status')}"
    if m.get("market_type") not in (None, "binary"):
        return f"market_type={m.get('market_type')}"
    if m.get("mve_selected_legs") or m.get("mve_collection_ticker"):
        return "multi-leg parlay"
    if shards is not None and m.get("exchange_index") is not None and int(m["exchange_index"]) not in shards:
        return "no cash on that exchange shard"
    close = _parse_time(m.get("close_time"))
    if close is None:
        return "no close_time"
    hours = (close - now).total_seconds() / 3600
    if hours < cfg.min_hours_to_close:
        return "closes too soon"
    if hours > cfg.max_days_to_close * 24:
        return "closes too far out"
    if volume(m) < cfg.min_volume:
        return "volume below min_volume"
    spread = spread_cents(m)
    if spread is None:
        return "no bid/ask"
    if spread > cfg.max_spread_cents:
        return "spread too wide"
    mid = mid_cents(m)
    if mid is None or mid < cfg.min_price_cents or mid > cfg.max_price_cents:
        return "price outside band"
    series = str(m.get("series_ticker") or m.get("event_ticker") or m.get("ticker", "")).split("-")[0]
    if cfg.include_series and series not in cfg.include_series:
        return "series not included"
    if series in cfg.exclude_series:
        return "series excluded"
    title = (m.get("title") or "").lower()
    if any(k.lower() in title for k in cfg.exclude_keywords):
        return "keyword excluded"
    return None


def select_markets(markets: list[dict], cfg: AutopilotSettings, now: datetime | None = None,
                   stats: dict[str, int] | None = None, shards: set[int] | None = None) -> list[dict]:
    """Filter and rank candidate markets, highest volume first.

    If ``stats`` is given, it is filled with a count of rejection reasons.
    """
    now = now or datetime.now(timezone.utc)
    keep = []
    for m in markets:
        reason = rejection_reason(m, cfg, now, shards)
        if reason is None:
            keep.append(m)
        elif stats is not None:
            stats[reason] = stats.get(reason, 0) + 1
    keep.sort(key=volume, reverse=True)
    return keep


def account_exposure_cents(positions: dict, resting_orders: list[dict]) -> int:
    """Cash at risk: open position exposure plus worst-case cost of resting orders."""
    total = 0.0
    for p in positions.get("market_positions", []):
        total += exposure_cents(p)
    for o in resting_orders:
        price = price_cents(o, "yes_price" if o.get("side") == "yes" else "no_price")
        n = count(o, "remaining_count") or count(o, "count")
        if price is None:
            continue
        per = price if o.get("action") == "buy" else 100 - price
        total += per * n
    return int(round(total))


def estimate_to_plan(est: Estimate, cfg: AutopilotSettings) -> MarketPlan | None:
    if not est.should_trade:
        return None
    if CONFIDENCE_RANK.get(est.confidence, 0) < CONFIDENCE_RANK.get(cfg.min_confidence, 1):
        return None
    if not 0.01 <= est.yes_prob <= 0.99:
        return None
    return MarketPlan(ticker=est.ticker, yes_prob=est.yes_prob, edge_cents=cfg.edge_cents, max_contracts=cfg.max_contracts)


class Autopilot:
    def __init__(self, client: KalshiClient, cfg: AutopilotSettings, cache: ResearchCache, journal_path,
                 researcher: Researcher | None = None, dry_run: bool = False):
        self.client = client
        self.cfg = cfg
        self.cache = cache
        self.journal_path = journal_path
        self.researcher = researcher
        self.dry_run = dry_run

    def _researcher(self) -> Researcher:
        if self.researcher is None:
            self.researcher = Researcher(model=self.cfg.model, max_searches=self.cfg.max_searches_per_market)
        return self.researcher

    def gather_estimates(self, markets: list[dict]) -> list[Estimate]:
        """Return fresh estimates for the top candidates, researching as needed."""
        estimates: list[Estimate] = []
        budget = self.cfg.max_research_per_pass
        for m in markets:
            cached = self.cache.get(m["ticker"])
            if cached:
                estimates.append(cached)
                continue
            if budget <= 0:
                continue
            budget -= 1
            try:
                est = self._researcher().estimate(m)
            except ResearchFailed as exc:
                log.warning("%s: research failed: %s", m["ticker"], exc)
                journal(self.journal_path, {"kind": "research_failed", "ticker": m["ticker"], "error": str(exc)})
                continue
            except Exception as exc:  # API/network errors: log and move on, never crash the pass
                detail = _error_chain(exc)
                log.error("%s: research error: %s", m["ticker"], detail)
                journal(self.journal_path, {"kind": "research_error", "ticker": m["ticker"], "error": detail})
                continue
            self.cache.put(est)
            journal(self.journal_path, {"kind": "estimate", **est.to_dict(), "title": m.get("title")})
            log.info("%s: estimate %.0f%% (%s confidence, market %sc) %s", est.ticker, est.yes_prob * 100, est.confidence,
                     est.market_yes_price, "" if est.should_trade else f"SKIP: {est.skip_reason}")
            estimates.append(est)
        return estimates

    def run_once(self, place) -> list[dict]:
        """One full pass. ``place(order)`` submits through the Trader's risk rails."""
        balance = self.client.balance()   # first: proves the API key signs correctly
        shards = funded_exchange_indexes(balance)
        log.info("account ok: cash $%.2f%s", int(balance.get("balance") or 0) / 100,
                 f", funded exchange shards {sorted(shards)}" if shards is not None else "")

        markets = self.client.markets(status="open", limit=1000, max_pages=10)
        stats: dict[str, int] = {}
        candidates = select_markets(markets, self.cfg, stats=stats, shards=shards)
        log.info("%d open markets, %d candidates after filters", len(markets), len(candidates))
        top = sorted(markets, key=volume, reverse=True)[:5]
        log.info("highest-volume open markets: %s", "; ".join(f"{m.get('ticker')} vol={volume(m):.0f} mid={mid_cents(m)}" for m in top))
        if not candidates:
            log.warning("rejection reasons: %s", ", ".join(f"{k}: {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1])))
            if top:
                log.warning("highest-volume market as returned by the API: %s", json.dumps(top[0], default=str)[:1500])
            return []

        estimates = self.gather_estimates(candidates)
        plans = [p for p in (estimate_to_plan(e, self.cfg) for e in estimates) if p]
        log.info("%d estimates, %d tradeable", len(estimates), len(plans))
        if not plans:
            return []

        balance = self.client.balance()   # again: cash may have changed during research
        positions = self.client.positions()
        resting = self.client.orders(status="resting")
        cash = int(balance.get("balance") or 0)
        exposure = account_exposure_cents(positions, resting)
        log.info("cash $%.2f, exposure $%.2f (cap $%.2f)", cash / 100, exposure / 100, self.cfg.max_total_exposure_cents / 100)

        placed = []
        for plan in plans:
            if cash < self.cfg.min_balance_cents:
                log.warning("cash $%.2f below min_balance; stopping", cash / 100)
                break
            try:
                book = self.client.orderbook(plan.ticker)
            except Exception as exc:
                log.warning("%s: could not fetch order book: %s", plan.ticker, exc)
                continue
            pos = current_position(positions, plan.ticker)
            order = decide(plan, book, pos)
            if order is None:
                log.info("%s: no edge (fair %dc, yes ask %s, no ask %s, pos %+d)", plan.ticker, plan.fair_yes_cents,
                         best_ask(book, "yes"), best_ask(book, "no"), pos)
                continue
            order = self._fit_exposure(order, exposure)
            if order is None:
                log.warning("%s: exposure cap reached; skipping", plan.ticker)
                continue
            resp = place(order)
            journal(self.journal_path, {"kind": "order", "ticker": plan.ticker, "fair_yes_cents": plan.fair_yes_cents,
                                        **order.to_body(), "dry_run": self.dry_run, "result": resp})
            if resp is not None:
                cost = order.max_cost_cents()
                exposure += cost
                cash -= cost
                placed.append(resp)
        return placed

    def _fit_exposure(self, order: OrderRequest, exposure: int) -> OrderRequest | None:
        room = self.cfg.max_total_exposure_cents - exposure
        if room <= 0:
            return None
        per = order.price_cents if order.action == "buy" else 100 - (order.price_cents or 99)
        count = min(order.count, room // max(per, 1))
        if count < 1:
            return None
        if count == order.count:
            return order
        return OrderRequest(order.ticker, order.action, order.side, count, order.price_cents, order.expiration_ts, order.client_order_id)
