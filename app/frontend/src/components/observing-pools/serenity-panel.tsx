import { Search } from 'lucide-react';
import { FormEvent, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useI18n } from '@/i18n/use-i18n';
import { observingPoolsApi, SerenityRecord } from '@/services/observing-pools-api';

import { DisclaimerBanner } from './disclaimer-banner';
import { actionVariant, EM_DASH, fmt, gradeVariant } from './lib';

export function SerenityPanel() {
  const { t } = useI18n();
  const [ticker, setTicker] = useState('');
  const [records, setRecords] = useState<SerenityRecord[]>([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Guards setState after unmount: the search is user-triggered (not an effect), so a fetch can be
  // in flight when the panel/tab is closed. Re-set to true in the effect body so a React StrictMode
  // remount (which runs cleanup once) restores it — otherwise results would never render.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const search = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = ticker.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    observingPoolsApi
      .getSerenity(trimmed)
      .then((result) => {
        if (!mounted.current) return;
        setRecords(result);
        setSearched(true);
      })
      .catch((err: unknown) => {
        if (mounted.current) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (mounted.current) setLoading(false);
      });
  };

  return (
    <div className="space-y-4">
      <form onSubmit={search} className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <label htmlFor="serenity-ticker" className="text-sm font-medium">
            {t('observingPools.ticker')}
          </label>
          <Input
            id="serenity-ticker"
            value={ticker}
            onChange={(event) => setTicker(event.target.value)}
            placeholder={t('observingPools.serenityPlaceholder')}
            className="w-48"
          />
        </div>
        <Button type="submit" disabled={loading || !ticker.trim()} className="gap-1">
          <Search className="size-4" aria-hidden="true" />
          {loading ? t('observingPools.loading') : t('observingPools.search')}
        </Button>
      </form>

      {error && <div className="text-sm text-destructive">{t('observingPools.error')}: {error}</div>}

      {searched && !loading && !error && records.length === 0 && (
        <div className="text-sm text-muted-foreground">{t('observingPools.noSerenity')}</div>
      )}

      <div className="space-y-3">
        {records.map((record) => (
          <div key={record.id} className="rounded-lg border bg-card p-3 text-card-foreground shadow-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{record.ticker ?? EM_DASH}</span>
              {record.theme && <span className="text-sm text-muted-foreground">{record.theme}</span>}
              <Badge variant={gradeVariant(record.evidence_grade)}>
                {t('observingPools.grade')}: {record.evidence_grade ?? EM_DASH}
              </Badge>
              {record.recommended_action && (
                <Badge variant={actionVariant(record.recommended_action)} className="capitalize">
                  {record.recommended_action}
                </Badge>
              )}
              <span className="text-xs text-muted-foreground">
                {t('observingPools.serenityScore')}: {fmt(record.serenity_score, 1)}
              </span>
            </div>

            <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
              {record.chain_layer && (
                <>
                  <dt className="font-medium">{t('observingPools.chainLayer')}</dt>
                  <dd className="text-muted-foreground">{record.chain_layer}</dd>
                </>
              )}
              {record.bottleneck_hypothesis && (
                <>
                  <dt className="font-medium">{t('observingPools.hypothesis')}</dt>
                  <dd className="text-muted-foreground">{record.bottleneck_hypothesis}</dd>
                </>
              )}
              {record.platform_key && (
                <>
                  <dt className="font-medium">{t('observingPools.platform')}</dt>
                  <dd className="text-muted-foreground">{record.platform_key}</dd>
                </>
              )}
            </dl>

            <DisclaimerBanner
              variant="inline"
              text={record.disclaimer || EM_DASH}
              version={record.disclaimer_version}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
