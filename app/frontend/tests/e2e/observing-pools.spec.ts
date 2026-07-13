import { expect, test, type Page } from '@playwright/test';

import { DISCOVER_RESULT, mockBackend, REFRESH_RESULT, STORED_DISCLAIMER } from './_backend-mock';

// Exact backend URLs for the POST endpoints — per-test overrides use them so Playwright's LIFO
// route ordering fires them before the base mockBackend handler.
const REFRESH_URL = 'http://localhost:8000/observing-pools/refresh';
const DISCOVER_URL = 'http://localhost:8000/serenity/discover';

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

  // Serenity Discover flow (POST /serenity/discover): fills the form, asserts the request carries
  // the user's inputs, and pins the success/partial/error/in-flight surfaces.
  async function openSerenityDiscover(page: Page) {
    await openObservingPools(page);
    await page.getByRole('tab', { name: 'Serenity' }).click();
    await page.getByLabel('Ticker').fill('AAPL');
    await page.getByLabel('Theme').fill('AI infra');
    await page.getByLabel('Keywords (comma-separated)').fill('HBM, supply');
  }

  test('serenity discover: happy path posts the form, reports the built records, and re-fetches research', async ({ page }) => {
    let discoverBody: Record<string, unknown> | null = null;
    page.on('request', (req) => {
      if (req.method() === 'POST' && req.url() === DISCOVER_URL) discoverBody = req.postDataJSON();
    });

    await openSerenityDiscover(page);
    await page.getByRole('button', { name: 'Discover' }).click();

    // Success summary comes from the API response (1 record, 3 references).
    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();
    // The follow-up GET re-fetched research: the record card renders with the stored disclaimer.
    await expect(page.getByText(/Grade: B/)).toBeVisible();
    await expect(page.getByText(new RegExp(STORED_DISCLAIMER))).toBeVisible();
    // The POST carried the user's inputs, keywords split on commas, and the full 5-dim scorecard.
    expect(discoverBody).not.toBeNull();
    expect(discoverBody!.ticker).toBe('AAPL');
    expect(discoverBody!.theme).toBe('AI infra');
    expect(discoverBody!.keywords).toEqual(['HBM', 'supply']);
    expect(Object.keys(discoverBody!.scorecard as Record<string, number>)).toHaveLength(5);
  });

  test('serenity discover: partial source failures are surfaced, not swallowed', async ({ page }) => {
    await openSerenityDiscover(page);
    await page.route(DISCOVER_URL, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...DISCOVER_RESULT, source_errors: { federal_register: 'Timeout' } }),
      }),
    );

    await page.getByRole('button', { name: 'Discover' }).click();

    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();
    await expect(page.getByText('Some sources failed: federal_register')).toBeVisible();
  });

  test('serenity discover: an upstream failure surfaces "Discovery failed." and stays retryable', async ({ page }) => {
    await openSerenityDiscover(page);
    await page.route(DISCOVER_URL, (route) =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({ detail: "all evidence sources errored for 'AAPL'" }),
      }),
    );

    await page.getByRole('button', { name: 'Discover' }).click();

    await expect(page.getByText(/Discovery failed\./)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Discover' })).toBeEnabled(); // retryable after failure
  });

  test('serenity discover: button is disabled while the discovery is in flight', async ({ page }) => {
    await openSerenityDiscover(page);
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route(DISCOVER_URL, async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(DISCOVER_RESULT),
      });
    });

    const discover = page.getByRole('button', { name: 'Discover' });
    await discover.click();
    try {
      await expect(discover).toBeDisabled(); // in-flight discovery disables the control
      await expect(discover).toHaveText('Discovering…'); // label flips while in-flight
    } finally {
      release(); // guarantee unblock even if assertion fails — avoids 30s CI hang on regression
    }
    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();
    await expect(discover).toBeEnabled(); // settled → re-enabled
  });

  test('serenity discover: button is disabled until ticker, theme, and keywords are all filled', async ({ page }) => {
    await openObservingPools(page);
    await page.getByRole('tab', { name: 'Serenity' }).click();
    const discover = page.getByRole('button', { name: 'Discover' });
    await expect(discover).toBeDisabled(); // nothing filled

    await page.getByLabel('Ticker').fill('AAPL');
    await expect(discover).toBeDisabled(); // theme + keywords still missing
    await page.getByLabel('Theme').fill('AI infra');
    await expect(discover).toBeDisabled(); // keywords still missing
    await page.getByLabel('Keywords (comma-separated)').fill(' , ');
    await expect(discover).toBeDisabled(); // only blank keywords
    await page.getByLabel('Keywords (comma-separated)').fill('HBM');
    await expect(discover).toBeEnabled();
  });

  // Issue #76 — E2E coverage for the Pools Refresh button (shipped in PR #75). `name: 'Refresh'`
  // is a substring match, so the same locator resolves the button as its label flips to "Refreshing…".
  test('pools refresh: happy path surfaces "Refresh complete." and re-fetches the pool', async ({ page }) => {
    // Count GET /observing-pools/ai — loadPool's endpoint. A successful refresh calls loadPool
    // AGAIN (pools-panel handleRefresh), so this must strictly increase; the count pins the
    // re-fetch branch (asserting a rendered cell can't fail, since it renders on the first load too).
    let poolGets = 0;
    page.on('request', (req) => {
      const u = new URL(req.url());
      if (req.method() === 'GET' && u.host === 'localhost:8000' && u.pathname === '/observing-pools/ai') poolGets += 1;
    });

    await openObservingPools(page);
    const refresh = page.getByRole('button', { name: 'Refresh' });
    await expect(refresh).toBeEnabled();
    const getsBeforeRefresh = poolGets; // ≥1 from the initial load

    await refresh.click();

    // The role=status region is unique to the refresh feedback; asserts the success i18n string.
    await expect(page.getByRole('status')).toHaveText('Refresh complete.');
    // The post-refresh loadPool re-fetch fired a fresh GET — strictly more than before the click.
    await expect.poll(() => poolGets).toBeGreaterThan(getsBeforeRefresh);
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
      await expect(refresh).toHaveText('Refreshing…'); // label flips while in-flight
    } finally {
      release(); // guarantee unblock even if assertion fails — avoids 30s CI hang on regression
    }
    await expect(page.getByRole('status')).toHaveText('Refresh complete.');
    await expect(refresh).toBeEnabled(); // settled → re-enabled
  });

  test('pools refresh: a generic error surfaces "Refresh failed."', async ({ page }) => {
    await openObservingPools(page);
    await page.route(REFRESH_URL, (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'internal server error' }),
      }),
    );

    await page.getByRole('button', { name: 'Refresh' }).click();

    await expect(page.getByRole('status')).toHaveText('Refresh failed.');
    await expect(page.getByRole('button', { name: 'Refresh' })).toBeEnabled(); // must be retryable
  });

  // rh1 risk-haircut audit (score_breakdown.components.risk_adjusted_momentum.risk_haircut). The
  // robotics pool is scored under an rh1 formula and carries the audit; the "ai" default pool does
  // not. RHF = clean haircut, RHT = degraded (missing price data), NOH = default formula (no audit).
  test('pools breakdown: renders risk-haircut audit fields and a Degraded badge only for a degraded haircut', async ({ page }) => {
    await openObservingPools(page);
    await page.getByLabel('Platform').selectOption('robotics');
    const rhfRow = page.getByRole('row', { name: /RHF/ });
    await expect(rhfRow).toBeVisible();

    // Expand the clean-haircut entry: pre-haircut momentum, haircut points, and volatility % render.
    await rhfRow.getByRole('button', { name: 'Toggle score breakdown' }).click();
    await expect(page.getByText(/Raw momentum: 70\.0/)).toBeVisible();
    await expect(page.getByText(/Haircut: -12\.0/)).toBeVisible();
    await expect(page.getByText('Volatility: 42.0%')).toBeVisible();
    // degraded:false ⇒ NO Degraded badge (the robotics pool has no degraded agents, so the only
    // possible "Degraded" badge on screen comes from a risk_haircut).
    await expect(page.getByText('Degraded', { exact: true })).toHaveCount(0);

    // Expand the degraded-haircut entry (price data too short): the Degraded badge now shows, and
    // the null volatility renders as "—", never 0 or a fabricated percentage.
    await page.getByRole('row', { name: /RHT/ }).getByRole('button', { name: 'Toggle score breakdown' }).click();
    await expect(page.getByText('Degraded', { exact: true })).toHaveCount(1);
    await expect(page.getByText('Volatility: —')).toBeVisible();
  });

  test('pools breakdown: no risk-haircut UI for an entry scored under the default (no-haircut) formula', async ({ page }) => {
    await openObservingPools(page);
    await page.getByLabel('Platform').selectOption('robotics');
    const nohRow = page.getByRole('row', { name: /NOH/ });
    await expect(nohRow).toBeVisible();

    // Expand ONLY the no-haircut entry: none of the haircut labels or a placeholder dash render for
    // a field that simply does not apply under the default momentum-only formula.
    await nohRow.getByRole('button', { name: 'Toggle score breakdown' }).click();
    // The breakdown IS shown (positive control: the risk_adjusted_momentum component value renders)…
    await expect(page.getByText('risk adjusted momentum', { exact: false })).toBeVisible();
    // …but with no haircut audit fields at all.
    await expect(page.getByText(/Raw momentum/)).toHaveCount(0);
    await expect(page.getByText(/Haircut:/)).toHaveCount(0);
    await expect(page.getByText(/Volatility/)).toHaveCount(0);
    await expect(page.getByText('Degraded', { exact: true })).toHaveCount(0);
  });
});
