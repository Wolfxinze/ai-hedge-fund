# Muse Hedge Lab

Muse Hedge Lab is a multi-agent AI stock **research** lab maintained by Wolfxinze. It is a fork of [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), extended with observing pools, committee monitoring, bottleneck research and a daily paper-trading simulation of your own holdings (Bit Pool). This project is for **educational and research** purposes only and is not intended for real trading or investment.

This system employs several agents working together:

1. Aswath Damodaran Agent - The Dean of Valuation, focuses on story, numbers, and disciplined valuation
2. Ben Graham Agent - The godfather of value investing, only buys hidden gems with a margin of safety
3. Bill Ackman Agent - An activist investor, takes bold positions and pushes for change
4. Cathie Wood Agent - The queen of growth investing, believes in the power of innovation and disruption
5. Charlie Munger Agent - Warren Buffett's partner, only buys wonderful businesses at fair prices
6. Michael Burry Agent - The Big Short contrarian who hunts for deep value
7. Mohnish Pabrai Agent - The Dhandho investor, who looks for doubles at low risk
8. Peter Lynch Agent - Practical investor who seeks "ten-baggers" in everyday businesses
9. Phil Fisher Agent - Meticulous growth investor who uses deep "scuttlebutt" research 
10. Rakesh Jhunjhunwala Agent - The Big Bull of India
11. Stanley Druckenmiller Agent - Macro legend who hunts for asymmetric opportunities with growth potential
12. Warren Buffett Agent - The oracle of Omaha, seeks wonderful companies at a fair price
13. Valuation Agent - Calculates the intrinsic value of a stock and generates trading signals
14. Sentiment Agent - Analyzes market sentiment and generates trading signals
15. Fundamentals Agent - Analyzes fundamental data and generates trading signals
16. Technicals Agent - Analyzes technical indicators and generates trading signals
17. Risk Manager - Calculates risk metrics and sets position limits
18. Portfolio Manager - Makes final trading decisions and generates orders

<img width="1042" alt="Screenshot 2025-03-22 at 6 19 07 PM" src="https://github.com/user-attachments/assets/cbae3dcf-b571-490d-b0ad-3f0f035ac0d4" />

Note: the system does not actually make any trades.

## Disclaimer

This project is for **educational and research purposes only**.

- **No order placement.** Muse Hedge Lab never connects to a brokerage and never places, routes or executes orders. Any "trade" it shows is a simulated signal on paper.
- **This is not investment advice.** Nothing it outputs is a recommendation to buy, sell or hold any security, and no guarantees are provided.
- **You place any real trades yourself.** The owner decides on, and places, any real trade in their own brokerage account, at their own risk.
- Not intended for real trading or investment
- The authors and contributors assume no liability for financial losses
- Consult a licensed financial advisor for investment decisions
- Past performance does not indicate future results

By using this software, you agree to use it solely for learning and research purposes.

## Table of Contents
- [What Muse Hedge Lab adds](#what-muse-hedge-lab-adds)
- [Bit Pool — daily paper-trading simulation of your holdings](#bit-pool--daily-paper-trading-simulation-of-your-holdings)
- [How to Install](#how-to-install)
- [How to Run](#how-to-run)
  - [⌨️ Command Line Interface](#️-command-line-interface)
  - [🖥️ Web Application](#️-web-application)
- [How to Contribute](#how-to-contribute)
- [Feature Requests](#feature-requests)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## What Muse Hedge Lab adds

On top of the upstream agents, backtester and web app, this fork adds:

- **Observing Pools** (`src/observing_pools`) — five innovation platforms (Artificial Intelligence, Robotics, Energy Storage, Blockchain Technology, Multiomic Sequencing) with universe classification and a composite ranking per pool.
- **Monitoring + committee flow** (`src/monitoring`) — runs the analyst committee over a watch list and records the outcome.
- **Serenity bottleneck research** (`src/serenity`) — evidence gathering and grading for supply-chain bottleneck research.
- **Scheduler** (`src/scheduler`) — scheduled jobs for the pools and monitors.
- **Evals and safety rails** (`src/evals`) — eval suites including a no-trade guard and a disclaimer check, plus classification, evidence, injection, scoring and SSRF suites.
- **Optional local LLM providers** — the agents can run against any supported provider, including local models (for example via Ollama); none is a default.
- **Bit Pool** (new) — a daily paper-trading simulation of your own holdings; see below.

## Bit Pool — daily paper-trading simulation of your holdings

Bit Pool tracks the holdings you tell it about, marks them to the latest close each day, raises alerts, and (optionally) asks the analyst committee for a view. It is a **paper simulation only**: it never places orders. If you act on anything, you place the trade yourself in your own brokerage and then log the fill in the trades ledger so the simulation keeps tracking your real account.

The persona layer and portfolio manager are an external AI agent called **Muse**, not a local model. Muse's orders are **simulated (paper) only**; real trades are placed only by the owner.

**US-listed equities only.** The intraday guard (16:15 ET) and the `STALE` rule assume the US stock-market session. Non-US listings and crypto tickers are unsupported: their closes would be judged against the wrong session.

### Holdings file

Every `data/pools/*.csv` except `*.example.csv` is gitignored, so your real holdings and ledgers never get committed. Copy the example and edit it:

```bash
cp data/pools/bit_pool.example.csv data/pools/bit_pool.csv
```

Header (all five columns are required; the `cost_basis` *value* may be left blank when unknown):

```csv
ticker,shares,baseline_price,baseline_date,cost_basis
```

The baseline is the position at the **end of day on `baseline_date`**. A blank `cost_basis` stays unknown: buys do not invent one, unrealized P&L is not shown, and realized P&L on a sell is measured against `baseline_price` and labelled "(vs baseline price — cost unknown)". Every malformed row (missing or extra fields, a non-number, `inf`/`nan`, a bad date or ticker) fails the run with its line number; a file with only a header is an error. UTF-8 with or without a BOM is accepted. A row may leave out the trailing `cost_basis` entirely (4 fields, no trailing comma) and it reads as blank; any other short row is an error.

**Quote numbers that contain commas** (`"1,234.50"`), or write them without the separator (`1234.50`). An unquoted thousands separator splits one number into two fields, which shows up as an extra field and fails the run.

Override the path with the `BIT_POOL_PATH` environment variable or the `--holdings` flag. The default `data/pools/bit_pool.csv` is resolved from the repo root, not the current directory, so a cron job started elsewhere still finds it.

### Trades ledger (optional)

`data/pools/bit_pool_trades.csv` records real fills so the simulation follows the account. `side` is `buy` or `sell`.

```csv
date,ticker,side,shares,price,note
```

A row may leave out the trailing `note` entirely (5 fields, no trailing comma). Quote numbers that contain commas here too: unquoted, `2026-10-10,AAPL,buy,10,1,234.50` would read as price `1` with note `234.50`, so a note that is only a number is rejected with `note looks like a number on line N — quote numbers containing commas`.

Path precedence: `--trades` beats the `BIT_POOL_TRADES_PATH` environment variable beats `data/pools/bit_pool_trades.csv` (resolved from the repo root, not the current directory). Only that implicit default may be absent (meaning no trades). A path you name explicitly, with the flag or the environment variable, must exist, or the run exits `1` with `trades ledger not found: <path>`, so a typo cannot silently drop your fills. Trades for a ticker in the holdings file dated **on or before its `baseline_date` are rejected** (they are already in the baseline), and so is a `--date` before any `baseline_date`. Selling more than you hold, including any sell of a ticker you do not hold, is an error. The report shows `Trades applied: N (source)`; the JSON has `trades_source` and `trades_applied`.

### Run it

```bash
python -m src.bit_pool run [--date YYYY-MM-DD] [--holdings PATH] [--trades PATH] [--committee {none,core,full,muse}] [--provider NAME] [--model NAME] [--muse-signals PATH] [--label NAME] [--max-weight PCT] [--out DIR]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--date` | today (local date) | As-of date to simulate (`YYYY-MM-DD`); a future date exits `1` |
| `--holdings` | `data/pools/bit_pool.csv` (or `BIT_POOL_PATH`) | Holdings CSV |
| `--trades` | `BIT_POOL_TRADES_PATH`, else `data/pools/bit_pool_trades.csv` | Trades ledger CSV; must exist when named (only the implicit default may be absent) |
| `--committee` | `none` | `none` = no analysts; `core` = 5 LLM-free analysts (technical, fundamentals, growth, sentiment, valuation); `full` = all 16 analysts (needs `--provider` and `--model`); `muse` = the Muse agent's signals file |
| `--provider` | none | LLM provider; required for `full` (there is no default provider) |
| `--model` | none | LLM model; required for `full` (there is no default model) |
| `--muse-signals` | `MUSE_SIGNALS_PATH` | For `muse`: a signals file, or a directory of `YYYY-MM-DD.json` files (the newest dated on or before `--date` is used) |
| `--label` | `real` | Book name (`^[a-z0-9_-]{1,32}$`): `real` = the owner's fills, `muse` = Muse's paper orders |
| `--max-weight` | off | Policy alert `OVER_WEIGHT` when a priced position is more than `PCT`% of equity; `PCT` must be in (0, 100], anything else (0, -5, 101, `abc`) exits `1` |
| `--out` | `reports/bit_pool` | Output directory |

Outputs `<out>/<date>.json` and `<out>/<date>.md`, both carrying the research-only disclaimer. Each file is rendered in full and then moved into place, so a failed run never leaves a half-written pair. The JSON records `book` (the label), `committee_source` (`core`, `full`, `muse` or null), `committee_signals_as_of`, `prices_as_of`, `partial` and `excluded_tickers`; the markdown title names the book.

**Run after the US close.** Before 16:15 ET the current day's bar is still forming, so it is ignored and the previous close is used. The report always shows which close it used: a `Close date 收盘日` column per position and a `Prices as of 价格日期` line (marked "latest available close; as-of date has no close yet" when it is older than `--date`). If some positions could not be priced, the summary starts with `PARTIAL — excludes X, Y` and the totals cover only the priced positions; if none could be priced the totals are `n/a` (null in JSON), never 0.

**Equity, cash and initial capital.** A book is self-funded from the holdings it starts with: no new money is added, and a sell turns shares into cash. `Total value 总市值` counts positions only, so on its own a sell would look like a loss. The summary therefore also shows, right after it:

- `账户权益 Equity (positions + cash)`: `equity = total_value + cash`; selling at the close leaves it unchanged.
- `现金 Cash`: sell proceeds minus buy cost (the ledger's net cash flow), starting at 0.
- `初始资本 Initial capital`: `sum(shares * baseline_price)` over the holdings file, before any trade; it stays the same every day.
- `权益收益 Equity vs initial`: `(equity / initial_capital - 1) * 100`.

The JSON has `initial_capital`, `cash`, `equity` and `equity_vs_initial_pct` (`equity` and `equity_vs_initial_pct` are null when no position could be priced). On a partial pool both equity lines are marked `(PARTIAL)`, because equity then covers only the priced positions plus cash. On those days `equity_vs_initial_pct` is also null and shows `n/a (PARTIAL)`: initial capital covers every position, so comparing it with an equity that leaves some out would show a loss that is really a missing price. **Compare books by `equity_vs_initial_pct`**, not by `total_value` or `vs_baseline_pct`, which ignore cash.

The positions table has two weight columns. `Weight (positions)` is the position's share of the positions-only total (JSON `weight`). `Weight (equity) 占权益` is its share of equity, positions plus cash (JSON `weight_of_equity`; null when the position is unpriced or equity is zero or negative). `OVER_WEIGHT` uses the equity weight.

The committee table shows each ticker's coverage (valid analysts / committee size, e.g. `4/5`). An analyst whose output was unusable is shown as `neutral (DEGRADED)` (JSON: `{"signal": "neutral", "degraded": true}`) and is left out of the mean score.

### Muse committee and the two books

Muse is a manager/trader agent that runs in a separate Muse account. Each day it reads the Bit Pool reports from a private reports repo and writes two things back into that repo:

- `muse/signals/YYYY-MM-DD.json`: its analyst signals, consumed by `--committee muse`.
- `muse/orders.csv`: **paper** orders, appended in the same schema as the trades ledger (`date,ticker,side,shares,price,note`). Every order must be dated **after** the holdings' `baseline_date`; an order on or before it is rejected as already included in the baseline.

Signals file schema (analyst keys are the bare committee keys such as `warren_buffett` or `technical_analyst`, with no `_agent` suffix):

```json
{
  "as_of": "YYYY-MM-DD",
  "signals": {
    "<analyst_key>": {
      "<TICKER>": {"signal": "bullish|bearish|neutral", "confidence": 0-100, "reasoning": "optional"}
    }
  }
}
```

A malformed file (bad JSON, unknown analyst, bad ticker, signal or confidence) exits `1`. If no file is dated on or before the run date, the newest one is more than 4 days old, or it has no signals, every committee row is degraded and the run exits `1` (the report is still written). A file dated after the run date exits `1`. A ticker Muse gave no signal for is reported as insufficient evidence (degraded, exit `2`).

The daily run produces two books from the same holdings. The two books **must** use different `--out` directories: the CLI refuses to overwrite a report of a different book (`refusing to overwrite book X with book Y`, exit `1`, nothing written).

```bash
# Book 1: the owner's actual fills
python -m src.bit_pool run --label real --committee muse --muse-signals <reports>/muse/signals
# Book 2: Muse's paper orders
python -m src.bit_pool run --label muse --trades <reports>/muse/orders.csv --committee muse \
  --muse-signals <reports>/muse/signals --out reports/bit_pool/muse
```

Comparing the two books is how the system is verified. Muse's orders are simulated and are never sent to a broker; real trades are placed only by the owner.

### Alert rules

| Condition | Alert |
|-----------|-------|
| \|daily move\| >= 5% | `DAILY_MOVE` |
| vs baseline <= -10% | `BASELINE_WARN` |
| vs baseline <= -20% | `BASELINE_HEAVY` (replaces `BASELINE_WARN`) |
| vs baseline >= +20% | `BASELINE_REVIEW` |
| last close more than 4 days before the latest settled session allowed (`--date`, or earlier when the intraday guard drops it) | `STALE` (counts as degraded: exit `2`) |
| position's close date differs from the pool's `Prices as of` date | `MIXED_DATE` |
| `--max-weight PCT` given and the position's market value is more than `PCT`% of equity (exactly `PCT`% does not fire) | `OVER_WEIGHT` (policy alert; does not change the exit code) |
| cash below -0.005 (buys spent more than the book's own sell proceeds; the self-funding limit is breached) | `NEGATIVE_CASH`, a pool-level alert reported with ticker `(pool)` (policy alert; does not change the exit code) |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | ok |
| `2` | degraded: some (not all) positions or committee tickers could not be priced/scored, or a `STALE` close |
| `1` | error: bad CSV, oversell, trade on/before `baseline_date`, missing named trades ledger, future `--date`, bad arguments, a different book already in `--out`; also when **every** position or **every** committee ticker is degraded (systemic, e.g. the price provider is down; the report is still written) |

## How to Install

Before you can run Muse Hedge Lab, you'll need to install it and set up your API keys. These steps are common to both the full-stack web application and command line interface.

### 1. Clone the Repository

```bash
git clone https://github.com/Wolfxinze/ai-hedge-fund.git
cd ai-hedge-fund
```

### 2. Set up API keys

Create a `.env` file for your API keys:
```bash
# Create .env file for your API keys (in the root directory)
cp .env.example .env
```

Open and edit the `.env` file to add your API keys:
```bash
# For running LLMs hosted by openai (gpt-4o, gpt-4o-mini, etc.)
OPENAI_API_KEY=your-openai-api-key

# For getting financial data to power the hedge fund.
# yfinance is the default data provider and does not require an API key.
FINANCIAL_DATA_PROVIDER=yfinance

# Optional: use this only if FINANCIAL_DATA_PROVIDER=financialdatasets.
FINANCIAL_DATASETS_API_KEY=your-financial-datasets-api-key
```

**Important**: You must set at least one LLM API key (e.g. `OPENAI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY`) for the hedge fund to work. 

**Financial Data**: The default provider is `yfinance`, powered by Yahoo Finance data, and does not require an API key. If you want to use Financial Datasets instead, set `FINANCIAL_DATA_PROVIDER=financialdatasets` and configure `FINANCIAL_DATASETS_API_KEY` in the `.env` file.

## How to Run

### ⌨️ Command Line Interface

You can run Muse Hedge Lab directly via terminal. This approach offers more granular control and is useful for automation, scripting, and integration purposes.

<img width="992" alt="Screenshot 2025-01-06 at 5 50 17 PM" src="https://github.com/user-attachments/assets/e8ca04bf-9989-4a7d-a8b4-34e04666663b" />

#### Quick Start

1. Install Poetry (if not already installed):
```bash
curl -sSL https://install.python-poetry.org | python3 -
```

2. Install dependencies:
```bash
poetry install
```

#### Run the agents
```bash
poetry run python src/main.py --ticker AAPL,MSFT,NVDA
```

You can also specify a `--ollama` flag to run the agents using local LLMs.

```bash
poetry run python src/main.py --ticker AAPL,MSFT,NVDA --ollama
```

You can optionally specify the start and end dates to make decisions over a specific time period.

```bash
poetry run python src/main.py --ticker AAPL,MSFT,NVDA --start-date 2024-01-01 --end-date 2024-03-01
```

#### Run the Backtester
```bash
poetry run python src/backtester.py --ticker AAPL,MSFT,NVDA
```

**Example Output:**
<img width="941" alt="Screenshot 2025-01-06 at 5 47 52 PM" src="https://github.com/user-attachments/assets/00e794ea-8628-44e6-9a84-8f8a31ad3b47" />


Note: The `--ollama`, `--start-date`, and `--end-date` flags work for the backtester, as well!

### 🖥️ Web Application

The new way to run Muse Hedge Lab is through the web application that provides a user-friendly interface. This is recommended for users who prefer visual interfaces over command line tools.

Please see detailed instructions on how to install and run the web application [here](app/README.md).

<img width="1721" alt="Screenshot 2025-06-28 at 6 41 03 PM" src="https://github.com/user-attachments/assets/b95ab696-c9f4-416c-9ad1-51feb1f5374b" />


## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

**Important**: Please keep your pull requests small and focused.  This will make it easier to review and merge.

## Feature Requests

If you have a feature request, please open an [issue](https://github.com/Wolfxinze/ai-hedge-fund/issues) and make sure it is tagged with `enhancement`.

## Acknowledgements

Muse Hedge Lab is built on [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) by Virat Singh, which provides the original investor agents, backtester and web application. Thank you to the upstream author and contributors.

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details. Copyright (c) 2024 Virat Singh (original ai-hedge-fund) and Copyright (c) 2026 Wolfxinze (Muse Hedge Lab modifications).
