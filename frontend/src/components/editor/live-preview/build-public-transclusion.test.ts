/**
 * TDD for buildPublicTransclusionMap — the pure seed of `transcludeMap` on /s/:token.
 *
 * This is the regression guard for co-ownership: the module-level
 * `transcludeMap` is shared with the authed `useEditorReferenceSync`
 * (per-source ownership contract). The public path gets its OWN pure builder so
 * the seed is unit-testable without mounting the editor.
 *
 * Contract (mirrors the authed `refToEntry` shape):
 *   - image ref with file_path → { kind: 'ref-image', title, imageUrl: public path }
 *   - text ref with content?.trim() → { kind: 'ref-text', title, content }
 *   - empty text ref (no content / whitespace-only) → SKIPPED
 *   - doc → { kind: 'doc', title } with content undefined (loading; lazy fetch fills it)
 *
 * Source tagging: every entry is `source: 'ancestor-ref'` for refs and `source: 'doc'`
 * for docs — matches the authed per-source ownership contract so a future shared
 * writer cannot wipe the other's entries.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { transcludeMap } from './effects';
import { buildPublicTransclusionMap } from './build-public-transclusion';
import { setPublicFileContext } from '../../../utils/reference-url';
import type { Document, Reference } from '../../../types';

function makeDoc(id: string, title = id): Document {
  return {
    document_id: id,
    project_id: '',
    parent_id: null,
    title,
    content: '',
    path: '',
    is_index: false,
    created_at: '',
    updated_at: '',
  };
}

function makeImageRef(id: string, filePath: string, title = id): Reference {
  return {
    reference_id: id,
    project_id: '',
    document_id: 'owner',
    title,
    media_type: 'image',
    source_url: null,
    content: '',
    processing_status: 'ready',
    file_path: filePath,
    file_meta: null,
    updated_at: '',
    created_at: '',
  };
}

function makeTextRef(id: string, content: string | undefined, title = id): Reference {
  return {
    reference_id: id,
    project_id: '',
    document_id: 'owner',
    title,
    media_type: 'markdown',
    source_url: null,
    content,
    processing_status: 'ready',
    file_path: null,
    file_meta: null,
    updated_at: '',
    created_at: '',
  };
}

function makeFileRef(id: string, filePath: string, title = id): Reference {
  return {
    reference_id: id,
    project_id: '',
    document_id: 'owner',
    title,
    media_type: 'file',
    source_url: null,
    content: '',
    processing_status: null,
    file_path: filePath,
    file_meta: { mime_type: 'application/zip', file_size: 4096, original_name: 'page.zip' },
    updated_at: '',
    created_at: '',
  };
}

describe('buildPublicTransclusionMap', () => {
  beforeEach(() => {
    transcludeMap.clear();
    setPublicFileContext(null);
  });

  it('seeds an image ref as ref-image with the PUBLIC imageUrl', () => {
    setPublicFileContext('lore_pub');
    const ref = makeImageRef('img-1', 'proj/img-1/photo.png', 'Sunset');
    const map = buildPublicTransclusionMap([], [ref]);

    const entry = map.get('img-1');
    expect(entry).toBeDefined();
    expect(entry!.kind).toBe('ref-image');
    expect(entry!.title).toBe('Sunset');
    expect(entry!.imageUrl).toBe('/api/public/documents/lore_pub/files/img-1/photo.png');
    expect(entry!.source).toBe('ancestor-ref');
  });

  it('seeds an image ref with the authed imageUrl when no public token is set', () => {
    const ref = makeImageRef('img-1', 'proj/img-1/photo.png');
    const map = buildPublicTransclusionMap([], [ref]);

    const entry = map.get('img-1')!;
    expect(entry.kind).toBe('ref-image');
    expect(entry.imageUrl).toBe('/api/files/img-1/photo.png');
  });

  it('skips an image ref that has no file_path (broken upload)', () => {
    const ref = makeImageRef('img-1', '', 'No file');
    const map = buildPublicTransclusionMap([], [ref]);
    expect(map.has('img-1')).toBe(false);
  });

  it('seeds a file ref (agent archive) as ref-file with fileUrl + size', () => {
    const ref = makeFileRef('zip-1', 'proj/zip-1/page.zip', 'Bundle');
    const map = buildPublicTransclusionMap([], [ref]);

    const entry = map.get('zip-1');
    expect(entry).toBeDefined();
    expect(entry!.kind).toBe('ref-file');
    expect(entry!.title).toBe('Bundle');
    expect(entry!.fileUrl).toBe('/api/files/zip-1/page.zip');
    expect(entry!.fileSize).toBe(4096);
    expect(entry!.source).toBe('ancestor-ref');
    // A file ref has no body — content stays undefined (no loading band).
    expect(entry!.content).toBeUndefined();
  });

  it('skips a file ref that has no file_path (broken save)', () => {
    const ref = makeFileRef('zip-1', '', 'No file');
    const map = buildPublicTransclusionMap([], [ref]);
    expect(map.has('zip-1')).toBe(false);
  });

  it('seeds a text ref with content as ref-text', () => {
    const ref = makeTextRef('ref-1', '# Hello\nbody text', 'Notes');
    const map = buildPublicTransclusionMap([], [ref]);

    const entry = map.get('ref-1')!;
    expect(entry.kind).toBe('ref-text');
    expect(entry.title).toBe('Notes');
    expect(entry.content).toBe('# Hello\nbody text');
    expect(entry.source).toBe('ancestor-ref');
  });

  it('skips a text ref whose content is whitespace-only', () => {
    const ref = makeTextRef('ref-1', '   \n\t ', 'Empty');
    const map = buildPublicTransclusionMap([], [ref]);
    expect(map.has('ref-1')).toBe(false);
  });

  it('skips a text ref whose content is undefined (not yet hydrated)', () => {
    const ref = makeTextRef('ref-1', undefined, 'Pending');
    const map = buildPublicTransclusionMap([], [ref]);
    expect(map.has('ref-1')).toBe(false);
  });

  it('seeds a doc as a LOADING doc entry (content undefined)', () => {
    const doc = makeDoc('doc-1', 'Sibling');
    const map = buildPublicTransclusionMap([doc], []);

    const entry = map.get('doc-1')!;
    expect(entry.kind).toBe('doc');
    expect(entry.title).toBe('Sibling');
    expect(entry.content).toBeUndefined();
    expect(entry.source).toBe('doc');
  });

  it('seeds a doc that already carries content (open doc) as a resolved doc entry', () => {
    const doc: Document = { ...makeDoc('doc-1', 'Open'), content: '# Current doc body' };
    const map = buildPublicTransclusionMap([doc], []);

    const entry = map.get('doc-1')!;
    expect(entry.kind).toBe('doc');
    expect(entry.content).toBe('# Current doc body');
  });

  it('mixes refs and docs in one call without collision', () => {
    setPublicFileContext('lore_pub');
    const doc = makeDoc('d-1', 'Doc');
    const img = makeImageRef('i-1', 'p/i-1/x.png', 'Img');
    const txt = makeTextRef('t-1', 'body', 'Txt');

    const map = buildPublicTransclusionMap([doc], [img, txt]);

    expect(map.size).toBe(3);
    expect(map.get('d-1')!.kind).toBe('doc');
    expect(map.get('i-1')!.kind).toBe('ref-image');
    expect(map.get('t-1')!.kind).toBe('ref-text');
  });

  it('returns a fresh Map and does NOT mutate the module-level transcludeMap', () => {
    const ref = makeTextRef('r-1', 'body');
    const map = buildPublicTransclusionMap([], [ref]);

    expect(map.get('r-1')).toBeDefined();
    // The builder returns the next-state map; the caller (the hook) owns the
    // commit to the module-level map. This separation is what makes the builder
    // unit-testable in isolation.
    expect(transcludeMap.has('r-1')).toBe(false);
  });

  it('produces NO project-ref / cross-doc leakage (public scope is ancestor-only)', () => {
    // The authed path seeds project-wide cross-doc refs (source: 'project-ref').
    // The public surface has no project-ref fetch — every ref entry MUST be
    // tagged 'ancestor-ref', never 'project-ref'.
    setPublicFileContext('lore_pub');
    const refs = [makeImageRef('a', 'p/a/x.png'), makeTextRef('b', 'body')];
    const map = buildPublicTransclusionMap([], refs);
    for (const entry of map.values()) {
      expect(entry.source).not.toBe('project-ref');
    }
  });
});
