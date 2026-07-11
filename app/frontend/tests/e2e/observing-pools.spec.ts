import { expect, test, type Page } from '@playwright/test';

import { mockBackend, REFRESH_RESULT, STORED_DISCLAIMER } from './_backend-mock';

// Exact backend URL for the live-refresh POST — per-test overrides use it so Playwright's LIFO
// route ordering fires them before the base mockBackend handler.
const REFRESH_URL = 'http://localhost:8000/observing-pools/refresh';

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

  // Issue #76 — E2E coverage for the Pools Refresh button (shipped in PR #75). `name: 'Refresh'`
  // is a substring match, so the same locator resolves the button as its label flips to "Refreshing…".
  test('pools refresh: happy path surfaces "Refresh complete."', async ({ page }) => {
    await openObservingPools(page);
    const refresh = page.getByRole('button', { name: 'Refresh' });
    await expect(refresh).toBeEnabled();

    await refresh.click();

    // The role=status region is unique to the refresh feedback; asserts the success i18n string.
    await expect(page.getByRole('status')).toHaveText('Refresh complete.');
  });

  test('pools refresh: a 409 surfaces "Refresh already in progress."', async ({ page }) => {
    await openObservingPools(page);
    // Registered AFTER mockBackend (beforeEach) → Playwright LIFO fires this override first.
    await page.route(REFRESH_URL, (route) =>
      route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'refresh already in progress' }),
      }),
    );

    await page.getByRole('button', { name: 'Refresh' }).click();

    await expect(page.getByRole('status')).toHaveText('Refresh already in progress.');
    await expect(page.getByRole('button', { name: 'Refresh' })).toBeEnabled(); // must be retryable after 409
  });

  test('pools refresh: a 503 surfaces "Refresh temporarily unavailable; please try again."', async ({ page }) => {
    await openObservingPools(page);
    await page.route(REFRESH_URL, (route) =>
      route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'database is locked' }),
      }),
    );

    await page.getByRole('button', { name: 'Refresh' }).click();

    await expect(page.getByRole('status')).toHaveText('Refresh temporarily unavailable; please try again.');
    await expect(page.getByRole('button', { name: 'Refresh' })).toBeEnabled(); // must be retryable after 503
  });

  test('pools refresh: button is disabled while the refresh is in flight', async ({ page }) => {
    await openObservingPools(page);
    // Hold the POST response mid-flight via a Promise (no timing/sleep): the button stays disabled
    // until we release it.
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route(REFRESH_URL, async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(REFRESH_RESULT),
      });
    });

    const refresh = page.getByRole('button', { name: 'Refresh' });
    await refresh.click();
    try {
      await expect(refresh).toBeDisabled(); // in-flight refresh disables the control
    } finally {
      release(); // guarantee unblock even if assertion fails — avoids 30s CI hang on regression
    }
    await expect(page.getByRole('status')).toHaveText('Refresh complete.');
    await expect(refresh).toBeEnabled(); // settled → re-enabled
  });
});
