"""Research a Kalshi market with Claude and web search, and return a probability.

Two calls per market:

1. **Research**: Claude Opus with the hosted ``web_search`` and ``web_fetch``
   tools reads about the event and writes up what it found.
2. **Extract**: a second call with a JSON schema turns that write-up into a
   validated :class:`Estimate` (probability, confidence, reasoning, sources).

The estimate is deliberately conservative: the prompt asks Claude to say when
it could not find enough information, and the autopilot skips those markets.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fields import mid_cents, price_cents, volume

log = logging.getLogger("kalshi.research")

DEFAULT_MODEL = "claude-opus-5"

# USD per million tokens (input, output) and per web search. Used only to estimate spend
# for the daily research budget; Anthropic's bill is authoritative.
MODEL_PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
WEB_SEARCH_PRICE = 0.01


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int, web_searches: int) -> float:
    inp, out = MODEL_PRICES.get(model, (5.00, 25.00))
    return input_tokens / 1e6 * inp + output_tokens / 1e6 * out + web_searches * WEB_SEARCH_PRICE

RESEARCH_SYSTEM = """You are a careful forecaster helping estimate the probability of a real-world event that trades as a binary contract on the Kalshi prediction market.

Your job is to research the question and produce a well-calibrated probability that the YES outcome occurs by the market's close time. Work like a superforecaster:

- Read the market rules carefully. Resolution criteria matter more than headlines.
- Search for the most recent, authoritative information: official data releases, primary sources, polling aggregates, weather models, schedules, court dockets, league standings, and reputable news.
- Establish a base rate before considering the specifics, then adjust.
- Note the date and time now versus the close time. Events close to resolution should have probabilities near 0 or 1 if the outcome is effectively known.
- The current market price is given to you. Treat it as a strong informed prior. Only diverge from it when you found specific evidence the market may not be pricing in, and explain what that evidence is.
- Be explicit about uncertainty. If you cannot find enough information to have a real view, say so; the trading system will skip the market. That is a good outcome, not a failure.
- Never fabricate sources or numbers.

Finish with a short written assessment: what the market asks, what you found, your probability for YES, how confident you are in that probability, and the key sources you relied on."""

EXTRACT_SYSTEM = """Extract the forecaster's conclusion from the research write-up into the JSON schema. Do not add your own analysis. If the write-up says it could not find enough information, set should_trade to false and give the reason."""

ESTIMATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "yes_prob": {"type": "number", "description": "Probability the market resolves YES, between 0 and 1."},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"],
                       "description": "How confident the forecaster is in the probability itself."},
        "should_trade": {"type": "boolean", "description": "False if the forecaster lacked enough information to have a real view."},
        "skip_reason": {"type": "string", "description": "Why not to trade, or empty string."},
        "reasoning": {"type": "string", "description": "Two to five sentences summarising the case."},
        "key_sources": {"type": "array", "items": {"type": "string"}, "description": "URLs or source names relied on."},
    },
    "required": ["yes_prob", "confidence", "should_trade", "skip_reason", "reasoning", "key_sources"],
    "additionalProperties": False,
}


@dataclass
class Estimate:
    ticker: str
    yes_prob: float
    confidence: str
    should_trade: bool
    skip_reason: str
    reasoning: str
    key_sources: list[str] = field(default_factory=list)
    market_yes_price: int | None = None   # mid-price in cents when researched
    researched_at: str = ""
    model: str = DEFAULT_MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe_market(market: dict, extra_context: list[str] | None = None) -> str:
    """Render the market fields Claude needs into a compact prompt block."""
    lines = [f"Ticker: {market.get('ticker')}", f"Title: {market.get('title')}"]
    for key, label in (("yes_sub_title", "YES means"), ("subtitle", "Subtitle"), ("rules_primary", "Rules"),
                       ("rules_secondary", "Additional rules"), ("event_ticker", "Event"), ("category", "Category"),
                       ("close_time", "Close time (UTC)"), ("expected_expiration_time", "Expected settlement (UTC)")):
        if market.get(key):
            lines.append(f"{label}: {market[key]}")
    yb, ya = price_cents(market, "yes_bid"), price_cents(market, "yes_ask")
    if yb is not None or ya is not None:
        last = price_cents(market, "last_price")
        lines.append(f"Current YES market: bid {_fmt(yb)}c / ask {_fmt(ya)}c (last trade {_fmt(last)}c, volume {volume(market):.0f} contracts)")
    lines.append(f"Now (UTC): {_now_iso()}")
    for ctx in extra_context or []:
        lines.append(f"Context: {ctx}")
    return "\n".join(lines)


def _fmt(cents: float | None) -> str:
    return "?" if cents is None else (f"{cents:.0f}" if float(cents).is_integer() else f"{cents:.1f}")


def mid_price(market: dict) -> int | None:
    mid = mid_cents(market)
    return round(mid) if mid is not None else None


class ResearchFailed(RuntimeError):
    pass


class ResearchRefused(ResearchFailed):
    pass


class Researcher:
    """Wraps an Anthropic client. Pass ``client`` explicitly in tests."""

    def __init__(self, client=None, model: str = DEFAULT_MODEL, max_searches: int = 8, effort: str = "high"):
        if client is None:
            import anthropic  # imported lazily so the CLI works without the SDK for non-autopilot commands
            client = anthropic.Anthropic(timeout=300.0, max_retries=1)
        self.client = client
        self.model = model
        self.max_searches = max_searches
        self.effort = effort

    # ------------------------------------------------------------ research

    @staticmethod
    def _searches(resp) -> int:
        stu = getattr(getattr(resp, "usage", None), "server_tool_use", None)
        return int(getattr(stu, "web_search_requests", 0) or 0) + int(getattr(stu, "web_fetch_requests", 0) or 0)

    def _research_text(self, market: dict, extra_context: list[str] | None = None) -> tuple[str, int, int, int]:
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": self.max_searches},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": min(self.max_searches, 3), "max_content_tokens": 4000},
        ]
        messages: list[dict] = [{"role": "user", "content": "Research this Kalshi market and estimate the probability of YES.\n\n" + describe_market(market, extra_context)}]
        in_tok = out_tok = searches = 0
        for _ in range(6):  # pause_turn continuations
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=RESEARCH_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                tools=tools,
                messages=messages,
            )
            in_tok += getattr(resp.usage, "input_tokens", 0) or 0
            out_tok += getattr(resp.usage, "output_tokens", 0) or 0
            searches += self._searches(resp)
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason == "refusal":
                raise ResearchRefused(getattr(getattr(resp, "stop_details", None), "explanation", "") or "refused")
            text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            return text, in_tok, out_tok, searches
        raise ResearchFailed("research did not finish after repeated pause_turn continuations")

    def _extract(self, market: dict, research_text: str) -> tuple[dict, int, int]:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4000,
            system=EXTRACT_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ESTIMATE_SCHEMA}},
            messages=[{"role": "user", "content": f"Market:\n{describe_market(market)}\n\nResearch write-up:\n{research_text}"}],
        )
        if resp.stop_reason == "refusal":
            raise ResearchRefused("extraction refused")
        text = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return json.loads(text), getattr(resp.usage, "input_tokens", 0) or 0, getattr(resp.usage, "output_tokens", 0) or 0

    def estimate(self, market: dict, extra_context: list[str] | None = None) -> Estimate:
        text, i1, o1, searches = self._research_text(market, extra_context)
        if not text.strip():
            raise ResearchFailed("empty research response")
        data, i2, o2 = self._extract(market, text)
        prob = min(max(float(data["yes_prob"]), 0.0), 1.0)
        cost = estimate_cost_usd(self.model, i1 + i2, o1 + o2, searches)
        return Estimate(
            ticker=market["ticker"], yes_prob=prob, confidence=data["confidence"], should_trade=bool(data["should_trade"]),
            skip_reason=data.get("skip_reason", ""), reasoning=data.get("reasoning", ""), key_sources=list(data.get("key_sources", [])),
            market_yes_price=mid_price(market), researched_at=_now_iso(), model=self.model,
            input_tokens=i1 + i2, output_tokens=o1 + o2, web_searches=searches, cost_usd=round(cost, 4),
        )


# ------------------------------------------------------------------- cache

class ResearchCache:
    """Estimates keyed by ticker, persisted as JSON, with a time-to-live.

    Research spend is kept in its own per-day ledger so that dropping a stale
    estimate does not make today's budget look less used than it was.
    """

    def __init__(self, path: Path, ttl_hours: float):
        self.path = path
        self.ttl = ttl_hours * 3600
        self._data: dict[str, dict] = {}
        self._spend: dict[str, float] = {}
        try:
            raw = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            raw = {}
        if isinstance(raw, dict) and "estimates" in raw:
            self._data = dict(raw.get("estimates") or {})
            self._spend = {k: float(v) for k, v in (raw.get("spend") or {}).items()}
        elif isinstance(raw, dict):                      # legacy flat file: rebuild the ledger from the estimates
            self._data = raw
            for d in raw.values():
                day = str(d.get("researched_at", ""))[:10]
                if day:
                    self._spend[day] = self._spend.get(day, 0.0) + float(d.get("cost_usd") or 0)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"estimates": self._data, "spend": self._spend}, indent=2))

    def get(self, ticker: str) -> Estimate | None:
        d = self._data.get(ticker)
        if not d:
            return None
        try:
            age = time.time() - datetime.fromisoformat(d["researched_at"]).timestamp()
        except (KeyError, ValueError):
            return None
        return Estimate.from_dict(d) if age < self.ttl else None

    def put(self, est: Estimate) -> None:
        if not est.researched_at:
            est.researched_at = _now_iso()
        day = est.researched_at[:10]
        self._spend[day] = round(self._spend.get(day, 0.0) + float(est.cost_usd or 0), 4)
        self._data[est.ticker] = est.to_dict()
        self._save()

    def all(self) -> list[Estimate]:
        return [Estimate.from_dict(d) for d in self._data.values()]

    def drop(self, ticker: str) -> None:
        if self._data.pop(ticker, None) is not None:
            self._save()

    def spent_today_usd(self) -> float:
        """Estimated research spend today (UTC), including estimates since dropped as stale."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._spend.get(today, 0.0)


def journal(path: Path, record: dict) -> None:
    """Append one JSON line describing a research result or trade decision."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps({"at": _now_iso(), **record}, default=str) + "\n")
