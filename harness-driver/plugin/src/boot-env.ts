/**
 * Harness entry: fold the driver secret, retire legacy state, then run the
 * dsh CLI in the same process.
 *
 * # ARCH: the harness boots STANDALONE — every admin-owned value (the AI
 *   gateway, the session title model, the web-search provider and its
 *   credential) rides a TURN payload and is applied with dsh's own calls at
 *   the turn's start (plugin index.ts), so there is no boot-time backend
 *   round trip and no `docker compose restart harness` class of setting any
 *   more.
 */

import { existsSync, readFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The generated driver secret secrets-init writes into the `secrets` volume
 * (mounted at /secrets — the `harness` subpath). The file, when present, IS
 * the secret: it overrides `LORE_DRIVER_SECRET` unconditionally, mirroring
 * the backend's constant (config.py reads the same volume) — a settable env
 * value was a value the two sides could disagree on. No file (no volume
 * mounted) leaves the env untouched: the TEST seam (driver.test.ts boots
 * with LORE_DRIVER_SECRET). Runs before anything reads the env (tools.ts and
 * index.ts read it at import, inside `runCli`).
 */
export const DRIVER_SECRET_FILE = '/secrets/driver_secret'

export function foldDriverSecret(
  env: NodeJS.ProcessEnv = process.env,
  file: string = DRIVER_SECRET_FILE,
): void {
  if (!existsSync(file)) return
  env.LORE_DRIVER_SECRET = readFileSync(file, 'utf8').trim()
}

/**
 * The Tool-API base URL: the compose service name constant, with the env read
 * kept ONLY as the TEST seam (boot-env.test.ts / the stub-server tests point
 * it at a local listener). Wiring, not configuration (plan
 * component-wiring-not-settings step 2) — no compose sets LORE_TOOL_API_URL.
 * tools.ts carries its own copy of the literal: it cannot import from here
 * (see the entry-module INVARIANT test — a re-import deadlocks the boot).
 */
export function toolApiUrl(env: NodeJS.ProcessEnv = process.env): string {
  return env.LORE_TOOL_API_URL || 'http://backend:8001'
}

/** The 0.1.5 settings.yaml is DERIVED state (its own header: "nothing is
 * hand-maintained here"), but its llm-pi-ai.providers.lore carries model
 * entries WITHOUT the api/baseURL the 0.2.0 catalog validation requires —
 * the on-boot legacy import composes them into the active profile patch and
 * llm-pi-ai refuses the WHOLE boot ("provider lore model … needs an api",
 * INVALID_CONFIG) before any plugin write can heal it. Retire the file
 * BEFORE runCli: the turns re-land every used model with the route scaffold
 * (ensureModelEntry / ensureTitleEntry), so only unused ids stay unlanded.
 * Bytes kept for forensics; idempotent — dsh's own import renames the
 * file after the first successful boot, so this fires at most once per home. */
export function retireLegacySettings(
  dshHome: string, log: (line: string) => void = () => {},
): boolean {
  const legacy = join(dshHome, 'settings.yaml')
  if (!existsSync(legacy)) return false
  renameSync(legacy, `${legacy}.retired-0.1.5`)
  log('[boot-env] retired the 0.1.5 settings.yaml (derived models, re-landed per turn) '
    + '— its providers.lore would import without api/baseURL and fail 0.2.0 validation')
  return true
}

if (import.meta.main) {
  foldDriverSecret()
  if (process.env.DSH_HOME) {
    retireLegacySettings(process.env.DSH_HOME, line => console.error(line))
  }
  // WHY runCli() and not a bare import: bin.ts only self-runs as the entry
  // module (`import.meta.main`); imported, it just exports runCli.
  const { runCli } = await import('../../apps/cli/src/bin.ts')
  await runCli()
}
