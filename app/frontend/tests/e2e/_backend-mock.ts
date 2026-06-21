import { Page } from '@playwright/test';

// A distinctive sentinel for the STORED disclaimer string, so a test can prove the UI renders the
// disclaimer that came from the API (not a hard-coded one) on every report / serenity record.
export const STORED_DISCLAIMER = 'SENTINEL-DISCLAIMER: research only, not investment advice';
const VERSION = '2026-06';

const PLATFORMS = [
  { key: 'ai', name: 'AI', description: null, enabled: true },
  { key: 'robotics', name: 'Robotics', description: null, enabled: true },
];

// NVDA = clean entry; XYZ = degraded (a degraded agent in the breakdown) + a null component
// (value_investor) that must render as "—", never 0.
const POOL_AI = {
  platform_key: 'ai',
  count: 2,
  entries: [
    {
      ticker: 'NVDA', platform_key: 'ai', status: 'complete', rank: 1,
      composite_score: 82.5, composite_formula_version: 'v3-5comp',
      components: { platform_fit: 90, value_investor: 70, innovation_growth: 85, risk_adjusted_momentum: 60, serenity_bottleneck: 75 },
      score_breakdown: { components: { value_investor: { value: 70, agents: { buffett: { signal: 'bullish', confidence: 80, degraded: false } } } } },
      rationale: 'Strong AI platform fit.',
    },
    {
      ticker: 'XYZ', platform_key: 'ai', status: 'complete', rank: 2,
      composite_score: 55.0, composite_formula_version: 'v3-5comp',
      components: { platform_fit: 50, value_investor: null, innovation_growth: 60, risk_adjusted_momentum: null, serenity_bottleneck: 40 },
      score_breakdown: { components: { value_investor: { value: null, agents: { munger: { signal: 'neutral', confidence: 0, degraded: true } } } } },
      rationale: null,
    },
  ],
};

const REFRESH_RUNS = [
  {
    id: 1, started_at: null, completed_at: '2026-06-21', status: 'PARTIAL', provider_name: 'yfinance',
    universe_source: null, universe_version: null, composite_formula_version: 'v3-5comp',
    platform_keys: ['ai'], candidate_count: 10, fetch_errors: null, rejected: null, token_cost: null,
    summary: { ranked: 2, data_unavailable: 1, candidates: 10, top_tickers: ['NVDA', 'XYZ'] }, error: null,
  },
];

const REPORTS = [
  {
    id: 1, monitor_id: 1, ticker: 'NVDA', generated_at: '2026-06-21', label: 'thesis-supportive',
    confidence: 72, degraded: false, time_horizon: 'medium', summary: 'Looks promising.',
    agent_signals: null, serenity_context: null, risks: ['Valuation risk'], next_checks: ['Earnings'],
    disclaimer: STORED_DISCLAIMER, disclaimer_version: VERSION,
  },
  {
    id: 2, monitor_id: 1, ticker: 'XYZ', generated_at: '2026-06-21', label: 'insufficient-evidence',
    confidence: null, degraded: true, time_horizon: null, summary: null,
    agent_signals: null, serenity_context: null, risks: null, next_checks: null,
    disclaimer: STORED_DISCLAIMER, disclaimer_version: VERSION,
  },
];

const MONITORS = [
  {
    id: 1, name: 'AI weekly', tickers: ['NVDA', 'XYZ'], platform_keys: ['ai'], granularity: 'weekly',
    schedule: null, selected_analysts: null, lookback_window: null, enabled: true, created_at: '2026-06-21',
  },
];

const SERENITY_AAPL = [
  {
    id: 1, ticker: 'AAPL', platform_key: 'ai', theme: 'AI infra', chain_layer: 'compute',
    bottleneck_hypothesis: 'HBM supply constraint', evidence_grade: 'B', serenity_score: 68,
    recommended_action: 'hold', disclaimer: STORED_DISCLAIMER, disclaimer_version: VERSION,
  },
];

const API_KEYS_LIST = [
  { id: 1, provider: 'OPENAI_API_KEY', is_set: true, masked_tail: 'ab12', is_active: true, created_at: '2026-06-21' },
];

// Intercept every backend call (the app talks to http://localhost:8000) and return canned JSON,
// so the suite is hermetic — no backend, DB, or LLM. Unmatched paths get a benign empty 200.
export async function mockBackend(page: Page): Promise<void> {
  await page.route('http://localhost:8000/**', (route) => {
    const { pathname } = new URL(route.request().url());
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

    if (pathname === '/innovation-platforms') return json(PLATFORMS);
    if (pathname === '/observing-pools/refresh-runs') return json(REFRESH_RUNS);
    if (pathname === '/observing-pools/ai') return json(POOL_AI);
    if (pathname.startsWith('/observing-pools/')) return json({ platform_key: pathname.split('/').pop(), count: 0, entries: [] });
    if (pathname === '/opportunity-reports') return json(REPORTS);
    if (pathname === '/monitors') return json(MONITORS);
    if (pathname.startsWith('/serenity/research/')) return json(SERENITY_AAPL);
    if (pathname === '/api-keys') return json(API_KEYS_LIST);
    return json([]);
  });
}
