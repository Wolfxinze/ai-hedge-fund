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
