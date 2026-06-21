module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  // E2E specs + the Playwright config run in a Node context, not the browser env this config
  // targets, so they're intentionally excluded from the app lint gate (no separate lint step
  // runs on them yet). The Playwright runner type-checks and executes them via `npm run test:e2e`.
  ignorePatterns: ['dist', '.eslintrc.cjs', 'tests', 'playwright.config.ts', 'playwright-report', 'test-results'],
  parser: '@typescript-eslint/parser',
  plugins: ['react-refresh'],
  rules: {
    'react-refresh/only-export-components': [
      'warn',
      { allowConstantExport: true },
    ],
  },
}
