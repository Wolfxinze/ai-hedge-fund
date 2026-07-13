import { Search, Sparkles } from 'lucide-react';
import { FormEvent, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { TranslationKey } from '@/i18n/translations';
import { useI18n } from '@/i18n/use-i18n';
import { observingPoolsApi, SerenityRecord } from '@/services/observing-pools-api';

import { DisclaimerBanner } from './disclaimer-banner';
import { actionVariant, EM_DASH, fmt, gradeVariant } from './lib';

// Mirrors src/serenity/grading.SCORECARD_DIMENSIONS — order and keys must match, the backend
// 422s on any drift. Each dimension is scored 0-4 by the user (their judgment, never defaulted
// server-side).
const SCORECARD_DIMENSIONS = [
  'supplier_concentration',
  'validation_cycle',
  'capacity_expansion',
  'certification_strictness',
  'purity_precision',
] as const;
type ScorecardDim = (typeof SCORECARD_DIMENSIONS)[number];

const DIM_LABEL_KEYS: Record<ScorecardDim, TranslationKey> = {
  supplier_concentration: 'observingPools.dimSupplierConcentration',
  validation_cycle: 'observingPools.dimValidationCycle',
  capacity_expansion: 'observingPools.dimCapacityExpansion',
  certification_strictness: 'observingPools.dimCertificationStrictness',
  purity_precision: 'observingPools.dimPurityPrecision',
};

const NEUTRAL_SCORECARD: Record<ScorecardDim, number> = {
  supplier_concentration: 2,
  validation_cycle: 2,
  capacity_expansion: 2,
  certification_strictness: 2,
  purity_precision: 2,
};

export function SerenityPanel() {
  const { t } = useI18n();
  const [ticker, setTicker] = useState('');
  const [records, setRecords] = useState<SerenityRecord[]>([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState('');
  const [keywords, setKeywords] = useState('');
  const [scorecard, setScorecard] = useState<Record<ScorecardDim, number>>(NEUTRAL_SCORECARD);
  const [discovering, setDiscovering] = useState(false);
  const [discoverMessage, setDiscoverMessage] = useState<string | null>(null);
  const [discoverError, setDiscoverError] = useState<string | null>(null);
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

  const parsedKeywords = keywords.split(',').map((k) => k.trim()).filter(Boolean);
  const canDiscover = Boolean(ticker.trim()) && Boolean(theme.trim()) && parsedKeywords.length > 0;

  const discover = (event: FormEvent) => {
    event.preventDefault();
    if (!canDiscover || discovering) return;
    const trimmedTicker = ticker.trim();
    setDiscovering(true);
    setDiscoverMessage(null);
    setDiscoverError(null);
    observingPoolsApi
      .discoverSerenity({ ticker: trimmedTicker, theme: theme.trim(), keywords: parsedKeywords, scorecard })
      .then((result) => {
        if (!mounted.current) return;
        setDiscoverMessage(
          result.records.length === 0 && result.reference_count === 0
            ? t('observingPools.discoverNoEvidence')
            : t('observingPools.discoverBuilt', { count: String(result.records.length), refs: String(result.reference_count) }),
        );
        const failedSources = Object.keys(result.source_errors);
        if (failedSources.length > 0) {
          setDiscoverError(t('observingPools.discoverPartial', { sources: failedSources.join(', ') }));
        }
        // Refresh the records list so the new research is immediately visible.
        return observingPoolsApi.getSerenity(trimmedTicker).then((refreshed) => {
          if (!mounted.current) return;
          setRecords(refreshed);
          setSearched(true);
        });
      })
      .catch((err: unknown) => {
        if (mounted.current) {
          setDiscoverError(`${t('observingPools.discoverFailed')} ${err instanceof Error ? err.message : String(err)}`);
        }
      })
      .finally(() => {
        if (mounted.current) setDiscovering(false);
      });
  };

  const setDim = (dim: ScorecardDim, raw: string) => {
    const value = Math.min(4, Math.max(0, Number(raw) || 0));
    setScorecard((prev) => ({ ...prev, [dim]: value }));
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

      <form onSubmit={discover} className="space-y-3 rounded-lg border bg-card p-3 text-card-foreground shadow-sm">
        <div>
          <h3 className="text-sm font-semibold">{t('observingPools.discoverTitle')}</h3>
          <p className="text-xs text-muted-foreground">{t('observingPools.discoverHint')}</p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <label htmlFor="discover-theme" className="text-sm font-medium">
              {t('observingPools.theme')}
            </label>
            <Input
              id="discover-theme"
              value={theme}
              onChange={(event) => setTheme(event.target.value)}
              placeholder={t('observingPools.themePlaceholder')}
              className="w-64"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="discover-keywords" className="text-sm font-medium">
              {t('observingPools.keywords')}
            </label>
            <Input
              id="discover-keywords"
              value={keywords}
              onChange={(event) => setKeywords(event.target.value)}
              placeholder={t('observingPools.keywordsPlaceholder')}
              className="w-64"
            />
          </div>
        </div>
        <fieldset className="space-y-1">
          <legend className="text-sm font-medium">{t('observingPools.scorecard')}</legend>
          <div className="flex flex-wrap gap-3">
            {SCORECARD_DIMENSIONS.map((dim) => (
              <div key={dim} className="flex flex-col gap-1">
                <label htmlFor={`discover-dim-${dim}`} className="text-xs text-muted-foreground">
                  {t(DIM_LABEL_KEYS[dim])}
                </label>
                <Input
                  id={`discover-dim-${dim}`}
                  type="number"
                  min={0}
                  max={4}
                  value={scorecard[dim]}
                  onChange={(event) => setDim(dim, event.target.value)}
                  className="w-20"
                />
              </div>
            ))}
          </div>
        </fieldset>
        <Button type="submit" disabled={discovering || !canDiscover} className="gap-1">
          <Sparkles className="size-4" aria-hidden="true" />
          {discovering ? t('observingPools.discovering') : t('observingPools.discover')}
        </Button>
        {discoverMessage && <div className="text-sm text-muted-foreground">{discoverMessage}</div>}
        {discoverError && <div className="text-sm text-destructive">{discoverError}</div>}
      </form>

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
