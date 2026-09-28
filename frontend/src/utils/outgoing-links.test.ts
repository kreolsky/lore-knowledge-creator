/** Tests for parseOutgoingLinks — pure link extraction from markdown content. */

// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { parseOutgoingLinks } from '../utils/outgoing-links';

describe('parseOutgoingLinks', () => {
  it('returns empty array for empty content', () => {
    expect(parseOutgoingLinks('')).toEqual([]);
  });

  it('returns empty array for content with no links', () => {
    expect(parseOutgoingLinks('Just some plain text without links.')).toEqual([]);
  });

  it('extracts document links', () => {
    const content = 'See [Chapter One](doc-abc-123) for details.';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'Chapter One', target: 'doc-abc-123', type: 'doc' },
    ]);
  });

  it('extracts reference links', () => {
    const content = 'Refer to [Source Material](ref:ref-001).';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'Source Material', target: 'ref:ref-001', type: 'ref' },
    ]);
  });

  it('extracts image reference links', () => {
    const content = '![Map of the realm](ref:img-42)';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'Map of the realm', target: 'ref:img-42', type: 'ref' },
    ]);
  });

  it('extracts external links', () => {
    const content = 'Visit [Wikipedia](https://en.wikipedia.org/wiki/Lore) for more.';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'Wikipedia', target: 'https://en.wikipedia.org/wiki/Lore', type: 'ext' },
    ]);
  });

  it('extracts http links', () => {
    const content = '[old site](http://example.com)';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'old site', target: 'http://example.com', type: 'ext' },
    ]);
  });

  it('excludes note links', () => {
    const content = 'Check [this note](note:note-001) and [another](note:note-002).';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([]);
  });

  it('excludes anchor links', () => {
    const content = 'Jump to [Section](#heading-1).';
    expect(parseOutgoingLinks(content)).toEqual([]);
  });

  it('excludes mailto links', () => {
    const content = 'Email [admin](mailto:admin@lore.app).';
    expect(parseOutgoingLinks(content)).toEqual([]);
  });

  it('groups all three types in order: doc, ref, ext', () => {
    const content = [
      '[Google](https://google.com)',
      '[Source](ref:r1)',
      '[Chapter](doc-1)',
      '[Bing](https://bing.com)',
      '[Other Doc](doc-2)',
    ].join('\n');

    const links = parseOutgoingLinks(content);
    const types = links.map(l => l.type);
    expect(types).toEqual(['doc', 'doc', 'ref', 'ext', 'ext']);
  });

  it('deduplicates same target within a type', () => {
    const content = [
      '[First mention](doc-1)',
      '[Second mention](doc-1)',
      '[Third mention](doc-1)',
    ].join('\n');

    const links = parseOutgoingLinks(content);
    expect(links).toHaveLength(1);
    expect(links[0].label).toBe('First mention');
  });

  it('does not deduplicate same target across different types', () => {
    // Unlikely in practice, but verifies the dedup key includes type
    const content = '[as doc](some-id)\n[as ref](ref:some-id)';
    const links = parseOutgoingLinks(content);
    expect(links).toHaveLength(2);
  });

  it('handles mixed content with notes excluded', () => {
    const content = [
      '# World Overview',
      '',
      'See [Geography](doc-geo) for the map.',
      'The [ancient scroll](ref:scroll-1) describes the lands.',
      'A [discussion](note:n1) was started.',
      'More at [wiki](https://wiki.example.com).',
      'Also see [History](doc-hist) and [another scroll](ref:scroll-1).',
    ].join('\n');

    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'Geography', target: 'doc-geo', type: 'doc' },
      { label: 'History', target: 'doc-hist', type: 'doc' },
      { label: 'ancient scroll', target: 'ref:scroll-1', type: 'ref' },
      { label: 'wiki', target: 'https://wiki.example.com', type: 'ext' },
    ]);
  });

  it('handles content with only notes (returns empty)', () => {
    const content = '[note A](note:a)\n[note B](note:b)';
    expect(parseOutgoingLinks(content)).toEqual([]);
  });

  it('extracts doc: prefixed links as doc type', () => {
    const content = 'See [Chapter](doc:doc-abc-123) for details.';
    const links = parseOutgoingLinks(content);
    expect(links).toEqual([
      { label: 'Chapter', target: 'doc-abc-123', type: 'doc' },
    ]);
  });

  it('deduplicates doc: prefix and no-prefix for same ID', () => {
    const content = '[first](doc-geo)\n[second](doc:doc-geo)';
    const links = parseOutgoingLinks(content);
    expect(links).toHaveLength(1);
    expect(links[0].label).toBe('first');
    expect(links[0].target).toBe('doc-geo');
  });
});
