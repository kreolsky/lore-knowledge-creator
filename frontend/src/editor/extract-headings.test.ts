/** Unit tests for extractHeadings — mirrors backend deps.py::extract_headings.
 *
 * Cases ported from tests/backend/test_unit_deps.py::TestExtractHeadings to keep
 * the client-side algorithm in lockstep with the server's persisted headings.
 */
import { describe, it, expect } from 'vitest';
import { extractHeadings, extractHeadingsFromLines } from './extract-headings';

describe('extractHeadings', () => {
  it('parses h1 through h4 and ignores h5', () => {
    const result = extractHeadings('# H1\n## H2\n### H3\n#### H4\n##### H5');
    expect(result).toEqual([
      { level: 1, text: 'H1', line: 1 },
      { level: 2, text: 'H2', line: 2 },
      { level: 3, text: 'H3', line: 3 },
      { level: 4, text: 'H4', line: 4 },
    ]);
  });

  it('skips # comments inside backtick fenced code blocks', () => {
    const result = extractHeadings('# Real\n```\n# Fake\n```\n## Also Real');
    expect(result).toHaveLength(2);
    expect(result[0].text).toBe('Real');
    expect(result[1].text).toBe('Also Real');
  });

  it('skips # comments inside tilde fenced code blocks', () => {
    const result = extractHeadings('# Real\n~~~\n# Fake\n~~~\n## Also Real');
    expect(result).toHaveLength(2);
  });

  it('only closes a fence with a matching-or-longer run of the same char', () => {
    const result = extractHeadings('````\n```\n# Fake\n```\n````\n# Real');
    expect(result).toEqual([{ level: 1, text: 'Real', line: 6 }]);
  });

  it('returns [] for empty content', () => {
    expect(extractHeadings('')).toEqual([]);
  });

  it('returns [] for plain text with no headings', () => {
    expect(extractHeadings('just text\nmore text')).toEqual([]);
  });

  it('reports 1-based line numbers', () => {
    const result = extractHeadings('text\n# First\ntext\n## Second');
    expect(result[0].line).toBe(2);
    expect(result[1].line).toBe(4);
  });

  it('keeps inline markdown in the heading text', () => {
    const result = extractHeadings('## Title with **bold** and `code`');
    expect(result[0].text).toBe('Title with **bold** and `code`');
  });
});

// extractHeadingsFromLines is the form the CM6 plugin actually runs (O(1)/line
// over the live Text). It must produce identical output to the split form, since
// both share buildHeadings — this pins the line-iterator wiring (1-based doc.line).
describe('extractHeadingsFromLines', () => {
  function fromString(content: string) {
    const lines = content.split('\n');
    return extractHeadingsFromLines(lines.length, i => lines[i]);
  }

  it('matches extractHeadings for a mixed document', () => {
    const content = 'text\n# First\n```\n# Fake\n```\n## Second';
    expect(fromString(content)).toEqual(extractHeadings(content));
  });

  it('reports 1-based line numbers via the line iterator', () => {
    const result = fromString('text\n# First\ntext\n## Second');
    expect(result).toEqual([
      { level: 1, text: 'First', line: 2 },
      { level: 2, text: 'Second', line: 4 },
    ]);
  });
});
