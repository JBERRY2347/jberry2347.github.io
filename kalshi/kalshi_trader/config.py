"""Configuration: environment selection, credentials and risk limits.

Everything can come from environment variables, a TOML file, or CLI flags.
Precedence is CLI flag > environment variable > config file > default.

Environment variables:

    KALSHI_ENV              "demo" (default) or "prod"
    KALSHI_API_KEY_ID       API key id from the Kalshi settings page
    KALSHI_PRIVATE_KEY      path to the PEM private key file
    KALSHI_CONFIG           path to a TOML config file (default ~/.config/kalshi-trader/config.toml)
    KALSHI_STATE            path of the state file; the research cache and journal live beside it
    ANTHROPIC_API_KEY       needed by the autopilot's research step (read by the Anthropic SDK)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOSTS = {
    "demo": "https://demo-api.kalshi.co",
    "prod": "https://api.elections.kalshi.com",
}

DEFAULT_CONFIG_PATH = Path("~/.config/kalshi-trader/config.toml")
DEFAULT_STATE_PATH = Path("~/.config/kalshi-trader/state.json")


@dataclass
class RiskLimits:
    """Hard caps enforced client-side before any order is sent.

    All money amounts are in cents. A value of 0 disables that particular cap.
    """

    max_order_cost_cents: int = 2_000        # $20 per order
    max_position_contracts: int = 100        # per market, after this order
    max_open_orders: int = 20                # resting orders across all markets
    max_daily_spend_cents: int = 10_000      # $100 of new exposure per UTC day
    min_price_cents: int = 2                 # refuse to trade contracts priced below this
    max_price_cents: int = 98                # or above this

    @classmethod
    def from_mapping(cls, m: dict) -> "RiskLimits":
        allowed = {k: v for k, v in m.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


@dataclass
class Settings:
    env: str = "demo"
    api_key_id: str | None = None
    private_key_path: str | None = None
    risk: RiskLimits = field(default_factory=RiskLimits)
    state_path: Path = DEFAULT_STATE_PATH
    autopilot: dict = field(default_factory=dict)   # raw [autopilot] table; parsed by autopilot.AutopilotSettings

    @property
    def state_dir(self) -> Path:
        return self.state_path.parent

    @property
    def research_cache_path(self) -> Path:
        return self.state_dir / f"research-{self.env}.json"

    @property
    def journal_path(self) -> Path:
        return self.state_dir / f"journal-{self.env}.jsonl"

    @property
    def host(self) -> str:
        try:
            return HOSTS[self.env]
        except KeyError:
            raise ValueError(f"unknown environment {self.env!r}; use 'demo' or 'prod'") from None

    @property
    def is_live(self) -> bool:
        return self.env == "prod"

    def require_credentials(self) -> None:
        missing = []
        if not self.api_key_id:
            missing.append("KALSHI_API_KEY_ID")
        if not self.private_key_path:
            missing.append("KALSHI_PRIVATE_KEY")
        if missing:
            raise SystemExit(
                "missing credentials: set " + " and ".join(missing)
                + " (or put them in the [auth] section of the config file)"
            )


def load_settings(config_path: str | Path | None = None, env_override: str | None = None) -> Settings:
    path = Path(config_path or os.environ.get("KALSHI_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    file_cfg: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            file_cfg = tomllib.load(fh)

    auth = file_cfg.get("auth", {})
    settings = Settings(
        env=env_override or os.environ.get("KALSHI_ENV") or file_cfg.get("env", "demo"),
        api_key_id=os.environ.get("KALSHI_API_KEY_ID") or auth.get("api_key_id"),
        private_key_path=os.environ.get("KALSHI_PRIVATE_KEY") or auth.get("private_key"),
        risk=RiskLimits.from_mapping(file_cfg.get("risk", {})),
        state_path=Path(os.environ.get("KALSHI_STATE") or file_cfg.get("state_path", DEFAULT_STATE_PATH)).expanduser(),
        autopilot=dict(file_cfg.get("autopilot", {})),
    )
    settings.host  # validate env early
    return settings
