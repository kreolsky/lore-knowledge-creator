/**
 * Ghost-pin materialization decision.
 *
 * A ghost pin is transferred to the materialized row whenever the ghost carries
 * one (every AI chat is an agent chat, so a pin always implies an agent session).
 * The helper returns the createSession params to spread, or null when there is no pin.
 *
 * There is no mode arg — the only signal is "has a region".
 */
import { describe, it, expect } from 'vitest';
import { ghostPinTransfer } from './ghost-pin-transfer';
import type { PinnedRegion } from '../types';

const REGION: PinnedRegion = { doc_id: 'doc-1', relFrom: { a: 1 }, relTo: { a: 2 } };

describe('ghostPinTransfer', () => {
  it('returns the has_region params when the ghost has a pin', () => {
    expect(ghostPinTransfer(REGION)).toEqual({ hasRegion: true, region: REGION });
  });

  it('returns null when there is no ghost region', () => {
    expect(ghostPinTransfer(null)).toBeNull();
  });
});
