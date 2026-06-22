import { expect, test } from '@playwright/test';

import { mockBackend } from './_backend-mock';

const READBACK_PATH = /^\/api-keys\/.+/; // GET /api-keys/{provider} — the read-back hole Phase 1b closed

// The hard Phase-1b invariant: the API-key settings UI drives off is_set + masked_tail and NEVER
// reads back a secret value (no GET /api-keys/{provider}) — on load OR in the Replace flow.
test('API Keys settings: masked view + Replace, and no per-provider secret read-back', async ({ page }) => {
  await mockBackend(page);

  const apiKeyPaths: string[] = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.host === 'localhost:8000' && u.pathname.startsWith('/api-keys')) apiKeyPaths.push(u.pathname);
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Open Settings' }).click();

  // Settings opens to the API Keys section by default; the configured OpenAI key shows a masked
  // tail + a "Replace key" action — the raw secret is never placed in the DOM.
  await expect(page.getByText('ab12').first()).toBeVisible();
  const replace = page.getByRole('button', { name: 'Replace key' }).first();
  await expect(replace).toBeVisible();

  // The list endpoint was hit, but the per-provider read-back path never was.
  expect(apiKeyPaths.length).toBeGreaterThan(0);
  expect(apiKeyPaths.some((p) => READBACK_PATH.test(p))).toBe(false);

  // The read-back hole is most likely to reopen in the Replace flow — exercise it: entering replace
  // mode reveals a draft input but must still NOT fetch the stored secret.
  await replace.click();
  await expect(page.getByPlaceholder('Enter new key').first()).toBeVisible();
  expect(apiKeyPaths.some((p) => READBACK_PATH.test(p))).toBe(false);
});

// The write-safety invariant (api-keys.tsx handleDraftChange is local-only): editing a key field
// only mutates a transient draft, so neither typing nor clearing + blurring may ever issue a write
// (POST/PUT) or delete (DELETE/PATCH). Only an explicit Save writes; only the Trash button deletes.
test('API Keys settings: editing or clearing a draft issues no write or delete request', async ({ page }) => {
  await mockBackend(page);

  const mutations: string[] = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (
      u.host === 'localhost:8000' &&
      u.pathname.startsWith('/api-keys') &&
      ['POST', 'PUT', 'DELETE', 'PATCH'].includes(req.method())
    ) {
      mutations.push(`${req.method()} ${u.pathname}`);
    }
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Open Settings' }).click();

  // Scope to the CONFIGURED OpenAI row. Its Replace draft inserts at OpenAI's own mid-list DOM
  // position, so a page-wide `.first()` would resolve to the first unset provider (Financial
  // Datasets, always an input) instead — passing for the wrong element and never covering the
  // stored-key Replace path this test documents.
  const openAiRow = page
    .locator('div.space-y-2')
    .filter({ has: page.getByRole('button', { name: 'OpenAI API', exact: true }) });

  // Enter the Replace flow for the configured OpenAI key — its stored secret stays on the backend,
  // so this is the path where an accidental write/delete would be most damaging.
  await openAiRow.getByRole('button', { name: 'Replace key' }).click();
  const input = openAiRow.getByPlaceholder('Enter new key');
  await expect(input).toBeVisible();

  // Typing updates only the local draft — no request fires on keystroke.
  await input.fill('sk-typed-but-not-saved');
  await expect(input).toHaveValue('sk-typed-but-not-saved'); // round-trip: lets any errant fetch surface
  expect(mutations, 'typing a draft must not write or delete').toEqual([]);

  // Clearing the draft + blurring must not delete the stored key (there is no onBlur/auto-delete).
  await input.fill('');
  await input.blur();
  await expect(openAiRow.getByRole('button', { name: 'Save' })).toBeDisabled();
  // Settle the network so a DEBOUNCED auto-write (not just a synchronous one) would also surface.
  await page.waitForLoadState('networkidle');
  expect(mutations, 'clearing + blurring must not delete the stored key').toEqual([]);
});

// The clearKey path (api-keys.tsx) deletes then RE-SYNCS from the backend rather than optimistically
// dropping the row — key-presence is security-sensitive and must reflect the server, never an
// unconfirmed local mutation. Proof: a SECOND list GET fires after the DELETE (an optimistic local
// delete would issue none), and the stateless mock still reports OpenAI set, so the masked view persists.
test('API Keys settings: deleting a key issues one DELETE then re-fetches the authoritative list', async ({ page }) => {
  await mockBackend(page);

  const deletes: string[] = [];
  const listGets: string[] = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.host !== 'localhost:8000') return;
    if (req.method() === 'DELETE' && u.pathname.startsWith('/api-keys')) deletes.push(u.pathname);
    if (req.method() === 'GET' && u.pathname === '/api-keys') listGets.push(u.pathname);
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Open Settings' }).click();

  const openAiRow = page
    .locator('div.space-y-2')
    .filter({ has: page.getByRole('button', { name: 'OpenAI API', exact: true }) });

  // Configured: masked tail + the (now-labelled, icon-only) delete control are present.
  await expect(openAiRow.getByText('ab12')).toBeVisible();
  await expect.poll(() => listGets.length).toBeGreaterThanOrEqual(1); // initial mount load
  const listGetsBeforeDelete = listGets.length;

  await openAiRow.getByRole('button', { name: 'Delete OpenAI API key' }).click();

  // Exactly one DELETE, scoped to OpenAI's provider.
  await expect.poll(() => deletes).toEqual(['/api-keys/OPENAI_API_KEY']);
  // The core of the clearKey change: a fresh list GET fires AFTER the delete (an optimistic local
  // removal would issue none) — the deterministic, non-vacuous proof of the re-sync.
  await expect.poll(() => listGets.length).toBeGreaterThan(listGetsBeforeDelete);
  // And the row re-renders from that authoritative refetch: the stateless mock still reports OpenAI
  // set, so presence is SERVER-driven, not an optimistic removal.
  await expect(openAiRow.getByText('ab12')).toBeVisible();
});
