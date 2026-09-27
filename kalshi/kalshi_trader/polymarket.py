"""Read-only Polymarket prices, used as a second opinion on Kalshi markets.

Polymarket's Gamma API needs no login. The autopilot pulls the active markets
once per pass, matches each Kalshi candidate to the closest Polymarket
question by wording and close date, and:

* prioritises candidates where the two exchanges disagree by a wide margin
  (that is where a cheap-to-find edge is most likely), and
* passes the Polymarket price to the researcher as context, with an
  instruction to check the two contracts really resolve on the same terms.

Nothing here places orders on Polymarket.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger("kalshi.polymarket")

GAMMA_URL = "https://gamma-api.polymarket.com"

_STOP = {"will", "the", "a", "an", "of", "in", "on", "at", "to", "for", "by", "be", "is", "vs", "and", "or", "than",
         "more", "less", "over", "under", "yes", "no", "win", "wins", "game", "match", "score", "any", "this", "that",
         "before", "after", "day", "week", "month", "year", "does", "do", "who", "what", "which", "with", "from", "its",
         "it", "as", "am", "pm", "et", "pt", "utc", "s"}


@dataclass(frozen=True)
class PolyMarket:
    question: str
    slug: str
    yes_price: float          # dollars, 0-1
    end_date: datetime | None
    volume: float
    liquidity: float
    tokens: frozenset[str]


@dataclass(frozen=True)
class Comparable:
    kalshi_ticker: str
    poly: PolyMarket
    score: float              # 0-1 wording similarity
    kalshi_mid_cents: float
    poly_yes_cents: float

    @property
    def gap_cents(self) -> float:
        """Kalshi price minus Polymarket price. Positive: cheaper on Polymarket."""
        return self.kalshi_mid_cents - self.poly_yes_cents


def tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(w for w in words if w not in _STOP and len(w) > 1)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_date(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_market(raw: dict) -> PolyMarket | None:
    """Turn a Gamma market object into a PolyMarket, or None if it is not a plain Yes/No market."""
    try:
        outcomes = raw.get("outcomes")
        prices = raw.get("outcomePrices")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if not outcomes or not prices or len(outcomes) != len(prices):
            return None
        idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
        yes = float(prices[idx])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    question = raw.get("question") or raw.get("title") or ""
    return PolyMarket(
        question=question,
        slug=raw.get("slug") or "",
        yes_price=yes,
        end_date=_parse_date(raw.get("endDate") or raw.get("end_date_iso")),
        volume=float(raw.get("volumeNum") or raw.get("volume") or 0),
        liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
        tokens=tokens(question),
    )


class PolymarketClient:
    def __init__(self, session: requests.Session | None = None, base_url: str = GAMMA_URL, timeout: float = 20.0):
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def active_markets(self, max_pages: int = 10, page_size: int = 500) -> list[PolyMarket]:
        out: list[PolyMarket] = []
        for page in range(max_pages):
            resp = self.session.get(f"{self.base_url}/markets", params={"closed": "false", "active": "true", "limit": page_size,
                                                                        "offset": page * page_size}, timeout=self.timeout)
            if resp.status_code != 200:
                log.warning("polymarket: HTTP %s on page %d; using %d markets fetched so far", resp.status_code, page, len(out))
                break
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(m for m in (parse_market(r) for r in batch if isinstance(r, dict)) if m)
            if len(batch) < page_size:
                break
        return out


def find_comparable(kalshi_market: dict, kalshi_mid_cents: float | None, poly_markets: list[PolyMarket],
                    min_similarity: float = 0.5, max_days_apart: float = 3.0) -> Comparable | None:
    """Best Polymarket match for a Kalshi market, or None if nothing is close enough."""
    if kalshi_mid_cents is None:
        return None
    text = " ".join(str(kalshi_market.get(k) or "") for k in ("title", "yes_sub_title", "subtitle"))
    ktok = tokens(text)
    kclose = _parse_date(kalshi_market.get("close_time"))
    best: tuple[float, PolyMarket] | None = None
    for pm in poly_markets:
        s = similarity(ktok, pm.tokens)
        if s < min_similarity:
            continue
        if kclose and pm.end_date and abs((kclose - pm.end_date).total_seconds()) > max_days_apart * 86400:
            continue
        if best is None or s > best[0] or (s == best[0] and pm.volume > best[1].volume):
            best = (s, pm)
    if best is None:
        return None
    s, pm = best
    return Comparable(kalshi_ticker=kalshi_market["ticker"], poly=pm, score=round(s, 3),
                      kalshi_mid_cents=float(kalshi_mid_cents), poly_yes_cents=round(pm.yes_price * 100, 1))


def describe_comparable(c: Comparable) -> str:
    """Prompt text handed to the researcher."""
    when = c.poly.end_date.strftime("%Y-%m-%d") if c.poly.end_date else "unknown"
    return (f"Possibly the same event on Polymarket (wording similarity {c.score:.0%}): \"{c.poly.question}\" "
            f"has YES at {c.poly_yes_cents:.0f}c (ends {when}, volume ${c.poly.volume:,.0f}). "
            f"Kalshi's mid is {c.kalshi_mid_cents:.0f}c. Before treating the Polymarket price as evidence, check that the two "
            f"contracts resolve on the same terms and dates; if they differ, say so and ignore it.")
