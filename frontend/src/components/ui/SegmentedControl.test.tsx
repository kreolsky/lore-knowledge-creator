/** SegmentedControl: one active stop; a click reports the stop's value; a disabled stop is inert. */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { SegmentedControl } from './SegmentedControl';

const OPTIONS = [
  { value: 'a', label: 'A' },
  { value: 'b', label: 'B', disabled: true },
  { value: 'c', label: 'C' },
];

function stop(container: HTMLElement, label: string): HTMLButtonElement {
  return Array.from(container.querySelectorAll<HTMLButtonElement>('[role=radio]'))
    .find(b => b.textContent === label)!;
}

describe('SegmentedControl', () => {
  it('exactly one stop is aria-checked', () => {
    const { container } = render(<SegmentedControl options={OPTIONS} value="c" onChange={() => {}} ariaLabel="x" />);
    const checked = container.querySelectorAll('[aria-checked="true"]');
    expect(checked).toHaveLength(1);
    expect(checked[0].textContent).toBe('C');
  });

  it('clicking a stop calls onChange with its value', () => {
    const onChange = vi.fn();
    const { container } = render(<SegmentedControl options={OPTIONS} value="c" onChange={onChange} ariaLabel="x" />);
    fireEvent.click(stop(container, 'A'));
    expect(onChange).toHaveBeenCalledWith('a');
  });

  it('a disabled stop does not call onChange', () => {
    const onChange = vi.fn();
    const { container } = render(<SegmentedControl options={OPTIONS} value="c" onChange={onChange} ariaLabel="x" />);
    fireEvent.click(stop(container, 'B'));
    expect(onChange).not.toHaveBeenCalled();
  });
});
