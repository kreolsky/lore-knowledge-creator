import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import http from 'node:http'
import type { AddressInfo } from 'node:net'
import { fileURLToPath } from 'node:url'

import { applyBootEnv, BootEnvRefused, fetchBootEnv, WEB_SEARCH_PIN } from '../src/boot-env.ts'

const BOOT_ENV = fileURLToPath(new URL('../src/boot-env.ts', import.meta.url))

function answer(status: number, body: unknown) {
  const calls: { url: string, headers: Record<string, string> }[] = []
  const fetchImpl = async (url: string, init?: RequestInit) => {
    calls.push({ url, headers: init?.headers as Record<string, string> })
    return new Response(JSON.stringify(body), { status })
  }
  return { calls, fetchImpl }
}

test('fetchBootEnv sends the driver secret and returns the env map', async () => {
  const { calls, fetchImpl } = answer(200, { DSH_WEB_SEARCH_PROVIDER: 'lore-brave', BRAVE_API_KEY: 'k' })
  const env = await fetchBootEnv('http://backend:8001/', 's3cret', { fetchImpl })
  assert.deepEqual(env, { DSH_WEB_SEARCH_PROVIDER: 'lore-brave', BRAVE_API_KEY: 'k' })
  assert.equal(calls[0].url, 'http://backend:8001/api/driver/web-search')
  assert.equal(calls[0].headers['X-Driver-Secret'], 's3cret')
})

test('fetchBootEnv retries a connect error, then succeeds', async () => {
  let n = 0
  const fetchImpl = async () => {
    n += 1
    if (n < 3) throw new TypeError('fetch failed')
    return new Response('{}', { status: 200 })
  }
  assert.deepEqual(await fetchBootEnv('http://b', 's', { fetchImpl, delayMs: 0 }), {})
  assert.equal(n, 3)
})

test('fetchBootEnv gives up after the last connect attempt', async () => {
  let n = 0
  const fetchImpl = async () => { n += 1; throw new TypeError('fetch failed') }
  await assert.rejects(fetchBootEnv('http://b', 's', { fetchImpl, delayMs: 0, attempts: 4 }), TypeError)
  assert.equal(n, 4)
})

test('fetchBootEnv does not retry a refusal: 401 names the secret, 500 carries the body', async () => {
  const refused = answer(401, { detail: 'Driver secret required' })
  await assert.rejects(fetchBootEnv('http://b', 'bad', { fetchImpl: refused.fetchImpl, delayMs: 0 }),
    (e: unknown) => e instanceof BootEnvRefused && /HARNESS_DRIVER_SECRET/.test(e.message))
  assert.equal(refused.calls.length, 1)
  const broken = answer(500, { detail: 'boom' })
  await assert.rejects(fetchBootEnv('http://b', 's', { fetchImpl: broken.fetchImpl, delayMs: 0 }),
    (e: unknown) => e instanceof BootEnvRefused && /500/.test(e.message) && /boom/.test(e.message))
  assert.equal(broken.calls.length, 1)
})

test('applyBootEnv sets the pairs and removes a pin the response omits', () => {
  const target: NodeJS.ProcessEnv = { [WEB_SEARCH_PIN]: 'stale', DEEPSEEK_API_KEY: 'env-key', OTHER: 'x' }
  applyBootEnv({ DEEPSEEK_API_KEY: 'db-key', DSH_WEB_SEARCH_PROVIDER: 'deepseek-official' }, target)
  assert.deepEqual(target, { DSH_WEB_SEARCH_PROVIDER: 'deepseek-official', DEEPSEEK_API_KEY: 'db-key', OTHER: 'x' })
  applyBootEnv({}, target)
  assert.equal(target[WEB_SEARCH_PIN], undefined)
})

test('the entry exits non-zero on a 401, before the CLI starts', async () => {
  const server = http.createServer((_req, res) => {
    res.writeHead(401, { 'content-type': 'application/json' })
    res.end('{"detail":"Driver secret required"}')
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  try {
    // WHY async spawn: the fake backend answers on THIS process's event loop,
    // which spawnSync would block.
    const child = await new Promise<{ status: number | null, stderr: string }>((resolve) => {
      const proc = spawn(process.execPath, ['--import', 'tsx/esm', BOOT_ENV, '--profile', 'lore'], {
        env: { ...process.env, LORE_TOOL_API_URL: `http://127.0.0.1:${port}`, LORE_DRIVER_SECRET: 'bad' },
      })
      let stderr = ''
      proc.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString() })
      proc.on('close', status => resolve({ status, stderr }))
    })
    assert.notEqual(child.status, 0)
    assert.match(child.stderr, /harness boot failed/)
    assert.match(child.stderr, /401/)
  } finally {
    server.close()
  }
})

test('the entry exits non-zero when LORE_TOOL_API_URL is unset', () => {
  const env = { ...process.env }
  delete env.LORE_TOOL_API_URL
  const child = spawnSync(process.execPath, ['--import', 'tsx/esm', BOOT_ENV], { env, encoding: 'utf8' })
  assert.notEqual(child.status, 0)
  assert.match(child.stderr, /LORE_TOOL_API_URL is not set/)
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
