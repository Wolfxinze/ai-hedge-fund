// Shared formatting + Badge-variant helpers for the Observing Pools panels.
// Keeps the invariant mappings (grade/action/status/degraded → variant, data-unavailable → "—")
// in one place so every panel renders them identically.

import type { BadgeProps } from '@/components/ui/badge';
import type { PoolEntry } from '@/services/observing-pools-api';

type BadgeVariant = NonNullable<BadgeProps['variant']>;

export const EM_DASH = '—';

/**
 * Data-unavailable values render as an em dash, NEVER as 0 (PRD §11 / Phase-10 invariant).
 * Guards on `typeof number` because score_breakdown carries untrusted agent JSON (an agent's raw
 * `confidence` may not be numeric) — a non-number must degrade to "—", not throw on `.toFixed`.
 */
export function fmt(value: unknown, digits = 0): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return EM_DASH;
  return value.toFixed(digits);
}

/** Evidence grade A–F → existing Badge variant (success=blue / warning=yellow / destructive=red). */
export function gradeVariant(grade: string | null | undefined): BadgeVariant {
  if (!grade) return 'outline';
  const head = grade.trim().charAt(0).toUpperCase();
  if (head === 'A' || head === 'B') return 'success';
  if (head === 'C') return 'warning';
  if (head === 'D' || head === 'F') return 'destructive';
  return 'outline';
}

/** Recommended action (research label, NOT a trade order) → Badge variant. */
export function actionVariant(action: string | null | undefined): BadgeVariant {
  switch ((action || '').toLowerCase()) {
    case 'promote':
      return 'success';
    case 'demote':
      return 'destructive';
    case 'hold':
      return 'secondary';
    default:
      return 'outline';
  }
}

/** Refresh-run status → Badge variant (PARTIAL is surfaced as a warning, never hidden). */
export function runStatusVariant(status: string | null | undefined): BadgeVariant {
  switch ((status || '').toUpperCase()) {
    case 'COMPLETE':
      return 'success';
    case 'PARTIAL':
      return 'warning';
    case 'FAILED':
    case 'ERROR':
      return 'destructive';
    default:
      return 'secondary';
  }
}

/**
 * A pool entry is degraded if ANY agent in its score_breakdown is marked degraded
 * (those agents were excluded from the component mean — agents_bridge). Used to flag
 * the row visibly rather than silently showing a degraded composite as normal.
 */
export function entryIsDegraded(entry: PoolEntry): boolean {
  const components = entry.score_breakdown?.components;
  if (!components) return false;
  return Object.values(components).some((component) =>
    Object.values(component.agents ?? {}).some((agent) => agent.degraded),
  );
}
