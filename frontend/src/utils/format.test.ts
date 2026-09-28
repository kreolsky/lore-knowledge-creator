import { describe, it, expect } from 'vitest';
import { formatDate } from './format';

describe('formatDate', () => {
  it('formats ISO datetime to YYYY-MM-DD HH:MM', () => {
    // Use UTC midnight so timezone doesn't shift the date
    const result = formatDate('2025-03-15T00:00:00Z');
    // Exact output depends on local timezone; just verify the shape
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it('pads single-digit months and days', () => {
    const result = formatDate('2025-01-05T00:00:00Z');
    expect(result).toMatch(/^\d{4}-01-0[45] /);
  });

  it('returns NaN-containing string for invalid input', () => {
    const result = formatDate('not-a-date');
    expect(result).toContain('NaN');
  });

  it('handles ISO string without Z suffix', () => {
    const result = formatDate('2025-06-20T14:30:00+00:00');
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  });
});
