/** Reloads once when a lazy chunk of this tab's build is gone from the server after a deploy. */
// WHY: a tab opened before a deploy still asks for its own hashed chunks (Dashboard-<hash>.js),
// which the new release no longer serves, so the lazy route crashes into the ErrorBoundary.
// Vite raises `vite:preloadError` for exactly that; a reload fetches the new index.html and
// its chunks. The once-per-minute latch keeps a chunk that is missing for any OTHER reason
// (a broken deploy, an offline tab) from looping: the second failure falls through to the
// ErrorBoundary and its own reload button.

const RELOAD_KEY = 'lore:stale-chunk-reload-at';
const RELOAD_LATCH_MS = 60_000;

function reloadedRecently(): boolean {
  try {
    return Date.now() - Number(sessionStorage.getItem(RELOAD_KEY) ?? 0) < RELOAD_LATCH_MS;
  } catch {
    // WHY: storage refused (private mode) — no latch means no proof the loop is safe.
    return true;
  }
}

export function installStaleChunkReload(): void {
  window.addEventListener('vite:preloadError', event => {
    if (reloadedRecently()) return;
    try {
      sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
    } catch {
      return;
    }
    event.preventDefault();
    window.location.reload();
  });
}
