// Typed client for the Observing Pools / Serenity / Monitors research API (PRD v4 §14).
//
// Loopback, research-only. Bare `fetch` matching the existing services/* pattern and the
// exact JSON shapes of app/backend/routes/{observing_pools,monitors}.py. This client never
// touches any /api-keys route — there is no secret read-back path here (PRD §9.10).

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

// ---- Pools -----------------------------------------------------------------

export interface InnovationPlatform {
  key: string;
  name: string;
  description: string | null;
  enabled: boolean;
}

// Per-agent contribution stored inside score_breakdown (agents_bridge._safe_agent_score):
// degraded agents are excluded from the component mean and carry degraded=true.
export interface AgentBreakdown {
  signal: string | null;
  confidence: number | null;
  degraded: boolean;
}

// B1 risk-haircut audit (src/quant/volatility.apply_risk_haircut). Present ONLY on the
// risk_adjusted_momentum component and ONLY when the pool was scored under an rh1 formula version
// (v3-4comp-rh1 / v3-5comp-rh1). Absent under the default momentum-only formula, so it is optional
// everywhere and must never be assumed present. `degraded` ⇔ price data was missing/too short
// (annualized_volatility null, haircut_points 0 — momentum passed through un-haircut).
export interface RiskHaircut {
  raw_momentum: number | null;
  haircut_points: number;
  annualized_volatility: number | null;
  degraded: boolean;
  policy: string;
}

export interface ComponentBreakdown {
  value: number | null;
  agents?: Record<string, AgentBreakdown>;
  risk_haircut?: RiskHaircut;
}

export interface ScoreBreakdown {
  platform_fit?: { value: number | null; source?: string };
  components?: Record<string, ComponentBreakdown>;
  formula_version?: string;
  weights?: Record<string, number>;
  composite?: number | null;
}

export interface PoolComponents {
  platform_fit: number | null;
  value_investor: number | null;
  innovation_growth: number | null;
  risk_adjusted_momentum: number | null;
  serenity_bottleneck: number | null;
}

export interface PoolEntry {
  ticker: string;
  platform_key: string;
  status: string;
  rank: number | null;
  composite_score: number | null;
  composite_formula_version: string | null;
  components: PoolComponents;
  score_breakdown: ScoreBreakdown | null;
  rationale: string | null;
}

export interface PoolResponse {
  platform_key: string;
  count: number;
  entries: PoolEntry[];
}

// run.summary is a JSON dict (src/observing_pools/pipeline.py), NOT a string — must be rendered
// field-by-field, never as a raw React child.
export interface RefreshRunSummary {
  ranked: number;
  data_unavailable: number;
  candidates: number;
  top_tickers: string[];
}

export interface RefreshRun {
  id: number;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  provider_name: string | null;
  universe_source: string | null;
  universe_version: string | null;
  composite_formula_version: string | null;
  platform_keys: string[] | null;
  candidate_count: number | null;
  fetch_errors: unknown;
  rejected: unknown;
  token_cost: unknown; // JSON dict {calls, tokens, est_usd}; opaque to the UI
  summary: RefreshRunSummary | null;
  error: string | null;
}

// POST /observing-pools/refresh response (app/backend/routes/observing_pools.py). A live (non
// dry-run) trigger persists a run and returns its id; summary mirrors RefreshRunSummary.
export interface RefreshResult {
  id: number | null;
  status: string;
  platform_key: string;
  dry_run: boolean;
  summary: RefreshRunSummary | null;
  error: string | null;
}

// Error thrown by triggerRefresh carrying the HTTP status, so the caller can distinguish a 409
// (refresh already in progress) or 503 (DB locked) from a generic failure without string-matching.
export class RefreshError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'RefreshError';
    this.status = status;
  }
}

// ---- Serenity --------------------------------------------------------------

export interface SerenityRecord {
  id: number;
  ticker: string | null;
  platform_key: string | null;
  theme: string | null;
  chain_layer: string | null;
  bottleneck_hypothesis: string | null;
  evidence_grade: string | null;
  serenity_score: number | null;
  recommended_action: string | null;
  disclaimer: string;
  disclaimer_version: string;
}

// POST /serenity/discover request/response (app/backend/routes/observing_pools.py). The scorecard
// is the caller's judgment — the 5 bottleneck dimensions (src/serenity/grading.SCORECARD_DIMENSIONS),
// each 0-4; the backend rejects anything else with a 422 (never defaulted server-side).
export interface SerenityDiscoverRequest {
  ticker: string;
  theme: string;
  keywords: string[];
  platform_key?: string | null;
  chain_layer?: string | null;
  hypothesis?: string | null;
  sources?: string[];
  max_per_source?: number;
  scorecard: Record<string, number>;
}

export interface SerenityDiscoverResult {
  ticker: string;
  records: SerenityRecord[];
  reference_count: number;
  source_errors: Record<string, string>; // {source: exc_type} — degraded sources, surfaced not swallowed
  failed_groups: number;
}

// ---- Monitors & reports ----------------------------------------------------

export interface Monitor {
  id: number;
  name: string;
  tickers: string[];
  platform_keys: string[] | null;
  granularity: string;
  schedule: string | null;
  selected_analysts: string[] | null;
  lookback_window: string | null; // MonitorConfig.lookback_window is String(32), not a number
  enabled: boolean;
  created_at: string | null;
}

export interface MonitorCreateRequest {
  name: string;
  tickers: string[];
  granularity?: string;
  platform_keys?: string[] | null;
  selected_analysts?: string[] | null;
  schedule?: string | null;
}

export interface OpportunityReport {
  id: number;
  monitor_id: number | null;
  ticker: string;
  generated_at: string | null;
  label: string;
  confidence: number | null;
  degraded: boolean;
  time_horizon: string | null;
  summary: string | null;
  agent_signals: unknown;
  serenity_context: unknown;
  risks: unknown;
  next_checks: unknown;
  disclaimer: string;
  disclaimer_version: string;
}

export interface MonitorRunResult {
  monitor_name: string;
  reports: OpportunityReport[];
  degraded_count: number;
  any_degraded: boolean;
}

// ---- HTTP helpers ----------------------------------------------------------

/** Parse a JSON error body's `detail` (FastAPI HTTPException shape) for a useful message. */
async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (body && typeof body.detail === 'string') return body.detail;
  } catch {
    // non-JSON body — fall through to the status text
  }
  return `HTTP ${response.status} ${response.statusText}`.trim();
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json() as Promise<T>;
}

async function sendJson<T>(path: string, method: 'POST' | 'PATCH', body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json() as Promise<T>;
}

// ---- Service ---------------------------------------------------------------

export const observingPoolsApi = {
  listPlatforms: (): Promise<InnovationPlatform[]> => getJson('/innovation-platforms'),

  getPool: (platformKey: string): Promise<PoolResponse> =>
    getJson(`/observing-pools/${encodeURIComponent(platformKey)}`),

  listRefreshRuns: (limit = 25): Promise<RefreshRun[]> =>
    getJson(`/observing-pools/refresh-runs?limit=${limit}`),

  // Trigger one live (persisted) refresh for a platform. Throws RefreshError with the HTTP status
  // on failure so 409 (already refreshing) / 503 (DB locked) can be surfaced distinctly.
  triggerRefresh: async (platformKey: string): Promise<RefreshResult> => {
    const response = await fetch(`${API_BASE_URL}/observing-pools/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform_key: platformKey, dry_run: false }),
    });
    if (!response.ok) {
      throw new RefreshError(response.status, await errorMessage(response));
    }
    return response.json() as Promise<RefreshResult>;
  },

  getSerenity: (ticker: string, limit = 50): Promise<SerenityRecord[]> =>
    getJson(`/serenity/research/${encodeURIComponent(ticker)}?limit=${limit}`),

  // Trigger evidence discovery (EDGAR + Federal Register fan-out) and build research records —
  // the API twin of `python -m src.serenity discover`. Research-only; can take several seconds
  // (outbound fetches to allowlisted hosts happen server-side).
  discoverSerenity: (request: SerenityDiscoverRequest): Promise<SerenityDiscoverResult> =>
    sendJson('/serenity/discover', 'POST', request),

  listMonitors: (limit = 50): Promise<Monitor[]> => getJson(`/monitors?limit=${limit}`),

  createMonitor: (request: MonitorCreateRequest): Promise<Monitor> =>
    sendJson('/monitors', 'POST', request),

  runMonitor: (monitorId: number, tradeDate?: string): Promise<MonitorRunResult> =>
    sendJson(`/monitors/${monitorId}/run`, 'POST', tradeDate ? { trade_date: tradeDate } : {}),

  listReports: (limit = 50): Promise<OpportunityReport[]> =>
    getJson(`/opportunity-reports?limit=${limit}`),
};
