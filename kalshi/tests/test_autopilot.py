import json
from datetime import datetime, timedelta, timezone

from kalshi_trader.autopilot import Autopilot, AutopilotSettings, account_exposure_cents, estimate_to_plan, select_markets
from kalshi_trader.client import KalshiClient
from kalshi_trader.research import Estimate, ResearchCache, ResearchFailed

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def market(ticker, hours=48, volume=1000, bid=40, ask=44, status="open", title="Something", series=None):
    return {"ticker": ticker, "series_ticker": series or ticker.split("-")[0], "title": title, "status": status, "volume": volume,
            "yes_bid": bid, "yes_ask": ask, "close_time": (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")}


def test_select_markets_filters_and_ranks():
    cfg = AutopilotSettings(min_volume=500, max_spread_cents=6, min_hours_to_close=6, max_days_to_close=21,
                            exclude_series=["BAD"], exclude_keywords=["crypto"])
    ms = [
        market("A-1", volume=900),
        market("B-1", volume=5000),
        market("C-1", hours=2),                       # closes too soon
        market("D-1", hours=30 * 24),                 # too far out
        market("E-1", volume=100),                    # illiquid
        market("F-1", bid=30, ask=45),                # wide spread
        market("G-1", bid=96, ask=98),                # near certain
        market("BAD-1"),                              # excluded series
        market("H-1", title="Bitcoin crypto price"),  # excluded keyword
        market("I-1", status="settled"),
    ]
    assert [m["ticker"] for m in select_markets(ms, cfg, now=NOW)] == ["B-1", "A-1"]
    cfg.include_series = ["A"]
    assert [m["ticker"] for m in select_markets(ms, cfg, now=NOW)] == ["A-1"]


def test_account_exposure():
    positions = {"market_positions": [{"ticker": "X", "market_exposure": 1500}, {"ticker": "Y", "market_exposure": -250}]}
    resting = [{"side": "yes", "action": "buy", "yes_price": 40, "remaining_count": 5},
               {"side": "no", "action": "sell", "no_price": 30, "count": 2}]
    assert account_exposure_cents(positions, resting) == 1500 + 250 + 200 + 140


def test_estimate_to_plan_gates():
    cfg = AutopilotSettings(min_confidence="medium", edge_cents=7, max_contracts=4)
    ok = Estimate("T", 0.6, "high", True, "", "r")
    plan = estimate_to_plan(ok, cfg)
    assert plan.ticker == "T" and plan.edge_cents == 7 and plan.max_contracts == 4
    assert estimate_to_plan(Estimate("T", 0.6, "low", True, "", "r"), cfg) is None
    assert estimate_to_plan(Estimate("T", 0.6, "high", False, "no info", "r"), cfg) is None
    assert estimate_to_plan(Estimate("T", 0.999, "high", True, "", "r"), cfg) is None


class ScriptedResearcher:
    def __init__(self, results):
        self.results = results
        self.asked = []

    def estimate(self, m):
        self.asked.append(m["ticker"])
        r = self.results[m["ticker"]]
        if isinstance(r, Exception):
            raise r
        return r


def test_run_once_researches_trades_and_journals(settings, fake_session, tmp_path):
    ms = [market("A-1", volume=3000, bid=40, ask=44), market("B-1", volume=2000, bid=70, ask=74), market("C-1", volume=1000)]
    fake_session.route("GET", "/markets", {"markets": ms, "cursor": ""})
    fake_session.route("GET", "/portfolio/balance", {"balance": 50_000})
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    fake_session.route("GET", "/markets/A-1/orderbook", {"orderbook": {"yes": [[40, 50]], "no": [[56, 50]]}})  # yes ask 44
    fake_session.route("GET", "/markets/B-1/orderbook", {"orderbook": {"yes": [[70, 50]], "no": [[26, 50]]}})  # yes ask 74
    client = KalshiClient(settings, session=fake_session)

    cfg = AutopilotSettings(min_volume=500, max_research_per_pass=2, edge_cents=10, max_contracts=5,
                            max_total_exposure_cents=150, min_confidence="medium")
    researcher = ScriptedResearcher({
        "A-1": Estimate("A-1", 0.60, "high", True, "", "rain", market_yes_price=42),        # fair 60 vs ask 44 -> edge 16
        "B-1": ResearchFailed("no info"),
        "C-1": Estimate("C-1", 0.5, "low", True, "", "meh"),
    })
    cache = ResearchCache(tmp_path / "cache.json", ttl_hours=12)
    journal_path = tmp_path / "journal.jsonl"
    placed = []
    pilot = Autopilot(client, cfg, cache, journal_path, researcher=researcher)
    resp = pilot.run_once(lambda order: placed.append(order) or {"order_id": "o1"})

    assert researcher.asked == ["A-1", "B-1"]          # C-1 beyond the research budget
    assert len(placed) == 1
    order = placed[0]
    assert order.ticker == "A-1" and order.side == "yes" and order.price_cents == 44
    assert order.count == 3                            # 5 wanted, exposure cap $1.50 / 44c = 3
    assert resp == [{"order_id": "o1"}]
    kinds = [json.loads(l)["kind"] for l in journal_path.read_text().splitlines()]
    assert kinds == ["estimate", "research_failed", "order"]
    assert cache.get("A-1").yes_prob == 0.60

    # second pass: A-1 comes from cache, so the budget of two reaches the retry of B-1 and then C-1
    placed.clear()
    pilot.run_once(lambda order: placed.append(order) or {})
    assert researcher.asked == ["A-1", "B-1", "B-1", "C-1"]
    assert placed[0].ticker == "A-1"                   # still trades the cached estimate; C-1 is low confidence


def test_run_once_stops_below_min_balance(settings, fake_session, tmp_path):
    fake_session.route("GET", "/markets", {"markets": [market("A-1", volume=3000)], "cursor": ""})
    fake_session.route("GET", "/portfolio/balance", {"balance": 100})
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    client = KalshiClient(settings, session=fake_session)
    cfg = AutopilotSettings(min_volume=500, min_balance_cents=5000)
    researcher = ScriptedResearcher({"A-1": Estimate("A-1", 0.9, "high", True, "", "r")})
    pilot = Autopilot(client, cfg, ResearchCache(tmp_path / "c.json", 1), tmp_path / "j.jsonl", researcher=researcher)
    assert pilot.run_once(lambda o: {"x": 1}) == []
    assert not [c for c in fake_session.calls if "orderbook" in c["path"]]
