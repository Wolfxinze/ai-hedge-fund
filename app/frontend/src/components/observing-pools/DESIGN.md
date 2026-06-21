# Observing Pools UI — Design Decisions (Phase 10)

A thin, **reuse-first** research panel layer over the live, tested API. No new
aesthetic: every surface reuses the existing shadcn/ui + Tailwind design system
(`components/ui/*`, theme tokens `bg-card` / `text-muted-foreground` / `border` /
`text-foreground` / `bg-muted`). Read-only except the Monitors panel (create/run).

## Hard invariants encoded in the UI

- **Disclaimer on every product output.** `DisclaimerBanner` is rendered once at the
  top of the view (persistent research-only notice) AND inline on every opportunity
  report and Serenity record, from the **stored** `disclaimer` + `disclaimer_version`
  strings — never hard-coded, never stripped.
- **Degraded is always visibly flagged** — a `warning` Badge labelled "Degraded" plus
  an amber left-border accent. Color is never the only signal (text label + border).
- **No trade/order affordance** anywhere. The UI only triggers research paths
  (monitor create / manual run → `serialize_report`); there are no buy/sell/quantity
  controls and no order endpoints are called.
- **No secret read-back.** This view never requests or renders an API-key value. Key
  configuration lives in Settings → API Keys, which drives off `is_set` + `masked_tail`.
- **Data-unavailable → "—", never 0.** `fmt()` maps `null`/`undefined` to an em dash.
- **Formula version is rendered verbatim** (the actual stored `composite_formula_version`,
  e.g. `v3-4comp` / `v3-5comp`), not a reformatted/PRD label.
- **Suppression-as-ranking is shown, not hidden** — PARTIAL refresh-run status and the
  "Serenity axis inactive" condition surface in the provenance/runs area.

## Badge variant mapping (existing variants: secondary/destructive/warning/success/outline)

- Evidence grade: A/B → `success`, C → `warning`, D/F → `destructive`, none → `outline`.
- Recommended action: promote → `success`, hold → `secondary`, demote → `destructive`.
- Degraded: `warning`. Refresh-run status: COMPLETE → `success`, PARTIAL → `warning`,
  FAILED → `destructive`, else `secondary`.

## Component decomposition

- `observing-pools-view.tsx` — container: persistent `DisclaimerBanner` + `Tabs`
  (Pools / Serenity / Monitors) + language toggle.
- `disclaimer-banner.tsx` — reusable disclaimer callout (info icon + text + version).
- `report-card.tsx` — opportunity-report card (always renders disclaimer, flags degraded).
- `pools-panel.tsx` — platform selector + ranked top-N table (5-component breakdown,
  formula version, per-row degraded mark, expandable per-agent detail) + refresh-run
  provenance.
- `serenity-panel.tsx` — ticker lookup + bottleneck records (theme/chain-layer/hypothesis,
  grade badge, recommended action, disclaimer).
- `monitors-panel.tsx` — monitor list + create form + manual run + recent reports.
- `lib.ts` — `fmt`, grade/action/status → Badge variant, degraded detection helpers.

Verification gates (no FE test harness exists): `npx tsc --noEmit` (exit 0) and
`npm run lint` (`eslint --max-warnings 0`). All new i18n keys exist in BOTH `en` and
`zhCN` blocks of `src/i18n/translations.ts`.
