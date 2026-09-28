/**
 * Subtree-delete checkbox in DeleteModal: rendered ONLY when subtreeOption is
 * present (documents with live descendants), checked by default, and its state
 * flows to onConfirm(true/false). Without subtreeOption there is no checkbox and
 * onConfirm receives undefined (flag irrelevant — subtree = the item itself).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let DeleteModal: typeof import('./DeleteModal').DeleteModal;
let onConfirm: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  onConfirm = vi.fn().mockResolvedValue(undefined);

  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
  const appState = { showToast: vi.fn() };
  vi.doMock('../store/app-store', () => ({
    useAppStore: (selector: (s: typeof appState) => unknown) => selector(appState),
  }));

  ({ DeleteModal } = await import('./DeleteModal'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
});

function renderModal(props: Partial<React.ComponentProps<typeof DeleteModal>> = {}) {
  act(() => {
    root.render(createElement(DeleteModal, {
      title: 'Delete',
      itemName: 'Alpha',
      itemType: 'document',
      onConfirm: onConfirm as (deleteChildren?: boolean) => void | Promise<void>,
      onCancel: vi.fn(),
      ...props,
    }));
  });
}

/** Type the exact item name into the type-to-confirm input. */
function typeName(name = 'Alpha') {
  const input = document.querySelector<HTMLInputElement>('input[type="text"]');
  if (!input) throw new Error('confirm input not rendered');
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    nativeSetter.call(input, name);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function clickConfirm() {
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => b.textContent === 'deleteType');
  if (!btn) throw new Error('confirm button not rendered');
  await act(async () => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
}

describe('DeleteModal subtree checkbox', () => {
  it('renders a checked checkbox when subtreeOption is present', () => {
    renderModal({ subtreeOption: { childDocs: 2, childRefs: 1 } });
    const box = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(box).not.toBeNull();
    expect(box?.checked).toBe(true);
    expect(box?.parentElement?.textContent).toContain('deleteSubtree');
  });

  it('renders NO checkbox without subtreeOption (leaf doc / project / note)', () => {
    renderModal();
    expect(document.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it('default (checked) flows to onConfirm(true)', async () => {
    renderModal({ subtreeOption: { childDocs: 1, childRefs: 0 } });
    typeName();
    await clickConfirm();
    expect(onConfirm).toHaveBeenCalledWith(true);
  });

  it('unchecking flows to onConfirm(false) — legacy lift mode', async () => {
    renderModal({ subtreeOption: { childDocs: 1, childRefs: 0 } });
    const box = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!box) throw new Error('checkbox not rendered');
    act(() => { box.click(); });
    typeName();
    await clickConfirm();
    expect(onConfirm).toHaveBeenCalledWith(false);
  });

  it('without subtreeOption onConfirm receives undefined', async () => {
    renderModal();
    typeName();
    await clickConfirm();
    expect(onConfirm).toHaveBeenCalledWith(undefined);
  });
});
