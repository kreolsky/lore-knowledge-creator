/** Locate a chat search hit's snippet inside the rendered message.
 *
 * The snippet is the indexed chunk's RAW markdown; the bubble holds its
 * rendering. Both sides are folded to bare lower-case words (markup, list
 * markers and punctuation dropped), and the longest leading run of the
 * snippet's words found in the rendered text names the block to land on. */

const MIN_WORDS = 3;
const MAX_WORDS = 12;
const BLOCKS = 'p, li, h1, h2, h3, h4, h5, h6, pre, blockquote, td, th';

// A link target carries no rendered text: `[label](url)` renders as `label`.
const words = (text: string): string[] =>
  text.replace(/\]\([^)]*\)/g, ' ').toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];

export function locateSnippet(root: HTMLElement, snippet: string): HTMLElement | null {
  const wanted = words(snippet);
  // Every rendered word remembers the text node it came from.
  const seq: string[] = [];
  const owners: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (const w of words((node as Text).data)) {
      seq.push(w);
      owners.push(node as Text);
    }
  }
  const shortest = Math.min(MIN_WORDS, wanted.length);
  for (let len = Math.min(MAX_WORDS, wanted.length); len >= shortest && len > 0; len--) {
    const needle = wanted.slice(0, len);
    for (let i = 0; i + len <= seq.length; i++) {
      if (!needle.every((w, k) => seq[i + k] === w)) continue;
      const inline = owners[i].parentElement;
      const block = inline?.closest<HTMLElement>(BLOCKS);
      return block && root.contains(block) ? block : inline;
    }
  }
  return null;
}
