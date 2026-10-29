/** ChatRow date source — shows last_message_at when present, else created_at. */
// @vitest-environment jsdom

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ChatRow reads useAppStore (references/documents/currentDocument) + i18n t. Mock
// both so the meta string is just the formatted date (no parent label resolution).
vi.mock('../../store/app-store', () => ({
  useAppStore: (selector: (s: unknown) => unknown) =>
    selector({ references: [{ reference_id: 'ref-1', title: 'RefTitle' }], documents: [] }),
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
// The per-session running set — a mutable Set so a test can seat its session.
const chatStoreState = vi.hoisted(() => ({ runningSessions: new Set<string>() }));
vi.mock('../../store/chat-store', () => ({
  useChatStore: (selector: (s: { runningSessions: Set<string> }) => unknown) =>
    selector({ runningSessions: chatStoreState.runningSessions }),
}));

const { emitMock } = vi.hoisted(() => ({
  emitMock: vi.fn(),
}));
vi.mock('../../events', () => ({ on: vi.fn(), off: vi.fn(), emit: emitMock }));

import { ChatRow } from './ChatRow';
import type { ChatSession } from '../../types';

function baseSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    session_id: 's1',
    document_id: null,
    reference_id: null,
    user_id: 'u1',
    title: 'Hello',
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    updated_at: '2026-01-15T12:00:00Z',
    created_at: '2026-01-15T12:00:00Z',
    ...overrides,
  } as ChatSession;
}

function renderSession(session: ChatSession): { container: HTMLElement; root: Root } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(createElement(ChatRow, { session, onSelect: vi.fn() }));
  });
  return { container, root };
}

describe('ChatRow — parent hover preview forwarding', () => {

  it('forwards hover with the reference id for a ref-scoped session (ref: label)', () => {
    // INVARIANT: a reference-scoped chat's parent IS the reference — the label is
    // `ref: <title>` and hover/navigation target the reference id (not a doc).  Why: a ref-scoped chat's parent is the reference itself, so the label is `ref: <title>` and hover/nav target the reference id, not a doc.
    const session = baseSession({ document_id: 'ref-1', reference_id: 'ref-1', reference_title: 'RefTitle' });
    const onParentHover = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => {
      root.render(createElement(ChatRow, { session, onSelect: vi.fn(), onParentHover, onParentHoverLeave: vi.fn() }));
    });
    // currentReferenceId is undefined (mocked store) → ref-session is clickable.
    const span = container.querySelector('span[role="button"]') as HTMLElement;
    expect(span).toBeTruthy();
    act(() => {
      span.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true }));
    });
    expect(onParentHover).toHaveBeenCalledTimes(1);
    expect(onParentHover.mock.calls[0][0]).toBe('ref-1');
    act(() => root.unmount());
  });
});

describe('ChatRow — parent navigation payload', () => {
  beforeEach(() => {
    emitMock.mockClear();
  });

  it('doc-session parent click emits navigate-to-document (default: the doc body)', () => {
    // currentDocumentId is undefined (mocked store) → the parent label is clickable.
    const session = baseSession({ document_id: 'doc-9', document_title: 'Doc Title' });
    const { container, root } = renderSession(session);
    const span = container.querySelector('span[role="button"]') as HTMLElement;
    expect(span).toBeTruthy();
    act(() => {
      span.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    expect(emitMock).toHaveBeenCalledWith('navigate-to-document', { documentId: 'doc-9' });
    act(() => root.unmount());
  });

  it('ref-session parent click still emits navigate-to-reference (no doc flag)', () => {
    const session = baseSession({ document_id: 'ref-1', reference_id: 'ref-1', reference_title: 'RefTitle' });
    const { container, root } = renderSession(session);
    const span = container.querySelector('span[role="button"]') as HTMLElement;
    expect(span).toBeTruthy();
    act(() => {
      span.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    expect(emitMock).toHaveBeenCalledWith('navigate-to-reference', { referenceId: 'ref-1' });
    expect(emitMock).not.toHaveBeenCalledWith('navigate-to-document', expect.anything());
    act(() => root.unmount());
  });
});
