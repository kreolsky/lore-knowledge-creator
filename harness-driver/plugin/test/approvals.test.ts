/**
 * The approval bridge contract (plan collapse-agent-stack-onto-dsh-vocabulary
 * step 7): dsh's approval/request waterfall is answered by a PARKED question
 * that the driver's /approvals endpoints resolve — the verdict vocabulary
 * maps onto dsh's closed outcomes, the allow_session grant is minted from the
 * ASK's record (never the body), an expired ask self-resolves rejected and
 * answers the owning session's late verdict with 409 while anyone else keeps
 * the uniform 404.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { mock } from 'node:test'

import {
  askLoreApproval, bareAskKey, grantKey, hasLoreGrant, pendingHoldsFor, postVerdictAnswer,
  registerLoreApprovalBridge, resetLoreApprovalBridgeForTest, resolveSessionAsks,
  resolveVerdict, verdictOutcome,
} from '../src/approvals.ts'
import type { TurnToolCtx } from '../src/tools.ts'

function tctx(loreSessionId: string): TurnToolCtx {
  return {
    loreSessionId,
    messageId: `msg-${loreSessionId}`,
    agentKey: 'k',
    applyMode: 'confirm',
    document_id: null,
    mutating: new Set(['edit_document']),
    holdable: new Set(['edit_document']),
    regionTools: new Set(),
    region: null,
  }
}

/** A fake cordis ctx: captures the approval/request listener and offers an
 * approval.request that dispatches straight into it (next = fail-closed). */
function fakeBridge(lookup: (dshId: string) => TurnToolCtx | undefined) {
  let listener: ((req: any, next: () => Promise<any>) => Promise<any>) | null = null
  const ctx: any = {
    on: (_name: string, fn: any) => { listener = fn },
    approval: {
      request: (req: any) => listener!(req, () => Promise.resolve('unavailable')),
    },
  }
  registerLoreApprovalBridge(ctx, lookup)
  const ask = (callId: string, dshId: string, toolName: string): Promise<string> =>
    ctx.approval.request({ callId, toolName, agent: { session: { id: dshId } } })
  return { ask }
}

test.beforeEach(() => resetLoreApprovalBridgeForTest())
test.afterEach(() => resetLoreApprovalBridgeForTest())

// ─── the pure vocabulary mapping ─────────────────────────────────────────────

test('our verdict vocabulary maps onto the closed dsh outcomes', () => {
  assert.equal(verdictOutcome('allow_once'), 'allowed-once')
  assert.equal(verdictOutcome('allow_session'), 'allowed-once')
  assert.equal(verdictOutcome('reject'), 'rejected')
  assert.equal(verdictOutcome('nonsense'), null)
})

test('the grant key namespaces session and tool', () => {
  assert.equal(grantKey('s1', 'edit_document'), 's1 edit_document')
  assert.notEqual(grantKey('s1', 'a'), grantKey('s1', 'a b'))
})

// ─── postVerdictAnswer: what POST /approvals answers for one verdict ─────────

test('an unknown ask is the uniform 404, whoever asks', () => {
  const state = { pending: new Map(), expired: new Map() }
  assert.equal(postVerdictAnswer('nope', 's1', 'allow_once', null, state).status, 404)
})

test('a malformed action is 422', () => {
  const state = { pending: new Map(), expired: new Map() }
  assert.equal(postVerdictAnswer('c1', 's1', 'allow_forever', null, state).status, 422)
})

test('the owning session\'s expired ask answers 409; another session keeps 404', () => {
  const state = { pending: new Map(), expired: new Map([['c1', 's1']]) }
  assert.equal(postVerdictAnswer('c1', 's1', 'allow_once', null, state).status, 409)
  assert.equal(postVerdictAnswer('c1', 's2', 'allow_once', null, state).status, 404)
})

// ─── the parked ask lifecycle (register → list → resolve) ────────────────────

test('a parked ask lists under its lore session and resolves allowed-once', async () => {
  const bridge = fakeBridge((dshId) => (dshId === 'dsh-1' ? tctx('lore-1') : undefined))
  const parked = bridge.ask('c1', 'dsh-1', 'edit_document')
  // The park is SYNCHRONOUS — the listing sees it in the same tick.
  assert.deepEqual(pendingHoldsFor('lore-1'), [
    { call_id: 'c1', tool_name: 'edit_document', message_id: 'msg-lore-1' },
  ])
  assert.equal(pendingHoldsFor('other').length, 0)
  assert.equal(resolveVerdict('c1', 'lore-1', 'allow_once', null, null), 200)
  assert.equal(await parked, 'allowed-once')
  // Resolved: no longer listed, a second verdict is the uniform 404.
  assert.equal(pendingHoldsFor('lore-1').length, 0)
  assert.equal(resolveVerdict('c1', 'lore-1', 'allow_once', null, null), 404)
})

test('a foreign session cannot resolve the ask', async () => {
  const bridge = fakeBridge((dshId) => (dshId === 'dsh-1' ? tctx('lore-1') : undefined))
  const parked = bridge.ask('c2', 'dsh-1', 'edit_document')
  assert.equal(resolveVerdict('c2', 'lore-2', 'allow_once', null, null), 404)
  assert.equal(pendingHoldsFor('lore-1').length, 1)
  assert.equal(resolveVerdict('c2', 'lore-1', 'allow_once', null, null), 200)
  assert.equal(await parked, 'allowed-once')
})

test('a reject carries the user\'s words to the asker as the call\'s own result', async () => {
  const ctx = tctx('lore-1')
  fakeBridge((dshId) => (dshId === 'dsh-1' ? ctx : undefined))
  const exec = { callId: 'c3', agent: { session: { id: 'dsh-1' } }, signal: undefined }
  const asked = askLoreApproval(exec, ctx, 'edit_document', 'needs approval')
  assert.equal(resolveVerdict('c3', 'lore-1', 'reject', null, 'user said no'), 200)
  assert.deepEqual(await asked, { outcome: 'rejected', reason: 'user said no' })
})

test('allow_session mints the grant from the ASK\'s tool, never the body', async () => {
  const ctx = tctx('lore-1')
  const bridge = fakeBridge((dshId) => (dshId === 'dsh-1' ? ctx : undefined))
  const parked = bridge.ask('c4', 'dsh-1', 'edit_document')
  // A body claiming a different tool mints nothing (404) — the ask stays parked.
  assert.equal(resolveVerdict('c4', 'lore-1', 'allow_session', 'import_file', null), 404)
  assert.equal(pendingHoldsFor('lore-1').length, 1)
  // Missing tool_name is 422.
  assert.equal(resolveVerdict('c4', 'lore-1', 'allow_session', null, null), 422)
  assert.equal(pendingHoldsFor('lore-1').length, 1)
  // The matching tool resolves AND grants follow-up asks without asking.
  assert.equal(resolveVerdict('c4', 'lore-1', 'allow_session', 'edit_document', null), 200)
  assert.equal(await parked, 'allowed-once')
  assert.ok(hasLoreGrant('lore-1', 'edit_document'))
  assert.equal(hasLoreGrant('lore-2', 'edit_document'), false)
  const exec2 = { callId: 'c5', agent: { session: { id: 'dsh-1' } }, signal: undefined }
  assert.deepEqual(
    await askLoreApproval(exec2, ctx, 'edit_document', 'r'),
    { outcome: 'allowed-once' },
  )
})

test('a session resolve rejects every parked ask of that session only', async () => {
  const bridge = fakeBridge((dshId) => (dshId.startsWith('dsh-a') ? tctx('lore-a') : tctx('lore-b')))
  const a1 = bridge.ask('c6', 'dsh-a', 'edit_document')
  const b1 = bridge.ask('c7', 'dsh-b', 'edit_document')
  assert.equal(resolveSessionAsks('lore-a', 'session_closed'), 1)
  assert.equal(await a1, 'rejected')
  assert.equal(pendingHoldsFor('lore-b').length, 1)
  // b1 is still parked — resolving it now cleans up for the next test.
  assert.equal(resolveVerdict('c7', 'lore-b', 'reject', null, null), 200)
  assert.equal(await b1, 'rejected')
})

test('an ask it cannot own passes through (next), never parks', async () => {
  const bridge = fakeBridge(() => undefined)
  const outcome = await bridge.ask('c8', 'dsh-unknown', 'edit_document')
  assert.equal(outcome, 'unavailable')
  assert.equal(pendingHoldsFor('lore-any').length, 0)
})

test('an unanswered ask self-resolves rejected past the bound and traces its owner', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const ctx = tctx('lore-1')
    fakeBridge((dshId) => (dshId === 'dsh-1' ? ctx : undefined))
    const exec = { callId: 'c9', agent: { session: { id: 'dsh-1' } }, signal: undefined }
    const asked = askLoreApproval(exec, ctx, 'edit_document', 'r')
    mock.timers.tick(600 * 1000 + 10)
    assert.deepEqual(await asked, { outcome: 'rejected', reason: 'hold_expired' })
    // The trace is session-bound: the owner gets 409, anyone else 404.
    assert.equal(resolveVerdict('c9', 'lore-1', 'allow_once', null, null), 409)
    assert.equal(resolveVerdict('c9', 'lore-2', 'allow_once', null, null), 404)
  } finally {
    mock.timers.reset()
  }
})

test('a child wire id resolves the parked ask (the # strip)', async () => {
  const ctx = tctx('lore-1')
  const bridge = fakeBridge((dshId) => (dshId === 'dsh-child-1' ? ctx : undefined))
  // The ask parks under dsh's RAW call id (the answerer sees the raw id).
  const parked = bridge.ask('c-c1', 'dsh-child-1', 'create_document')
  // The card a child ask renders on carries the session-prefixed wire id.
  assert.equal(bareAskKey('dsh-child-1#c-c1'), 'c-c1')
  assert.equal(bareAskKey('c-parent-1'), 'c-parent-1')
  assert.equal(resolveVerdict('dsh-child-1#c-c1', 'lore-1', 'allow_once', null, null), 200)
  assert.equal(await parked, 'allowed-once')
})

test('closing a session sweeps its grants — a later session cannot inherit them', async () => {
  const bridge = fakeBridge(() => tctx('lore-sweep'))
  const ask = bridge.ask('c20', 'dsh-sweep', 'edit_document')
  assert.equal(
    resolveVerdict('c20', 'lore-sweep', 'allow_session', 'edit_document', null), 200)
  assert.equal(await ask, 'allowed-once')
  assert.equal(hasLoreGrant('lore-sweep', 'edit_document'), true)

  resolveSessionAsks('lore-sweep', 'session_closed')
  // The session is over: nothing may short-circuit an ask against it again,
  // and the entry does not sit in the map for the driver's whole uptime.
  assert.equal(hasLoreGrant('lore-sweep', 'edit_document'), false)
  // Another session's grant is untouched by the sweep.
  const other = fakeBridge(() => tctx('lore-keep'))
  const ask2 = other.ask('c21', 'dsh-keep', 'edit_document')
  assert.equal(
    resolveVerdict('c21', 'lore-keep', 'allow_session', 'edit_document', null), 200)
  assert.equal(await ask2, 'allowed-once')
  resolveSessionAsks('lore-sweep', 'session_closed')
  assert.equal(hasLoreGrant('lore-keep', 'edit_document'), true)
})
