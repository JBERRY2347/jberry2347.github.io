import pytest

from kalshi_trader import cli
from kalshi_trader.client import KalshiClient


@pytest.fixture
def wired(monkeypatch, settings, fake_session):
    """Route the CLI at fake settings and a fake HTTP session."""
    monkeypatch.setattr(cli, "load_settings", lambda cfg, env: settings.__class__(
        env=env or "demo", api_key_id=settings.api_key_id, private_key_path=settings.private_key_path,
        risk=settings.risk, state_path=settings.state_path))
    real_init = KalshiClient.__init__

    def init(self, s, session=None, timeout=15.0):
        real_init(self, s, session=fake_session, timeout=timeout)

    monkeypatch.setattr(KalshiClient, "__init__", init)
    fake_session.route("GET", "/portfolio/positions", {"market_positions": []})
    fake_session.route("GET", "/portfolio/orders", {"orders": [], "cursor": ""})
    fake_session.route("POST", "/portfolio/orders", {"order": {"order_id": "o1", "status": "resting"}})
    return fake_session


def test_prod_trading_requires_live_flag(wired, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--env", "prod", "-y", "buy", "T", "--side", "yes", "--count", "1", "--price", "50"])
    assert "--live" in str(ei.value)
    assert not [c for c in wired.calls if c["method"] == "POST"]


def test_dry_run_sends_nothing(wired, capsys):
    assert cli.main(["-y", "--dry-run", "buy", "T", "--side", "yes", "--count", "1", "--price", "50"]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert not [c for c in wired.calls if c["method"] == "POST"]


def test_buy_sends_signed_order(wired, capsys):
    assert cli.main(["-y", "buy", "T", "--side", "no", "--count", "2", "--price", "30"]) == 0
    post = [c for c in wired.calls if c["method"] == "POST"][0]
    assert post["json"]["no_price"] == 30 and post["json"]["count"] == 2
    assert post["headers"]["KALSHI-ACCESS-SIGNATURE"]
    assert '"order_id": "o1"' in capsys.readouterr().out


def test_risk_violation_blocks_order(wired):
    assert cli.main(["-y", "buy", "T", "--side", "yes", "--count", "100", "--price", "50"]) == 3
    assert not [c for c in wired.calls if c["method"] == "POST"]


def test_bot_once_places_from_plan(wired, tmp_path, capsys):
    wired.route("GET", "/markets/T/orderbook", {"orderbook": {"yes": [[40, 10]], "no": [[50, 10]]}})
    plan = tmp_path / "plan.json"
    plan.write_text('{"edge_cents": 5, "max_contracts": 3, "markets": {"T": {"yes_prob": 0.6}}}')
    assert cli.main(["-y", "bot", "--plan", str(plan), "--once"]) == 0
    post = [c for c in wired.calls if c["method"] == "POST"][0]
    assert post["json"] == {**post["json"], "side": "yes", "yes_price": 50, "count": 3}
