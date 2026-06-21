import { Play, Plus } from 'lucide-react';
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useI18n } from '@/i18n/use-i18n';
import type { TranslationKey } from '@/i18n/translations';
import {
  Monitor,
  MonitorRunResult,
  observingPoolsApi,
  OpportunityReport,
} from '@/services/observing-pools-api';

import { ReportCard } from './report-card';

const GRANULARITIES = ['daily', 'weekly', 'monthly'] as const;
// Translation keys for the known finite granularity set (the select offers only these).
const GRANULARITY_KEY: Record<(typeof GRANULARITIES)[number], TranslationKey> = {
  daily: 'observingPools.daily',
  weekly: 'observingPools.weekly',
  monthly: 'observingPools.monthly',
};

export function MonitorsPanel() {
  const { t } = useI18n();
  const [monitors, setMonitors] = useState<Monitor[]>([]);
  const [reports, setReports] = useState<OpportunityReport[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Create form
  const [name, setName] = useState('');
  const [tickers, setTickers] = useState('');
  const [granularity, setGranularity] = useState<string>('weekly');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Manual run
  const [runningId, setRunningId] = useState<number | null>(null);
  const [runResult, setRunResult] = useState<MonitorRunResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  // Guards setState after unmount across the user-triggered async paths (load/create/run).
  // Re-set to true in the effect body so a React StrictMode remount (which runs cleanup once)
  // restores it — otherwise every guard would stay false and data would never load.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Show a translated label for the known granularities; fall back to the raw value (e.g. "custom").
  const granularityLabel = (value: string): string =>
    value in GRANULARITY_KEY ? t(GRANULARITY_KEY[value as keyof typeof GRANULARITY_KEY]) : value;

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([
      observingPoolsApi.listMonitors(),
      observingPoolsApi.listReports().catch(() => [] as OpportunityReport[]),
    ])
      .then(([monitorList, reportList]) => {
        if (!mounted.current) return;
        setMonitors(monitorList);
        setReports(reportList);
      })
      .catch((err: unknown) => {
        if (mounted.current) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (mounted.current) setLoading(false);
      });
  }, []);

  useEffect(() => load(), [load]);

  const createMonitor = (event: FormEvent) => {
    event.preventDefault();
    const tickerList = tickers
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean);
    if (!name.trim() || tickerList.length === 0) return;
    setCreating(true);
    setCreateError(null);
    observingPoolsApi
      .createMonitor({ name: name.trim(), tickers: tickerList, granularity })
      .then(() => {
        if (!mounted.current) return;
        setName('');
        setTickers('');
        setGranularity('weekly');
        load();
      })
      .catch((err: unknown) => {
        if (mounted.current) setCreateError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (mounted.current) setCreating(false);
      });
  };

  const runMonitor = (monitorId: number) => {
    setRunningId(monitorId);
    setRunError(null);
    setRunResult(null);
    observingPoolsApi
      .runMonitor(monitorId)
      .then((result) => {
        if (!mounted.current) return;
        setRunResult(result);
        load();
      })
      .catch((err: unknown) => {
        if (mounted.current) setRunError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (mounted.current) setRunningId(null);
      });
  };

  return (
    <div className="space-y-6">
      {/* Create monitor (research watchlist — no trade/order fields). */}
      <form onSubmit={createMonitor} className="space-y-2 rounded-lg border bg-card p-3">
        <h3 className="text-sm font-semibold">{t('observingPools.createMonitor')}</h3>
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <label htmlFor="monitor-name" className="text-xs font-medium">
              {t('observingPools.monitorName')}
            </label>
            <Input
              id="monitor-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t('observingPools.monitorNamePlaceholder')}
              className="w-56"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="monitor-tickers" className="text-xs font-medium">
              {t('observingPools.monitorTickers')}
            </label>
            <Input
              id="monitor-tickers"
              value={tickers}
              onChange={(event) => setTickers(event.target.value)}
              placeholder={t('observingPools.monitorTickersPlaceholder')}
              className="w-56"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="monitor-granularity" className="text-xs font-medium">
              {t('observingPools.granularity')}
            </label>
            <select
              id="monitor-granularity"
              className="h-9 cursor-pointer rounded-md border border-border bg-background px-2 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              value={granularity}
              onChange={(event) => setGranularity(event.target.value)}
            >
              {GRANULARITIES.map((value) => (
                <option key={value} value={value}>
                  {granularityLabel(value)}
                </option>
              ))}
            </select>
          </div>
          <Button type="submit" disabled={creating || !name.trim() || !tickers.trim()} className="gap-1">
            <Plus className="size-4" aria-hidden="true" />
            {creating ? t('observingPools.creating') : t('observingPools.create')}
          </Button>
        </div>
        {createError && <p className="text-xs text-destructive">{createError}</p>}
      </form>

      {/* Monitor list */}
      <div>
        <h3 className="mb-2 text-sm font-semibold">{t('observingPools.monitors')}</h3>
        {loading && <div className="text-sm text-muted-foreground">{t('observingPools.loading')}</div>}
        {error && <div className="text-sm text-destructive">{t('observingPools.error')}: {error}</div>}
        {!loading && !error && monitors.length === 0 && (
          <div className="text-sm text-muted-foreground">{t('observingPools.noMonitors')}</div>
        )}
        <div className="space-y-1">
          {monitors.map((monitor) => (
            <div
              key={monitor.id}
              className="flex flex-wrap items-center gap-2 rounded border px-2 py-1 text-sm"
            >
              <span className="font-medium">{monitor.name}</span>
              <Badge variant={monitor.enabled ? 'success' : 'secondary'}>
                {monitor.enabled ? t('observingPools.enabled') : t('observingPools.disabled')}
              </Badge>
              <span className="text-xs text-muted-foreground">{granularityLabel(monitor.granularity)}</span>
              <span className="text-xs text-muted-foreground">{monitor.tickers.join(', ')}</span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="ml-auto gap-1"
                disabled={runningId !== null}
                onClick={() => runMonitor(monitor.id)}
              >
                <Play className="size-3" aria-hidden="true" />
                {runningId === monitor.id ? t('observingPools.running') : t('observingPools.run')}
              </Button>
            </div>
          ))}
        </div>
        {runningId !== null && (
          <p className="mt-1 text-xs text-muted-foreground">{t('observingPools.runNote')}</p>
        )}
        {runError && <p className="mt-1 text-xs text-destructive">{runError}</p>}
      </div>

      {/* Latest run result */}
      {runResult && (
        <div>
          <h3 className="mb-2 text-sm font-semibold">
            {t('observingPools.runResult')}: {runResult.monitor_name}
          </h3>
          <p className="mb-2 text-xs text-muted-foreground">
            {t('observingPools.reportsGenerated', {
              total: String(runResult.reports.length),
              degraded: String(runResult.degraded_count),
            })}
          </p>
          <div className="space-y-2">
            {runResult.reports.map((report) => (
              <ReportCard key={report.id} report={report} />
            ))}
          </div>
        </div>
      )}

      {/* Recent opportunity reports (disclaimer enforced server-side via serialize_report). */}
      <div>
        <h3 className="mb-2 text-sm font-semibold">{t('observingPools.reportsTitle')}</h3>
        {!loading && reports.length === 0 && (
          <div className="text-sm text-muted-foreground">{t('observingPools.noReports')}</div>
        )}
        <div className="space-y-2">
          {reports.map((report) => (
            <ReportCard key={report.id} report={report} />
          ))}
        </div>
      </div>
    </div>
  );
}
