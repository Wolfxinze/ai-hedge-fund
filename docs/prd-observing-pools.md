# PRD — Innovation Observing Pools

> **Status:** Shipped on `main` (backend Phases 1–11 + Phase 10 UI + monitor committee engine #51, PRs #3 → #54). Research-only feature.
> **Document type:** Reconstructed PRD (see *Provenance* below).
> **Last reconciled against code:** 2026-07-02.

---

## Provenance & how to read this document

This PRD is a **post-hoc reconstruction**, not the original authoring document.

The feature was built from a PRD series (`docs/prd-innovation-observing-pools{,-v2,-v3,-v4}.md`, v4 the build target) that was **validated 4× with PRDValidator but never committed to git** — it lived only in an uncommitted working tree in an earlier session and was subsequently lost. This file reconstructs those requirements from three surviving ground-truth sources:

1. **The shipped code** — verified module-by-module (the authoritative source; where this doc and the code disagree, the code wins).
2. **The git commit / PR history** — PRs #3–#50, whose messages cite the original PRD section numbers (`§9.5`, `§11.5`, `§14`, `§20`, …).
3. **The PAI project-memory phase-log** — the running status doc that tracked the build across sessions.

The original section numbers (`§n`) are **preserved** so the `§`-citations already embedded in the code and commits resolve here. Sections whose original scope could not be recovered from a citation are marked *(reconstructed from code)* or *(original scope uncertain)*. Numbering gaps (e.g. §1–§7, §15, §18) are original sections not independently recoverable; they are summarized where the code implies them and omitted where it does not.

**This doc equating "shipped" with "verified":** every requirement below maps to code that exists on `main`. Delivery is marked ✅ per phase in §21.

---

## §1 Overview

**Innovation Observing Pools** is a research-only subsystem layered onto the ai-hedge-fund codebase. It ranks public securities into per-*platform* "pools" by an innovation-and-quality composite, backs each ranking with auditable evidence (the **Serenity** supply-chain-bottleneck research layer), and lets a user stand up **monitors** that periodically re-run the analysis and emit disclaimer-stamped opportunity reports.

It is **not** a trading system. It places no orders, exposes no buy/sell/quantity affordance, and every product output carries a persisted research disclaimer. The entire surface is loopback / read-only except a single lock-guarded refresh trigger.

## §2 Goals & Non-Goals

**Goals**
- Deterministically classify a candidate universe into innovation platforms (no LLM in the classification path).
- Rank each platform's securities by a reproducible, versioned composite score with a fully inspectable component breakdown.
- Ground every "bottleneck" thesis in **substantiated** evidence from allowlisted primary sources (SEC EDGAR, Federal Register, patents), graded deterministically.
- Let users create/run monitors and receive disclaimer-carrying reports on a schedule.
- Fail **loud**: a data/provider/LLM failure degrades a component and is surfaced — it must never silently manufacture a signal or mask a real one.

**Non-Goals**
- No order placement, position sizing, or trade execution — ever.
- No LLM in the grading, classification, or substantiation paths (judgment-free deterministic gates).
- No secret read-back over the network (API keys are write-only from the client's perspective).
- No external/non-loopback exposure without a recorded counsel sign-off (§19).

## §3 Core invariants (hard constraints)

These are enforced in code and asserted by the eval suite (§11). They override any convenience.

| # | Invariant | Where enforced |
|---|-----------|----------------|
| I1 | **No trade path.** The scoring graph wires no risk/portfolio/execution node; no module in the feature imports a trade path. | `scoring_graph.py`; eval `no_trade` |
| I2 | **Disclaimer on every product output**, from the *stored* string + version — never hard-coded at the edge, never stripped. | `serialize_report`/`serialize_serenity`; DB CHECK; UI `DisclaimerBanner`; eval `disclaimer` |
| I3 | **Loud-fail providers.** A provider fetch failure raises `ProviderFetchError` (genuine empty result still returns falsy) — never a silent `[]`. | `src/data/providers/*`; node-boundary handler |
| I4 | **Degraded ≠ neutral.** A degraded analyst/component is excluded from the mean (scores `None`), never imputed to a rankable `50`. | `agents_bridge.py`, `scoring.py`; eval `scoring` |
| I5 | **Data-unavailable ≠ 0.** A missing REQUIRED component excludes the entry (status `data_unavailable`); the UI renders "—", never `0`. | `pipeline.py`, `lib.ts` |
| I6 | **No secret read-back.** No route returns `key_value`; keys are encrypted at rest; the client never reads a raw key. | `crypto.py`, `schemas.py`; §9.10 |
| I7 | **Evidence is deterministic.** `source_type` and `substantiated` are host-/rule-derived, never LLM- or user-set. | `evidence.py`, `grading.py`; evals `evidence`, `injection` |

## §8 Provider resilience (the loud-fail contract)

*Phase 1 / 1a hardening. Fixes the original must-fixes **X1–X4**.*

- **§8.1 — Model discovery.** Alembic `env.py` + `main.py` register storage models via one aggregator import.
- **§8.2 — Node-boundary degrade handler.** `resilient_analyst_node` (in `src/utils/analysts.py`, applied at the single DRY seam `get_analyst_nodes()`, used by all three graph builders incl. `app/backend/services/graph.py`) catches `ProviderFetchError`, logs loudly, returns valid state, and records the node into `state.data.degraded_analysts`. Real bugs still surface — only `ProviderFetchError` is caught. Granularity is per-node (an analyst that can't fetch is skipped for the run).
- **§8.3 — Safe defaults.** Degraded components map to *neutral/excluded* before scoring, never to a directional signal. (Pairs with the `create_default_response` fix: an LLM failure defaults signal-bearing fields to `neutral` + a degraded flag, never the first `Literal` "bullish".)
- **Provider 3-state.** `yfinance` and `financial_datasets` public methods RAISE on fetch failure; genuine NoData returns falsy `[]`. `exceptions.py` defines `ProviderError`/`ProviderFetchError`/`ProviderAmbiguousError`.
- **Cache.** `src/data/cache.py` stores `(data, fetched_at)` with whole-key TTL eviction (`CACHE_TTL_SECONDS`, default 1 day, `<=0` disables); no negative cache (loud-fail raises rather than caching errors).

**Must-fix map (original X-items):** X1 → PoolLock (§10); X2 → provider chokepoint loud-fail; X3 → (folded into §8.2); X4 → degraded-analyst undercount + degraded-50 ranking bug (§11.2 / I4).

## §9 Feature specification

### §9.4 Candidate universe
Ingested from a seed CSV (`DEFAULT_UNIVERSE = data/universes/ai_seed.csv`, one constant in `pipeline.py` consumed by route + scheduler + CLI). `universe.py` validates/de-dupes tickers, rejects malformed rows, upserts `candidate_securities`.

### §9.5 Deterministic classification
`classify.py` assigns platform labels with confidence ∈ [0,1] — **no LLM**. Curated seed labels win (confidence `0.9`); otherwise keyword match on name/sector/industry (base `0.30` + `0.15`/hit, capped `0.85`). Single-token seeds match **whole-word** (blocks the `'ai'`-in-`'retail'` trap); phrase seeds match substring. Low-confidence stays `candidate` (never auto-promoted).

### §9.6 Serenity bottleneck research
See §11.5 (substantiation) and the Serenity subsystem below. Feeds the `serenity_bottleneck` scoring component, gated by evidence grade.

### §9.7 Monitors
A monitor = a reusable watchlist + analysis-flow config: `tickers`, `granularity` (daily/weekly/monthly/custom), optional `platform_keys`, optional `selected_analysts`, `schedule`. Each run emits one report per ticker, each disclaimer-stamped. CRUD + manual-run via CLI and API (§14).

**Analyzing engine (#51, PR #52).** The **default** analyzing flow is the ai-hedge-fund committee (`monitoring/committee_flow.py`), built from `monitor.selected_analysts` (`None`/`[]` → full committee; mean-score bands ≥60 bullish / ≤40 bearish). The Phase-0 vertical slice used **TradingAgents'** multi-agent debate graph as the engine; that path is **demoted to an injectable adapter** (`src/integrations/tradingagents_adapter.py` + `TradingAgent/tradingagents_runner.py`, process-isolated seam) — retained for tests and optional use, no longer the default.

### §9.8 CLI workflow
`python -m src.observing_pools {init|refresh|inspect}`; `python -m src.serenity {research|discover|apply}`; `python -m src.monitoring {create|run|export|list}`.

### §9.9 Disclaimer chokepoint
Every product output projects through `serialize_report` / `serialize_serenity`, which **refuse a blank/whitespace disclaimer** (`DisclaimerError`). Every UI product surface renders `DisclaimerBanner`. Labels are non-directional (see §12).

### §9.10 Secrets posture (KEY_ENCRYPTION)
*Phase 1b.* API keys encrypted at rest via a tagged Fernet codec (`enc:v1:` prefix): encrypt is **flag-gated** (`KEY_ENCRYPTION`), decrypt is **tag-gated** (mixed plaintext/ciphertext rows coexist — no migration). Master key resolves keyring → `AHF_MASTER_KEY` env → first-run provision (fail-closed probe) → loud `CryptoMasterKeyError`; headless first-run hard-fails (never a silent ephemeral key). Decrypt fails closed. `ApiKeyResponse` drops `key_value` → `is_set` + `masked_tail`. CORS is a loopback allowlist (never `*`). Master-key rotation (`rotate_master_key`) and a re-encrypt sweep script exist (`app/backend/scripts/`); the operator procedure — including the issue #66-A mid-rotation quiesce/restart data-loss warning — is in [`docs/api-key-encryption-runbook.md`](api-key-encryption-runbook.md). Key material never reaches an exception message (logged server-side, raised without it).

## §10 Concurrency, provenance & cost

- **PoolLock** — SQLite has no per-row write lock, so per-platform serialization is a **claim-row** in `pool_locks` (`platform_key` PK, `locked_at`, `locked_by`, `expires_at`, **`fence`** generation token). A refresh atomically claims (INSERT/`UPDATE … WHERE expires_at < now`, by rowcount — no SELECT→UPDATE TOCTOU), runs the long refresh **outside** the lock txn, and releases via a **fenced** delete (stale-but-alive holder release is a no-op → lost-update guard). Different platforms never contend; same-platform second claimant → `PoolLockContendedError`. `busy_timeout=30`; `PoolLockDatabaseLockedError` surfaced. TTL default 3600s.
- **Provenance** — `pool_refresh_runs` records provider, universe source/version, formula version, platform keys, candidate count, `fetch_errors`, `rejected`, `token_cost`, `summary` (JSON), status. Status ∈ {running, complete, partial, error}; PARTIAL is surfaced, not hidden.

## §11 Scoring & evaluation

### §11.1 Pure scoring contract
`scoring.py` — no I/O, no ORM. Deterministic.

### §11.2 Composite formula (verified against `scoring.py`)
Weighted mean of components, weights sum to 1.00:

| Component | Weight | Source axis |
|-----------|-------:|-------------|
| `platform_fit` | **0.25** | classifier confidence (§9.5) |
| `value_investor` | **0.30** | Buffett/Munger/Graham/Pabrai/Fisher/Lynch/Damodaran/Valuation/Fundamentals |
| `innovation_growth` | **0.20** | Cathie Wood + Growth Analyst |
| `risk_adjusted_momentum` | **0.10** | Technical/Sentiment/News/Burry/Druckenmiller − risk haircut |
| `serenity_bottleneck` | **0.15** | Serenity record, gated by evidence grade (§9.6) |

- **REQUIRED** = `{platform_fit, value_investor}` — missing either → entry excluded (`data_unavailable`), not scored.
- **Versioned formulas:** `FORMULA_4COMP = "v3-4comp"` (Phase 5, pre-Serenity — serenity omitted entirely) and `FORMULA_5COMP = "v3-5comp"` (all five; a `None` serenity value is bootstrap-imputed at the pool median — **F2 bootstrap**).
- **Degraded handling (I4):** a degraded analyst is excluded from its component mean; a fully-degraded component scores `None` and excludes the entry from ranking — a degraded read can never outrank a real bearish one.
- *Naming note:* code constants are `v3-*`; the original PRD called them `v4-*`. This is a documented naming drift — the **code constants are authoritative**; do not rename without a migration.

### §11.3 Reproducibility
Composite is deterministic across trials (asserted `pass^k`).

### §11.4 Scoring-only analyst graph
`scoring_graph.py` runs the analyst committee for **scoring**, dropping every risk/portfolio/trade node (I1).

### §11.5 Evidence substantiation (three-gate deterministic check)
`is_substantiated` in `src/serenity/evidence.py`, gates ordered **overlap → numeric → salad** (overlap must be first). All three match on a single normalization seam `_norm` = lowercase + NFKC.

1. **Overlap gate** — excerpt must overlap the claim's unique 3+-char tokens (a 200-OK-but-irrelevant page does not count).
2. **Numeric gate** — every *figure* the claim states must appear in the excerpt. A **figure requires a unit/scale/%/$** (`$2.4B`, `3nm`, `40%`, `μm`); a **bare integer** (year, version, form #, count) is **not** a figure (so ordinary prose isn't falsely rejected). `$2.4B ≡ 2.4 billion ≠ 2.4 trillion`; `40% ≡ 40 percent ≡ 40 pct`; identifier digits excluded (`H100 ≠ 100`).
3. **Anti-stuffing (salad) gate** — a *relevant* excerpt that packs claim terms with **zero function words** (≥8 words) is rejected as fabricated density; genuine dense prose still counts.
   NFKC hardening: full-width digits fold to ASCII; category-`No` chars (superscripts/fractions) are replaced with a **space** before NFKC (a delete would mint/join phantom digits). `substantiation_reason` ∈ {…, `figure_missing`, `keyword_stuffing`} threaded through `classify_reference` so a withheld grade stays auditable.
   *Deliberately deferred (issue #43):* surface-form equality (`$1,200 ≡ 1200`, `10x ≡ 10 times`) and a salad table-header heuristic.

**Evidence grading** (`grading.py`): per-host cap **2** (anti-flooding); source-type weights FILING=3, REGULATORY=3, PATENT=2, EARNINGS=2, NEWS=1, UNVERIFIED=0; grade thresholds `6+ → A`, `4–5 → B`, `2–3 → C`, `1 → D`, `0 → F`. **No LLM grades evidence.**

**Serenity scorecard** — 5 dims each 0–4: supplier_concentration, validation_cycle, capacity_expansion, certification_strictness, purity_precision.

**Eval framework** (`src/evals/`) — Python-native, three-grader taxonomy: **CodeGrader** (deterministic, default), **ModelGrader** (nuance only, offline stub judge — no real LLM in Phase 11), **HumanGrader** (counsel sign-off; `grade()` raises until recorded). Metrics **pass@k** (capability) / **pass^k** (consistency, target 100%). Runs write JSONL transcripts under gitignored `evals_runs/` (repo-root anchored) — **no `eval_results` DB table** (dev/CI artifact). Suites: `classification`, `scoring`, `evidence`, `injection`, `ssrf`, `disclaimer`, `no_trade`. CLI `python -m src.evals`, exit 2 on fail.

## §12 / §20 Compliance & disclaimer (two-layer)
- **Serialization layer** — `serialize_report`/`serialize_serenity` refuse blank/whitespace (incl. `\xa0`) disclaimer or version.
- **Database layer** — CHECK constraints on `opportunity_reports` and `serenity_research_records` (migration `c7e2f1a4b9d6`): `length(trim(disclaimer, ' '||char(9)||char(10)||char(13))) > 0` and same for `disclaimer_version` (closes the empty-string gap NOT NULL alone left; ASCII-whitespace trim charset because bare SQLite `trim()` strips only spaces).
- Disclaimer text is a constant in `src/compliance.py`; version via `DISCLAIMER_VERSION` env (default `"2026-06"`). Labels are **non-directional** (see §12 label set below).
- **§20 export** — `monitoring export` re-projects persisted reports through the same chokepoint; the disclaimer survives a sqlite3 logical `.dump`/restore (asserted).

## §13 / §16 Research UI (Phase 10)
*(§16 original scope uncertain — cited in the Phase-10 commit alongside §13; treated here as the research-only UI-constraints section.)*

Thin, **reuse-first** React layer over the live API on the existing shadcn/ui + Tailwind system — no redesign, no backend file touched. One tab, three panels:
- **Pools** — platform select → ranked top-N table + 5-component breakdown + `composite_formula_version` **verbatim** + per-row degraded flag + expandable per-agent detail + refresh-run provenance (PARTIAL surfaced).
- **Serenity** — ticker lookup → bottleneck records (theme / chain-layer / hypothesis + A–F grade badge + promote/hold/demote + inline disclaimer).
- **Monitors** — list/create/run → `ReportCard`s; the **only** write surface (reaches `run_monitor` → `serialize_report`).

UI invariants: no `/api-keys`/`key_value` read-back; no trade/order affordance; disclaimer persistent + inline from the stored string on every product output; degraded flagged by **text + border** (not colour alone); data-unavailable → "—" not `0`; formula version rendered verbatim. Full **EN / zh-CN** parity (`observingPools.*` keys in both blocks).

## §14 Backend API (read-only + one lock-guarded write)
Bare-dict / `HTTPException` responses (matches existing `observing_pools.py`, not the envelope). Synchronous `def` handlers (FastAPI threadpools them; reuses the tested `refresh_pool_locked` so 409/503 raise synchronously at claim).

**Observing pools / reports**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/innovation-platforms` | list the 5 platforms |
| POST | `/observing-pools/refresh` | lock-guarded refresh (body: `platform_key`, `top_n` 1–200, `dry_run`, `end_date`, `provider_name`); `dry_run` takes no lock. **409** contended / **503** db-locked, never swallowed |
| GET | `/observing-pools/refresh-runs` | run provenance (limit 1–200 default 50; `platform_key`/`status` filters, applied before limit) |
| GET | `/observing-pools/{platform_key}` | ranked pool (404 unknown platform) |
| GET | `/serenity/research/{ticker}` | Serenity records (through `serialize_serenity`) |
| GET | `/opportunity-reports` · `/opportunity-reports/{id}` | reports (through `serialize_report`) |

**Monitors** (`monitors.py`)
| Method | Path | Purpose |
|--------|------|---------|
| GET · POST | `/monitors` | list (limit 1–500) · create (409 dup name; hot-registers scheduler job) |
| GET · PATCH · DELETE | `/monitors/{id}` | get · partial update (hot-reschedule) · delete (204, deactivates job) |
| POST | `/monitors/{id}/run` | manual run; **max 2 concurrent** (`BoundedSemaphore` → 429 over cap); `degraded_count` in response |

**Validation/limits:** ticker regex `^[A-Za-z0-9.\-]{1,16}$` → 422; tickers ≤ 100; `platform_key` ∈ the 5 keys; limits bounded; `selected_analysts` validated against `ANALYST_CONFIG` (lazy import) and — since **#51 (PR #52)** — **load-bearing** in the monitor run path via `committee_flow.py` (`None`/`[]` → full committee); schedule validated against `MONITOR_MIN_INTERVAL_SECONDS` floor (default 3600) → 422.

**Scheduler** (Phase 8) — in-process APScheduler (`BackgroundScheduler`, UTC, `max_workers=2`, `max_instances=1`, `coalesce=True`): a weekly refresh (`OBSERVING_POOL_REFRESH_CRON`, default Mon 08:00) + per-enabled-monitor jobs; wired via `main.py` startup/shutdown (bad cron → app still boots). Heavy scoring stack lazy-imported.

## §17 Workflow / E2E tests
Covered by `tests/observing_pools/test_api_e2e.py`, `tests/monitoring/`, `tests/serenity/`, and the eval suites; mandatory file-backed threaded tests pin the PoolLock and refresh-concurrency atomicity (one 200 + one 409 under concurrent same-platform refresh).

## §19 Counsel sign-off gate
A **recorded human line** (not automated) required before any non-loopback exposure. `record_signoff` (evals) + `src/compliance.py`. This remains the one open, human-only release precondition.

---

## Data model (reconstructed from `src/storage/models/`)

**Observing pools** — `innovation_platforms` (key unique, keywords JSON), `candidate_securities` (ticker unique), `observation_pool_entries` (per-component scores, `composite_score`, `composite_formula_version`, `score_breakdown` JSON, `rank`, `status`, `last_refresh_run_id`), `pool_refresh_runs` (provenance, §10), `pool_locks` (§10 fence).
**Monitoring** — `monitor_configs` (name unique, tickers/platform_keys/selected_analysts JSON, granularity, schedule), `opportunity_reports` (label, confidence, degraded, disclaimer + version NOT NULL + CHECK).
**Serenity** — `serenity_research_records` (theme, chain_layer, bottleneck_hypothesis, scorecard JSON, evidence_grade, recommended_action, disclaimer + version + CHECK), `evidence_references` (source_url, source_host, source_type host-derived, substantiated deterministic, excerpt, claim_summary).

**Enums** — `PoolEntryStatus` {candidate, active, data_unavailable, dropped} · `RefreshRunStatus` {running, complete, partial, error} · `ReportLabel` {thesis-supportive, thesis-challenging, mixed, insufficient-evidence} (non-directional) · `EvidenceGrade` {A,B,C,D,F} · `SourceType` {filing, patent, regulatory, earnings, news, unverified} · `RecommendedAction` {promote, hold, demote}.

**Migrations** — `58e25bfcb251` (7 feature tables) → `b8f3c1a92d04` (`pool_locks`, §10) → `c7e2f1a4b9d6` (disclaimer CHECKs, §12/§20).

## Serenity evidence adapters (Phases 6–7c)
- **§ SSRF-guarded fetcher** (`src/serenity/fetch.py`, P6) — `fetch_excerpt` never raises; pipeline = https-only → allowlist → raw-IP gate → resolve-once → reject-if-internal → IP-pin (TOCTOU close) → streamed byte cap → content-type gate. `build_record(fetch_missing=…)` opt-in live fetch; blocked → unsubstantiated but persisted.
- **EDGAR** (`adapters/edgar.py`, P7) — ticker→CIK→filings→doc URLs; `SEC_EDGAR_USER_AGENT` carried in fetch headers without touching the SSRF guard.
- **Federal Register** (`adapters/federal_register.py`, P7b) + **gather/discover** (`adapters/gather.py`, CLI `serenity discover`) — fan EDGAR+FedReg, dedupe by normalized URL, per-source groups.
- **Patents** (`adapters/patents.py`, P7c) — **number-driven only** (`serenity research --patent US…`), zero HTTP in the builder (fixed template), `_NUMBER_RE` fullmatch. Ticker→patent *discovery* is **NO-GO / closed** (no keyless API exists; keyed USPTO ODP parked in #15).

---

## §21 Phase plan & delivery status

All phases merged to `main`. (P-numbers are the original PRD's; delivery order differed — hardening and integration came before the higher-numbered API/UI.)

| Phase | Scope | PR(s) | Status |
|-------|-------|-------|--------|
| P1 / 1a | Provider loud-fail contract, node-boundary handler, cache TTL, degraded split (X1–X4) | #4 | ✅ merged |
| P1b | KEY_ENCRYPTION + read-back close + FE rewrite (§9.10) | #24, #26 | ✅ merged |
| P2 | Alembic migration for all feature tables | (in #4 line) | ✅ merged |
| P5 | `refresh_pool` scoring pipeline | (foundation) | ✅ merged |
| P6 | Serenity SSRF-guarded fetch | #11 | ✅ merged |
| P7 / 7b / 7c | Serenity adapters: EDGAR / FedReg+gather+discover / number-driven patents | #12, #14, #16, #17 | ✅ merged |
| P8 | In-process APScheduler + PoolLock (§10) | #19 | ✅ merged |
| P9 | Backend API (§14) | #20 + follow-ups #27 | ✅ merged |
| P11 | Eval framework + disclaimer DB-CHECK + export (§11/§12/§20) | #22 | ✅ merged |
| P10 | Research UI (§13/§16) | #29 | ✅ merged |
| §11.5 | Numeric + anti-stuffing substantiation gates | #42, #44 | ✅ merged |
| #21 | Monitor allowlist (validate-only) + concurrency + route-shadow doc | #50 | ✅ merged |
| #51 | Monitor committee analyzing engine — `selected_analysts` load-bearing (supersedes the Phase-0 TradingAgents engine; TradingAgents → injectable) | #52 (+#53/#54) | ✅ merged |

## §22 Open backlog (deferred — not defects)

- **#23 — counsel sign-off** (§19): human-only release precondition. *Open by design.*
- **#25 — key rotation / re-encrypt sweep**: `rotate_master_key` shipped (PR #65); lazy upgrade-on-write is the current re-encrypt path.
- **#43 — §11.5 polish**: surface-form equality (`$1,200 ≡ 1200`, `10x ≡ 10 times`), salad table-header heuristic.
- **#51 — committee wiring**: ✅ *shipped (PR #52).* `monitor.selected_analysts` is now threaded into the monitor run path via `committee_flow.py` (default engine = ai-hedge-fund committee; TradingAgents demoted to injectable — see §9.7). No longer open backlog.
- **Naming drift**: `v3-*` code constants vs `v4-*` PRD naming — resolve by rename+migrate *or* by accepting the code as authoritative (this doc does the latter).

---

*Reconstructed 2026-07-01 from shipped code + PR history #3–#50 + PAI project memory. Where this document and the code disagree, the code is authoritative — file an issue and correct this doc.*
