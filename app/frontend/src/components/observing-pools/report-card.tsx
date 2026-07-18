import { AlertTriangle } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { useI18n } from '@/i18n/use-i18n';
import { cn } from '@/lib/utils';
import type { OpportunityReport } from '@/services/observing-pools-api';

import { DisclaimerBanner } from './disclaimer-banner';
import { EM_DASH, fmt, localizeDisclaimer } from './lib';

// Renders a string[] section (risks / next checks) only when the JSON actually is a string list,
// so an unexpected shape degrades to "hidden" rather than to a broken render.
function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function humanize(label: string): string {
  return label.replace(/_/g, ' ');
}

export function ReportCard({ report }: { report: OpportunityReport }) {
  const { t } = useI18n();
  const risks = asStringList(report.risks);
  const nextChecks = asStringList(report.next_checks);

  return (
    <div
      className={cn(
        'rounded-lg border bg-card p-3 text-card-foreground shadow-sm',
        // Degraded reports are visibly flagged — amber accent border in addition to the badge,
        // so colour is never the only signal.
        report.degraded && 'border-l-4 border-l-yellow-500',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{report.ticker}</span>
        <Badge variant="outline" className="capitalize">
          {humanize(report.label)}
        </Badge>
        {report.degraded && (
          <Badge variant="warning" className="gap-1">
            <AlertTriangle className="size-3" aria-hidden="true" />
            {t('observingPools.degraded')}
          </Badge>
        )}
        <span className="text-xs text-muted-foreground">
          {t('observingPools.confidence')}: {fmt(report.confidence)}
        </span>
        {report.time_horizon && (
          <span className="text-xs text-muted-foreground">
            {t('observingPools.timeHorizon')}: {report.time_horizon}
          </span>
        )}
      </div>

      {report.summary && <p className="mt-2 text-sm text-muted-foreground">{report.summary}</p>}

      {report.degraded && (
        <p className="mt-1 text-xs text-yellow-700 dark:text-yellow-500">
          {t('observingPools.degradedReportNote')}
        </p>
      )}

      {risks.length > 0 && (
        <div className="mt-2">
          <p className="text-xs font-medium">{t('observingPools.risks')}</p>
          <ul className="ml-4 list-disc text-xs text-muted-foreground">
            {risks.map((risk, index) => (
              <li key={`${index}-${risk}`}>{risk}</li>
            ))}
          </ul>
        </div>
      )}

      {nextChecks.length > 0 && (
        <div className="mt-2">
          <p className="text-xs font-medium">{t('observingPools.nextChecks')}</p>
          <ul className="ml-4 list-disc text-xs text-muted-foreground">
            {nextChecks.map((check, index) => (
              <li key={`${index}-${check}`}>{check}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Disclaimer invariant: rendered from the stored string on every report. */}
      <DisclaimerBanner
        variant="inline"
        text={localizeDisclaimer(report.disclaimer, t) ?? EM_DASH}
        version={report.disclaimer_version}
      />
    </div>
  );
}
