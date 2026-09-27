"""Thin REST client for the Kalshi trade API v2."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

import requests

from .auth import API_PREFIX, auth_headers, load_private_key
from .config import Settings


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: Any, method: str, path: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")


@dataclass(frozen=True)
class OrderRequest:
    """A validated order, ready to send. Prices are in cents (1-99)."""

    ticker: str
    action: str          # "buy" | "sell"
    side: str            # "yes" | "no"
    count: int
    price_cents: int | None = None   # None => market order
    expiration_ts: int | None = None  # unix seconds; None => good-til-cancelled
    client_order_id: str | None = None

    def __post_init__(self):
        if self.action not in ("buy", "sell"):
            raise ValueError("action must be 'buy' or 'sell'")
        if self.side not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        if self.count < 1:
            raise ValueError("count must be at least 1")
        if self.price_cents is not None and not 1 <= self.price_cents <= 99:
            raise ValueError("price must be between 1 and 99 cents")

    @property
    def is_market(self) -> bool:
        return self.price_cents is None

    def max_cost_cents(self) -> int:
        """Worst-case cash outlay for a buy (or collateral for a sell)."""
        per_contract = self.price_cents if self.price_cents is not None else 99
        if self.action == "sell":
            # Selling a contract you don't own posts 100 - price as collateral.
            per_contract = 100 - per_contract
        return per_contract * self.count

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "count": self.count,
            "type": "market" if self.is_market else "limit",
            "client_order_id": self.client_order_id or str(uuid.uuid4()),
        }
        if self.price_cents is not None:
            body["yes_price" if self.side == "yes" else "no_price"] = self.price_cents
        if self.expiration_ts is not None:
            body["expiration_ts"] = self.expiration_ts
        return body


class KalshiClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None, timeout: float = 15.0):
        self.settings = settings
        self.base_url = settings.host + API_PREFIX
        self.session = session or requests.Session()
        self.timeout = timeout
        self._key_id = settings.api_key_id
        self._key = load_private_key(settings.private_key_path) if settings.private_key_path else None
        self.last_response = None

    @property
    def key_type(self) -> str | None:
        from .auth import key_type
        return key_type(self._key) if self._key else None

    @property
    def key_id_hint(self) -> str:
        """First characters of the key id, enough to tell keys apart in a log."""
        return (self._key_id or "")[:8] + "…" if self._key_id else "(none)"

    def api_keys(self) -> list[dict]:
        """List the API keys on the account (an authenticated, read-only call)."""
        return self._request("GET", "/api_keys").get("api_keys", [])

    # ------------------------------------------------------------------ core

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None, auth: bool = True) -> Any:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        if auth:
            if not self._key or not self._key_id:
                self.settings.require_credentials()
            headers.update(auth_headers(self._key_id, self._key, method, path))
        attempts = 5 if method == "GET" else 1
        for attempt in range(attempts):
            if auth and attempt:  # fresh timestamp and signature for each retry
                headers.update(auth_headers(self._key_id, self._key, method, path))
            resp = self.session.request(method, url, params=params, json=json, headers=headers, timeout=self.timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        self.last_response = resp
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise KalshiError(resp.status_code, body, method, path)
        if not resp.content:
            return {}
        return resp.json()

    def _paginate(self, path: str, key: str, params: dict | None = None, *, auth: bool = True, max_pages: int = 20) -> Iterator[dict]:
        params = dict(params or {})
        for _ in range(max_pages):
            data = self._request("GET", path, params=params, auth=auth)
            for item in data.get(key, []):
                yield item
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    # -------------------------------------------------------------- exchange

    def exchange_status(self) -> dict:
        return self._request("GET", "/exchange/status", auth=False)

    # --------------------------------------------------------------- markets

    def markets(self, *, status: str | None = "open", event_ticker: str | None = None, series_ticker: str | None = None,
                tickers: list[str] | None = None, limit: int = 100, max_pages: int = 5) -> list[dict]:
        params: dict[str, Any] = {"limit": min(limit, 1000)}
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)
        return list(self._paginate("/markets", "markets", params, auth=False, max_pages=max_pages))

    def market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}", auth=False)["market"]

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, auth=False)["orderbook"]

    def events(self, *, status: str | None = "open", series_ticker: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": min(limit, 200)}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        return list(self._paginate("/events", "events", params, auth=False, max_pages=3))

    # ------------------------------------------------------------- portfolio

    def balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def positions(self) -> dict:
        return self._request("GET", "/portfolio/positions", params={"limit": 200})

    def orders(self, *, status: str | None = "resting", ticker: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 200}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        return list(self._paginate("/portfolio/orders", "orders", params))

    def fills(self, *, ticker: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": min(limit, 200)}
        if ticker:
            params["ticker"] = ticker
        return list(self._paginate("/portfolio/fills", "fills", params, max_pages=1))

    def create_order(self, order: OrderRequest) -> dict:
        return self._request("POST", "/portfolio/orders", json=order.to_body()).get("order", {})

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def cancel_all(self) -> list[dict]:
        cancelled = []
        for o in self.orders(status="resting"):
            cancelled.append(self.cancel_order(o["order_id"]))
        return cancelled
