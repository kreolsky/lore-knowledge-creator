/**
 * Side-by-side coverage of the two link-metadata branches feeding the global
 * hover preview (plan chat-markdown-on-dsh, step 3):
 * - the data-link-* attribute branch — sources list items (.chat-source),
 *   editor CM6 decorations, legacy .chat-link tags;
 * - the href-derived branch — dsh chat anchors whose internal doc:/ref:/note:
 *   scheme the chat wrapper rewrote to https://lore.local/l/<type>/<id>.
 *
 * Both must resolve the SAME preview for the SAME entity: the attribute
 * branch keeps working unchanged, the href branch derives its LinkMeta from
 * the anchor's href when the attributes are absent.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { EditorLinkPreview } from './EditorLinkPreview';

vi.mock('../../hooks/useDocumentPreview', () => ({
  useDocumentPreview: vi.fn((id: string | null) => ({
    content: id ? `preview:${id}` : undefined,
    error: false,
    loading: false,
  })),
}));

vi.mock('../../hooks/useReferencePreview', () => ({
  useReferencePreview: vi.fn(() => ({ preview: null, error: false, loading: false })),
}));

/** Mount the host, append a link, hover it (mouseenter, as the host listens on
 * the document with capture), and let the 300ms delay elapse via waitFor. */
async function renderHostAndHover(link: HTMLAnchorElement) {
  render(<EditorLinkPreview />);
  document.body.appendChild(link);
  link.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
  return link;
}

function testLink(): HTMLAnchorElement {
  const a = document.createElement('a');
  a.setAttribute('data-test-link', '');
  a.textContent = 'link';
  return a;
}

afterEach(() => {
  cleanup();
  document.querySelectorAll('a[data-test-link]').forEach(el => el.remove());
});

describe('EditorLinkPreview — metadata branches', () => {
  it('attribute branch: a .chat-link with data-link-type/id shows the doc preview', async () => {
    const a = testLink();
    a.className = 'chat-link';
    a.setAttribute('data-link-type', 'doc');
    a.setAttribute('data-link-id', 'doc123');
    await renderHostAndHover(a);
    expect(await screen.findByText('preview:doc123')).toBeTruthy();
  });

  it('href branch: a dsh chat anchor with an internal lore.local href shows the doc preview', async () => {
    const a = testLink();
    a.className = 'chat-link-dsh'; // any class — matched by the .chat-markdown a[href^=…] selector
    a.href = 'https://lore.local/l/doc/doc123';
    const host = document.createElement('div');
    host.className = 'chat-markdown';
    host.appendChild(a);
    render(<EditorLinkPreview />);
    document.body.appendChild(host);
    a.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
    expect(await screen.findByText('preview:doc123')).toBeTruthy();
  });

  it('attribute branch: a prefixed ref id resolves through decodeLinkId', async () => {
    const a = testLink();
    a.className = 'chat-source';
    a.setAttribute('data-link-type', 'ref');
    a.setAttribute('data-link-id', 'ref:ref456');
    await renderHostAndHover(a);
    // Ref previews resolve from the (empty) store + the mocked fetch hook;
    // the popup must be VISIBLE with the ref branch active — assert the
    // popup container exists (content branch is covered by the doc cases).
    await waitFor(() => {
      const popup = document.querySelector('.fixed.z-55');
      if (!popup) throw new Error('popup not mounted');
    });
  });

  it('href branch: an external dsh anchor gets NO preview (no data attributes, external href)', async () => {
    const a = testLink();
    a.href = 'https://example.com/x';
    const host = document.createElement('div');
    host.className = 'chat-markdown';
    host.appendChild(a);
    render(<EditorLinkPreview />);
    document.body.appendChild(host);
    a.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
    // Give the 300ms delay room to fire; the popup must stay absent.
    await new Promise(r => setTimeout(r, 500));
    expect(document.querySelector('.fixed.z-55')).toBeNull();
  });
});
