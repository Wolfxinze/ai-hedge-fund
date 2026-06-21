import { Info } from 'lucide-react';

import { cn } from '@/lib/utils';

// Reusable disclaimer callout. Used both for the view-level persistent research-only notice
// and for the per-report / per-record stored disclaimer. The disclaimer is a hard invariant:
// every product output path renders it (PRD §9.9), so this component never hides its text.

interface DisclaimerBannerProps {
  text: string;
  version?: string | null;
  /** `notice` = the prominent view-level banner; `inline` = the compact per-record footnote. */
  variant?: 'notice' | 'inline';
  className?: string;
}

export function DisclaimerBanner({ text, version, variant = 'notice', className }: DisclaimerBannerProps) {
  if (variant === 'inline') {
    // The disclaimer is compliance-sensitive, so it stays legible: text-xs (not 11px) and not italic.
    return (
      <p className={cn('mt-2 text-xs text-muted-foreground', className)}>
        {text}
        {version ? ` (${version})` : ''}
      </p>
    );
  }
  return (
    <div
      role="note"
      aria-label={text}
      className={cn(
        'flex items-start gap-2 rounded-md border border-border bg-muted/50 px-3 py-2 text-xs text-muted-foreground',
        className,
      )}
    >
      <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>
        {text}
        {version ? ` (${version})` : ''}
      </span>
    </div>
  );
}
