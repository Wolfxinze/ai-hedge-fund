---
id: PRD-20261017-bit-pool-muse-hedge-lab
status: DRAFT
owner: Woo
created: 2026-10-17
---

# Bit Pool + Muse Hedge Lab rename + daily simulation

## Problem
Woo's "bit pool" (11 real holdings) was built by Lotus inside Muse's cloud workspace
(`data/pools/ken_bits.csv`, never pushed). The fork has no holdings concept, still carries
upstream branding, and README points at a LICENSE file upstream deleted. There is no daily,
verifiable run of *this* system against Woo's real positions.

## Solution
1. `src/bit_pool/` — holdings pool + paper-trading simulation (research only, never orders).
2. Rename to **Muse Hedge Lab**; MIT LICENSE keeping virattt's notice + ours; README rewritten.
3. Daily Claude scheduled task: run the sim + LLM-free committee after US close, write report,
   push to PRIVATE repo `Wolfxinze/muse-hedge-lab-reports`; a second Muse account's agent
   (not Lotus) pulls and cross-checks.

## Design
- Holdings CSV `ticker,shares,baseline_price,baseline_date,cost_basis` (cost optional).
  Real file `data/pools/bit_pool.csv` is **gitignored** (public repo); committed template
  `data/pools/bit_pool.example.csv` with fake numbers. Override via `--holdings` / `BIT_POOL_PATH`.
- Optional trade ledger `data/pools/bit_pool_trades.csv` (gitignored)
  `date,ticker,side,shares,price,note` — Woo mirrors real fills so the sim tracks reality.
  Overselling fails loud.
- `simulate_day(holdings, trades, as_of, fetch_prices=get_prices)` → frozen snapshot:
  per-position close / prev close / daily % / market value / weight / vs-baseline % /
  unrealized P&L vs cost; pool totals; realized P&L + net cash flow from ledger.
- Alerts (Lotus's rules, constants): |daily| ≥ 5%; vs baseline ≤ −10% warn, ≤ −20% heavy,
  ≥ +20% review; STALE when latest close is > 4 calendar days before `as_of`.
- Fetch failure → position DEGRADED, excluded from totals, listed loudly, exit code 2.
- Committee (optional `--committee {core,full}`): reuses `make_committee_analyzing_flow`
  with injected `run_analysts` → `run_scoring_analysts(model_name, model_provider)`.
  `core` = 5 LLM-free analysts (technical, fundamentals, growth, sentiment, valuation);
  `full` = all 16 via `--provider Ollama --model gemma4:31b` (weekly).
- Output: `reports/bit_pool/YYYY-MM-DD.{json,md}` (gitignored), both carrying
  `research_disclaimer()`; markdown in Chinese+English headings.
- CLI: `python -m src.bit_pool run [--date] [--holdings] [--trades] [--committee core|full]
  [--provider] [--model] [--out]`; exit 0 ok / 2 degraded / 1 error.
- No-trade guard: add bit_pool modules to `src/evals/suites/no_trade.py` scan.

## Design revision 2026-10-17 (Woo): Muse (Browser 4 account) is manager/trader — no Ollama
- The LLM persona layer is the **Muse agent** in the Browser-4 Muse account (not Lotus, not
  local Ollama). Muse pulls the private reports repo daily and writes back:
  - `muse/signals/YYYY-MM-DD.json` = `{"as_of": "...", "signals": {"<analyst_key>": {"<TICKER>":
    {"signal": "bullish|bearish|neutral", "confidence": 0-100, "reasoning": "..."}}}}`
    (keys from `committee_analyst_keys()`; bridge adds the `_agent` suffix).
  - `muse/orders.csv` = cumulative PAPER order ledger (trades-ledger schema) — Muse as
    portfolio manager of a simulated book seeded with Woo's 11 holdings.
- `--committee muse --muse-signals PATH` feeds Muse's signals through the unchanged
  committee aggregation (`run_analysts` injection). Missing ticker → degraded; signals file
  older than 4 days → stale → degraded.
- Two books per day: **real** (Woo's fills ledger) and **muse** (`--trades muse/orders.csv`,
  `--label muse`). Comparing them is the "does the system work" evidence.
- `--committee full` no longer defaults to Ollama; requires explicit `--provider/--model`.
- Real trades stay Woo's: Muse's orders are paper; Woo decides and executes in his broker.

## Anti-scope
No broker connection, no order placement, no personalized advice; no UI; no change to
monitor/committee internals; no GitHub Actions (budget at 100%); no repo rename on GitHub;
docker image names untouched.

## ISC
- [ ] ISC-1 Holdings CSV loads, rejects bad ticker / non-positive shares / bad price loudly.
- [ ] ISC-2 Snapshot math: market value, weights sum to 1, daily %, vs-baseline % exact on fixtures.
- [ ] ISC-3 Each alert threshold fires at boundary and not just inside it.
- [ ] ISC-4 Ledger buy/sell adjusts shares, realized P&L; oversell raises.
- [ ] ISC-5 Fetch failure → degraded, excluded from totals, exit 2.
- [ ] ISC-6 Report JSON+MD always carry the disclaimer.
- [ ] ISC-7 bit_pool modules scanned by no-trade guard; suite green.
- [ ] ISC-8 Real holdings file gitignored; only example committed; no account number anywhere.
- [ ] ISC-9 LICENSE = MIT with both copyright lines; README/pyproject/titles say Muse Hedge Lab.
- [ ] ISC-10 Live run on Woo's 11 holdings produces a report (fresh-context verifier).
- [ ] ISC-11 Claude scheduled task created; private reports repo receives first report.
