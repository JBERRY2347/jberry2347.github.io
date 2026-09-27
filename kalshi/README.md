# kalshi-trader

A small command line tool that places and manages trades on [Kalshi](https://kalshi.com)
through their public trade API. It talks to the **demo exchange by default**, refuses to
touch real money unless you say `--live`, and runs every order through client-side risk
caps you control. It includes two ways to trade unattended:

* **bot**: buys contracts when the market price is cheaper than a probability *you* wrote
  down in a plan file.
* **autopilot**: picks liquid markets, has Claude research each one with web search and
  estimate the probability, and trades when that estimate beats the market by a margin.

Execution is deliberately boring: limit orders only, no leverage, hard caps on every axis.

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

## Letting it do the research too: autopilot

If you don't want to pick the numbers yourself, `autopilot` does the whole loop:

1. **Selects markets.** Pulls every open market and keeps the liquid ones (volume, spread)
   that close between 6 hours and a few weeks from now and aren't priced near 0 or 100.
   You can restrict it to certain series or exclude topics in the `[autopilot]` config.
2. **Researches them with Claude.** For each candidate (up to `max_research_per_pass` per
   pass, highest volume first), Claude Opus reads the market rules, searches the web for
   current primary sources, and writes a forecast. A second call extracts a probability, a
   confidence level, the reasoning and the sources into a strict JSON schema. Estimates are
   cached for `research_ttl_hours` so the same market isn't re-researched every pass.
3. **Trades the edge.** Every estimate with at least `min_confidence` becomes a plan entry
   and goes through exactly the same fair-value logic and risk caps as the manual bot. Two
   extra account-level caps apply: `max_total_exposure_cents` across all positions and
   resting orders, and `min_balance_cents`, a cash floor.
4. **Journals everything.** Every estimate, skipped market, and order is appended to
   `journal-<env>.jsonl` next to the state file, so you can see exactly why it did what it did.

```bash
pip install -r requirements.txt            # includes the anthropic SDK
export ANTHROPIC_API_KEY=sk-ant-...        # from console.anthropic.com

python -m kalshi_trader autopilot --once --dry-run      # research + log, no orders
python -m kalshi_trader autopilot -y                    # demo, one pass an hour
python -m kalshi_trader --env prod --live autopilot -y  # real money
python -m kalshi_trader review                          # score past estimates once markets settle
```

`review` compares each recorded estimate with the settled result and prints a Brier score
for the model next to the Brier score the market price would have had at the same moment.
If the model's score isn't lower than the market's after a few dozen settled markets, the
autopilot has no edge and you should stop running it. Run it on demo long enough to see
that number before switching to prod.

**Cost.** Each researched market is one Claude Opus call with up to `max_searches_per_market`
web searches plus a small extraction call, roughly $0.10 to $0.50. With the default 5 markets
per pass and a 12 hour cache, expect a few dollars a day.

**What the prompt asks for.** Claude is told to treat the current market price as a strong
prior, to only diverge when it found specific evidence, and to say when it couldn't find
enough information, in which case the market is skipped. It is not asked to be clever.

### Running it on a schedule with GitHub Actions

`.github/workflows/kalshi-autopilot.yml` runs one autopilot pass every 4 hours using the
settings in `kalshi/autopilot.config.toml`, so it trades without your computer being on.
It is off until you opt in. In the repo go to **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Variable | `KALSHI_AUTOPILOT_ENABLED` | `true` |
| Variable | `KALSHI_ENV` | `demo` to start, `prod` when you trust it |
| Secret | `KALSHI_API_KEY_ID` | key id from Kalshi's API Keys page (demo or prod to match `KALSHI_ENV`) |
| Secret | `KALSHI_PRIVATE_KEY_PEM` | the full contents of the `.pem` file |
| Secret | `ANTHROPIC_API_KEY` | from console.anthropic.com |

Then run it once by hand from the **Actions** tab (**Kalshi autopilot → Run workflow**,
optionally ticking dry run) and read the log. The research cache and journal are carried
between runs with the Actions cache, and each run uploads the journal as an artifact. Edit
`autopilot.config.toml` to tune it; set `KALSHI_AUTOPILOT_ENABLED` to anything but `true` to
pause it.

## Layout

```
kalshi/
├── kalshi_trader/
│   ├── auth.py        RSA-PSS request signing (KALSHI-ACCESS-* headers)
│   ├── client.py      REST client: markets, order book, portfolio, orders
│   ├── config.py      env/TOML settings and RiskLimits
│   ├── risk.py        pre-trade checks and the daily spend ledger
│   ├── strategy.py    fair-value plan loader and decision logic
│   ├── research.py    Claude + web search -> probability estimate, cache, journal
│   ├── autopilot.py   market selection, exposure caps, the research-then-trade pass
│   └── cli.py         argparse commands
├── tests/             pytest suite (signing, client, risk, strategy, research, autopilot, CLI)
├── config.example.toml
├── autopilot.config.toml   settings used by the scheduled GitHub Actions run
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
