/** Parse a download filename from a Content-Disposition header value.
 * Prefers the UTF-8 `filename*` form (RFC 5987, percent-encoded), then the
 * ASCII `filename="..."` form. Returns `fallback` when the header is absent,
 * has no filename token, or the UTF-8 value is malformed. */
export function parseContentDispositionFilename(cd: string, fallback: string): string {
  if (!cd) return fallback;
  const utf8Match = cd.match(/filename\*=UTF-8''(.+)/i);
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return fallback;
    }
  }
  const asciiMatch = cd.match(/filename="([^"]+)"/i);
  if (asciiMatch) return asciiMatch[1];
  return fallback;
}
