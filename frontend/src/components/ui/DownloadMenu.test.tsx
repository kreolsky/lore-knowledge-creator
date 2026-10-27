/** DownloadMenu subtree zip: the `*.zip` item appears only on a live view of a
 * parent document (caller-gated via `subtree`), never beside a selected
 * snapshot, and clicks hit /api/documents/{id}/export/subtree. */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { DownloadMenu } from './DownloadMenu';

function renderMenu(props: Partial<Parameters<typeof DownloadMenu>[0]> = {}) {
  return render(<DownloadMenu documentId="doc1" hasText {...props} />);
}

function openMenu(container: HTMLElement) {
  fireEvent.click(container.querySelector('button')!);
}

function itemLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.download-menu__item'))
    .map(el => el.textContent!.trim());
}

describe('DownloadMenu subtree zip item', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows *.zip with subtree, between the format items and the original file', () => {
    const { container } = renderMenu({ subtree: true, original: { url: '/f.bin', ext: '.bin', name: 'f.bin' } });
    openMenu(container);
    expect(itemLabels(container)).toEqual(['*.pdf', '*.docx', '*.md', '*.zip', '*.bin']);
  });

  it('no *.zip without the subtree prop (leaf doc)', () => {
    const { container } = renderMenu();
    openMenu(container);
    expect(itemLabels(container)).not.toContain('*.zip');
  });

  it('no *.zip while a snapshot is selected (checkpointId set)', () => {
    const { container } = renderMenu({ subtree: true, checkpointId: 'cp1' });
    openMenu(container);
    expect(itemLabels(container)).not.toContain('*.zip');
  });

  it('clicking *.zip downloads /api/documents/{id}/export/subtree with the served filename', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(['zip'], { type: 'application/zip' }),
      headers: new Headers({ 'content-disposition': "attachment; filename=\"subtree.zip\"" }),
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:zip'), revokeObjectURL: vi.fn() });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const { container } = renderMenu({ subtree: true });
    openMenu(container);
    const zipItem = Array.from(container.querySelectorAll('.download-menu__item'))
      .find(el => el.textContent!.trim() === '*.zip')!;
    fireEvent.click(zipItem);

    await waitFor(() => expect(clickSpy).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/documents/doc1/export/subtree');
    expect(fetchMock.mock.calls[0][1]).toEqual({ credentials: 'include' });
    expect((clickSpy.mock.contexts[0] as HTMLAnchorElement).download).toBe('subtree.zip');
  });
});
