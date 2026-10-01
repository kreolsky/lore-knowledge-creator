import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import http from 'node:http'
import type { AddressInfo } from 'node:net'
import { fileURLToPath } from 'node:url'

import { foldDriverSecret, retireLegacySettings, toolApiUrl } from '../src/boot-env.ts'

const BOOT_ENV = fileURLToPath(new URL('../src/boot-env.ts', import.meta.url))

test('the entry boots with NO backend reachable — the boot fetch is gone', async () => {
  // The title model and the web-search provider/credential ride every TURN
  // payload now, so NOTHING calls the backend from the entry. The stub
  // server FAILS the test if it is ever reached; the spawned entry must get
  // PAST the entry stage — it either reaches the plugin's listener (a
  // runtime layout with the lore profile, like the dev/gray container) or
  // dies inside the CLI boot on the missing profile (the image build's test
  // layer runs before `COPY home`): both prove no boot fetch gates the boot.
  let hits = 0
  const server = http.createServer((_req, res) => {
    hits += 1
    res.writeHead(500).end('must not be called')
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  const proc = spawn(
    process.execPath,
    ['--import', 'tsx/esm', BOOT_ENV, '--profile', 'lore'],
    { env: { ...process.env, LORE_TOOL_API_URL: `http://127.0.0.1:${port}`, PORT: '18091' } },
  )
  let stderr = ''
  proc.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString() })
  const outcome = new Promise<'listened' | 'exited' | 'timeout'>((resolve) => {
    proc.on('close', () => resolve('exited'))
    const done = (value: 'listened' | 'exited' | 'timeout') => { clearInterval(poll); clearTimeout(timer); resolve(value) }
    const poll = setInterval(() => {
      if (/lore-driver\] listening/.test(stderr)) done('listened')
    }, 100)
    const timer = setTimeout(() => done('timeout'), 60_000)
  })
  try {
    const result = await outcome
    assert.equal(hits, 0, `the entry called the backend ${hits} time(s) at boot`)
    assert.notEqual(result, 'timeout', `the harness never listened nor exited; stderr:\n${stderr}`)
    if (result === 'exited') {
      // The build layout: acceptable ONLY as the CLI's own profile error —
      // anything earlier (a fetch, a secret gate) is a boot-time coupling.
      assert.match(stderr, /dsh: profile "lore" does not exist/, `stderr:\n${stderr}`)
    }
  } finally {
    proc.kill('SIGTERM')
    server.close()
  }
})

test('toolApiUrl: no env resolves the compose constant; the env stays the test seam', () => {
  // No LORE_TOOL_API_URL: the tools talk to the backend by its compose
  // service name — wiring, not configuration (plan
  // component-wiring-not-settings step 2). The env read remains ONLY as the
  // seam (the boot test above points it at a stub).
  assert.equal(toolApiUrl({}), 'http://backend:8001')
  assert.equal(toolApiUrl({ LORE_TOOL_API_URL: 'http://stub:1' }), 'http://stub:1')
})

test('retireLegacySettings moves a 0.1.5 settings.yaml aside (models-only lore import is INVALID at 0.2.0)', async () => {
  const { mkdtempSync, writeFileSync, existsSync, rmSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const dir = mkdtempSync(join(tmpdir(), 'boot-env-retire-'))
  try {
    // The exact legacy shape: providers.lore with models, NO api/baseURL —
    // the 0.2.0 on-boot import composes it into the active profile patch and
    // llm-pi-ai refuses the boot ("provider lore model … needs an api")
    // before any plugin write can heal it.
    writeFileSync(join(dir, 'settings.yaml'),
      'llm-pi-ai:\n  providers:\n    lore:\n      models:\n        - id: openai/luna\n')
    const lines: string[] = []
    assert.equal(retireLegacySettings(dir, l => lines.push(l)), true)
    assert.equal(existsSync(join(dir, 'settings.yaml')), false)
    assert.equal(existsSync(join(dir, 'settings.yaml.retired-0.1.5')), true)
    assert.match(lines[0] ?? '', /retired the 0\.1\.5 settings\.yaml/)
    // Idempotent: the next boot (the import renames the file itself) finds nothing.
    const again: string[] = []
    assert.equal(retireLegacySettings(dir, l => again.push(l)), false)
    assert.deepEqual(again, [])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('no plugin module imports boot-env.ts — it is the entry, and a re-import deadlocks the boot', async () => {
  const { readdirSync, readFileSync } = await import('node:fs')
  const { join } = await import('node:path')
  const src = fileURLToPath(new URL('../src', import.meta.url))
  const importers = readdirSync(src, { recursive: true, encoding: 'utf8' })
    .filter(rel => rel.endsWith('.ts') && !rel.endsWith('boot-env.ts'))
    .filter(rel => /from\s+['"][^'"]*boot-env(\.ts)?['"]/.test(readFileSync(join(src, rel), 'utf8')))
  assert.deepEqual(importers, [])
})

test('foldDriverSecret: the file wins over env; no file leaves env untouched', () => {
  const dir = mkdtempSync(join(tmpdir(), 'driver-secret-'))
  const file = join(dir, 'driver_secret')
  writeFileSync(file, 'from-file\n')

  // The generated file IS the secret — env cannot shadow it (the backend's
  // twin INVARIANT: a settable value was a value the two sides could
  // disagree on).
  const shadowed: NodeJS.ProcessEnv = { LORE_DRIVER_SECRET: 'from-env' }
  foldDriverSecret(shadowed, file)
  assert.equal(shadowed.LORE_DRIVER_SECRET, 'from-file')

  // Absent file ⇒ env untouched — the TEST seam (driver.test.ts boots with
  // LORE_DRIVER_SECRET when no secrets volume is mounted).
  const kept: NodeJS.ProcessEnv = { LORE_DRIVER_SECRET: 'from-env' }
  foldDriverSecret(kept, join(dir, 'absent'))
  assert.equal(kept.LORE_DRIVER_SECRET, 'from-env')

  const none: NodeJS.ProcessEnv = {}
  foldDriverSecret(none, join(dir, 'absent'))
  assert.equal(none.LORE_DRIVER_SECRET, undefined)
})
