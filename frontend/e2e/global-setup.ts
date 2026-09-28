/**
 * Global setup: assert the app is reachable on the compose network, then log in
 * once via the auth API (cookie `lore_session` lands in the browser context's
 * jar) and persist `storageState` for reuse across specs.
 *
 * ARCH: fail FAST with a clear message if the app service isn't on the network —
 * a `localhost`/service-name misconfiguration otherwise surfaces as opaque
 * navigation timeouts in every spec.
 *
 * WHY API login (not the form): the cookie is httpOnly and the only auth signal
 * specs need; driving the form adds a flaky client-navigation race for zero
 * coverage here (a dedicated form-login smoke spec belongs in a later phase).
 *
 * DETERMINISM: this setup intentionally does NOT reset per-project panel prefs to
 * a baseline. The right panel's active tab is per-document and persisted
 * server-side (user_preferences), so each spec that depends on a specific tab
 * SEEDS it explicitly via `helpers/prefs.ts` (seedChatTabOpen / seedRefsTabOpen)
 * and merges over existing keys — no clobber, no cross-run carry-over guesswork.
 */
import { chromium, request, type FullConfig } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://frontend:5173';
const STORAGE_STATE = './e2e/.out/storage-state.json';
const EMAIL = process.env.E2E_EMAIL ?? 'red@lore.app';
const PASSWORD = process.env.E2E_PASSWORD ?? 'red';

async function assertReachable(): Promise<void> {
  const ctx = await request.newContext();
  try {
    const res = await ctx.get(BASE_URL, { timeout: 10_000 });
    if (!res.ok()) {
      throw new Error(`App responded ${res.status()} at ${BASE_URL}`);
    }
  } catch (err) {
    throw new Error(
      `E2E app not reachable at ${BASE_URL}. Inside the compose network use the ` +
        `service name (E2E_BASE_URL=http://frontend:5173), not localhost. ` +
        `Underlying error: ${(err as Error).message}`,
    );
  } finally {
    await ctx.dispose();
  }
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  await assertReachable();
  await mkdir(dirname(STORAGE_STATE), { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: BASE_URL });
  try {
    const res = await context.request.post('/api/auth/login', {
      data: { email: EMAIL, password: PASSWORD },
    });
    if (!res.ok()) {
      throw new Error(`Login failed for ${EMAIL}: ${res.status()} ${await res.text()}`);
    }
    await context.storageState({ path: STORAGE_STATE });
  } finally {
    await browser.close();
  }
}
