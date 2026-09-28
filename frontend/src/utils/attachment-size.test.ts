import { describe, it, expect } from 'vitest';
import { dataUrlBytes, totalAttachmentBytes, historyAttachmentBytes } from './attachment-size';

describe('dataUrlBytes', () => {
  it('estimates decoded bytes from a base64 data URL', () => {
    // "Hello!" → base64 "SGVsbG8h" (8 chars, no padding) → 6 bytes
    expect(dataUrlBytes('data:image/png;base64,SGVsbG8h')).toBe(6);
  });

  it('accounts for base64 padding', () => {
    // "Hi" → base64 "SGk=" (1 pad) → 2 bytes
    expect(dataUrlBytes('data:image/png;base64,SGk=')).toBe(2);
    // "H" → base64 "SQ==" (2 pad) → 1 byte
    expect(dataUrlBytes('data:image/png;base64,SQ==')).toBe(1);
  });

  it('returns 0 for strings without a payload', () => {
    expect(dataUrlBytes('not-a-data-url')).toBe(0);
    expect(dataUrlBytes('data:image/png;base64,')).toBe(0);
  });
});

describe('totalAttachmentBytes', () => {
  it('sums bytes across multiple data URLs', () => {
    const urls = ['data:image/png;base64,SGVsbG8h', 'data:image/png;base64,SGk='];
    expect(totalAttachmentBytes(urls)).toBe(8);
  });

  it('returns 0 for an empty list', () => {
    expect(totalAttachmentBytes([])).toBe(0);
  });
});

describe('historyAttachmentBytes', () => {
  const img = 'data:image/png;base64,SGVsbG8h'; // 6 bytes

  it('sums image bytes across messages, skipping image-less ones', () => {
    const msgs = [
      { images: [img, img] }, // 12
      {},                     // 0 (no images)
      { images: [img] },      // 6
    ];
    expect(historyAttachmentBytes(msgs)).toBe(18);
  });

  it('returns 0 for an empty history', () => {
    expect(historyAttachmentBytes([])).toBe(0);
  });

  it('treats undefined/empty images as 0', () => {
    expect(historyAttachmentBytes([{}, { images: [] }])).toBe(0);
  });
});
