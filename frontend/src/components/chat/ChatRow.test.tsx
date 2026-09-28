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

const { emitMock } = vi.hoisted(() => ({
  emitMock: vi.fn(),
}));
vi.mock('../../events', () => ({ on: vi.fn(), off: vi.fn(), emit: emitMock }));

import { ChatRow } from './ChatRow';
import { formatDate } from '../../utils/format';
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

describe('ChatRow — date source', () => {
  it('renders the last_message_at date when present (not updated_at)', () => {
    const session = baseSession({
      updated_at: '2026-01-15T12:00:00Z',
      last_message_at: '2026-03-15T12:00:00Z',
    });
    const { container, root } = renderSession(session);
    // Both sides go through the SAME formatter → TZ-independent.
    expect(container.textContent).toContain(formatDate('2026-03-15T12:00:00Z'));
    expect(container.textContent).not.toContain(formatDate('2026-01-15T12:00:00Z'));
    act(() => root.unmount());
  });

  it('falls back to created_at (not updated_at) when last_message_at is absent (F3)', () => {
    // created_at and updated_at deliberately differ: the card must show created_at.
    const session = baseSession({
      created_at: '2026-02-20T08:00:00Z',
      updated_at: '2026-01-15T12:00:00Z',
    });
    const { container, root } = renderSession(session);
    expect(container.textContent).toContain(formatDate('2026-02-20T08:00:00Z'));
    expect(container.textContent).not.toContain(formatDate('2026-01-15T12:00:00Z'));
    act(() => root.unmount());
  });

  it('falls back to created_at (not updated_at) when last_message_at is null (F3)', () => {
    const session = baseSession({
      created_at: '2026-02-20T08:00:00Z',
      updated_at: '2026-01-15T12:00:00Z',
      last_message_at: null,
    });
    const { container, root } = renderSession(session);
    expect(container.textContent).toContain(formatDate('2026-02-20T08:00:00Z'));
    expect(container.textContent).not.toContain(formatDate('2026-01-15T12:00:00Z'));
    act(() => root.unmount());
  });
});

describe('ChatRow — parent hover preview forwarding', () => {
  it('forwards onParentHover with docId + the span element on mouseenter when a parent doc exists', () => {
    // document_id set + document_title → parentLabel = 'Doc Title'; currentDocumentId
    // is undefined (mocked store has no currentDocument) → clickable branch (role=button).
    const session = baseSession({ document_id: 'doc-9', document_title: 'Doc Title' });
    const onParentHover = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => {
      root.render(createElement(ChatRow, { session, onSelect: vi.fn(), onParentHover, onParentHoverLeave: vi.fn() }));
    });
    // ListPill renders a div[role=button] (the clickable pill); the parent label
    // is a nested span[role=button] — disambiguate by tag.
    const span = container.querySelector('span[role="button"]') as HTMLElement;
    expect(span).toBeTruthy();
    act(() => {
      span.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true }));
    });
    expect(onParentHover).toHaveBeenCalledTimes(1);
    expect(onParentHover.mock.calls[0][0]).toBe('doc-9');
    expect(onParentHover.mock.calls[0][1]).toBe(span);
    act(() => root.unmount());
  });

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

describe('ChatRow — inline rename', () => {
  function renderWithRename(onRename: (id: string, title: string) => Promise<void>) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => {
      root.render(createElement(ChatRow, { session: baseSession(), onSelect: vi.fn(), onDelete: vi.fn(), onRename }));
    });
    return { container, root };
  }
  const pencil = (c: HTMLElement) => c.querySelector('button[title="rename"]') as HTMLButtonElement;
  const input = (c: HTMLElement) => c.querySelector('input[data-rename-input]') as HTMLInputElement | null;
  const setValue = (el: HTMLInputElement, v: string) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  };

  it('shows no pencil without onRename', () => {
    const { container, root } = renderSession(baseSession());
    expect(pencil(container)).toBeNull();
    act(() => root.unmount());
  });

  it('pencil opens the input with the current title; Enter commits the trimmed value', async () => {
    const onRename = vi.fn().mockResolvedValue(undefined);
    const { container, root } = renderWithRename(onRename);
    expect(input(container)).toBeNull();
    act(() => { pencil(container).click(); });
    const el = input(container)!;
    expect(el.value).toBe('Hello');
    act(() => { setValue(el, '  New name '); });
    await act(async () => {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(onRename).toHaveBeenCalledWith('s1', 'New name');
    expect(input(container)).toBeNull();
    act(() => root.unmount());
  });

  it('Escape cancels without a write', () => {
    const onRename = vi.fn().mockResolvedValue(undefined);
    const { container, root } = renderWithRename(onRename);
    act(() => { pencil(container).click(); });
    const el = input(container)!;
    act(() => { setValue(el, 'Changed'); });
    act(() => { el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(onRename).not.toHaveBeenCalled();
    expect(input(container)).toBeNull();
    act(() => root.unmount());
  });

  it('blur commits; an unchanged or empty title closes without a write', async () => {
    const onRename = vi.fn().mockResolvedValue(undefined);
    const { container, root } = renderWithRename(onRename);
    act(() => { pencil(container).click(); });
    await act(async () => { input(container)!.dispatchEvent(new FocusEvent('focusout', { bubbles: true })); });
    expect(onRename).not.toHaveBeenCalled();
    expect(input(container)).toBeNull();

    act(() => { pencil(container).click(); });
    act(() => { setValue(input(container)!, '   '); });
    await act(async () => { input(container)!.dispatchEvent(new FocusEvent('focusout', { bubbles: true })); });
    expect(onRename).not.toHaveBeenCalled();

    act(() => { pencil(container).click(); });
    act(() => { setValue(input(container)!, 'Via blur'); });
    await act(async () => { input(container)!.dispatchEvent(new FocusEvent('focusout', { bubbles: true })); });
    expect(onRename).toHaveBeenCalledWith('s1', 'Via blur');
    act(() => root.unmount());
  });
});
