/** Attachment-size helpers — estimate raw source bytes from data URLs. */

/**
 * Estimate the decoded byte length of a base64 `data:` URL.
 *
 * The chat stores pending images as data URLs. The attach-time budget is
 * expressed in RAW source bytes (the file's original size), and base64 inflates
 * ~33%. Decoding length * 3/4 (minus any `=` padding) recovers the source size
 * closely enough to enforce a byte budget without allocating the full blob.
 */
export function dataUrlBytes(dataUrl: string): number {
  const comma = dataUrl.indexOf(',');
  if (comma < 0) return 0;
  const b64 = dataUrl.slice(comma + 1);
  const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
  const len = b64.length - padding;
  if (len <= 0) return 0;
  return Math.floor((len * 3) / 4);
}

/** Sum of raw bytes across an array of data URLs. */
export function totalAttachmentBytes(dataUrls: string[]): number {
  return dataUrls.reduce((sum, u) => sum + dataUrlBytes(u), 0);
}

/** Convert a megabyte budget to bytes (1024² base, matching the backend cap). */
export function attachmentBudgetBytes(maxMb: number): number {
  return maxMb * 1024 * 1024;
}

/** True when adding `addedBytes` to the already-queued `pendingBytes` would
 * exceed the shared attachment budget. Single source of the limit math so the
 * chat / note composer / note drop paths cannot drift. */
export function attachmentBudgetExceeded(pendingBytes: number, addedBytes: number, maxMb: number): boolean {
  return pendingBytes + addedBytes > attachmentBudgetBytes(maxMb);
}

/** Format a byte count as a one-decimal megabyte string for toast messages. */
export function formatAttachmentMb(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(1);
}

/**
 * Sum of image bytes across history messages that ride along in `apiMessages`.
 *
 * Every send builder ships ALL non-deleted history images as LLM context
 * (`activePath.map(... images: m.images)`), so the attach-time budget must count
 * them too — not just the new turn's attachments — or the request body can exceed
 * the middleware cap and 413 silently.
 */
export function historyAttachmentBytes(messages: { images?: string[] }[]): number {
  return messages.reduce((sum, m) => sum + (m.images ? totalAttachmentBytes(m.images) : 0), 0);
}
