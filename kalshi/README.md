# kalshi-trader

A small command line tool that places and manages trades on [Kalshi](https://kalshi.com)
through their public trade API. It talks to the **demo exchange by default**, refuses to
touch real money unless you say `--live`, and runs every order through client-side risk
caps you control. It also includes a simple bot that buys contracts when the market price
is cheaper than the probability you assign, so it can trade for you unattended.

It is deliberately boring: no predictions, no leverage, no cleverness. You decide what
things are worth; the tool executes and keeps you inside the limits you set.

> Kalshi contracts are real money bets. Anything the bot buys can go to zero. Start on the
> demo exchange, keep the risk caps low, and treat the plan file as your opinion, not the tool's.

## Setup

Requires Python 3.11+.

```bash
cd kalshi
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

### API key

The Kalshi phone app and website share one account, but API keys are created on the
website: **kalshi.com → Account → Settings → API Keys → Create**. Kalshi shows the key id
and lets you download a `.pem` private key once. Save it somewhere private, for example
`~/.config/kalshi-trader/kalshi.pem`, and never commit it (this folder's `.gitignore`
ignores `*.pem`).

For practice, create a separate demo account at **demo.kalshi.co** and make an API key
there. Demo keys only work with `--env demo`, production keys only with `--env prod`.

### Configuration

Either export environment variables:

```bash
export KALSHI_ENV=demo                       # or prod
export KALSHI_API_KEY_ID=...                 # from the API Keys page
export KALSHI_PRIVATE_KEY=~/.config/kalshi-trader/kalshi.pem
```

or copy `config.example.toml` to `~/.config/kalshi-trader/config.toml` and fill it in.
Environment variables win over the file, and `--env` on the command line wins over both.

## Usage

```bash
python -m kalshi_trader --help

python -m kalshi_trader status                          # is the exchange open?
python -m kalshi_trader markets --search "temperature"  # find tickers
python -m kalshi_trader markets --series KXHIGHNY       # everything in a series
python -m kalshi_trader market KXHIGHNY-25SEP28-B70     # quotes + order book
python -m kalshi_trader balance
python -m kalshi_trader positions
python -m kalshi_trader orders                          # resting orders
python -m kalshi_trader fills

# Limit order: buy 5 YES at 42 cents. Asks for confirmation unless -y is given.
python -m kalshi_trader buy KXHIGHNY-25SEP28-B70 --side yes --count 5 --price 42
# Market order that expires in 10 minutes if it does not fill
python -m kalshi_trader buy SOME-TICKER --side no --count 2 --market --ttl 600
python -m kalshi_trader sell SOME-TICKER --side yes --count 5 --price 60
python -m kalshi_trader cancel <order_id>
python -m kalshi_trader cancel --all                    # kill switch
```

Prices are in cents (1 to 99). YES and NO are two views of the same contract: buying NO
at 40 is the same exposure as selling YES at 60. Add `--json` to any command for raw output.

### Safety rails

| Rail | What it does |
|---|---|
| Demo by default | Nothing reaches the real exchange unless `KALSHI_ENV=prod` or `--env prod`. |
| `--live` | Even on prod, `buy`, `sell`, `cancel` and `bot` refuse to run without this flag. |
| Confirmation prompt | Interactive orders ask `Send? [y/N]` unless you pass `-y`. |
| `--dry-run` | Prints the exact order that would be sent and sends nothing. |
| Risk caps | Per-order worst-case cost, per-market position size, resting-order count, daily new exposure, and a price band. Configured in `[risk]`; a refused order exits with code 3. |
| Daily ledger | Worst-case cost of every order you send is recorded in `state.json` per exchange per UTC day to enforce the daily cap across runs. |

`--force` overrides a risk refusal. It exists for emergencies such as closing a position;
don't use it routinely.

## Letting it trade for you: the bot

Write a plan file with your probability for each market you care about
(see `plan.example.json`):

```json
{
  "edge_cents": 5,
  "max_contracts": 10,
  "markets": {
    "KXHIGHNY-25SEP28-B70": { "yes_prob": 0.62 },
    "SOME-OTHER-TICKER":    { "yes_prob": 0.15, "edge_cents": 8, "max_contracts": 5 }
  }
}
```

Then run it:

```bash
python -m kalshi_trader bot --plan plan.json --dry-run --once   # see what it would do
python -m kalshi_trader bot --plan plan.json -y                 # demo, every 60s
python -m kalshi_trader --env prod --live bot --plan plan.json -y --interval 120
```

On each pass, for each market, the bot reads the order book and:

1. Works out the best price to buy YES and to buy NO right now.
2. If buying YES costs at least `edge_cents` less than your `yes_prob`, or buying NO costs
   at least `edge_cents` less than `1 - yes_prob`, it places a **limit order at the quoted
   ask** for as many contracts as it takes to reach `max_contracts` on that side.
3. Runs the order through the same risk caps as a manual order. Refused orders are logged
   and skipped, and the bot moves on.

It never sells, never chases, and never trades a market that isn't in the plan. Update the
plan file whenever your view changes; it is re-read only at start, so restart the bot after
editing. Stop it with Ctrl-C, then `cancel --all` if you want to pull resting orders.

To keep it running on a server, wrap the `bot` command in `tmux`, `systemd`, or `nohup`.

## Layout

```
kalshi/
├── kalshi_trader/
│   ├── auth.py        RSA-PSS request signing (KALSHI-ACCESS-* headers)
│   ├── client.py      REST client: markets, order book, portfolio, orders
│   ├── config.py      env/TOML settings and RiskLimits
│   ├── risk.py        pre-trade checks and the daily spend ledger
│   ├── strategy.py    fair-value plan loader and decision logic
│   └── cli.py         argparse commands
├── tests/             pytest suite (signing, client, risk, strategy, CLI)
├── config.example.toml
├── plan.example.json
└── requirements.txt
```

Run the tests with `python -m pytest` from this folder.

## Notes and limits

* Built against Kalshi trade API v2 (`https://api.elections.kalshi.com/trade-api/v2`, demo at
  `https://demo-api.kalshi.co/trade-api/v2`). If Kalshi renames a field the tables may show
  `-`; use `--json` to see the raw response.
* Kalshi rate-limits API keys. The client retries 429s a couple of times; keep the bot
  interval at 30 seconds or more.
* This is not a market-making or arbitrage bot and has no view of its own. If you want it to
  make money you have to be right about probabilities more often than the market is.
