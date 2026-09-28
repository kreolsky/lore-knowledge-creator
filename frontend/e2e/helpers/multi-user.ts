/**
 * Second-user login + context factory for multi-user e2e specs.
 *
 * The default `storageState` (`global-setup.ts`) is `red@lore.app`. Specs that
 * need a SECOND collaborator (e.g. cross-user table sync) must authenticate a
 * separate identity and build a `BrowserContext` carrying its session cookie.
 *
 * WHY API login (not the form): mirrors `global-setup.ts` — the httpOnly
 * `lore_session` cookie is the only auth signal; driving the form adds a flaky
 * client-navigation race for zero coverage.
 *
 * Parametrized via env (`E2E_EMAIL_2` / `E2E_PASSWORD_2`) with the seed dev
 * fallback (`black@lore.app` / `black`) so it is not hard-coupled to the seed.
 */
import { request, type APIRequestContext, type Browser, type BrowserContext } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://frontend:5173';
const EMAIL_2 = process.env.E2E_EMAIL_2 ?? 'black@lore.app';
const PASSWORD_2 = process.env.E2E_PASSWORD_2 ?? 'black';
const STORAGE_STATE_2 = './e2e/.out/storage-state-black.json';

export interface SecondUser {
  email: string;
  storageState: string;
  newContext: (browser: Browser) => Promise<BrowserContext>;
}

/**
 * Authenticate the second user via the auth API and persist its storageState.
 * Returns a factory bound to that storage state so a spec can spawn contexts for
 * the second user from the same shared browser.
 */
export async function loginSecondUser(): Promise<SecondUser> {
  await mkdir(dirname(STORAGE_STATE_2), { recursive: true });
  const ctx: APIRequestContext = await request.newContext({ baseURL: BASE_URL });
  try {
    const res = await ctx.post('/api/auth/login', {
      data: { email: EMAIL_2, password: PASSWORD_2 },
    });
    if (!res.ok()) {
      throw new Error(`Second-user login failed for ${EMAIL_2}: ${res.status()} ${await res.text()}`);
    }
    await ctx.storageState({ path: STORAGE_STATE_2 });
  } finally {
    await ctx.dispose();
  }
  return {
    email: EMAIL_2,
    storageState: STORAGE_STATE_2,
    newContext: (browser: Browser) =>
      browser.newContext({ baseURL: BASE_URL, storageState: STORAGE_STATE_2 }),
  };
}
