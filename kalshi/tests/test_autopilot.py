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
    fake_session.route("GET", "/markets/C-1/orderbook", {"orderbook": {"yes": [[50, 5]], "no": [[45, 5]]}})
    for m in ms:  # re-check on the second pass: prices unchanged, so stored estimates stay valid
        fake_session.route("GET", f"/markets/{m['ticker']}", {"market": m})
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
    fake_session.route("GET", "/markets/A-1/orderbook", {"orderbook": {"yes": [[40, 50]], "no": [[50, 50]]}})
    client = KalshiClient(settings, session=fake_session)
    cfg = AutopilotSettings(min_volume=500, min_balance_cents=5000)
    researcher = ScriptedResearcher({"A-1": Estimate("A-1", 0.9, "high", True, "", "r")})
    pilot = Autopilot(client, cfg, ResearchCache(tmp_path / "c.json", 1), tmp_path / "j.jsonl", researcher=researcher)
    placed = []
    assert pilot.run_once(lambda o: placed.append(o) or {"x": 1}) == []
    assert placed == []                                   # researched, but refused to buy below the cash floor


def test_select_markets_reports_reasons_and_reads_dollar_prices():
    cfg = AutopilotSettings(min_volume=500)
    ms = [market("A-1", volume=10), market("B-1", hours=1), {**market("C-1"), "yes_bid": None, "yes_ask": None,
          "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44"}]
    stats = {}
    kept = select_markets(ms, cfg, now=NOW, stats=stats)
    assert [m["ticker"] for m in kept] == ["C-1"]
    assert stats == {"volume below min_volume": 1, "closes too soon": 1}


def test_env_overrides_apply_only_to_that_env():
    raw = {"min_volume": 1000, "edge_cents": 10, "demo": {"min_volume": 20}, "prod": {"edge_cents": 12}}
    assert AutopilotSettings.from_mapping(raw, env="demo").min_volume == 20
    assert AutopilotSettings.from_mapping(raw, env="demo").edge_cents == 10
    assert AutopilotSettings.from_mapping(raw, env="prod").min_volume == 1000
    assert AutopilotSettings.from_mapping(raw, env="prod").edge_cents == 12
    assert AutopilotSettings.from_mapping(raw).min_volume == 1000


def test_redact_hides_keys_headers_and_pem():
    from kalshi_trader.autopilot import _error_chain, redact
    text = 'Illegal header value b\'x\\n --header "x-api-key: sk-ant-api03-abcDEF_123"\' and -----BEGIN PRIVATE KEY-----\nMC4C\n-----END PRIVATE KEY-----'
    out = redact(text)
    assert "sk-ant" not in out and "MC4C" not in out and "[redacted]" in out
    try:
        try:
            raise ValueError("inner sk-ant-api03-zzz")
        except ValueError as e:
            raise RuntimeError("outer") from e
    except RuntimeError as exc:
        chain = _error_chain(exc)
    assert chain.startswith("RuntimeError: outer <- ValueError: inner") and "zzz" not in chain


def test_empty_book_skips_research(settings, fake_session, tmp_path):
    fake_session.route("GET", "/markets", {"markets": [market("A-1", volume=3000), market("B-1", volume=2000)], "cursor": ""})
    fake_session.route("GET", "/portfolio/balance", {"balance": 50_000})
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    fake_session.route("GET", "/markets/A-1/orderbook", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})   # empty
    fake_session.route("GET", "/markets/B-1/orderbook", {"orderbook_fp": {"yes_dollars": [["0.40", "5"]], "no_dollars": [["0.50", "5"]]}})
    client = KalshiClient(settings, session=fake_session)
    researcher = ScriptedResearcher({"B-1": Estimate("B-1", 0.5, "high", True, "", "r")})
    pilot = Autopilot(client, AutopilotSettings(min_volume=500, max_research_per_pass=2), ResearchCache(tmp_path / "c.json", 1),
                      tmp_path / "j.jsonl", researcher=researcher)
    pilot.run_once(lambda o: {"order_id": "x"})
    assert researcher.asked == ["B-1"]


def test_daily_research_budget_stops_new_research(settings, fake_session, tmp_path):
    from datetime import datetime, timezone
    fake_session.route("GET", "/markets", {"markets": [market("A-1", volume=3000), market("B-1", volume=2000)], "cursor": ""})
    fake_session.route("GET", "/portfolio/balance", {"balance": 50_000})
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    for t in ("A-1", "B-1"):
        fake_session.route("GET", f"/markets/{t}/orderbook", {"orderbook": {"yes": [[40, 5]], "no": [[50, 5]]}})
    client = KalshiClient(settings, session=fake_session)
    cache = ResearchCache(tmp_path / "c.json", 12)
    cache.put(Estimate("OLD", 0.5, "high", True, "", "r", researched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), cost_usd=2.9))
    researcher = ScriptedResearcher({
        "A-1": Estimate("A-1", 0.5, "high", True, "", "r", cost_usd=0.5),
        "B-1": Estimate("B-1", 0.5, "high", True, "", "r", cost_usd=0.5),
    })
    cfg = AutopilotSettings(min_volume=500, max_research_per_pass=5, max_research_usd_per_day=3.0)
    Autopilot(client, cfg, cache, tmp_path / "j.jsonl", researcher=researcher).run_once(lambda o: {})
    assert researcher.asked == ["A-1"]          # $2.90 + $0.50 crosses the $3 cap; B-1 is not researched


def test_cached_estimates_trade_even_when_scan_finds_no_candidates(settings, fake_session, tmp_path):
    from datetime import datetime, timezone
    fake_session.route("GET", "/markets", {"markets": [], "cursor": ""})       # scan finds nothing
    fake_session.route("GET", "/portfolio/balance", {"balance": 50_000})
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    fake_session.route("GET", "/markets/OLD-1/orderbook", {"orderbook": {"yes": [[40, 50]], "no": [[56, 50]]}})  # yes ask 44
    fake_session.route("GET", "/markets/OLD-1", {"market": market("OLD-1", hours=48, bid=40, ask=44)})
    client = KalshiClient(settings, session=fake_session)
    cache = ResearchCache(tmp_path / "c.json", 12)
    cache.put(Estimate("OLD-1", 0.60, "high", True, "", "r", market_yes_price=42,
                       researched_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))
    placed = []
    pilot = Autopilot(client, AutopilotSettings(edge_cents=10, max_contracts=3), cache, tmp_path / "j.jsonl", researcher=ScriptedResearcher({}))
    pilot.run_once(lambda o: placed.append(o) or {"order_id": "o1"})
    assert [(o.ticker, o.side, o.price_cents, o.count) for o in placed] == [("OLD-1", "yes", 44, 3)]



def test_stored_estimate_is_dropped_when_price_drifted_or_market_closing(settings, fake_session, tmp_path):
    from datetime import datetime, timezone
    fake_session.route("GET", "/markets", {"markets": [], "cursor": ""})
    fake_session.route("GET", "/portfolio/balance", {"balance": 50_000})
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    # DRIFT: researched at 6c mid, now trading at 37c -> stale, dropped from cache
    fake_session.route("GET", "/markets/DRIFT", {"market": market("DRIFT", hours=48, bid=35, ask=39)})
    # SOON: price unchanged but the market closes in 2 hours -> not traded
    fake_session.route("GET", "/markets/SOON", {"market": market("SOON", hours=2, bid=40, ask=44)})
    for t in ("DRIFT", "SOON"):
        fake_session.route("GET", f"/markets/{t}/orderbook", {"orderbook": {"yes": [[5, 50]], "no": [[60, 50]]}})
    client = KalshiClient(settings, session=fake_session)
    cache = ResearchCache(tmp_path / "c.json", 12)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache.put(Estimate("DRIFT", 0.06, "high", True, "", "r", market_yes_price=6, researched_at=now))
    cache.put(Estimate("SOON", 0.60, "high", True, "", "r", market_yes_price=42, researched_at=now))
    placed = []
    pilot = Autopilot(client, AutopilotSettings(edge_cents=3, max_price_drift_cents=15, min_hours_to_close=6), cache,
                      tmp_path / "j.jsonl", researcher=ScriptedResearcher({}))
    pilot.run_once(lambda o: placed.append(o) or {})
    assert placed == []
    assert cache.get("DRIFT") is None          # dropped so it gets re-researched
    assert cache.get("SOON") is not None       # kept; just not traded this close to settlement
    assert "estimate_stale" in (tmp_path / "j.jsonl").read_text()
