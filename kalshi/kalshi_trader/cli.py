"""Command line interface. Run ``python -m kalshi_trader --help``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from . import __version__
from .client import KalshiClient, KalshiError, OrderRequest
from .config import load_settings
from .risk import DailyLedger, RiskViolation, check_order
from .strategy import load_plan, run_once

log = logging.getLogger("kalshi")


# ----------------------------------------------------------------- helpers

def _cents(v) -> str:
    return f"${int(v)/100:,.2f}"


def _ts(v) -> str:
    if not v:
        return "-"
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    except ValueError:
        return str(v)


def _out(args, data, text: str | None = None) -> None:
    if args.json or text is None:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(text)


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*[str(c) for c in r]) for r in rows]
    return "\n".join(lines)


class Trader:
    """Wraps a client with the safety rails every order must pass through."""

    def __init__(self, args, settings):
        self.args = args
        self.settings = settings
        self.client = KalshiClient(settings)
        self.ledger = DailyLedger(settings.state_path)

    def guard_live(self) -> None:
        if self.settings.is_live and not self.args.live:
            raise SystemExit(
                "refusing to trade on the PRODUCTION exchange without --live. "
                "Use --env demo to practice, or add --live if you really mean it."
            )

    def place(self, order: OrderRequest):
        self.guard_live()
        problems = check_order(order, self.settings.risk, self.client, self.ledger, self.settings.env)
        label = (f"{order.action.upper()} {order.count} {order.side.upper()} {order.ticker} "
                 f"@ {'MARKET' if order.is_market else str(order.price_cents) + 'c'} "
                 f"(worst case {_cents(order.max_cost_cents())})")
        if problems:
            msg = f"REFUSED {label}:\n  - " + "\n  - ".join(problems)
            if self.args.force:
                log.warning("%s\n  --force given, sending anyway", msg)
            else:
                log.error(msg)
                if self.args.command in ("buy", "sell"):
                    raise RiskViolation(msg)
                return None
        if self.args.dry_run:
            log.info("DRY RUN %s", label)
            return {"dry_run": True, **order.to_body()}
        if not self.args.yes and sys.stdin.isatty():
            env = "LIVE" if self.settings.is_live else "demo"
            answer = input(f"[{env}] {label}. Send? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                log.info("aborted")
                return None
        resp = self.client.create_order(order)
        self.ledger.record(self.settings.env, order.max_cost_cents())
        log.info("SENT %s -> order %s status=%s", label, resp.get("order_id"), resp.get("status"))
        return resp


# ---------------------------------------------------------------- commands

def cmd_status(t: Trader, args):
    _out(args, t.client.exchange_status())


def cmd_markets(t: Trader, args):
    markets = t.client.markets(status=args.status, event_ticker=args.event, series_ticker=args.series, limit=args.limit)
    if args.search:
        needle = args.search.lower()
        markets = [m for m in markets if needle in (m.get("title", "") + " " + m.get("ticker", "") + " " + m.get("yes_sub_title", "")).lower()]
    markets = markets[: args.limit]
    rows = [[m["ticker"], m.get("yes_bid", "-"), m.get("yes_ask", "-"), m.get("last_price", "-"), m.get("volume", 0),
             _ts(m.get("close_time")), (m.get("title") or "")[:60]] for m in markets]
    _out(args, markets, _table(rows, ["ticker", "yes_bid", "yes_ask", "last", "volume", "closes", "title"]))


def cmd_market(t: Trader, args):
    m = t.client.market(args.ticker)
    book = t.client.orderbook(args.ticker, depth=args.depth)
    if args.json:
        _out(args, {"market": m, "orderbook": book})
        return
    print(f"{m['ticker']}  [{m.get('status')}]  closes {_ts(m.get('close_time'))}")
    print(m.get("title", ""))
    if m.get("yes_sub_title"):
        print(m["yes_sub_title"])
    print(f"yes {m.get('yes_bid')}/{m.get('yes_ask')}   no {m.get('no_bid')}/{m.get('no_ask')}   "
          f"last {m.get('last_price')}   volume {m.get('volume')}   open interest {m.get('open_interest')}")
    print("\norder book (resting bids, price x qty):")
    yes = sorted(book.get("yes") or [], key=lambda l: -int(l[0]))[: args.depth]
    no = sorted(book.get("no") or [], key=lambda l: -int(l[0]))[: args.depth]
    rows = []
    for i in range(max(len(yes), len(no))):
        y = f"{yes[i][0]}c x {yes[i][1]}" if i < len(yes) else ""
        n = f"{no[i][0]}c x {no[i][1]}" if i < len(no) else ""
        rows.append([y, n])
    print(_table(rows, ["YES bids", "NO bids"]))


def cmd_balance(t: Trader, args):
    b = t.client.balance()
    _out(args, b, f"cash {_cents(b.get('balance', 0))}   portfolio value {_cents(b.get('portfolio_value', 0))}")


def cmd_positions(t: Trader, args):
    p = t.client.positions()
    rows = [[m["ticker"], m.get("position"), _cents(m.get("market_exposure", 0)), _cents(m.get("realized_pnl", 0)), _cents(m.get("fees_paid", 0))]
            for m in p.get("market_positions", []) if int(m.get("position", 0)) != 0 or int(m.get("resting_orders_count", 0))]
    _out(args, p, _table(rows, ["ticker", "position", "exposure", "realized pnl", "fees"]) if rows else "no open positions")


def cmd_orders(t: Trader, args):
    orders = t.client.orders(status=args.status, ticker=args.ticker)
    rows = [[o["order_id"], o["ticker"], o.get("action"), o.get("side"), o.get("yes_price") if o.get("side") == "yes" else o.get("no_price"),
             o.get("remaining_count", o.get("count")), o.get("status"), _ts(o.get("created_time"))] for o in orders]
    _out(args, orders, _table(rows, ["order_id", "ticker", "action", "side", "price", "remaining", "status", "created"]) if rows else "no orders")


def cmd_fills(t: Trader, args):
    fills = t.client.fills(ticker=args.ticker, limit=args.limit)
    rows = [[f["ticker"], f.get("action"), f.get("side"), f.get("yes_price") if f.get("side") == "yes" else f.get("no_price"),
             f.get("count"), _ts(f.get("created_time"))] for f in fills]
    _out(args, fills, _table(rows, ["ticker", "action", "side", "price", "count", "time"]) if rows else "no fills")


def _order_from_args(args, action: str) -> OrderRequest:
    if args.market and args.price is not None:
        raise SystemExit("use either --price or --market, not both")
    if not args.market and args.price is None:
        raise SystemExit("give a limit --price in cents, or --market for a market order")
    expiration = int(time.time()) + args.ttl if args.ttl else None
    return OrderRequest(ticker=args.ticker, action=action, side=args.side, count=args.count,
                        price_cents=None if args.market else args.price, expiration_ts=expiration)


def cmd_buy(t: Trader, args):
    resp = t.place(_order_from_args(args, "buy"))
    if resp is not None:
        _out(args, resp)


def cmd_sell(t: Trader, args):
    resp = t.place(_order_from_args(args, "sell"))
    if resp is not None:
        _out(args, resp)


def cmd_cancel(t: Trader, args):
    t.guard_live()
    if args.all:
        if args.dry_run:
            orders = t.client.orders(status="resting")
            _out(args, orders, f"DRY RUN: would cancel {len(orders)} resting orders")
            return
        _out(args, t.client.cancel_all(), None)
        return
    if not args.order_id:
        raise SystemExit("give an ORDER_ID or --all")
    if args.dry_run:
        print(f"DRY RUN: would cancel {args.order_id}")
        return
    _out(args, t.client.cancel_order(args.order_id))


def cmd_bot(t: Trader, args):
    t.guard_live()
    plans = load_plan(args.plan)
    log.info("loaded %d markets from %s; env=%s dry_run=%s interval=%ss", len(plans), args.plan, t.settings.env, args.dry_run, args.interval)
    while True:
        try:
            placed = run_once(t.client, plans, t.place)
            log.info("pass complete: %d order(s) placed", len(placed))
        except KalshiError as exc:
            log.error("API error: %s", exc)
        except Exception:
            log.exception("unexpected error during pass")
        if args.once:
            return
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopping")
            return


def cmd_autopilot(t: Trader, args):
    from .autopilot import Autopilot, AutopilotSettings
    from .research import ResearchCache

    t.guard_live()
    cfg = AutopilotSettings.from_mapping(t.settings.autopilot)
    if args.max_research is not None:
        cfg.max_research_per_pass = args.max_research
    if not args.dry_run and not args.yes and sys.stdin.isatty():
        answer = input(f"[{'LIVE' if t.settings.is_live else 'demo'}] autopilot will research up to {cfg.max_research_per_pass} "
                       f"markets per pass and place orders without asking. Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return
        args.yes = True
    cache = ResearchCache(t.settings.research_cache_path, cfg.research_ttl_hours)
    pilot = Autopilot(t.client, cfg, cache, t.settings.journal_path, dry_run=args.dry_run)
    log.info("autopilot on %s: edge %dc, max %d contracts/market, exposure cap $%.2f, model %s, journal %s",
             t.settings.env, cfg.edge_cents, cfg.max_contracts, cfg.max_total_exposure_cents / 100, cfg.model, t.settings.journal_path)
    while True:
        try:
            placed = pilot.run_once(t.place)
            log.info("pass complete: %d order(s) placed", len(placed))
        except KalshiError as exc:
            log.error("API error: %s", exc)
        except Exception:
            log.exception("unexpected error during pass")
        if args.once:
            return
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("stopping")
            return


def cmd_review(t: Trader, args):
    """Score past estimates against markets that have since settled."""
    from .research import ResearchCache

    cache = ResearchCache(t.settings.research_cache_path, ttl_hours=1e9)
    estimates = cache.all()
    if not estimates:
        print("no estimates recorded yet")
        return
    rows, brier_model, brier_market, n = [], 0.0, 0.0, 0
    for est in estimates:
        try:
            m = t.client.market(est.ticker)
        except KalshiError:
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        outcome = 1.0 if result == "yes" else 0.0
        brier_model += (est.yes_prob - outcome) ** 2
        if est.market_yes_price is not None:
            brier_market += (est.market_yes_price / 100 - outcome) ** 2
        n += 1
        rows.append([est.ticker, f"{est.yes_prob:.2f}", f"{(est.market_yes_price or 0)/100:.2f}", result, est.confidence])
    if not n:
        print(f"{len(estimates)} estimates, none settled yet")
        return
    summary = {"settled": n, "brier_model": round(brier_model / n, 4), "brier_market": round(brier_market / n, 4)}
    text = _table(rows, ["ticker", "estimate", "market", "result", "confidence"])
    text += f"\n\nsettled: {n}   Brier score (lower is better): model {summary['brier_model']:.4f}   market {summary['brier_market']:.4f}"
    _out(args, {"rows": rows, **summary}, text)


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi", description="Trade on Kalshi from the command line (defaults to the demo exchange).")
    p.add_argument("--version", action="version", version=f"kalshi-trader {__version__}")
    p.add_argument("--env", choices=["demo", "prod"], help="exchange to talk to (default: $KALSHI_ENV or demo)")
    p.add_argument("--config", help="path to a TOML config file")
    p.add_argument("--live", action="store_true", help="required for any order on the production exchange")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent without sending it")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation before sending orders")
    p.add_argument("--force", action="store_true", help="send even if a risk limit is violated (not recommended)")
    p.add_argument("--json", action="store_true", help="print raw JSON instead of tables")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="exchange status").set_defaults(func=cmd_status)

    s = sub.add_parser("markets", help="list markets")
    s.add_argument("--series", help="series ticker, e.g. KXHIGHNY")
    s.add_argument("--event", help="event ticker")
    s.add_argument("--status", default="open")
    s.add_argument("--search", help="filter by text in ticker/title")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_markets)

    s = sub.add_parser("market", help="details and order book for one market")
    s.add_argument("ticker")
    s.add_argument("--depth", type=int, default=5)
    s.set_defaults(func=cmd_market)

    sub.add_parser("balance", help="cash balance").set_defaults(func=cmd_balance)
    sub.add_parser("positions", help="open positions").set_defaults(func=cmd_positions)

    s = sub.add_parser("orders", help="list your orders")
    s.add_argument("--status", default="resting", help="resting, executed, canceled, or '' for all")
    s.add_argument("--ticker")
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("fills", help="recent fills")
    s.add_argument("--ticker")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_fills)

    for name, fn, helptext in (("buy", cmd_buy, "buy contracts"), ("sell", cmd_sell, "sell contracts")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("ticker")
        s.add_argument("--side", choices=["yes", "no"], required=True)
        s.add_argument("--count", type=int, required=True, help="number of contracts")
        s.add_argument("--price", type=int, help="limit price in cents (1-99)")
        s.add_argument("--market", action="store_true", help="market order instead of limit")
        s.add_argument("--ttl", type=int, help="seconds until the order expires (default: good til cancelled)")
        s.set_defaults(func=fn)

    s = sub.add_parser("cancel", help="cancel one order or all resting orders")
    s.add_argument("order_id", nargs="?")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("bot", help="run the fair-value strategy from a plan file")
    s.add_argument("--plan", required=True, help="JSON plan file (see README)")
    s.add_argument("--interval", type=int, default=60, help="seconds between passes")
    s.add_argument("--once", action="store_true", help="run a single pass and exit")
    s.set_defaults(func=cmd_bot)

    s = sub.add_parser("autopilot", help="research markets with Claude and trade the edge automatically")
    s.add_argument("--interval", type=int, default=3600, help="seconds between passes (default: 1 hour)")
    s.add_argument("--once", action="store_true", help="run a single pass and exit")
    s.add_argument("--max-research", type=int, help="override autopilot.max_research_per_pass")
    s.set_defaults(func=cmd_autopilot)

    s = sub.add_parser("review", help="score past autopilot estimates against settled markets")
    s.set_defaults(func=cmd_review)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = load_settings(args.config, args.env)
    if settings.is_live:
        log.warning("connected to the PRODUCTION exchange (real money)")
    trader = Trader(args, settings)
    try:
        args.func(trader, args)
    except KalshiError as exc:
        log.error("%s", exc)
        return 2
    except RiskViolation:
        return 3
    except KeyboardInterrupt:
        return 130
    return 0
