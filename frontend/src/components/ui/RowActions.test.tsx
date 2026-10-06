/** RowActions burger: opens on click/tap (touch has no hover), closes on a press outside. */
import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { RowActions } from './RowActions';

function renderRow() {
  return render(
    <div>
      <RowActions menu={<button>Delete</button>} />
      <button data-testid="outside">elsewhere</button>
    </div>,
  );
}

const menuOf = (container: HTMLElement) => container.querySelector('svg')!.closest('span')!.parentElement!;

describe('RowActions burger', () => {
  it('opens on click of the burger icon without waiting for hover', () => {
    const { container } = renderRow();
    const zone = menuOf(container);
    expect(zone.className).not.toMatch(/gearOpen/);
    fireEvent.click(container.querySelector('svg')!.closest('span')!);
    expect(zone.className).toMatch(/gearOpen/);
  });

  it('closes on a press outside the cluster', () => {
    const { container } = renderRow();
    const zone = menuOf(container);
    fireEvent.click(container.querySelector('svg')!.closest('span')!);
    fireEvent.mouseDown(container.querySelector('[data-testid="outside"]')!);
    expect(zone.className).not.toMatch(/gearOpen/);
  });
});
