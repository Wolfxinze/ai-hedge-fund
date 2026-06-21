// Shared types for API requests and responses
// Kept in sync with LanguageModel['provider'] (src/data/models.ts) and the backend
// src/llm/models.ModelProvider so `model.provider as ModelProvider` is a sound upcast.
export enum ModelProvider {
  OPENAI = 'OpenAI',
  ANTHROPIC = 'Anthropic',
  DEEPSEEK = 'DeepSeek',
  GOOGLE = 'Google',
  GROQ = 'Groq',
  OLLAMA = 'Ollama',
}

export interface AgentModelConfig {
  agent_id: string;
  model_name?: string;
  model_provider?: ModelProvider;
}

export interface GraphNode {
  id: string;
  type?: string;
  data?: Record<string, unknown>;
  position?: { x: number; y: number };
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type?: string;
  data?: Record<string, unknown>;
}

export interface PortfolioPosition {
  ticker: string;
  quantity: number;
  trade_price: number;
}

// Base interface for shared fields between HedgeFundRequest and BacktestRequest
export interface BaseHedgeFundRequest {
  tickers: string[];
  graph_nodes: GraphNode[];
  graph_edges: GraphEdge[];
  agent_models?: AgentModelConfig[];
  model_name?: string;
  model_provider?: ModelProvider;
  margin_requirement?: number;
  portfolio_positions?: PortfolioPosition[];
}

export interface HedgeFundRequest extends BaseHedgeFundRequest {
  end_date?: string;
  start_date?: string;
  initial_cash?: number;
}

export interface BacktestRequest extends BaseHedgeFundRequest {
  start_date: string;
  end_date: string;
  initial_capital?: number;
}

export interface BacktestDayResult {
  date: string;
  portfolio_value: number;
  cash: number;
  decisions: Record<string, TradingDecision>;
  executed_trades: Record<string, number>;
  analyst_signals: Record<string, Record<string, AnalystSignalDetail>>;
  current_prices: Record<string, number>;
  long_exposure: number;
  short_exposure: number;
  gross_exposure: number;
  net_exposure: number;
  long_short_ratio: number | null;
}

export interface BacktestPerformanceMetrics {
  sharpe_ratio?: number;
  sortino_ratio?: number;
  max_drawdown?: number;
  max_drawdown_date?: string;
  long_short_ratio?: number;
  gross_exposure?: number;
  net_exposure?: number;
}

// ---- Streamed analysis / backtest display shapes (rendered in the bottom output panels) ----
// Reasoning is a free-form string or JSON object, so it stays `unknown` and is narrowed at render.

export interface TradingDecision {
  action?: string;
  quantity?: number;
  confidence?: number;
  reasoning?: unknown;
}

export interface AnalystSignalDetail {
  signal?: string;
  confidence?: number;
  reasoning?: unknown;
}

export interface BacktestPosition {
  long: number;
  short: number;
  long_cost_basis: number;
  short_cost_basis: number;
}

export interface BacktestTickerDetail {
  ticker: string;
  action?: string;
  quantity?: number;
  price?: number;
  shares_owned?: number;
  long_shares?: number;
  short_shares?: number;
  position_value?: number;
  bullish_count?: number;
  bearish_count?: number;
  neutral_count?: number;
}

export interface BacktestPeriodResult {
  date: string;
  portfolio_value: number;
  cash: number;
  portfolio_return?: number;
  long_short_ratio?: number | null;
  performance_metrics?: BacktestPerformanceMetrics;
  ticker_details?: BacktestTickerDetail[];
} 