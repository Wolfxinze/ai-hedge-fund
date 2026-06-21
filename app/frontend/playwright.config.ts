import { defineConfig, devices } from '@playwright/test';

// Hermetic frontend E2E: Playwright drives the real Vite app with all backend calls
// (http://localhost:8000) intercepted via page.route(), so no backend / DB / LLM is needed.
// Kept separate from the tsc/lint/build gates — run with `npm run test:e2e`.
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'list' : 'list',
  use: {
    baseURL: 'http://localhost:5173',
    locale: 'en-US', // navigator.language → app defaults to the English i18n locale
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
