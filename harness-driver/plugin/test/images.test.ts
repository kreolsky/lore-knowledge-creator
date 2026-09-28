/**
 * Tests for the multimodal turn prompt + the per-turn model-catalog upsert
 * (plan harness-chat-images-reach-the-model; the output-token cap rides the
 * same entry — plan output-token-cap-from-the-router). Run at image build with
 * the other plugin suites: everything here is the pure seams — mapping,
 * orchestration, and the settings upsert — the dsh attachment store and the
 * settings service are duck-typed mocks; the live image wire is the drive's
 * to prove.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { ensureModelEntry, prepareUserContent, promptContent } from '../src/index.ts'

// ── fixtures ─────────────────────────────────────────────────────────────────

function ref(id: string) {
  return { attachmentId: `sha256:${id}`, mediaType: 'image/png', bytes: 3, width: 1, height: 1 }
}

function imgPart(mediaType: string, b64: string) {
  return { type: 'image_url', image_url: { url: `data:${mediaType};base64,${b64}` } }
}

/** Recording store mock; rejects entries whose b64 is listed in `reject`. */
function store(reject: Record<string, string> = {}) {
  const saved: { data: Buffer; mediaType: string }[] = []
  let n = 0
  return {
    saved,
    saveImage: async (input: { data: Uint8Array; mediaType: string }) => {
      saved.push({ data: Buffer.from(input.data), mediaType: input.mediaType })
      const b64 = input.data.toString('base64')
      if (reject[b64]) {
        throw Object.assign(new Error(reject[b64]), { code: reject[b64] })
      }
      n += 1
      return ref(`aa${n}`)
    },
  }
}

/** Settings seam mock: get() reflects every committed update immediately, in
 * the `llm-pi-ai` section shape ({providers: {lore: {models}}}) the plugin
 * speaks; update() applies the patch's providers.lore.models array WHOLESALE
 * (the settings seam's array semantics — objects merge, arrays replace). */
function fakeSettings(models: Record<string, unknown>[] = []) {
  const state = { models: [...models] }
  const updates: { ns: unknown; patch: Record<string, unknown> }[] = []
  return {
    updates,
    get: () => ({ providers: { lore: { models: state.models } } }),
    update: async (ns: unknown, patch: {
      providers?: { lore?: { models?: Record<string, unknown>[] } }
    }) => {
      await new Promise((resolve) => setTimeout(resolve, 5)) // force chain interleaving
      const next = patch.providers?.lore?.models
      if (!Array.isArray(next)) throw new Error('test seam: no providers.lore.models in patch')
      updates.push({ ns, patch: { providers: { lore: { models: [...next] } } } })
      for (const entry of next) {
        state.models = [...state.models.filter((m) => m.id !== entry.id), entry]
      }
    },
  }
}

// ── promptContent ────────────────────────────────────────────────────────────

test('a string prompt stays one text block, no store traffic', async () => {
  const s = store()
  const out = await promptContent('hello', s.saveImage)
  assert.deepEqual(out.blocks, [{ type: 'text', text: 'hello' }])
  assert.equal(out.images, 0)
  assert.equal(s.saved.length, 0)
})

test('a text-only block prompt maps part-for-part, no store traffic', async () => {
  const s = store()
  const out = await promptContent(
    [{ type: 'text', text: 'q1' }, { type: 'text', text: 'q2' }],
    s.saveImage,
  )
  assert.deepEqual(out.blocks, [
    { type: 'text', text: 'q1' },
    { type: 'text', text: 'q2' },
  ])
  assert.equal(out.images, 0)
  assert.equal(s.saved.length, 0)
})

test('one image part decodes, persists, and becomes an image block', async () => {
  const s = store()
  const out = await promptContent(
    [{ type: 'text', text: 'what is this?' }, imgPart('image/png', 'QUJDRA==')],
    s.saveImage,
  )
  assert.equal(out.images, 1)
  assert.equal(s.saved.length, 1)
  assert.deepEqual(s.saved[0]!.data, Buffer.from('QUJDRA==', 'base64'))
  assert.equal(s.saved[0]!.mediaType, 'image/png')
  assert.equal(out.blocks[0]!.type, 'text')
  const image = out.blocks[1]!
  assert.equal(image.type, 'image')
  assert.deepEqual((image as { attachment: unknown }).attachment, ref('aa1'))
})

test('two image parts become two image blocks in order', async () => {
  const s = store()
  const out = await promptContent(
    [imgPart('image/png', 'QUJD'), imgPart('image/jpeg', 'REVG')],
    s.saveImage,
  )
  assert.equal(out.images, 2)
  assert.equal(out.blocks.length, 2)
  assert.equal(s.saved[0]!.mediaType, 'image/png')
  assert.equal(s.saved[1]!.mediaType, 'image/jpeg')
  assert.deepEqual(
    out.blocks.map((b) => (b as { attachment: { attachmentId: string } }).attachment.attachmentId),
    ['sha256:aa1', 'sha256:aa2'],
  )
})

test('a non-data URL becomes a not-delivered text block, never a silent drop', async () => {
  const s = store()
  const out = await promptContent(
    [{ type: 'text', text: 'q' }, imgPart('image/png', 'QUJD'),
      { type: 'image_url', image_url: { url: 'https://example.com/x.png' } }],
    s.saveImage,
  )
  assert.equal(out.images, 1)
  assert.equal(s.saved.length, 1) // only the data-URI part reached the store
  assert.equal(out.blocks.length, 3)
  assert.deepEqual(out.blocks[2], {
    type: 'text',
    text: '[attachment not delivered: image url is not a data URI]',
  })
})

test('a part the store rejects names its stable code in the text block', async () => {
  // undecodable payload (sniffed bytes are not an image)
  const s1 = store({ '////': 'INVALID_IMAGE' })
  const out1 = await promptContent([imgPart('image/png', '////')], s1.saveImage)
  assert.equal(out1.images, 0)
  assert.deepEqual(out1.blocks, [{
    type: 'text',
    text: '[attachment not delivered: INVALID_IMAGE]',
  }])

  // declared type does not match the bytes
  const s2 = store({ QUJD: 'IMAGE_TYPE_MISMATCH' })
  const out2 = await promptContent([imgPart('image/png', 'QUJD')], s2.saveImage)
  assert.equal(out2.images, 0)
  assert.deepEqual(out2.blocks, [{
    type: 'text',
    text: '[attachment not delivered: IMAGE_TYPE_MISMATCH]',
  }])
})

test('an unknown part type is named, not silently emptied away', async () => {
  const s = store()
  const out = await promptContent(
    [{ type: 'video_url', video_url: { url: 'x' } }] as unknown[],
    s.saveImage,
  )
  assert.equal(out.images, 0)
  assert.deepEqual(out.blocks, [{
    type: 'text',
    text: '[attachment not delivered: unsupported prompt part "video_url"]',
  }])
})

test('a malformed prompt degrades to one empty text block (the old shape)', async () => {
  const s = store()
  assert.deepEqual(await promptContent(undefined, s.saveImage), {
    blocks: [{ type: 'text', text: '' }],
    images: 0,
  })
  assert.deepEqual(await promptContent([], s.saveImage), {
    blocks: [{ type: 'text', text: '' }],
    images: 0,
  })
})

// ── ensureModelEntry (the per-turn catalog upsert) ───────────────────────────

test('first image turn writes the image-capable entry to the llm-pi-ai section', async () => {
  const settings = fakeSettings()
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, true, null)
  assert.equal(settings.updates.length, 1)
  assert.equal(settings.updates[0]!.ns, 'llm-pi-ai')
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{
      id: 'm/x', input: ['text', 'image'],
      contextWindow: 128000, maxTokens: 65536,
    }] } },
  })
})

test('a text-only turn writes the entry with maxTokens and NO input field', async () => {
  const settings = fakeSettings()
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null)
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/x', contextWindow: 128000, maxTokens: 65536 }] } },
  })
})

test('advertised levels land as the reasoningEfforts declaration (pi-ai keys, gateway wire values)', async () => {
  const settings = fakeSettings()
  await ensureModelEntry(settings as never, 'openai/luna', 1050000, 128000, false, ['none', 'low', 'high'])
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{
      id: 'openai/luna',
      reasoningEfforts: { off: 'none', low: 'low', high: 'high' },
      contextWindow: 1050000, maxTokens: 128000,
    }] } },
  })
  // Identity levels pass through verbatim (orange/reasoner's offer).
  const settings2 = fakeSettings()
  await ensureModelEntry(settings2 as never, 'local/orange/reasoner', 131072, 65536, false, ['low', 'medium', 'xhigh'])
  assert.deepEqual(
    (settings2.updates[0]!.patch.providers as { lore: { models: Record<string, unknown>[] } }).lore.models[0]!.reasoningEfforts,
    { low: 'low', medium: 'medium', xhigh: 'xhigh' })
})

test('a changed offer rewrites the declaration; supported=false DROPS a committed one', async () => {
  const settings = fakeSettings([
    { id: 'm/x', reasoningEfforts: { low: 'low' }, contextWindow: 128000, maxTokens: 65536 },
  ])
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, [])
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/x', contextWindow: 128000, maxTokens: 65536 }] } },
  }, 'the empty offer is the WHOLE offer — no stale declaration survives')
})

test('an off-only offer writes NO declaration — and drops a committed one', async () => {
  // pi-ai refuses a declaration offering nothing beyond off, and a failed
  // catalog resolution takes the WHOLE lore route out — the model runs
  // non-reasoning instead of killing every sibling entry.
  const settings = fakeSettings()
  await ensureModelEntry(settings as never, 'm/none-only', 128000, 65536, false, ['none'])
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/none-only', contextWindow: 128000, maxTokens: 65536 }] } },
  })
  // A committed off-only declaration (written before this rule) is dropped:
  // the offer is the WHOLE offer.
  const stale = fakeSettings([
    { id: 'm/x', reasoningEfforts: { off: 'none' }, contextWindow: 128000, maxTokens: 8192 },
  ])
  await ensureModelEntry(stale as never, 'm/x', 128000, 65536, false, ['none'])
  assert.deepEqual((stale.updates[0]!.patch.providers as { lore: { models: Record<string, unknown>[] } }).lore.models[0], {
    id: 'm/x', contextWindow: 128000, maxTokens: 65536,
  }, 'no reasoningEfforts key survives the off-only offer')
})

test('null levels (unreadable gateway) carry the committed declaration verbatim', async () => {
  const settings = fakeSettings([
    { id: 'm/x', reasoningEfforts: { off: 'none', high: 'high' }, contextWindow: 128000, maxTokens: 8192 },
  ])
  // A rewrite forced by the stale maxTokens must NOT strip what it cannot know.
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null)
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{
      id: 'm/x', reasoningEfforts: { off: 'none', high: 'high' },
      contextWindow: 128000, maxTokens: 65536,
    }] } },
  })
  // And with nothing else to change, null levels skip the write entirely.
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null)
  assert.equal(settings.updates.length, 1)
})

test('a boot (ifAbsent) upsert leaves a committed entry untouched', async () => {
  // The boot write exists to land an id-only entry for ids the static layer
  // does not declare — it must NOT downgrade an entry a previous turn
  // already filled (NaN caps would strip contextWindow/maxTokens until the
  // model's next turn). Inside the write chain, so no race with a turn.
  const settings = fakeSettings([
    { id: 'm/x', contextWindow: 128000, maxTokens: 65536, reasoningEfforts: { low: 'low' } },
  ])
  await ensureModelEntry(settings as never, 'm/x', Number.NaN, Number.NaN, false, null, true)
  assert.equal(settings.updates.length, 0, 'no id-only downgrade of a committed entry on restart')
  const fresh = fakeSettings()
  await ensureModelEntry(fresh as never, 'm/y', Number.NaN, Number.NaN, false, null, true)
  assert.deepEqual(fresh.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/y' }] } },
  }, 'an absent model still lands its id-only entry')
})

test('a second turn on the same model with the same caps and offer writes nothing', async () => {
  const settings = fakeSettings([
    { id: 'm/x', input: ['text', 'image'], contextWindow: 128000, maxTokens: 65536,
      reasoningEfforts: { low: 'low' } },
  ])
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, true, ['low'])
  // The text-only twin leaves the image-capable entry untouched too — the
  // modality is only REQUIRED on image turns, so no rewrite, no strip.
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, ['low'])
  assert.equal(settings.updates.length, 0)
})

test('a turn whose maxTokens differs from the stale entry rewrites it', async () => {
  const settings = fakeSettings([
    { id: 'm/x', input: ['text', 'image'], contextWindow: 128000, maxTokens: 8192 },
  ])
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null)
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/x', contextWindow: 128000, maxTokens: 65536 }] } },
  })
})

test('a turn on a second model accumulates — both entries survive', async () => {
  const settings = fakeSettings([
    { id: 'm/x', input: ['text', 'image'], contextWindow: 128000, maxTokens: 65536 },
  ])
  await ensureModelEntry(settings as never, 'm/y', 524288, 131072, true, null)
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [
      { id: 'm/x', input: ['text', 'image'], contextWindow: 128000, maxTokens: 65536 },
      { id: 'm/y', input: ['text', 'image'], contextWindow: 524288, maxTokens: 131072 },
    ] } },
  })
})

test('a stale entry for the same id (text-only or old caps) is replaced, not duplicated', async () => {
  const settings = fakeSettings([{ id: 'm/x', input: ['text'] }])
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, true, null)
  const models = (settings.updates[0]!.patch.providers as { lore: { models: Record<string, unknown>[] } }).lore.models
  assert.equal(models.filter((m) => m.id === 'm/x').length, 1)
  assert.equal((models[0]!.input as string[]).includes('image'), true)
  assert.equal(models[0]!.maxTokens, 65536)
})

test('concurrent upserts serialize: two models, both entries survive one chain', async () => {
  const settings = fakeSettings()
  await Promise.all([
    ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null),
    ensureModelEntry(settings as never, 'm/y', 524288, 131072, false, null),
  ])
  assert.equal(settings.updates.length, 2)
  const finalModels = (settings.get() as { providers: { lore: { models: Record<string, unknown>[] } } }).providers.lore.models
  assert.deepEqual(finalModels.map((m) => m.id).sort(), ['m/x', 'm/y'])
  // each write computed from the committed state, not a shared stale snapshot
  const first = settings.updates[0]!.patch.providers as { lore: { models: unknown[] } }
  const second = settings.updates[1]!.patch.providers as { lore: { models: unknown[] } }
  assert.equal(first.lore.models.length, 1)
  assert.equal(second.lore.models.length, 2)
})

test('a garbage cap cannot poison the section — and does not rewrite either', async () => {
  const settings = fakeSettings()
  await ensureModelEntry(settings as never, 'm/x', Number.NaN, Number.NaN, true, null)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/x', input: ['text', 'image'] }] } },
  })
  // The skip check compares the SANITIZED caps (undefined), so a second
  // garbage-cap turn on the same model writes nothing.
  await ensureModelEntry(settings as never, 'm/x', Number.NaN, Number.NaN, false, null)
  assert.equal(settings.updates.length, 1)
})

// ── prepareUserContent (mapping + the unconditional catalog upsert) ─────────

test('a text-only turn upserts the catalog entry (maxTokens) before returning blocks', async () => {
  const s = store()
  const settings = fakeSettings()
  const blocks = await prepareUserContent(
    [{ type: 'text', text: 'q' }], s.saveImage, settings, 'm/x', 128000, 65536, null,
  )
  assert.deepEqual(blocks, [{ type: 'text', text: 'q' }])
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{ id: 'm/x', contextWindow: 128000, maxTokens: 65536 }] } },
  })
})

test('an image turn upserts the capability entry, then returns the blocks', async () => {
  const s = store()
  const settings = fakeSettings()
  const blocks = await prepareUserContent(
    [{ type: 'text', text: 'q' }, imgPart('image/png', 'QUJD')], s.saveImage, settings, 'm/x', 128000, 65536, null,
  )
  assert.equal(settings.updates.length, 1)
  assert.deepEqual(settings.updates[0]!.patch, {
    providers: { lore: { models: [{
      id: 'm/x', input: ['text', 'image'],
      contextWindow: 128000, maxTokens: 65536,
    }] } },
  })
  assert.equal(blocks[0]!.type, 'text')
  assert.equal(blocks[1]!.type, 'image')
})

test('a turn without a settings seam fails loudly, not silently — text-only too', async () => {
  const s = store()
  await assert.rejects(
    prepareUserContent([imgPart('image/png', 'QUJD')], s.saveImage, undefined, 'm/x', 128000, 65536, null),
    /settings service is not composed/,
  )
  await assert.rejects(
    prepareUserContent('plain text', s.saveImage, undefined, 'm/x', 128000, 65536, null),
    /settings service is not composed/,
  )
})
