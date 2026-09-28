/** navigateToDocKeepingChat — chat-origin document navigation: pins the chat tab on
 * the TARGET doc (the right panel is per-document, so the target's remembered tab
 * would otherwise win) and emits navigate-to-document (default: the doc body, not
 * its remembered reference). The pin itself is
 * the ui-store's pinRightPanel (documents-slice). */

import { describe, it, expect, beforeEach, vi } from 'vitest';

const { emitMock } = vi.hoisted(() => ({
  emitMock: vi.fn(),
}));

vi.mock('../events', () => ({ emit: emitMock }));

import { navigateToDocKeepingChat } from './navigate';
import { useUIStore } from '../store/ui-store';

describe('navigateToDocKeepingChat', () => {
  beforeEach(() => {
    emitMock.mockClear();
  });

  it('emits navigate-to-document for the destination doc', () => {
    navigateToDocKeepingChat('doc-dest');
    expect(emitMock).toHaveBeenCalledWith('navigate-to-document', { documentId: 'doc-dest' });
  });

  it('always navigates regardless of active chat state (chat persists by default)', () => {
    navigateToDocKeepingChat('doc-9');
    expect(emitMock).toHaveBeenCalledTimes(1);
    expect(emitMock).toHaveBeenCalledWith('navigate-to-document', { documentId: 'doc-9' });
  });
});

describe('navigateToDocKeepingChat', () => {
  beforeEach(() => { useUIStore.setState({ documents: {}, searchTabDocId: null }); });

  it('pins chat on the target doc before emitting', () => {
    navigateToDocKeepingChat('doc-y');
    const d = useUIStore.getState().documents['doc-y'];
    expect(d.rightPanelTab).toBe('chat');
    expect(d.rightPanelOpen).toBe(true);
    expect(emitMock).toHaveBeenCalledWith('navigate-to-document', { documentId: 'doc-y' });
  });
});
