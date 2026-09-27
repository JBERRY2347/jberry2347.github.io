import json
from datetime import datetime, timezone

from kalshi_trader.autopilot import Autopilot, AutopilotSettings
from kalshi_trader.polymarket import PolyMarket, PolymarketClient, describe_comparable, find_comparable, parse_market, similarity, tokens
from kalshi_trader.research import Estimate, ResearchCache

from tests.conftest import FakeResponse


class GammaSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        page = params["offset"] // params["limit"]
        return FakeResponse(200, self.pages[page] if page < len(self.pages) else [])


def gamma_market(question, yes, end="2026-10-03T00:00:00Z", volume=1000):
    return {"question": question, "slug": question.lower().replace(" ", "-"), "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps([str(yes), str(1 - yes)]), "endDate": end, "volumeNum": volume, "liquidityNum": 500}


def test_parse_market_handles_json_encoded_fields():
    pm = parse_market(gamma_market("Will the Bills beat the Chargers?", 0.62))
    assert pm.yes_price == 0.62
    assert pm.end_date == datetime(2026, 10, 3, tzinfo=timezone.utc)
    assert "bills" in pm.tokens and "will" not in pm.tokens


def test_parse_market_rejects_multi_outcome():
    raw = gamma_market("Who wins?", 0.5)
    raw["outcomes"] = json.dumps(["A", "B", "C"])
    raw["outcomePrices"] = json.dumps(["0.2", "0.3", "0.5"])
    pm = parse_market(raw)
    # three outcomes with three prices parses (first outcome used), mismatched lengths do not
    assert pm is not None
    raw["outcomePrices"] = json.dumps(["0.2", "0.8"])
    assert parse_market(raw) is None


def test_client_pages_until_short_page():
    session = GammaSession([[gamma_market(f"Q{i}", 0.5) for i in range(3)], [gamma_market("Q-last", 0.4)]])
    client = PolymarketClient(session=session)
    markets = client.active_markets(page_size=3)
    assert [m.question for m in markets] == ["Q0", "Q1", "Q2", "Q-last"]
    assert len(session.calls) == 2


def test_similarity_and_matching():
    assert similarity(tokens("Bills beat Chargers Sunday"), tokens("Will the Bills beat the Chargers on Sunday?")) == 1.0
    poly = [parse_market(gamma_market("Will the Bills beat the Chargers?", 0.62)),
            parse_market(gamma_market("Will the Chiefs beat the Dolphins?", 0.70))]
    kalshi = {"ticker": "KXNFL-BUF", "title": "Bills beat Chargers", "close_time": "2026-10-03T20:00:00Z"}
    comp = find_comparable(kalshi, 50.0, poly)
    assert comp.poly.question.startswith("Will the Bills")
    assert comp.poly_yes_cents == 62.0
    assert comp.gap_cents == -12.0     # Kalshi cheaper
    assert "Polymarket" in describe_comparable(comp)


def test_matching_respects_close_date_and_similarity():
    poly = [parse_market(gamma_market("Will the Bills beat the Chargers?", 0.62, end="2026-12-01T00:00:00Z"))]
    kalshi = {"ticker": "K", "title": "Bills beat Chargers", "close_time": "2026-10-03T20:00:00Z"}
    assert find_comparable(kalshi, 50.0, poly) is None         # two months apart
    assert find_comparable({"ticker": "K", "title": "Lakers beat Celtics"}, 50.0, poly) is None
    assert find_comparable(kalshi, None, poly) is None


def market(ticker, title, yes_bid, yes_ask, volume=5000):
    return {"ticker": ticker, "title": title, "status": "open", "yes_bid": yes_bid, "yes_ask": yes_ask, "volume": volume,
            "close_time": "2026-10-03T20:00:00Z", "open_time": "2026-09-01T00:00:00Z"}


def test_autopilot_ranks_disagreements_first_and_passes_context(tmp_path):
    from tests.test_autopilot import ScriptedResearcher  # noqa: F401  (reuse the fake)

    class FakePoly:
        def active_markets(self, **kw):
            return [parse_market(gamma_market("Will the Bills beat the Chargers?", 0.80)),
                    parse_market(gamma_market("Will the Chiefs beat the Dolphins?", 0.51))]

    pilot = Autopilot(client=None, cfg=AutopilotSettings(min_cross_exchange_gap_cents=8), cache=ResearchCache(tmp_path / "c.json", 1),
                      journal_path=tmp_path / "j.jsonl", researcher=ScriptedResearcher({}), polymarket=FakePoly())
    candidates = [market("KC", "Chiefs beat Dolphins", 49, 51, volume=9000), market("BUF", "Bills beat Chargers", 49, 51, volume=100)]
    ranked = pilot._rank_by_cross_exchange_gap(candidates)
    assert [m["ticker"] for m in ranked] == ["BUF", "KC"]       # 30c gap beats higher volume
    assert pilot._comparables["BUF"].gap_cents == -30.0
    lines = [json.loads(l) for l in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert {l["kind"] for l in lines} == {"cross_market"}
    assert [l["ticker"] for l in lines][0] == "BUF"
