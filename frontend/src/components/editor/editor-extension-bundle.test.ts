// @vitest-environment jsdom
/**
 * Tests for editor-extension-bundle — buildEditorExtensions mounts a working
 * EditorView (the full live-editor extension array) and routes focus through
 * claimFocus; makeRawModeToggle detaches/reattaches the render compartment.
 *
 * SYSTEM: editor — the extension array Editor.tsx mounts, pinned here without
 * mounting the component.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState, Compartment } from '@codemirror/state';

const { claimFocus } = vi.hoisted(() => ({ claimFocus: vi.fn() }));
vi.mock('../../editor/active-editor', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../editor/active-editor')>()),
  claimFocus,
}));

import {
  buildEditorExtensions,
  type EditorExtensionDeps,
} from './editor-extension-bundle';

function makeDeps(overrides: Partial<EditorExtensionDeps> = {}): EditorExtensionDeps {
  return {
    renderCompartment: new Compartment(),
    rawModeRef: { current: false },
    fileDropExtension: EditorState.languageData.of(() => []),
    yjsCompartment: new Compartment(),
    activeItemRef: { current: null },
    refIsEmptySetterRef: { current: () => {} },
    activeItemIdRef: { current: 'doc-1' },
    snapshotPreviewRef: { current: null },
    roleRef: { current: 'primary' },
    isReferenceRef: { current: false },
    collabConnectionRef: { current: null },
    regionResolver: () => null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('buildEditorExtensions', () => {
  it('mounts a live EditorView with the full bundle, NOT editable until the gate opens it', () => {
    const deps = makeDeps();
    const extensions = buildEditorExtensions(deps);
    const view = new EditorView({
      state: EditorState.create({ doc: '# Title\n\nbody', extensions }),
      parent: document.createElement('div'),
    });
    // A fresh view is built on every entity switch (key={activeItemId}); it must not
    // accept input before Editor.tsx's gate confirms the phase machine is live for the
    // entity being rendered. See INVARIANT(data-loss) on the compartment seed.
    expect(view.state.facet(EditorView.editable)).toBe(false);
    view.destroy();
  });

  it('carries the caller-provided file-drop extension verbatim', () => {
    const deps = makeDeps();
    const extensions = buildEditorExtensions(deps);
    expect(extensions).toContain(deps.fileDropExtension);
  });

  it('claims the focus-sensitive slots via claimFocus on view focus', () => {
    const handle = { synced: true };
    const deps = makeDeps({
      roleRef: { current: 'secondary' },
      isReferenceRef: { current: true },
      collabConnectionRef: { current: handle as never },
    });
    const view = new EditorView({
      state: EditorState.create({ doc: 'x', extensions: buildEditorExtensions(deps) }),
      parent: document.body,
    });
    view.contentDOM.dispatchEvent(new FocusEvent('focus'));
    expect(claimFocus).toHaveBeenCalledTimes(1);
    expect(claimFocus).toHaveBeenCalledWith(
      expect.objectContaining({ role: 'secondary', isReference: true, handle }),
    );
    view.destroy();
  });
});
