import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useI18n } from '@/i18n/use-i18n';

import { DisclaimerBanner } from './disclaimer-banner';
import { MonitorsPanel } from './monitors-panel';
import { PoolsPanel } from './pools-panel';
import { SerenityPanel } from './serenity-panel';

// Research-only Observing Pools view (PRD v4 §13 Phase 10): a thin, reuse-first React layer over
// the live API. Read-only Pools + Serenity research; Monitors adds create/run (no trade/order path).
// Every product output renders the disclaimer; degraded entries/reports are visibly flagged.
export function ObservingPoolsView() {
  const { t, locale, toggleLocale } = useI18n();

  return (
    <div className="space-y-4 p-4 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">{t('observingPools.title')}</h2>
          <p className="text-muted-foreground">{t('observingPools.subtitle')}</p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={toggleLocale}>
          {t('observingPools.toggleLanguage')} ({locale})
        </Button>
      </div>

      {/* View-level persistent research-only notice; per-record stored disclaimers render inline too. */}
      <DisclaimerBanner text={t('observingPools.researchOnly')} />

      <Tabs defaultValue="pools">
        <TabsList>
          <TabsTrigger value="pools">{t('observingPools.tabPools')}</TabsTrigger>
          <TabsTrigger value="serenity">{t('observingPools.tabSerenity')}</TabsTrigger>
          <TabsTrigger value="monitors">{t('observingPools.tabMonitors')}</TabsTrigger>
        </TabsList>
        <TabsContent value="pools">
          <PoolsPanel />
        </TabsContent>
        <TabsContent value="serenity">
          <SerenityPanel />
        </TabsContent>
        <TabsContent value="monitors">
          <MonitorsPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}
