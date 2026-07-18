import { expect, test, type Page } from '@playwright/test';

import { DISCOVER_RESULT, mockBackend, REFRESH_RESULT, SEEK_RESULT, STORED_DISCLAIMER } from './_backend-mock';

// Exact backend URLs for the POST endpoints — per-test overrides use them so Playwright's LIFO
// route ordering fires them before the base mockBackend handler.
const REFRESH_URL = 'http://localhost:8000/observing-pools/refresh';
const DISCOVER_URL = 'http://localhost:8000/serenity/discover';
const SEEK_URL = 'http://localhost:8000/serenity/seek';
// GET /serenity/research/<ticker>?limit=50 — the discover follow-up re-fetch. Glob so a per-test
// override matches any ticker + query string; LIFO fires it before mockBackend's catch-all.
const RESEARCH_URL_GLOB = 'http://localhost:8000/serenity/research/**';

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
    expect(Object.keys(discoverBody!.scorecard as Record<string, number>).sort()).toEqual([
      'capacity_expansion',
      'certification_strictness',
      'purity_precision',
      'supplier_concentration',
      'validation_cycle',
    ]);
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

  test('serenity discover: rolled-back evidence groups are surfaced alongside the success summary', async ({ page }) => {
    await openSerenityDiscover(page);
    await page.route(DISCOVER_URL, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...DISCOVER_RESULT, failed_groups: 1 }),
      }),
    );

    await page.getByRole('button', { name: 'Discover' }).click();

    // The success summary and the failed-groups warning coexist — neither overwrites the other.
    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();
    await expect(page.getByText('Failed to persist 1 evidence group(s); see server logs.')).toBeVisible();
  });

  test('serenity discover: a no-evidence result reports the no-evidence copy', async ({ page }) => {
    await openSerenityDiscover(page);
    await page.route(DISCOVER_URL, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...DISCOVER_RESULT, records: [], reference_count: 0 }),
      }),
    );

    await page.getByRole('button', { name: 'Discover' }).click();

    await expect(page.getByText('No allowlisted evidence found for this ticker.')).toBeVisible();
  });

  test('serenity discover: clearing a scorecard dimension disables Discover; a valid 0-4 integer re-enables', async ({ page }) => {
    await openSerenityDiscover(page); // ticker + theme + keywords filled → scorecard is the only gate
    const discover = page.getByRole('button', { name: 'Discover' });
    await expect(discover).toBeEnabled(); // scorecard prefilled with valid values

    // Clearing a dimension is not a silent 0 — it is invalid and disables the control.
    await page.getByLabel('Supplier concentration').fill('');
    await expect(discover).toBeDisabled();

    // Refilling a valid 0-4 integer re-enables it.
    await page.getByLabel('Supplier concentration').fill('2');
    await expect(discover).toBeEnabled();
  });

  test('serenity discover: a 422 validation error renders the offending field from the detail array', async ({ page }) => {
    await openSerenityDiscover(page);
    // FastAPI RequestValidationError shape: detail is an array of {loc, msg, type}. Pins the
    // errorMessage() array branch that renders the first item as "<loc.join('.')>: <msg>".
    await page.route(DISCOVER_URL, (route) =>
      route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: [
            { loc: ['body', 'sources'], msg: 'List should have at least 1 item after validation, not 0', type: 'too_short' },
          ],
        }),
      }),
    );

    await page.getByRole('button', { name: 'Discover' }).click();

    // The discover error area prefixes "Discovery failed." then the rendered array detail.
    await expect(page.getByText(/Discovery failed\./)).toBeVisible();
    await expect(page.getByText(/body\.sources: List should have at least 1 item/)).toBeVisible();
  });

  test('serenity discover: a re-fetch failure is a search error, not a discovery failure', async ({ page }) => {
    await openSerenityDiscover(page);
    // POST /serenity/discover stays on the default success mock; only the follow-up GET fails. Its
    // failure must surface as the generic search error, leaving the discover success intact and
    // NEVER mislabelling it as "Discovery failed.".
    await page.route(RESEARCH_URL_GLOB, (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'boom' }),
      }),
    );

    await page.getByRole('button', { name: 'Discover' }).click();

    // Discovery itself succeeded — its summary stays visible.
    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();
    // The list re-fetch failure surfaces as the generic search error (observingPools.error + message).
    await expect(page.getByText(/boom|Error/)).toBeVisible();
    // Critically: the re-fetch failure is NOT rendered as a discovery failure.
    await expect(page.getByText(/Discovery failed\./)).toHaveCount(0);
  });

  test('serenity discover: editing the ticker clears the stale discover success message', async ({ page }) => {
    await openSerenityDiscover(page);
    await page.getByRole('button', { name: 'Discover' }).click();
    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();

    // Typing a new ticker means the prior success is about a different ticker — it must clear so it
    // doesn't read as being about the newly typed one.
    await page.getByLabel('Ticker').fill('MSFT');
    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toHaveCount(0);
  });

  test('serenity discover: success scrolls the report card into view', async ({ page }) => {
    // A short viewport forces the record card to start below the fold, so the auto-scroll effect in
    // serenity-panel.tsx is the only thing that can bring it into view — remove that effect and this
    // test regresses (toBeInViewport fails at 500px height).
    await page.setViewportSize({ width: 1280, height: 500 });

    await openSerenityDiscover(page);
    await page.getByRole('button', { name: 'Discover' }).click();

    await expect(page.getByText('Built 1 research record(s) from 3 reference(s).')).toBeVisible();
    // The record card is scrolled into view after the discover re-fetch populates it.
    await expect(page.getByText(/Grade: B/)).toBeInViewport();
  });

  test('serenity research: zh-CN renders translated action + canonical disclaimer, sentinel text passes through verbatim', async ({ page }) => {
    // Byte-exact copy of src/compliance.py DISCLAIMER (== lib.ts CANONICAL_DISCLAIMER_EN). Only this
    // exact stored value is swapped for the localized (zh) disclaimer; any other value renders verbatim.
    const CANONICAL_EN =
      'Research and educational use only. This output is not investment advice, not a recommendation to buy or sell any security, and carries no guarantee of accuracy or performance. It contains no trade-execution instructions. Descriptive labels and promote/hold/demote statuses describe research priority, not trading directives. Conduct your own due diligence; consult a licensed professional before investing.';
    // Two records copying the SERENITY_AAPL shape: record 1 carries the canonical disclaimer (→ localized
    // in zh) with a demote action; record 2 carries the sentinel UNKNOWN disclaimer (→ verbatim, drift-safe).
    const records = [
      {
        id: 1, ticker: 'AAPL', platform_key: 'ai', theme: 'AI infra', chain_layer: 'compute',
        bottleneck_hypothesis: 'HBM supply constraint', evidence_grade: 'B', serenity_score: 68,
        recommended_action: 'demote', disclaimer: CANONICAL_EN, disclaimer_version: '2026-06',
      },
      {
        id: 2, ticker: 'AAPL', platform_key: 'ai', theme: 'AI infra', chain_layer: 'compute',
        bottleneck_hypothesis: 'HBM supply constraint', evidence_grade: 'B', serenity_score: 68,
        recommended_action: 'hold', disclaimer: STORED_DISCLAIMER, disclaimer_version: '2026-06',
      },
    ];
    await page.route(RESEARCH_URL_GLOB, (route) => route.fulfill({ json: records }));

    await openObservingPools(page);
    await page.getByRole('tab', { name: 'Serenity' }).click();
    await page.getByLabel('Ticker').fill('AAPL');
    // Switch to zh-CN, then search via the translated button label.
    await page.getByRole('button', { name: 'EN / 中文' }).click();
    await page.getByRole('button', { name: '搜索' }).click();

    // (1) The demote action badge is translated. `exact` targets the badge (whose text is exactly
    // "降级") and excludes the localized disclaimer, which also contains "降级" as a substring.
    await expect(page.getByText('降级', { exact: true })).toBeVisible();
    // (2) The canonical disclaimer renders in Chinese (only the localized canonical text matches this).
    await expect(page.getByText(/仅供研究与教育用途。本输出不构成投资建议/)).toBeVisible();
    // (3) The sentinel record's UNKNOWN disclaimer still renders verbatim — drift-safety pin.
    await expect(page.getByText(new RegExp(STORED_DISCLAIMER))).toBeVisible();
  });

  // Serenity Seek flow (POST /serenity/seek): the UNKNOWN-ticker path. Keywords alone (no ticker)
  // enable the seek; the ranked candidate list pre-fills the ticker on click.
  async function openSerenitySeek(page: Page) {
    await openObservingPools(page);
    await page.getByRole('tab', { name: 'Serenity' }).click();
    await page.getByLabel('Keywords (comma-separated)').fill('HBM, packaging');
  }

  test('serenity seek: keywords-only search lists candidates and clicking one fills the ticker', async ({ page }) => {
    let seekBody: Record<string, unknown> | null = null;
    page.on('request', (req) => {
      if (req.method() === 'POST' && req.url() === SEEK_URL) seekBody = req.postDataJSON();
    });

    await openSerenitySeek(page);
    // The ticker is empty and seek is enabled anyway — a ticker is NOT required for this flow.
    await expect(page.getByLabel('Ticker')).toHaveValue('');
    const seek = page.getByRole('button', { name: 'Seek Candidates' });
    await expect(seek).toBeEnabled();

    await seek.click();

    // The ranked candidate list renders; the TSM filer is clickable, the ticker-less filer is not.
    const tsm = page.getByRole('button', { name: /Taiwan Semiconductor/ });
    await expect(tsm).toBeVisible();
    await expect(page.getByRole('button', { name: /ASML Holding Foundry/ })).toBeDisabled();
    // The POST carried the keywords split on commas.
    expect(seekBody).not.toBeNull();
    expect(seekBody!.keywords).toEqual(['HBM', 'packaging']);

    // Clicking the TSM candidate fills the ticker input with its first symbol.
    await tsm.click();
    await expect(page.getByLabel('Ticker')).toHaveValue('TSM');
  });

  test('serenity seek: an upstream error surfaces "Seek failed." and the button re-enables', async ({ page }) => {
    await openSerenitySeek(page);
    await page.route(SEEK_URL, (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'edgar fts unavailable' }),
      }),
    );

    await page.getByRole('button', { name: 'Seek Candidates' }).click();

    await expect(page.getByText(/Seek failed\./)).toBeVisible();
    await expect(page.getByText(/edgar fts unavailable/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Seek Candidates' })).toBeEnabled(); // retryable
  });

  test('serenity seek: a zero-candidate result renders the explicit empty-state message', async ({ page }) => {
    await openSerenitySeek(page);
    await page.route(SEEK_URL, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ candidates: [], errors: [] }),
      }),
    );

    await page.getByRole('button', { name: 'Seek Candidates' }).click();

    await expect(page.getByText('No candidates matched these keywords.')).toBeVisible();
  });

  test('serenity seek: the button is disabled while the seek is in flight', async ({ page }) => {
    await openSerenitySeek(page);
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route(SEEK_URL, async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(SEEK_RESULT),
      });
    });

    const seek = page.getByRole('button', { name: 'Seek' });
    await seek.click();
    try {
      await expect(seek).toBeDisabled(); // in-flight seek disables the control
      await expect(seek).toHaveText('Seeking…'); // label flips while in-flight
    } finally {
      release(); // guarantee unblock even if an assertion fails — avoids a 30s CI hang on regression
    }
    await expect(page.getByRole('button', { name: /Taiwan Semiconductor/ })).toBeVisible();
    await expect(seek).toBeEnabled(); // settled → re-enabled
  });

  test('serenity seek: a long candidate list is height-bounded and scrolls internally', async ({ page }) => {
    // A 12-row result must not push the seek panel arbitrarily tall — the ul carries
    // `max-h-64 overflow-y-auto`, so it clamps and scrolls internally. Strip that bound and this
    // regresses: the box grows past 280px and scrollHeight collapses to clientHeight.
    const candidates = Array.from({ length: 12 }, (_, idx) => {
      const i = idx + 1;
      return {
        cik: String(i).padStart(10, '0'),
        company: `Filler Corp ${i}`,
        tickers: [`FIL${i}`],
        hits: 13 - i, // descending: 12 → 1
        latest_filing_date: '2026-01-01',
      };
    });

    await openSerenitySeek(page);
    await page.route(SEEK_URL, (route) => route.fulfill({ json: { candidates, errors: [] } }));

    await page.getByRole('button', { name: 'Seek Candidates' }).click();

    // (1) all 12 rows are attached — first is visible, last is present in the (scrollable) DOM.
    await expect(page.getByRole('button', { name: /^Filler Corp 1 FIL1\b/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Filler Corp 12/ })).toBeAttached();

    // (2) the candidate <ul> is height-bounded (max-h-64 == 16rem == 256px, so <= 280 with padding).
    const list = page
      .getByRole('list')
      .filter({ has: page.getByRole('button', { name: /Filler Corp 1/ }) });
    const box = await list.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.height).toBeLessThanOrEqual(280);

    // (3) the content overflows the clamped box — it scrolls internally rather than growing the panel.
    expect(await list.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
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
    // A degraded haircut has haircut_points 0 → render an em dash, never a contradictory "-0.0".
    await expect(page.getByText('Haircut: —')).toBeVisible();
    await expect(page.getByText(/Haircut: -0/)).toHaveCount(0);
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
