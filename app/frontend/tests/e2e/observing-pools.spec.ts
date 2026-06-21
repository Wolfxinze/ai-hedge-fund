import { expect, test, type Page } from '@playwright/test';

import { mockBackend, STORED_DISCLAIMER } from './_backend-mock';

// Order-placement verbs — a research-only UI must expose NONE of these as a control.
const TRADE_ACTION = /\b(buy|sell|short|cover|trade|order|execute|place order)\b/i;

// Pins the Phase-10 research-only invariants that the security reviews verified by hand.
test.describe('Observing Pools — research-only invariants', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
  });

  async function openObservingPools(page: Page) {
    await page.goto('/');
    await page.getByRole('button', { name: 'Open Observing Pools' }).click();
    // The view is up once its persistent research-only note (role=note) has rendered.
    await expect(page.getByRole('note')).toBeVisible();
  }

  test('pools view: disclaimer banner, degraded mark, verbatim formula, data-unavailable "—" (never 0), PARTIAL run, no key read-back, no trade control', async ({ page }) => {
    const backendPaths: string[] = [];
    page.on('request', (req) => {
      const u = new URL(req.url());
      if (u.host === 'localhost:8000') backendPaths.push(u.pathname);
    });

    await openObservingPools(page);

    // Persistent research-only banner: target the stable role + its aria-label (the i18n notice
    // that ends "...places no trades"), not brittle body text.
    await expect(page.getByRole('note', { name: /places no trades/i })).toBeVisible();

    // Ranked table renders both candidates.
    await expect(page.getByRole('cell', { name: 'NVDA' })).toBeVisible();
    const xyzRow = page.getByRole('row', { name: /XYZ/ });
    await expect(xyzRow).toBeVisible();

    // Composite formula version is rendered verbatim (the stored string, not a reformatted label).
    await expect(page.getByText('v3-5comp').first()).toBeVisible();

    // The degraded entry (XYZ has a degraded agent) is flagged; the clean NVDA row is NOT.
    await expect(xyzRow.getByText('Degraded', { exact: true })).toHaveCount(1);
    await expect(page.getByRole('row', { name: /NVDA/ }).getByText('Degraded', { exact: true })).toHaveCount(0);

    // Data-unavailable components (XYZ value_investor + momentum are null) render "—" and NEVER 0.
    await expect(xyzRow.getByText('—').first()).toBeVisible();
    await expect(xyzRow.getByText('0', { exact: true })).toHaveCount(0);

    // Suppression-as-ranking is shown: the PARTIAL refresh-run status is surfaced.
    await expect(page.getByText('PARTIAL')).toBeVisible();

    // No trade / order affordance (button or link) in the research view.
    await expect(page.getByRole('button', { name: TRADE_ACTION })).toHaveCount(0);
    await expect(page.getByRole('link', { name: TRADE_ACTION })).toHaveCount(0);

    // SECURITY: the view demonstrably did its real work (fetched pools) AND never requested an
    // API-key value — the positive control means "no /api-keys" can't pass on a dead render.
    expect(backendPaths.some((p) => p.startsWith('/observing-pools/'))).toBe(true);
    expect(backendPaths.some((p) => p.startsWith('/api-keys'))).toBe(false);
  });

  test('no trade/order control on any Observing Pools tab', async ({ page }) => {
    await openObservingPools(page);
    for (const tab of ['Pools', 'Serenity', 'Monitors']) {
      await page.getByRole('tab', { name: tab }).click();
      await expect(page.getByRole('tab', { name: tab })).toHaveAttribute('data-state', 'active');
      await expect(page.getByRole('button', { name: TRADE_ACTION })).toHaveCount(0);
      await expect(page.getByRole('link', { name: TRADE_ACTION })).toHaveCount(0);
    }
  });

  test('opportunity reports always render the STORED disclaimer and flag only degraded reports', async ({ page }) => {
    await openObservingPools(page);
    await page.getByRole('tab', { name: 'Monitors' }).click();

    // Both report cards render the disclaimer that came from the API (sentinel proves it's the
    // stored string, not a hard-coded one) — exactly one per report.
    await expect(page.getByText(new RegExp(STORED_DISCLAIMER))).toHaveCount(2);

    // Exactly one report (XYZ) is degraded; the clean NVDA report is not flagged. `exact` excludes
    // the longer "...this report is degraded..." caption so this counts only the badge.
    await expect(page.getByText('Degraded', { exact: true })).toHaveCount(1);
  });

  test('serenity research renders grade, recommended action, and the stored disclaimer', async ({ page }) => {
    await openObservingPools(page);
    await page.getByRole('tab', { name: 'Serenity' }).click();

    await page.getByLabel('Ticker').fill('AAPL');
    await page.getByRole('button', { name: 'Search' }).click();

    await expect(page.getByText(/Grade: B/)).toBeVisible();
    await expect(page.getByText('hold')).toBeVisible();
    await expect(page.getByText(new RegExp(STORED_DISCLAIMER))).toBeVisible();
  });
});
