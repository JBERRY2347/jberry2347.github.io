import pytest

from kalshi_trader.client import KalshiClient, KalshiError, OrderRequest


def test_order_request_validation():
    with pytest.raises(ValueError):
        OrderRequest("T", "hold", "yes", 1, 50)
    with pytest.raises(ValueError):
        OrderRequest("T", "buy", "yes", 1, 100)
    with pytest.raises(ValueError):
        OrderRequest("T", "buy", "yes", 0, 50)


def test_order_body_limit_and_market():
    o = OrderRequest("T", "buy", "no", 3, 40, expiration_ts=123, client_order_id="cid")
    assert o.to_body() == {"ticker": "T", "action": "buy", "side": "no", "count": 3, "type": "limit",
                           "client_order_id": "cid", "no_price": 40, "expiration_ts": 123}
    m = OrderRequest("T", "sell", "yes", 2).to_body()
    assert m["type"] == "market" and "yes_price" not in m and m["client_order_id"]


def test_worst_case_cost():
    assert OrderRequest("T", "buy", "yes", 10, 35).max_cost_cents() == 350
    assert OrderRequest("T", "sell", "yes", 10, 35).max_cost_cents() == 650
    assert OrderRequest("T", "buy", "no", 2).max_cost_cents() == 198


def test_client_signs_private_requests_only(settings, fake_session):
    fake_session.route("GET", "/exchange/status", {"trading_active": True})
    fake_session.route("GET", "/portfolio/balance", {"balance": 12345})
    c = KalshiClient(settings, session=fake_session)
    assert c.exchange_status()["trading_active"] is True
    assert c.balance()["balance"] == 12345
    public, private = fake_session.calls
    assert "KALSHI-ACCESS-SIGNATURE" not in public["headers"]
    assert private["headers"]["KALSHI-ACCESS-KEY"] == "key-123"
    assert private["headers"]["KALSHI-ACCESS-SIGNATURE"]
    assert fake_session.calls[1]["path"] == "/portfolio/balance"


def test_client_paginates_with_cursor(settings, fake_session):
    c = KalshiClient(settings, session=fake_session)
    pages = iter([{"markets": [{"ticker": "A"}], "cursor": "c1"}, {"markets": [{"ticker": "B"}], "cursor": ""}])

    def request(method, url, params=None, **kw):
        fake_session.calls.append(params)
        from tests.conftest import FakeResponse
        return FakeResponse(200, next(pages))

    fake_session.request = request
    assert [m["ticker"] for m in c.markets(series_ticker="S")] == ["A", "B"]
    assert fake_session.calls[1]["cursor"] == "c1"
    assert fake_session.calls[0]["series_ticker"] == "S"


def test_client_raises_on_http_error(settings, fake_session):
    fake_session.route("POST", "/portfolio/events/orders", {"error": {"code": "insufficient_balance"}}, status=400)
    c = KalshiClient(settings, session=fake_session)
    with pytest.raises(KalshiError) as ei:
        c.create_order(OrderRequest("T", "buy", "yes", 1, 50))
    assert ei.value.status == 400
    assert fake_session.calls[0]["json"]["price"] == "0.5000"


def test_v2_body_maps_yes_no_onto_bid_ask():
    yes_buy = OrderRequest("T", "buy", "yes", 3, 44, client_order_id="c1").to_v2_body()
    assert yes_buy == {"ticker": "T", "client_order_id": "c1", "side": "bid", "count": "3.00", "price": "0.4400",
                       "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross"}
    no_buy = OrderRequest("T", "buy", "no", 10, 93).to_v2_body()          # buy NO at 93c == sell YES at 7c
    assert (no_buy["side"], no_buy["price"], no_buy["count"]) == ("ask", "0.0700", "10.00")
    yes_sell = OrderRequest("T", "sell", "yes", 2, 60).to_v2_body()
    assert (yes_sell["side"], yes_sell["price"]) == ("ask", "0.6000")
    no_sell = OrderRequest("T", "sell", "no", 2, 60).to_v2_body()         # sell NO at 60c == buy YES at 40c
    assert (no_sell["side"], no_sell["price"]) == ("bid", "0.4000")
    timed = OrderRequest("T", "buy", "yes", 1, 50, expiration_ts=1_700_000_000).to_v2_body()
    assert timed["expiration_time"] == "2023-11-14T22:13:20Z"


def test_create_and_cancel_use_v2_paths(settings, fake_session):
    fake_session.route("POST", "/portfolio/events/orders", {"order": {"order_id": "o9", "status": "resting"}})
    fake_session.route("DELETE", "/portfolio/events/orders/o9", {"order": {"order_id": "o9", "status": "canceled"}})
    c = KalshiClient(settings, session=fake_session)
    assert c.create_order(OrderRequest("T", "buy", "yes", 1, 50))["order_id"] == "o9"
    assert c.cancel_order("o9")["order"]["status"] == "canceled"


def test_client_requires_credentials_for_private_calls(settings, fake_session):
    settings.api_key_id = None
    c = KalshiClient(settings, session=fake_session)
    with pytest.raises(SystemExit):
        c.balance()


def test_orderbook_accepts_both_top_level_keys(settings, fake_session):
    c = KalshiClient(settings, session=fake_session)
    fake_session.route("GET", "/markets/A/orderbook", {"orderbook_fp": {"yes_dollars": [["0.40", "5.00"]], "no_dollars": []}})
    assert c.orderbook("A") == {"yes_dollars": [["0.40", "5.00"]], "no_dollars": []}
    fake_session.route("GET", "/markets/B/orderbook", {"orderbook": {"yes": [[40, 5]], "no": []}})
    assert c.orderbook("B") == {"yes": [[40, 5]], "no": []}
    fake_session.route("GET", "/markets/C/orderbook", {"something_else": 1})
    with pytest.raises(KalshiError):
        c.orderbook("C")


def test_create_order_retries_server_errors_with_same_client_order_id(settings, fake_session, monkeypatch):
    monkeypatch.setattr("kalshi_trader.client.time.sleep", lambda s: None)
    answers = iter([(500, {"error": {"code": "internal_server_error"}}), (200, {"order": {"order_id": "o1", "status": "resting"}})])
    original = fake_session.request

    def request(method, url, **kw):
        if method == "POST":
            status, body = next(answers)
            fake_session.calls.append({"method": method, "path": url, "json": kw.get("json"), "headers": kw.get("headers")})
            from tests.conftest import FakeResponse
            return FakeResponse(status, body)
        return original(method, url, **kw)

    fake_session.request = request
    c = KalshiClient(settings, session=fake_session)
    assert c.create_order(OrderRequest("T", "buy", "no", 10, 56, client_order_id="fixed"))["order_id"] == "o1"
    posts = [x for x in fake_session.calls if x["method"] == "POST"]
    assert len(posts) == 2
    assert posts[0]["json"] == posts[1]["json"]                       # identical body, Kalshi de-duplicates on client_order_id
    assert posts[0]["headers"]["KALSHI-ACCESS-TIMESTAMP"] <= posts[1]["headers"]["KALSHI-ACCESS-TIMESTAMP"]


def test_create_order_gives_up_after_three_server_errors(settings, fake_session, monkeypatch):
    monkeypatch.setattr("kalshi_trader.client.time.sleep", lambda s: None)
    fake_session.route("POST", "/portfolio/events/orders", {"error": {"code": "internal_server_error"}}, status=500)
    c = KalshiClient(settings, session=fake_session)
    with pytest.raises(KalshiError) as ei:
        c.create_order(OrderRequest("T", "buy", "yes", 1, 50))
    assert ei.value.status == 500
    assert len(fake_session.calls) == 3
