/**
 * Unit tests for the CSV/TSV → native table import core (RFC-4180 state machine).
 *
 * see SYSTEM: table-block — the pure parse/serialize layer for delimited text import,
 * a sibling of `gfm-table-import.ts` (which handles the markdown pipe-table format).
 * CSV is a delimiter format: the same `string[][]` matrix feeds `createTable`.
 */
import { describe, it, expect } from 'vitest';
import {
  sniffDelimiter,
  parseCsv,
  serializeTableCsv,
  MAX_TABLE_IMPORT_ROWS,
  MAX_TABLE_IMPORT_COLS,
  MAX_TABLE_IMPORT_CELLS,
} from './csv-table';

describe('sniffDelimiter', () => {
  it('returns the most frequent delimiter among , ; tab on the first non-empty line', () => {
    expect(sniffDelimiter('a,b,c\n1,2,3', ',')).toBe(',');
    expect(sniffDelimiter('a;b;c\n1;2;3', ',')).toBe(';');
    expect(sniffDelimiter('a\tb\tc\n1\t2\t3', ',')).toBe('\t');
  });

  it('falls back to the provided fallback when no delimiter is present', () => {
    expect(sniffDelimiter('single column row\nx', ',')).toBe(',');
    expect(sniffDelimiter('\n\nfirst real line has no delim', ';')).toBe(';');
  });

  it('counts on the first NON-empty line (skips leading blanks)', () => {
    expect(sniffDelimiter('\n\na;b;c\n1;2;3', ',')).toBe(';');
  });
});

describe('parseCsv', () => {
  it('parses a simple comma-delimited table', () => {
    expect(parseCsv('a,b,c\n1,2,3', ',')).toEqual([['a', 'b', 'c'], ['1', '2', '3']]);
  });

  it('strips a leading UTF-8 BOM', () => {
    expect(parseCsv('\uFEFFa,b\n1,2', ',')).toEqual([['a', 'b'], ['1', '2']]);
  });

  it('preserves a quoted field containing the delimiter', () => {
    expect(parseCsv('"a,b",c\n"x,y",z', ',')).toEqual([['a,b', 'c'], ['x,y', 'z']]);
  });

  it('unescapes "" → " inside a quoted field', () => {
    expect(parseCsv('"he said ""hi""",b', ',')).toEqual([['he said "hi"', 'b']]);
  });

  it('preserves embedded newlines inside a quoted field', () => {
    const rows = parseCsv('"line1\nline2",b\n"c,d"', ',');
    expect(rows).toEqual([['line1\nline2', 'b'], ['c,d']]);
  });

  it('handles CRLF and LF line endings equivalently', () => {
    expect(parseCsv('a,b\r\nc,d', ',')).toEqual([['a', 'b'], ['c', 'd']]);
    expect(parseCsv('a,b\nc,d', ',')).toEqual([['a', 'b'], ['c', 'd']]);
  });

  it('accepts the semicolon delimiter (European CSV)', () => {
    expect(parseCsv('a;b;c\n1;2;3', ';')).toEqual([['a', 'b', 'c'], ['1', '2', '3']]);
  });

  it('returns [] for an empty file (after BOM strip)', () => {
    expect(parseCsv('', ',')).toEqual([]);
    expect(parseCsv('\uFEFF', ',')).toEqual([]);
  });

  it('keeps ragged rows as-is (no padding here — createTable pads)', () => {
    expect(parseCsv('a,b,c\n1,2', ',')).toEqual([['a', 'b', 'c'], ['1', '2']]);
  });

  it('does not require the last row to be newline-terminated', () => {
    expect(parseCsv('a,b\nc,d', ',')).toEqual([['a', 'b'], ['c', 'd']]);
  });
});

describe('serializeTableCsv', () => {
  it('prepends a UTF-8 BOM', () => {
    expect(serializeTableCsv({ rows: [['a']] }).startsWith('\uFEFF')).toBe(true);
  });

  it('joins cells by the delimiter and rows by CRLF', () => {
    const out = serializeTableCsv({ rows: [['a', 'b'], ['c', 'd']] });
    expect(out).toBe('\uFEFFa,b\r\nc,d');
  });

  it('quotes a field iff it contains delimiter, quote, CR or LF', () => {
    const out = serializeTableCsv({ rows: [['a,b', 'c"d', 'e\nf', 'g\rh', 'plain']] });
    // strip BOM then split first row
    expect(out.replace(/^\uFEFF/, '').split('\r\n')[0]).toBe('"a,b","c""d","e\nf","g\rh",plain');
  });

  it('round-trips through parseCsv for the simple case', () => {
    const rows = [['h1', 'h2'], ['1', '2']];
    const back = parseCsv(serializeTableCsv({ rows }), ',');
    expect(back).toEqual(rows);
  });

  it('round-trips fields with embedded commas, newlines and quotes', () => {
    const rows = [
      ['name', 'note'],
      ['a,b', 'line1\nline2'],
      ['quote "x"', 'semi;colon'],
    ];
    const back = parseCsv(serializeTableCsv({ rows }), ',');
    expect(back).toEqual(rows);
  });
});

describe('caps', () => {
  it('exposes sane import caps', () => {
    expect(MAX_TABLE_IMPORT_ROWS).toBeGreaterThan(0);
    expect(MAX_TABLE_IMPORT_COLS).toBeGreaterThan(0);
    expect(MAX_TABLE_IMPORT_ROWS).toBe(5000);
    expect(MAX_TABLE_IMPORT_COLS).toBe(200);
    // Product cap bounds the synchronous cell allocation (sub-second at 100k).
    expect(MAX_TABLE_IMPORT_CELLS).toBe(100_000);
    // The row×col caps alone would allow 1M cells; the product cap must be the tighter bound.
    expect(MAX_TABLE_IMPORT_CELLS).toBeLessThan(MAX_TABLE_IMPORT_ROWS * MAX_TABLE_IMPORT_COLS);
  });
});
