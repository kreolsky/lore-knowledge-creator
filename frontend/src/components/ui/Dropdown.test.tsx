/** Dropdown filter: the field narrows rows by label or note, Enter picks from the narrowed list, the query resets on close. */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { Dropdown } from './Dropdown';

const OPTIONS = [
  { value: 'bob@x.test', label: 'Bob', note: 'bob@x.test' },
  { value: 'ann@x.test', label: 'Ann', note: 'ann@x.test' },
  { value: 'zed@corp.test', label: 'Zed', note: 'zed@corp.test' },
];

function rows(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('[role=option]'))
    .map(r => r.querySelector('.truncate')!.textContent!);
}

function open(container: HTMLElement) {
  fireEvent.click(container.querySelector('button')!);
}

describe('Dropdown filterPlaceholder', () => {
  it('no filter field without the prop', () => {
    const { container } = render(<Dropdown value="" options={OPTIONS} onSelect={() => {}} />);
    open(container);
    expect(container.querySelector('input')).toBeNull();
    expect(rows(container)).toEqual(['Bob', 'Ann', 'Zed']);
  });

  it('narrows by label and by note; Enter picks the first narrowed row', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <Dropdown value="" options={OPTIONS} onSelect={onSelect} filterPlaceholder="filter" />,
    );
    open(container);
    const input = container.querySelector('input')!;
    expect(input.placeholder).toBe('filter');
    fireEvent.change(input, { target: { value: 'an' } });
    expect(rows(container)).toEqual(['Ann']);
    fireEvent.change(input, { target: { value: 'corp' } });
    expect(rows(container)).toEqual(['Zed']);
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith('zed@corp.test');
    expect(container.querySelector('[role=listbox]')).toBeNull();
  });

  it('the query resets when the panel closes', () => {
    const { container } = render(
      <Dropdown value="" options={OPTIONS} onSelect={() => {}} filterPlaceholder="filter" />,
    );
    open(container);
    fireEvent.change(container.querySelector('input')!, { target: { value: 'zed' } });
    expect(rows(container)).toEqual(['Zed']);
    fireEvent.keyDown(container.querySelector('input')!, { key: 'Escape' });
    open(container);
    expect(container.querySelector('input')!.value).toBe('');
    expect(rows(container)).toEqual(['Bob', 'Ann', 'Zed']);
  });
});
