import { expect, test } from '@playwright/test';

// Pipeline smoke test: the dev server boots, the app shell renders, and the top-bar
// "Open Observing Pools" control is present.
test('app shell renders with the Observing Pools control', async ({ page }) => {
  await page.route('http://localhost:8000/**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Open Observing Pools' })).toBeVisible();
});
