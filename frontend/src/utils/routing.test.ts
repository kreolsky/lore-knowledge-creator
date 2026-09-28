/** Tests for routing helpers — isEditorShell (URL-shape editor-shell selector)
 * and docUrl (the unified /docs/<id> link generator). plan "public-document-ids". */

import { describe, it, expect } from 'vitest';
import { isEditorShell, docUrl, isBareDocRoute } from './routing';

describe('isEditorShell', () => {
  it('matches /projects/<id>', () => {
    expect(isEditorShell('/projects/p1')).toBe(true);
  });
  it('matches /projects/<id>/docs/<id>', () => {
    expect(isEditorShell('/projects/p1/docs/d1')).toBe(true);
  });
  it('matches /docs/<id> (new canonical route)', () => {
    expect(isEditorShell('/docs/550e8400-e29b-41d4-a716-446655440000')).toBe(true);
  });
  it('does NOT match bare /projects (dashboard keeps the Header frame)', () => {
    expect(isEditorShell('/projects')).toBe(false);
  });
  it('does NOT match bare /docs', () => {
    expect(isEditorShell('/docs')).toBe(false);
  });
  it.each(['/cabinet', '/admin', '/', '/s/lore_token', '/projects/'])(
    'does NOT match top-level / trailing %s',
    (p) => {
      expect(isEditorShell(p)).toBe(false);
    },
  );
});

describe('docUrl', () => {
  it('builds /docs/<id>', () => {
    expect(docUrl('abc-123')).toBe('/docs/abc-123');
  });
});

describe('isBareDocRoute', () => {
  it('matches the bare /docs/<id> route', () => {
    expect(isBareDocRoute('/docs/550e8400-e29b-41d4-a716-446655440000')).toBe(true);
  });
  it('does NOT match the legacy nested /projects/<id>/docs/<id> route', () => {
    expect(isBareDocRoute('/projects/p1/docs/d1')).toBe(false);
  });
  it('does NOT match bare /docs (no id segment)', () => {
    expect(isBareDocRoute('/docs')).toBe(false);
  });
});
