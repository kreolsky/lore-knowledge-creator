import { describe, it, expect } from 'vitest';
import { parseContentDispositionFilename } from './content-disposition';

describe('parseContentDispositionFilename', () => {
  it('parses UTF-8 filename* (percent-encoded)', () => {
    const cd = "attachment; filename*=UTF-8''%D0%97%D0%B0%D0%B3%D0%BE%D0%BB%D0%BE%D0%B2%D0%BE%D0%BA.md";
    expect(parseContentDispositionFilename(cd, 'fallback.md')).toBe('Заголовок.md');
  });

  it('parses plain ASCII filename="..."', () => {
    const cd = 'attachment; filename="my-doc.pdf"';
    expect(parseContentDispositionFilename(cd, 'fallback.pdf')).toBe('my-doc.pdf');
  });

  it('prefers UTF-8 filename* over ASCII filename when both present', () => {
    const cd = "attachment; filename=\"ascii.pdf\"; filename*=UTF-8''real.pdf";
    expect(parseContentDispositionFilename(cd, 'fallback.pdf')).toBe('real.pdf');
  });

  it('returns fallback when header is empty', () => {
    expect(parseContentDispositionFilename('', 'fallback.docx')).toBe('fallback.docx');
  });

  it('returns fallback when header is garbage (no filename token)', () => {
    expect(parseContentDispositionFilename('attachment', 'fallback.md')).toBe('fallback.md');
  });

  it('returns fallback when UTF-8 value is malformed', () => {
    const cd = "attachment; filename*=UTF-8''%E0%A4%";
    expect(parseContentDispositionFilename(cd, 'fallback.md')).toBe('fallback.md');
  });
});
