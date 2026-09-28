import { describe, it, expect } from 'vitest';
import { resolveContentPickerAnchor } from './picker-anchor';

const refs = [
  { reference_id: 'ref-1', document_id: 'doc-A' },
  { reference_id: 'ref-2', document_id: 'doc-B' },
  { reference_id: 'ref-3', document_id: 'doc-C' },
];

describe('resolveContentPickerAnchor', () => {
  it('open doc wins over session doc', () => {
    const session = { reference_id: 'ref-1', document_id: 'doc-A' };
    expect(resolveContentPickerAnchor('doc-B', session, 'ref-1', refs)).toBe('doc-B');
  });

  it('open doc wins over session ref\'s owning doc', () => {
    const session = { reference_id: 'ref-1', document_id: null };
    expect(resolveContentPickerAnchor('doc-B', session, 'ref-1', refs)).toBe('doc-B');
  });

  it('ghost chat (no session) + open doc → open doc', () => {
    expect(resolveContentPickerAnchor('doc-A', null, null, refs)).toBe('doc-A');
  });

  it('no open doc + session with reference_id → that ref\'s owning doc', () => {
    const session = { reference_id: 'ref-2', document_id: null };
    expect(resolveContentPickerAnchor(null, session, null, refs)).toBe('doc-B');
  });

  it('no open doc + session with document_id → session doc', () => {
    const session = { reference_id: null, document_id: 'doc-X' };
    expect(resolveContentPickerAnchor(null, session, null, refs)).toBe('doc-X');
  });

  it('no open doc + only sessionRefId → owning doc', () => {
    expect(resolveContentPickerAnchor(null, null, 'ref-3', refs)).toBe('doc-C');
  });

  it('nothing resolves → null', () => {
    expect(resolveContentPickerAnchor(null, null, null, refs)).toBeNull();
  });

  it('session ref id not found in references → falls through to session doc', () => {
    const session = { reference_id: 'ref-missing', document_id: 'doc-X' };
    expect(resolveContentPickerAnchor(null, session, null, refs)).toBe('doc-X');
  });
});
