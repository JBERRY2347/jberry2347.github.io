from datetime import datetime, timedelta, timezone

from kalshi_trader.autopilot import AutopilotSettings, account_exposure_cents, select_markets
from kalshi_trader.fields import (ceil_cents, count, exposure_cents, funded_exchange_indexes, mid_cents, orderbook_levels,
                                  position_contracts, price_cents, spread_cents, volume)
from kalshi_trader.strategy import best_ask

NEW_STYLE = {  # what the demo exchange returns today: no integer-cent fields at all
    "ticker": "KXNEW-1", "status": "open", "market_type": "binary", "exchange_index": 0,
    "yes_bid_dollars": "0.4200", "yes_ask_dollars": "0.4450", "last_price_dollars": "0.4300",
    "volume_fp": "1234.00", "open_interest_fp": "100.00",
    "close_time": (datetime(2026, 9, 27, 12, tzinfo=timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
}


def test_price_and_volume_helpers_accept_both_shapes():
    assert price_cents({"yes_bid": 42}, "yes_bid") == 42
    assert price_cents(NEW_STYLE, "yes_bid") == 42.0
    assert price_cents(NEW_STYLE, "yes_ask") == 44.5
    assert price_cents({}, "yes_bid") is None
    assert volume({"volume": 7}) == 7 and volume(NEW_STYLE) == 1234.0 and volume({}) == 0
    assert spread_cents(NEW_STYLE) == 2.5 and mid_cents(NEW_STYLE) == 43.25
    assert count({"position_fp": "3.00"}, "position") == 3.0
    assert position_contracts({"position": -4}) == -4 and position_contracts({"position_fp": "2.00"}) == 2
    assert exposure_cents({"market_exposure_dollars": "-1.50"}) == 150.0
    assert ceil_cents(44.5) == 45 and ceil_cents(44.0) == 44


def test_orderbook_levels_and_best_ask_with_dollar_book():
    book = {"yes_dollars": [["0.4000", "50.00"], ["0.3800", "10.00"]], "no_dollars": [["0.5550", "20.00"]]}
    assert orderbook_levels(book, "yes") == [(40.0, 50.0), (38.0, 10.0)]
    assert best_ask(book, "yes") == 45          # 100 - 55.5, rounded up
    assert best_ask(book, "no") == 60
    assert best_ask({"yes": [[99, 5]]}, "no") == 1
    assert best_ask({"no": [[100, 5]]}, "yes") is None  # zero-priced ask is not tradeable


def test_select_markets_uses_new_fields_and_shards():
    cfg = AutopilotSettings(min_volume=500, max_spread_cents=5)
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    other_shard = {**NEW_STYLE, "ticker": "KXNEW-2", "exchange_index": 1}
    parlay = {**NEW_STYLE, "ticker": "KXMVE-1", "mve_selected_legs": [{"market_ticker": "A"}]}
    stats = {}
    kept = select_markets([NEW_STYLE, other_shard, parlay], cfg, now=now, stats=stats, shards={0})
    assert [m["ticker"] for m in kept] == ["KXNEW-1"]
    assert stats == {"no cash on that exchange shard": 1, "multi-leg parlay": 1}
    assert select_markets([other_shard], cfg, now=now, shards=None)[0]["ticker"] == "KXNEW-2"


def test_funded_shards_and_exposure_with_new_fields():
    balance = {"balance": 10000, "balance_breakdown": [{"balance": "100.0000", "exchange_index": 0}, {"balance": "0.0000", "exchange_index": 1}]}
    assert funded_exchange_indexes(balance) == {0}
    assert funded_exchange_indexes({"balance": 5}) is None
    positions = {"market_positions": [{"ticker": "X", "market_exposure_dollars": "15.00"}]}
    resting = [{"side": "yes", "action": "buy", "yes_price_dollars": "0.40", "remaining_count_fp": "5.00"}]
    assert account_exposure_cents(positions, resting) == 1500 + 200
