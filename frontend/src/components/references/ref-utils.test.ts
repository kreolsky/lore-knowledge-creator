import { describe, it, expect } from 'vitest';
import { isDocxFile, isPdfFile, isTableFile, mediaTypeLabel, referenceDownloadProps } from './ref-utils';
import type { Reference } from '../../types';

describe('mediaTypeLabel', () => {
  it('maps each media_type to its short group label', () => {
    expect(mediaTypeLabel('markdown')).toBe('txt');
    expect(mediaTypeLabel('audio')).toBe('audio');
    expect(mediaTypeLabel('image')).toBe('img');
    expect(mediaTypeLabel('file')).toBe('file');
  });
});

describe('isDocxFile', () => {
  it('accepts .docx case-insensitively', () => {
    expect(isDocxFile('report.docx')).toBe(true);
    expect(isDocxFile('REPORT.DOCX')).toBe(true);
  });

  it('rejects non-docx files', () => {
    expect(isDocxFile('notes.md')).toBe(false);
    expect(isDocxFile('photo.png')).toBe(false);
    // Legacy .doc is out of scope for this iteration.
    expect(isDocxFile('legacy.doc')).toBe(false);
  });
});

describe('isPdfFile', () => {
  it('accepts .pdf case-insensitively', () => {
    expect(isPdfFile('paper.pdf')).toBe(true);
    expect(isPdfFile('PAPER.PDF')).toBe(true);
  });

  it('rejects non-pdf files', () => {
    expect(isPdfFile('notes.md')).toBe(false);
    expect(isPdfFile('report.docx')).toBe(false);
    expect(isPdfFile('archive.zip')).toBe(false);
  });
});

describe('isTableFile', () => {
  it('accepts .csv and .tsv case-insensitively', () => {
    expect(isTableFile('data.csv')).toBe(true);
    expect(isTableFile('DATA.CSV')).toBe(true);
    expect(isTableFile('sheet.tsv')).toBe(true);
    expect(isTableFile('SHEET.TSV')).toBe(true);
  });

  it('rejects other text/spreadsheet-like files (xlsx/xls are out of scope)', () => {
    expect(isTableFile('notes.md')).toBe(false);
    expect(isTableFile('report.xlsx')).toBe(false);
    expect(isTableFile('legacy.xls')).toBe(false);
    expect(isTableFile('report.docx')).toBe(false);
  });
});

describe('referenceDownloadProps', () => {
  const base: Reference = {
    reference_id: 'r1', project_id: 'p1', document_id: 'd1', title: 'Notes', media_type: 'markdown',
    source_url: null, content: 'x', processing_status: 'ready', file_path: null, file_meta: null,
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  };

  it('markdown-only ref: no original, default export formats', () => {
    expect(referenceDownloadProps(base)).toEqual({ original: undefined, exportFormats: undefined });
  });

  it('converted PDF: original is the .pdf and the pdf export item is dropped', () => {
    const ref = { ...base, file_path: '/files/paper.pdf', file_meta: { original_name: 'paper.pdf' } as Reference['file_meta'] };
    const out = referenceDownloadProps(ref);
    expect(out.exportFormats).toEqual(['docx', 'md']);
    expect(out.original).toEqual({ url: '/api/files/r1/paper.pdf', ext: '.pdf', name: 'paper.pdf' });
  });

  it('imported .docx (markdown + file): original stays in the menu — no media surface hosts it', () => {
    const ref = { ...base, file_path: '/files/report.docx', file_meta: { original_name: 'report.docx' } as Reference['file_meta'] };
    expect(referenceDownloadProps(ref).original).toEqual({ url: '/api/files/r1/report.docx', ext: '.docx', name: 'report.docx' });
  });

  it.each(['audio', 'image', 'file'] as const)('%s ref with a file_path: original is undefined (download lives on the media)', (mediaType) => {
    const ref = { ...base, media_type: mediaType, file_path: '/files/a.bin', file_meta: { original_name: 'a.bin' } as Reference['file_meta'] };
    expect(referenceDownloadProps(ref).original).toBeUndefined();
    expect(referenceDownloadProps(ref).exportFormats).toBeUndefined();
  });
});
