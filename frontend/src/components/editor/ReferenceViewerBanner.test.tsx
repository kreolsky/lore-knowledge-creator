/** ReferenceViewerBanner — the header acts on the TEXT only: no toMarkdown, the
 * delete button appears only for a ref with neither text nor file (a ref with a
 * file is deleted on the media or via the Refs-list trash), and the download
 * menu carries the original ONLY for markdown refs with a file (converted PDF /
 * imported .docx — no media surface hosts the action). */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { Reference } from '../../types';

vi.mock('../../store/app-store', () => ({
  useAppStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      currentDocument: { document_id: 'd1', title: 'Host' },
      currentProject: { project_id: 'p1' },
      references: [],
    }),
}));
vi.mock('../../api/client', () => ({ apiClient: { post: vi.fn(), delete: vi.fn() } }));
vi.mock('../../events', () => ({ emit: vi.fn() }));
vi.mock('../../editor/active-editor', () => ({ useEditorContent: () => () => '' }));
vi.mock('../ui', () => ({
  Button: (props: { onClick?: () => void; children?: React.ReactNode }) => (
    <button onClick={props.onClick}>{props.children}</button>
  ),
  DownloadMenu: (props: { exportFormats?: string[]; original?: { name: string } }) => (
    <div
      data-testid="download-menu"
      data-formats={(props.exportFormats ?? ['pdf', 'docx', 'md']).join(',')}
      data-original={props.original?.name ?? ''}
    />
  ),
}));
vi.mock('../../utils/reference-url', () => ({
  referenceFileUrl: (id: string, path: string) => `/api/files/${id}/${path.split('/').pop()}`,
}));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../hooks/useReferenceDelete', () => ({
  useReferenceDelete: () => ({ scheduleDelete: vi.fn() }),
}));

import { ReferenceViewerBanner } from './ReferenceViewerBanner';

function baseRef(mediaType: Reference['media_type'], over: Partial<Reference> = {}): Reference {
  return {
    reference_id: 'r1',
    project_id: 'p',
    document_id: 'd1',
    title: 'page.zip',
    media_type: mediaType,
    source_url: null,
    content: '',
    processing_status: null,
    file_path: 'p/r1/page.zip',
    file_meta: { mime_type: 'application/zip', file_size: 10, original_name: 'page.zip' },
    ...over,
  } as Reference;
}

function pdfRef(over: Partial<Reference> = {}): Reference {
  return {
    ...baseRef('markdown'),
    title: 'paper.pdf',
    file_path: 'p/r1/paper.pdf',
    file_meta: { mime_type: 'application/pdf', file_size: 10, original_name: 'paper.pdf' },
    ...over,
  } as Reference;
}

function renderBanner(ref: Reference, opts: { isEmpty?: boolean; isReadonly?: boolean } = {}): HTMLElement {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root: Root = createRoot(container);
  act(() => {
    root.render(
      <ReferenceViewerBanner
        reference={ref}
        isReadonly={opts.isReadonly ?? false}
        isEmpty={opts.isEmpty ?? true}
        backLabel="Back to doc"
        onBack={() => {}}
      />,
    );
  });
  return container;
}

describe('ReferenceViewerBanner header scope', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('an EMPTY file ref WITH a file still shows the back button, but NO header delete (the file is dropped on the media)', () => {
    const el = renderBanner(baseRef('file'), { isEmpty: true });
    expect(el.textContent).toContain('Back to doc');
    expect(el.textContent).not.toContain('delete');
  });

  it.each(['audio', 'image', 'file'] as const)('no toMarkdown for a %s ref with text', (mediaType) => {
    const el = renderBanner(baseRef(mediaType), { isEmpty: false });
    expect(el.textContent).not.toContain('toMarkdown');
  });

  it.each(['markdown', 'audio', 'image', 'file'] as const)('delete shown for a text-less FILE-less %s ref', (mediaType) => {
    const el = renderBanner(baseRef(mediaType, { file_path: null, file_meta: null }), { isEmpty: true });
    expect(el.textContent).toContain('delete');
  });

  it.each(['audio', 'image', 'file'] as const)('delete hidden for a text-less %s ref WITH a file', (mediaType) => {
    const el = renderBanner(baseRef(mediaType), { isEmpty: true });
    expect(el.textContent).not.toContain('delete');
  });

  it('delete hidden when readonly', () => {
    const el = renderBanner(baseRef('markdown', { file_path: null, file_meta: null }), { isEmpty: true, isReadonly: true });
    expect(el.textContent).not.toContain('delete');
  });

  it.each(['audio', 'image', 'file'] as const)('%s ref: download menu has no original item (download lives on the media)', (mediaType) => {
    const el = renderBanner(baseRef(mediaType), { isEmpty: false });
    expect(el.querySelector('[data-testid="download-menu"]')!.getAttribute('data-original')).toBe('');
  });

  it('shows openOriginal and drops the pdf export item for a converted PDF ref', () => {
    const el = renderBanner(pdfRef(), { isEmpty: false });
    expect(el.textContent).toContain('openOriginal');
    const menu = el.querySelector('[data-testid="download-menu"]')!;
    expect(menu.getAttribute('data-formats')).toBe('docx,md');
    expect(menu.getAttribute('data-original')).toBe('paper.pdf');
  });

  it('leaves a non-pdf markdown ref without openOriginal and with every export format', () => {
    const el = renderBanner(pdfRef({ title: 'paper.docx', file_path: 'p/r1/paper.docx' }), { isEmpty: false });
    expect(el.textContent).not.toContain('openOriginal');
    expect(el.querySelector('[data-testid="download-menu"]')!.getAttribute('data-formats')).toBe('pdf,docx,md');
  });
});
