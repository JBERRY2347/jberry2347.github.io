from kalshi_trader.client import KalshiClient, OrderRequest
from kalshi_trader.config import RiskLimits
from kalshi_trader.risk import DailyLedger, check_order


def make_client(settings, fake_session, position=0, resting=0):
    fake_session.route("GET", "/portfolio/positions", {"market_positions": [{"ticker": "T", "position": position}]})
    fake_session.route("GET", "/portfolio/orders", {"orders": [{"order_id": str(i)} for i in range(resting)], "cursor": ""})
    return KalshiClient(settings, session=fake_session)


def test_order_within_limits_passes(settings, fake_session):
    c = make_client(settings, fake_session)
    ledger = DailyLedger(settings.state_path)
    assert check_order(OrderRequest("T", "buy", "yes", 10, 50), RiskLimits(), c, ledger, "demo") == []


def test_each_limit_is_reported(settings, fake_session):
    c = make_client(settings, fake_session, position=95, resting=20)
    ledger = DailyLedger(settings.state_path)
    ledger.record("demo", 9_900)
    limits = RiskLimits(max_order_cost_cents=500, max_position_contracts=100, max_open_orders=20,
                        max_daily_spend_cents=10_000, max_price_cents=90)
    problems = check_order(OrderRequest("T", "buy", "yes", 10, 95), limits, c, ledger, "demo")
    joined = "\n".join(problems)
    assert "max_price_cents" in joined
    assert "max_order_cost" in joined
    assert "daily spend" in joined
    assert "max_position_contracts" in joined
    assert "max_open_orders" in joined
    assert len(problems) == 5


def test_buying_no_reduces_a_yes_position(settings, fake_session):
    c = make_client(settings, fake_session, position=100)
    ledger = DailyLedger(settings.state_path)
    limits = RiskLimits(max_position_contracts=100)
    assert check_order(OrderRequest("T", "buy", "no", 10, 50), limits, c, ledger, "demo") == []
    assert check_order(OrderRequest("T", "buy", "yes", 1, 50), limits, c, ledger, "demo")


def test_ledger_is_per_env_and_per_day(settings):
    ledger = DailyLedger(settings.state_path)
    ledger.record("demo", 100)
    ledger.record("demo", 250)
    ledger.record("prod", 5)
    assert ledger.spent_today("demo") == 350
    assert ledger.spent_today("prod") == 5
    ledger.path.write_text('{"demo": {"date": "2000-01-01", "spent_cents": 999}}')
    assert ledger.spent_today("demo") == 0
