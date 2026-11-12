/**
 * Tests for the image-result arm of the Lore proxy tools: which proxies get
 * it (the payload's `image_result_tools`, never a name in the plugin) and
 * what meta the model reads. A stub Tool-API stands in for the backend; the
 * dsh attachment store is a duck-typed mock.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import type { AddressInfo } from 'node:net'

// The Tool-API stub must be up BEFORE tools.ts loads (TOOL_API is read at
// module load — its env read is the test seam).
const envelopes: Record<string, unknown> = {}
const server = http.createServer((req, res) => {
  const tool = String(req.url).split('/').pop() ?? ''
  req.resume()
  req.on('end', () => {
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(envelopes[tool] ?? { error: 'unknown' }))
  })
})
await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
process.env.LORE_TOOL_API_URL = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
const tools = await import('../src/tools.ts')
test.after(() => server.close())

const PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
const REF = { attachmentId: 'sha256:aa', mediaType: 'image/png', bytes: 3, width: 640, height: 480 }

function harness(imageResult: string[]) {
  const registered = new Map<string, any>()
  const ctx: any = {
    tools: { register: (def: any) => { registered.set(def.name, def); return () => {} } },
    get: (key: string) => (key === 'attachments' ? { saveImage: async () => REF } : undefined),
  }
  tools.disposeLoreTools()
  tools.registerLoreTools(ctx, [
    { function: { name: 'sandbox_view_image', description: 's', parameters: {} } },
    { function: { name: 'view_chat_image', description: 'v', parameters: {} } },
    { function: { name: 'read_document', description: 'r', parameters: {} } },
  ], true, new Set(), new Set(imageResult))
  const dshId = 'dsh-img'
  tools.setSessionToolCtx(dshId, {
    loreSessionId: 's1', agentKey: 'k', mutating: new Set(), holdable: new Set(),
    perCallApproval: new Set(),
  })
  tools.stampTurnRequestCtx(dshId, {
    messageId: 'm1', applyMode: 'auto', document_id: null, vision: true,
  })
  const exec = { agent: { session: { id: dshId } }, callId: 'c1' }
  return { registered, exec }
}

// TEST-WHY: cross-process-contract — the backend's `image_result_tools` decides the arm; a tool named there other than view_chat_image gets the image block and its envelope's non-image members as meta.
test('a tool named in image_result_tools gets the image arm with its own meta', async () => {
  envelopes.sandbox_view_image = { image: { media_type: 'image/png', data: PNG_B64 }, path: 'out/chart.png' }
  const { registered, exec } = harness(['sandbox_view_image', 'view_chat_image'])
  const def = registered.get('sandbox_view_image')
  const value = await def.execute({ file_path: 'out/chart.png' }, exec)
  assert.deepEqual(value.meta, { path: 'out/chart.png', media_type: 'image/png', width: 640, height: 480 })
  const blocks = def.output.render({}, value)
  assert.equal(blocks[0].type, 'text')
  assert.equal(blocks[1].type, 'image')
  assert.equal(registered.get('read_document').output.schema.type, 'string')
})

// TEST-WHY: cross-process-contract — the person's view_chat_image plate parses these exact meta keys; the generalized arm must keep them.
test('view_chat_image meta keys are unchanged', async () => {
  envelopes.view_chat_image = {
    image: { media_type: 'image/png', data: PNG_B64 },
    chat_id: 'c', message_id: 'm', index: 0, chat_title: 't',
  }
  const { registered, exec } = harness(['sandbox_view_image', 'view_chat_image'])
  const value = await registered.get('view_chat_image').execute({}, exec)
  assert.deepEqual(Object.keys(value.meta).sort(),
    ['chat_id', 'chat_title', 'height', 'index', 'media_type', 'message_id', 'width'])
})
