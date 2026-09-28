/** TDD for useImageReferenceNav — pure helpers + focus gate + window keydown wiring.
 *
 * Helper tests (nextImageIndex, isFocusInEditableZone) cover the pure math/predicate.
 * The integration test mounts the hook via a no-op component, sets up the store and
 * document.activeElement, dispatches ArrowLeft/ArrowRight on window, and asserts
 * setCurrentReference was called with the right cycled image reference.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useImageReferenceNav, nextImageIndex, isFocusInEditableZone } from './useImageReferenceNav';
import { useAppStore } from '../store/app-store';
import type { Reference } from '../types';

function imageRef(id: string): Reference {
  return {
    reference_id: id,
    project_id: 'p',
    document_id: 'd',
    title: id,
    media_type: 'image',
    source_url: null,
    content: '',
    processing_status: 'ready',
    file_path: null,
    file_meta: null,
    updated_at: '',
    created_at: '',
  };
}

function mdRef(id: string): Reference {
  return { ...imageRef(id), media_type: 'markdown' };
}

// ---------------------------------------------------------------------------
// nextImageIndex
// ---------------------------------------------------------------------------

describe('nextImageIndex', () => {
  it('returns -1 when count <= 1', () => {
    expect(nextImageIndex(0, 1, 1)).toBe(-1);
    expect(nextImageIndex(0, 0, 1)).toBe(-1);
    expect(nextImageIndex(0, 1, -1)).toBe(-1);
  });

  it('goes forward and wraps last → first', () => {
    expect(nextImageIndex(0, 3, 1)).toBe(1);
    expect(nextImageIndex(1, 3, 1)).toBe(2);
    expect(nextImageIndex(2, 3, 1)).toBe(0);
  });

  it('goes backward and wraps first → last', () => {
    expect(nextImageIndex(2, 3, -1)).toBe(1);
    expect(nextImageIndex(1, 3, -1)).toBe(0);
    expect(nextImageIndex(0, 3, -1)).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// isFocusInEditableZone
// ---------------------------------------------------------------------------

describe('isFocusInEditableZone', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('returns false for null', () => {
    expect(isFocusInEditableZone(null)).toBe(false);
  });

  it('returns true inside .cm-content', () => {
    const cm = document.createElement('div');
    cm.className = 'cm-content';
    const caret = document.createElement('span');
    cm.appendChild(caret);
    expect(isFocusInEditableZone(caret)).toBe(true);
  });

  it('returns true for input and textarea', () => {
    const input = document.createElement('input');
    const ta = document.createElement('textarea');
    expect(isFocusInEditableZone(input)).toBe(true);
    expect(isFocusInEditableZone(ta)).toBe(true);
  });

  it('returns true for contenteditable', () => {
    const el = document.createElement('div');
    el.setAttribute('contenteditable', 'true');
    document.body.appendChild(el); // jsdom: isContentEditable only true when connected
    expect(isFocusInEditableZone(el)).toBe(true);
  });

  it('returns true inside role="tree"', () => {
    const tree = document.createElement('div');
    tree.setAttribute('role', 'tree');
    const child = document.createElement('button');
    tree.appendChild(child);
    expect(isFocusInEditableZone(child)).toBe(true);
  });

  it('returns false for plain div / button / body', () => {
    const div = document.createElement('div');
    const btn = document.createElement('button');
    expect(isFocusInEditableZone(div)).toBe(false);
    expect(isFocusInEditableZone(btn)).toBe(false);
    expect(isFocusInEditableZone(document.body)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Hook integration (window keydown → setCurrentReference)
// ---------------------------------------------------------------------------

describe('useImageReferenceNav (integration)', () => {
  let container: HTMLDivElement;
  let root: Root;

  let setCurrentReference: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    setCurrentReference = vi.fn();
    useAppStore.setState({ setCurrentReference } as Partial<ReturnType<typeof useAppStore.getState>>);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  function fireKey(key: string, mods: Partial<KeyboardEventInit> = {}) {
    const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...mods });
    window.dispatchEvent(ev);
    return ev;
  }

  function renderHook() {
    function Probe() {
      useImageReferenceNav('primary');
      return null;
    }
    act(() => {
      root.render(createElement(Probe));
    });
  }

  function setState(refs: Reference[], current: Reference | null) {
    useAppStore.setState({
      references: refs,
      currentReference: current,
      setCurrentReference,
    } as Partial<ReturnType<typeof useAppStore.getState>>);
  }

  function setCurrent(ref: Reference) {
    useAppStore.setState({
      currentReference: ref,
      setCurrentReference,
    } as Partial<ReturnType<typeof useAppStore.getState>>);
  }

  it('does nothing when current reference is not an image', () => {
    const img = imageRef('img1');
    const md = mdRef('md1');
    setState([img, md], md);
    renderHook();
    fireKey('ArrowRight');
    expect(setCurrentReference).not.toHaveBeenCalled();
  });

  it('does nothing when fewer than 2 images', () => {
    const img = imageRef('img1');
    const md = mdRef('md1');
    setState([img, md], img);
    renderHook();
    fireKey('ArrowRight');
    expect(setCurrentReference).not.toHaveBeenCalled();
  });

  it('does nothing when modifier key is pressed', () => {
    const a = imageRef('img1');
    const b = imageRef('img2');
    setState([a, b], a);
    renderHook();
    fireKey('ArrowRight', { metaKey: true });
    fireKey('ArrowLeft', { ctrlKey: true });
    fireKey('ArrowRight', { altKey: true });
    expect(setCurrentReference).not.toHaveBeenCalled();
  });

  it('does nothing when focus is in editable zone', () => {
    const a = imageRef('img1');
    const b = imageRef('img2');
    setState([a, b], a);
    renderHook();
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    fireKey('ArrowRight');
    expect(setCurrentReference).not.toHaveBeenCalled();
  });

  it('does nothing for non-arrow keys', () => {
    const a = imageRef('img1');
    const b = imageRef('img2');
    setState([a, b], a);
    renderHook();
    fireKey('Enter');
    fireKey('ArrowUp');
    expect(setCurrentReference).not.toHaveBeenCalled();
  });

  it('ArrowRight cycles forward through images only and wraps', () => {
    const md = mdRef('md');
    const a = imageRef('a');
    const b = imageRef('b');
    const c = imageRef('c');
    setState([md, a, md, b, c], a);
    renderHook();

    fireKey('ArrowRight');
    expect(setCurrentReference).toHaveBeenLastCalledWith(b);
    setCurrent(b);
    fireKey('ArrowRight');
    expect(setCurrentReference).toHaveBeenLastCalledWith(c);
    setCurrent(c);
    fireKey('ArrowRight'); // wrap → a
    expect(setCurrentReference).toHaveBeenLastCalledWith(a);
  });

  it('ArrowLeft cycles backward through images only and wraps', () => {
    const a = imageRef('a');
    const b = imageRef('b');
    const c = imageRef('c');
    setState([a, b, c], a);
    renderHook();

    fireKey('ArrowLeft'); // wrap → c
    expect(setCurrentReference).toHaveBeenLastCalledWith(c);
    setCurrent(c);
    fireKey('ArrowLeft');
    expect(setCurrentReference).toHaveBeenLastCalledWith(b);
  });

  it('calls preventDefault when handling', () => {
    const a = imageRef('a');
    const b = imageRef('b');
    setState([a, b], a);
    renderHook();
    const ev = fireKey('ArrowRight');
    expect(ev.defaultPrevented).toBe(true);
  });

  it('does not preventDefault when bailing (focus in input)', () => {
    const a = imageRef('a');
    const b = imageRef('b');
    setState([a, b], a);
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    renderHook();
    const ev = fireKey('ArrowRight');
    expect(ev.defaultPrevented).toBe(false);
  });
});
