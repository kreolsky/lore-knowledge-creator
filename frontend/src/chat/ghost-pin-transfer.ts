/**
 * Ghost-pin materialization decision — pure,
 * unit-tested.
 *
 * The zero/ghost chat holds an in-memory pinned region (store.ghostRegion). On
 * first send, if the ghost carries a pin it is transferred to the materialized
 * row (createSession writes it to pending-selection BEFORE activation, then
 * clearGhostRegion drops the in-memory copy — see ChatInput.handleSend). This
 * helper computes the createSession params to spread, so the transfer decision
 * is unit-testable independent of the React component.
 *
 * With one AI line every ghost is an agent chat, so the only signal is "the
 * ghost has a region" — there is no mode arg.
 */

import type { PinnedRegion } from '../types';

/** The createSession pin params to spread into the materialization call. */
export interface GhostPinParams {
  hasRegion: true;
  region: PinnedRegion;
}

/**
 * Decide whether the ghost carries an in-memory pin to materialize on first send.
 * Returns the createSession params when the ghost has a pin, else null.
 * Extracted from ChatInput.handleSend so the param shape is unit-testable.
 */
export function ghostPinTransfer(
  ghostRegion: PinnedRegion | null,
): GhostPinParams | null {
  if (!ghostRegion) return null;
  return { hasRegion: true, region: ghostRegion };
}
