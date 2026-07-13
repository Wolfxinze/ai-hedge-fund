import { AlertTriangle, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import { Fragment, useCallback, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useI18n } from '@/i18n/use-i18n';
import { cn } from '@/lib/utils';
import {
  InnovationPlatform,
  observingPoolsApi,
  PoolEntry,
  PoolResponse,
  RefreshError,
  RefreshRun,
} from '@/services/observing-pools-api';

import { entryIsDegraded, EM_DASH, fmt, runStatusVariant } from './lib';

// Known platform keys (src/observing_pools/platforms.PLATFORM_KEYS) — used only as a fallback
// label set if /innovation-platforms is empty/unseeded. The selected key is still validated server-side.
const FALLBACK_PLATFORMS = ['ai', 'robotics', 'energy_storage', 'blockchain', 'multiomic_sequencing'];

// Annualized volatility is a fraction (0.42 → "42.0%"); "—" when unavailable (a degraded haircut
// carries a null volatility). Mirrors lib.fmt's non-number → em-dash guard for untrusted JSON.
function fmtPercent(value: number | null): string {
  return typeof value === 'number' && !Number.isNaN(value) ? `${(value * 100).toFixed(1)}%` : EM_DASH;
}

function PerAgentDetail({ entry }: { entry: PoolEntry }) {
  const { t } = useI18n();
  const breakdown = entry.score_breakdown;
  const components = breakdown?.components ?? {};
  const componentNames = Object.keys(components);

  if (componentNames.length === 0) {
    return <p className="text-xs text-muted-foreground">{t('observingPools.noBreakdown')}</p>;
  }

  return (
    <div className="space-y-3">
      {componentNames.map((name) => {
        const component = components[name];
        const agents = component.agents ?? {};
        const agentNames = Object.keys(agents);
        return (
          <div key={name}>
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium capitalize">{name.replace(/_/g, ' ')}</span>
              <span className="text-xs text-muted-foreground">{fmt(component.value, 1)}</span>
              {breakdown?.weights?.[name] !== undefined && (
                <span className="text-[10px] text-muted-foreground">
                  ×{breakdown.weights[name]}
                </span>
              )}
            </div>
            {component.risk_haircut && (
              // rh1 formula only — absent under the default momentum-only formula, so nothing
              // (no labels, no dash placeholders) renders for the common case.
              <div className="ml-3 mt-1 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
                <span>
                  {t('observingPools.rawMomentum')}: {fmt(component.risk_haircut.raw_momentum, 1)}
                </span>
                <span>
                  {t('observingPools.haircut')}: -{fmt(component.risk_haircut.haircut_points, 1)}
                </span>
                <span>
                  {t('observingPools.volatility')}: {fmtPercent(component.risk_haircut.annualized_volatility)}
                </span>
                {component.risk_haircut.degraded && (
                  <Badge variant="warning" className="px-1 py-0 text-[9px]">
                    {t('observingPools.degraded')}
                  </Badge>
                )}
              </div>
            )}
            {agentNames.length > 0 && (
              <div className="ml-3 mt-1 flex flex-wrap gap-1">
                {agentNames.map((agentName) => {
                  const agent = agents[agentName];
                  return (
                    <span
                      key={agentName}
                      className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground"
                      title={`${agentName}: ${agent.signal ?? EM_DASH} / ${fmt(agent.confidence, 0)}`}
                    >
                      {agentName.replace(/_/g, ' ')}
                      <span className="font-medium">{agent.signal ?? EM_DASH}</span>
                      {agent.degraded && (
                        <Badge variant="warning" className="px-1 py-0 text-[9px]">
                          {t('observingPools.degraded')}
                        </Badge>
                      )}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function PoolsPanel() {
  const { t } = useI18n();
  const [platforms, setPlatforms] = useState<InnovationPlatform[]>([]);
  const [platform, setPlatform] = useState<string>('ai');
  const [pool, setPool] = useState<PoolResponse | null>(null);
  const [runs, setRuns] = useState<RefreshRun[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshStatus, setRefreshStatus] = useState<string | null>(null);
  const mounted = useRef(true);
  // cancelRefreshRef holds the cancel fn for any in-flight handleRefresh call so the
  // platform-change effect can abort a stale-platform re-fetch before it writes state.
  const cancelRefreshRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Platform list is fetched once; the pool detail reloads on platform change.
  useEffect(() => {
    let cancelled = false;
    observingPoolsApi
      .listPlatforms()
      .then((list) => !cancelled && list.length > 0 && setPlatforms(list))
      .catch(() => {
        /* fall back to FALLBACK_PLATFORMS labels below */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch pool entries + refresh-run provenance for a platform. Returns a promise so a manual
  // refresh can await the re-fetch; `cancelled` guards against a stale platform switch.
  const loadPool = useCallback((platformKey: string, cancelled: () => boolean) => {
    setLoading(true);
    setError(null);
    setExpanded(new Set());
    return Promise.all([
      observingPoolsApi.getPool(platformKey),
      observingPoolsApi.listRefreshRuns(10).catch(() => [] as RefreshRun[]),
    ])
      .then(([poolResponse, runList]) => {
        if (cancelled()) return;
        setPool(poolResponse);
        setRuns(runList);
      })
      .catch((err: unknown) => {
        if (!cancelled()) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => !cancelled() && setLoading(false));
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Cancel any in-flight handleRefresh re-fetch that belongs to the old platform.
    cancelRefreshRef.current?.();
    cancelRefreshRef.current = null;
    setRefreshStatus(null);
    loadPool(platform, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [platform, loadPool]);

  // Trigger a live refresh, then re-fetch pool + runs on success. 409/503 surface distinct,
  // user-readable inline messages (never a raw lock-contention/DB string).
  const handleRefresh = useCallback(() => {
    cancelRefreshRef.current?.(); // cancel any prior in-flight refresh
    let cancelled = false;
    cancelRefreshRef.current = () => {
      cancelled = true;
    };
    setRefreshing(true);
    setRefreshStatus(null);
    const platformAtClick = platform; // snapshot — platform may change while fetch is in-flight
    observingPoolsApi
      .triggerRefresh(platformAtClick)
      .then(() => {
        if (cancelled || !mounted.current) return;
        setRefreshStatus(t('observingPools.refreshTriggered'));
        return loadPool(platformAtClick, () => cancelled);
      })
      .catch((err: unknown) => {
        if (cancelled || !mounted.current) return;
        if (err instanceof RefreshError && err.status === 409) {
          setRefreshStatus(t('observingPools.refreshInProgress'));
        } else if (err instanceof RefreshError && err.status === 503) {
          setRefreshStatus(t('observingPools.refreshUnavailable'));
        } else {
          setRefreshStatus(t('observingPools.refreshFailed'));
        }
      })
      .finally(() => {
        cancelRefreshRef.current = null;
        if (!cancelled && mounted.current) setRefreshing(false);
      });
  }, [platform, loadPool, t]);

  const toggle = useCallback((ticker: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(ticker)) next.delete(ticker);
      else next.add(ticker);
      return next;
    });
  }, []);

  const platformOptions = platforms.length > 0 ? platforms.map((p) => p.key) : FALLBACK_PLATFORMS;
  const entries = pool?.entries ?? [];
  const runsForPlatform = runs.filter((run) => (run.platform_keys ?? []).includes(platform));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="op-platform" className="text-sm font-medium">
          {t('observingPools.platform')}
        </label>
        <select
          id="op-platform"
          className="h-9 cursor-pointer rounded-md border border-border bg-background px-2 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          value={platform}
          onChange={(event) => setPlatform(event.target.value)}
        >
          {platformOptions.map((key) => (
            <option key={key} value={key}>
              {platforms.find((p) => p.key === key)?.name ?? key}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing || loading}
          className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border bg-background px-3 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw className={cn('size-4', refreshing && 'animate-spin')} aria-hidden="true" />
          {refreshing ? t('observingPools.refreshing') : t('observingPools.refresh')}
        </button>
        {refreshStatus && (
          <span role="status" className="text-xs text-muted-foreground">
            {refreshStatus}
          </span>
        )}
        {pool && (
          <span className="text-xs text-muted-foreground">
            {t('observingPools.rankedCount', { count: String(pool.count) })}
          </span>
        )}
      </div>

      {loading && <div className="text-sm text-muted-foreground">{t('observingPools.loading')}</div>}
      {error && <div className="text-sm text-destructive">{t('observingPools.error')}: {error}</div>}

      {!loading && !error && (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>{t('observingPools.rank')}</TableHead>
                <TableHead>{t('observingPools.ticker')}</TableHead>
                <TableHead>{t('observingPools.composite')}</TableHead>
                <TableHead>{t('observingPools.platformFit')}</TableHead>
                <TableHead>{t('observingPools.value')}</TableHead>
                <TableHead>{t('observingPools.growth')}</TableHead>
                <TableHead>{t('observingPools.momentum')}</TableHead>
                <TableHead>{t('observingPools.serenity')}</TableHead>
                <TableHead>{t('observingPools.formula')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry) => {
                const degraded = entryIsDegraded(entry);
                const isOpen = expanded.has(entry.ticker);
                return (
                  <Fragment key={entry.ticker}>
                    <TableRow
                      className={cn('cursor-pointer', degraded && 'border-l-4 border-l-yellow-500')}
                      onClick={() => toggle(entry.ticker)}
                    >
                      <TableCell className="py-1">
                        {/* Real focusable control so the per-agent breakdown is keyboard-reachable,
                            not mouse-only. stopPropagation avoids a double-toggle with the row click. */}
                        <button
                          type="button"
                          aria-expanded={isOpen}
                          aria-label={t('observingPools.toggleDetails')}
                          onClick={(event) => {
                            event.stopPropagation();
                            toggle(entry.ticker);
                          }}
                          className="rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        >
                          {isOpen ? (
                            <ChevronDown className="size-4" aria-hidden="true" />
                          ) : (
                            <ChevronRight className="size-4" aria-hidden="true" />
                          )}
                        </button>
                      </TableCell>
                      <TableCell className="py-1">{entry.rank ?? EM_DASH}</TableCell>
                      <TableCell className="py-1 font-medium">
                        <span className="inline-flex items-center gap-1">
                          {entry.ticker}
                          {degraded && (
                            <Badge variant="warning" className="gap-1 px-1 py-0 text-[10px]">
                              <AlertTriangle className="size-3" aria-hidden="true" />
                              {t('observingPools.degraded')}
                            </Badge>
                          )}
                        </span>
                      </TableCell>
                      <TableCell className="py-1">{fmt(entry.composite_score, 1)}</TableCell>
                      <TableCell className="py-1">{fmt(entry.components.platform_fit)}</TableCell>
                      <TableCell className="py-1">{fmt(entry.components.value_investor)}</TableCell>
                      <TableCell className="py-1">{fmt(entry.components.innovation_growth)}</TableCell>
                      <TableCell className="py-1">{fmt(entry.components.risk_adjusted_momentum)}</TableCell>
                      <TableCell className="py-1">{fmt(entry.components.serenity_bottleneck)}</TableCell>
                      <TableCell className="py-1 text-xs text-muted-foreground">
                        {entry.composite_formula_version ?? EM_DASH}
                      </TableCell>
                    </TableRow>
                    {isOpen && (
                      <TableRow className="bg-muted/30 hover:bg-muted/30">
                        <TableCell colSpan={10} className="py-2">
                          {entry.rationale && (
                            <p className="mb-2 text-xs text-muted-foreground">{entry.rationale}</p>
                          )}
                          <PerAgentDetail entry={entry} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
          {entries.length === 0 && (
            <div className="text-sm text-muted-foreground">{t('observingPools.empty')}</div>
          )}

          <div>
            <h3 className="mb-2 text-sm font-semibold">{t('observingPools.refreshRuns')}</h3>
            {runsForPlatform.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t('observingPools.noRuns')}</p>
            ) : (
              <div className="space-y-1">
                {runsForPlatform.map((run) => (
                  <div
                    key={run.id}
                    className="flex flex-wrap items-center gap-2 rounded border px-2 py-1 text-xs"
                  >
                    <Badge variant={runStatusVariant(run.status)}>{run.status}</Badge>
                    <span className="text-muted-foreground">
                      {t('observingPools.candidates')}: {fmt(run.candidate_count)}
                    </span>
                    {run.composite_formula_version && (
                      <span className="text-muted-foreground">{run.composite_formula_version}</span>
                    )}
                    {run.completed_at && (
                      <span className="text-muted-foreground">{run.completed_at}</span>
                    )}
                    {run.summary && (
                      // run.summary is a JSON dict — render derived fields, never the raw object.
                      <span className="text-muted-foreground">
                        {t('observingPools.rankedCount', { count: String(run.summary.ranked) })} ·{' '}
                        {t('observingPools.dataUnavailable')}: {run.summary.data_unavailable}
                      </span>
                    )}
                    {run.error && <span className="text-destructive">{run.error}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
