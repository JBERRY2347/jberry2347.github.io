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
