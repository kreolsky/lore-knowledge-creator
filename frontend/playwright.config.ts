/**
 * Playwright config for the Dockerized E2E harness.
 *
 * ARCH: runs inside the compose `e2e` service, never on the host. The app is
 * reached by compose SERVICE NAME (`http://frontend:5173`) via E2E_BASE_URL —
 * `localhost` inside the container is the container itself. One config serves
 * both the container (default) and host debugging (override E2E_BASE_URL).
 *
 * WHY domcontentloaded, not networkidle: doc pages hold long-lived WS/SSE
 * connections, so `networkidle` never settles. Specs wait on explicit
 * requests/state instead (see e2e/helpers/net.ts).
 */
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.E2E_BASE_URL ?? 'http://frontend:5173';

export default defineConfig({
  testDir: './e2e',
  outputDir: './e2e/.out/results',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  // WHY (D1, report isolation): the HTML report MUST live OUTSIDE the Vite-served
  // root (/app). Writing it under ./e2e/.out pulled report assets (e.g.
  // playwright-logo.svg referenced by the bundled report CSS) into Vite's module
  // graph; once cleaned, every transform of src/index.css re-resolved the stale ref
  // → "Internal server error". Absolute /e2e-html-report is bind-mounted from host
  // ./e2e-html-report (docker-compose.yml e2e service) — off the Vite root yet still
  // browsable on the host. See .kilo/plans/1783883552954-vite-e2e-report-isolation.md.
  reporter: [['list'], ['html', { outputFolder: '/e2e-html-report', open: 'never' }]],
  use: {
    baseURL,
    storageState: './e2e/.out/storage-state.json',
    trace: 'on-first-retry',
    video: 'on-first-retry',
    screenshot: 'only-on-failure',
    // INVARIANT: never default to 'networkidle' — see module WHY above.
    navigationTimeout: 15_000,
    actionTimeout: 10_000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
