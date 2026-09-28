/**
 * PinLockScreen — each typed digit replaces the dot in its own slot; the caret
 * shows only on the next empty slot while the field is focused.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { PinLockScreen } from './PinLockScreen';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('./editor/editor-host', () => ({ resetEditorHost: vi.fn() }));

let container: HTMLDivElement;
let root: Root;

function slots(): string[] {
  const input = container.querySelector('input')!;
  return Array.from(input.parentElement!.children)
    .filter(el => el !== input)
    .map(el => (el.querySelector('.animate-pulse') ? '|' : el.textContent ?? ''));
}

function type(value: string) {
  const input = container.querySelector('input')!;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => { root.render(createElement(MemoryRouter, null, createElement(PinLockScreen))); });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('PinLockScreen slots', () => {
  it('focused and empty: caret on the first slot, dots on the rest', () => {
    expect(slots()).toEqual(['|', '•', '•', '•']);
  });

  it('typed digits land in their slots and the caret moves to the next one', () => {
    type('12');
    expect(slots()).toEqual(['1', '2', '|', '•']);
  });

  it('non-digits are dropped and input stops at four', () => {
    type('9a8b765');
    expect(slots()).toEqual(['9', '8', '7', '6']);
  });

  it('blurred: no caret, only dots in the empty slots', () => {
    type('3');
    act(() => { container.querySelector('input')!.blur(); });
    expect(slots()).toEqual(['3', '•', '•', '•']);
  });
});
