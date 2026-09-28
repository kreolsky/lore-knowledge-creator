/**
 * Harness entry: fetch the web-search env from Lore, set it on `process.env`,
 * then run the dsh CLI in the same process.
 *
 * # ARCH: Lore owns the web-search settings (admin page); the harness reads
 *   them ONCE here, before dsh builds its launch-environment snapshot (inside
 *   `runCli` → `loadLayeredEnv`) and before the web seam reads its pin — so a
 *   change applies on `docker compose restart harness`. The backend is the
 *   one source: a pin absent from the response is DELETED from the env, so a
 *   stray `.env` value cannot re-enable a provider the admin turned off.
 */

import { WEB_SEARCH_PIN } from './web-search/pin.ts'

export { WEB_SEARCH_PIN }

/** A GET attempt that could not connect is retried this many times… */
const CONNECT_ATTEMPTS = 10
/** …this far apart (the backend may still be starting; compose restarts us after). */
const CONNECT_DELAY_MS = 2000

export interface FetchBootEnvOptions {
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>
  attempts?: number
  delayMs?: number
  log?: (line: string) => void
}

/** A refusal that retrying cannot fix (the backend answered, and said no). */
export class BootEnvRefused extends Error {}

/**
 * GET the env map. Connect errors are retried; any non-200 answer throws
 * `BootEnvRefused` at once with its status and body.
 */
export async function fetchBootEnv(
  baseUrl: string, secret: string, options: FetchBootEnvOptions = {},
): Promise<Record<string, string>> {
  const { fetchImpl = fetch, attempts = CONNECT_ATTEMPTS, delayMs = CONNECT_DELAY_MS, log = () => {} } = options
  const url = `${baseUrl.replace(/\/+$/, '')}/api/driver/web-search`
  for (let attempt = 1; ; attempt++) {
    let response: Response
    try {
      response = await fetchImpl(url, { headers: { 'X-Driver-Secret': secret } })
    } catch (error: unknown) {
      if (attempt >= attempts) throw error
      log(`[boot-env] ${url} unreachable (${String(error)}), retry ${attempt}/${attempts - 1}`)
      await new Promise(resolve => setTimeout(resolve, delayMs))
      continue
    }
    const body = await response.text()
    if (response.status === 401) {
      throw new BootEnvRefused(
        `${url} refused the driver secret (401) — check HARNESS_DRIVER_SECRET matches the backend's: ${body}`,
      )
    }
    if (response.status !== 200) throw new BootEnvRefused(`${url} answered ${response.status}: ${body}`)
    return JSON.parse(body) as Record<string, string>
  }
}

/** Apply the response to `target`: its pairs win, and a pin it omits is removed. */
export function applyBootEnv(env: Record<string, string>, target: NodeJS.ProcessEnv = process.env): void {
  delete target[WEB_SEARCH_PIN]
  Object.assign(target, env)
}

async function loadWebSearchEnv(): Promise<void> {
  const baseUrl = process.env.LORE_TOOL_API_URL
  if (!baseUrl) throw new Error('LORE_TOOL_API_URL is not set')
  const env = await fetchBootEnv(baseUrl, process.env.LORE_DRIVER_SECRET ?? '', {
    log: line => console.error(line),
  })
  applyBootEnv(env)
  console.error(`[boot-env] web search: ${env[WEB_SEARCH_PIN] ?? 'off'}`)
}

if (import.meta.main) {
  try {
    await loadWebSearchEnv()
  } catch (error: unknown) {
    console.error(`[boot-env] harness boot failed: ${error instanceof Error ? error.message : String(error)}`)
    process.exit(1)
  }
  // WHY runCli() and not a bare import: bin.ts only self-runs as the entry
  // module (`import.meta.main`); imported, it just exports runCli.
  const { runCli } = await import('../../apps/cli/src/bin.ts')
  await runCli()
}
