import json
from types import SimpleNamespace

import pytest

from kalshi_trader.research import ESTIMATE_SCHEMA, Estimate, ResearchCache, ResearchRefused, Researcher, describe_market, mid_price

MARKET = {"ticker": "T-1", "title": "Will it rain in NYC on Sunday?", "yes_sub_title": "Rain", "close_time": "2026-09-28T23:00:00Z",
          "yes_bid": 40, "yes_ask": 44, "last_price": 42, "volume": 1200, "rules_primary": "Resolves YES if Central Park records >0.01in."}


def _msg(text, stop_reason="end_turn", usage=(100, 50)):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]), stop_details=None)


class FakeAnthropic:
    """Replays scripted responses and records every request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


EXTRACT_JSON = json.dumps({"yes_prob": 0.55, "confidence": "medium", "should_trade": True, "skip_reason": "",
                           "reasoning": "Forecast shows 60% chance of rain.", "key_sources": ["https://weather.gov"]})


def test_describe_market_includes_rules_and_prices():
    text = describe_market(MARKET)
    assert "Central Park" in text and "bid 40c / ask 44c" in text and "Ticker: T-1" in text
    assert mid_price(MARKET) == 42
    assert mid_price({"last_price": 30}) == 30


def test_estimate_runs_research_then_extraction():
    fake = FakeAnthropic([_msg("Rain likely. I estimate 55%."), _msg(EXTRACT_JSON, usage=(20, 10))])
    est = Researcher(client=fake, model="claude-opus-5", max_searches=3).estimate(MARKET)
    assert est.ticker == "T-1" and est.yes_prob == 0.55 and est.confidence == "medium" and est.should_trade
    assert est.market_yes_price == 42 and est.input_tokens == 120 and est.output_tokens == 60
    research, extract = fake.calls
    assert research["model"] == "claude-opus-5"
    assert research["thinking"] == {"type": "adaptive"}
    assert [t["type"] for t in research["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    assert research["tools"][0]["max_uses"] == 3
    assert extract["output_config"]["format"] == {"type": "json_schema", "schema": ESTIMATE_SCHEMA}
    assert "Rain likely" in extract["messages"][0]["content"]


def test_pause_turn_is_continued():
    paused = _msg("", stop_reason="pause_turn")
    fake = FakeAnthropic([paused, _msg("Done: 30%"), _msg(EXTRACT_JSON)])
    Researcher(client=fake).estimate(MARKET)
    assert len(fake.calls) == 3
    assert fake.calls[1]["messages"][1]["role"] == "assistant"


def test_refusal_raises():
    fake = FakeAnthropic([_msg("", stop_reason="refusal")])
    with pytest.raises(ResearchRefused):
        Researcher(client=fake).estimate(MARKET)


def test_cache_ttl(tmp_path):
    path = tmp_path / "cache.json"
    cache = ResearchCache(path, ttl_hours=1)
    est = Estimate("T-1", 0.5, "high", True, "", "r", researched_at="2026-01-01T00:00:00+00:00")
    cache.put(est)
    assert ResearchCache(path, ttl_hours=1).get("T-1") is None          # too old
    assert ResearchCache(path, ttl_hours=1e9).get("T-1").yes_prob == 0.5  # huge ttl keeps it
    assert cache.get("missing") is None


def test_estimate_records_cost_and_search_count():
    from kalshi_trader.research import estimate_cost_usd
    resp1 = _msg("Research.", usage=(100_000, 5_000))
    resp1.usage.server_tool_use = SimpleNamespace(web_search_requests=4, web_fetch_requests=2)
    fake = FakeAnthropic([resp1, _msg(EXTRACT_JSON, usage=(1_000, 200))])
    est = Researcher(client=fake, model="claude-opus-5", effort="medium").estimate(MARKET)
    assert est.web_searches == 6
    assert est.cost_usd == round(estimate_cost_usd("claude-opus-5", 101_000, 5_200, 6), 4)
    assert abs(est.cost_usd - (0.505 + 0.13 + 0.06)) < 1e-6
    assert fake.calls[0]["output_config"]["effort"] == "medium"


def test_cache_sums_todays_spend(tmp_path):
    from datetime import datetime, timezone
    cache = ResearchCache(tmp_path / "c.json", ttl_hours=12)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache.put(Estimate("A", 0.5, "high", True, "", "r", researched_at=now, cost_usd=1.25))
    cache.put(Estimate("B", 0.5, "high", True, "", "r", researched_at=now, cost_usd=0.75))
    cache.put(Estimate("C", 0.5, "high", True, "", "r", researched_at="2020-01-01T00:00:00+00:00", cost_usd=9.0))
    assert cache.spent_today_usd() == 2.0


def test_cache_spend_survives_dropping_stale_estimates(tmp_path):
    from datetime import datetime, timezone
    path = tmp_path / "c.json"
    cache = ResearchCache(path, ttl_hours=12)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache.put(Estimate("A", 0.5, "high", True, "", "r", researched_at=now, cost_usd=1.25))
    cache.put(Estimate("B", 0.5, "high", True, "", "r", researched_at=now, cost_usd=0.75))
    cache.drop("A")
    assert cache.get("A") is None
    assert cache.spent_today_usd() == 2.0
    reopened = ResearchCache(path, ttl_hours=12)          # persisted in the new file format
    assert reopened.spent_today_usd() == 2.0
    assert [e.ticker for e in reopened.all()] == ["B"]


def test_cache_reads_legacy_flat_file(tmp_path):
    from datetime import datetime, timezone
    path = tmp_path / "c.json"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    legacy = {"A": Estimate("A", 0.5, "high", True, "", "r", researched_at=now, cost_usd=0.4).to_dict()}
    path.write_text(json.dumps(legacy))
    cache = ResearchCache(path, ttl_hours=12)
    assert cache.get("A").yes_prob == 0.5
    assert cache.spent_today_usd() == 0.4
