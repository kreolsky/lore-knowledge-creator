/** InfoTab — admin storage panel: two headline numbers + the per-table list.
 *
 * Pins the admin-info plan's frontend contract:
 * - renders the disk number and the deleted-logical number from the response;
 * - the table is sorted by deleted_bytes DESC;
 * - a missing mount (disk_error set) renders the backend's error text as an
 *   error and NO disk number — never a 0 standing in for "not mounted".
 *
 * Harness: manual createRoot + act (mirrors SettingsSection.test.tsx); the api
 * client is mocked at the module boundary.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let InfoTab: typeof import('./InfoTab').InfoTab;
let container: HTMLDivElement;
let root: Root;
let appState: Record<string, unknown>;
let apiGet: ReturnType<typeof vi.fn>;

const RESPONSE = {
  disk: { db_bytes: 1536, volume_bytes: 2048 },
  disk_error: null,
  tables: [
    { name: 'documents', live_rows: 10, live_bytes: 5000, deleted_rows: 2, deleted_bytes: 3000, in_deleted_projects_rows: 0, in_deleted_projects_bytes: 0 },
    { name: 'doc_chunks', live_rows: 40, live_bytes: 40000, deleted_rows: 0, deleted_bytes: 9000, in_deleted_projects_rows: 1, in_deleted_projects_bytes: 100 },
    { name: 'users', live_rows: 3, live_bytes: 300, deleted_rows: 0, deleted_bytes: 0, in_deleted_projects_rows: 0, in_deleted_projects_bytes: 0 },
  ],
  totals: { live_rows: 53, live_bytes: 45300, deleted_rows: 2, deleted_bytes: 12000, in_deleted_projects_rows: 1, in_deleted_projects_bytes: 100 },
  measured_ms: 42,
  measured_at: '2026-10-05T11:07:00+00:00',
};

const I18N: Record<string, string> = {
  adminInfo: 'Info',
  infoDbOnDisk: 'Database on disk',
  infoVolumeTotal: 'Volume total',
  infoDeletedData: 'Deleted data',
  infoLogicalEstimate: 'Byte columns are the logical size (estimate), not disk usage',
  infoTable: 'Table',
  infoLiveRows: 'Live rows',
  infoLiveBytes: 'Live bytes',
  infoDeletedRows: 'Deleted rows',
  infoDeletedBytes: 'Deleted bytes',
  infoInDeletedProjects: 'In deleted projects',
  infoRefresh: 'Refresh',
  infoMeasuring: 'Measuring…',
  infoMeasured: 'Measured at {at} in {ms} ms',
  failedToLoadStorageStats: 'Failed to load storage stats',
};
const tFn = (k: string, vars?: Record<string, string | number>) =>
  Object.entries(vars ?? {}).reduce((s, [v, x]) => s.replace(`{${v}}`, String(x)), I18N[k] ?? k);

beforeEach(async () => {
  vi.resetModules();
  appState = { showToast: vi.fn() };
  apiGet = vi.fn(() => Promise.resolve(RESPONSE));

  vi.doMock('../../api/client', async () => {
    const actual = await vi.importActual<Record<string, unknown>>('../../api/client');
    return {
      ...actual,
      apiClient: {
        get: apiGet,
        post: vi.fn(() => Promise.resolve({})),
        patch: vi.fn(() => Promise.resolve({})),
        put: vi.fn(() => Promise.resolve({})),
        delete: vi.fn(() => Promise.resolve({})),
      },
    };
  });
  vi.doMock('../../store/app-store', () => {
    const useAppStore = (sel: (s: Record<string, unknown>) => unknown) => sel(appState);
    (useAppStore as unknown as { getState: () => Record<string, unknown> }).getState = () => appState;
    return { useAppStore };
  });
  vi.doMock('../../i18n', () => ({
    t: tFn,
    useTranslation: () => ({ t: tFn }),
  }));

  ({ InfoTab } = await import('./InfoTab'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../../api/client');
  vi.doUnmock('../../store/app-store');
  vi.doUnmock('../../i18n');
});

async function render() {
  await act(async () => { root.render(createElement(InfoTab)); });
  await act(async () => {});
}

function rowNames(): string[] {
  return Array.from(container.querySelectorAll('tbody tr td:first-child')).map(e => e.textContent ?? '');
}

describe('InfoTab — numbers', () => {
  it('renders the two headline numbers and the volume total from the response', async () => {
    await render();
    const stats = Array.from(container.querySelectorAll('[data-info-stat]')).map(e => e.textContent);
    expect(stats).toEqual(['1.5 KB', '11.8 KB']); // db_bytes=1536; deleted 12000+100=12100B
    expect(container.textContent).toContain('Volume total: 2.0 KB');
    // Opening the tab reads the backend memo — no forced re-measure.
    expect(apiGet).toHaveBeenCalledWith('/admin/info/storage');
  });

  it('lists the tables sorted by deleted_bytes DESC', async () => {
    await render();
    // doc_chunks (9000) > documents (3000) > users (0)
    expect(rowNames()).toEqual(['doc_chunks', 'documents', 'users']);
    // The measurement time is always shown, so a memoized answer never reads as current.
    const at = new Date(RESPONSE.measured_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(container.textContent).toContain(`Measured at ${at} in 42 ms`);
  });

  it('Refresh forces a fresh measurement', async () => {
    await render();
    const before = apiGet.mock.calls.length;
    const btn = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.trim() === 'Refresh');
    expect(btn).toBeDefined();
    await act(async () => { btn!.click(); });
    await act(async () => {});
    expect(apiGet.mock.calls.length).toBe(before + 1);
    expect(apiGet).toHaveBeenLastCalledWith('/admin/info/storage?fresh=1');
  });

  it('disables Refresh while a measurement is in flight, so clicks cannot stack scans', async () => {
    await render();
    let resolve!: (v: unknown) => void;
    apiGet.mockImplementation(() => new Promise(r => { resolve = r; }));
    const btn = () => Array.from(container.querySelectorAll('button'))[0] as HTMLButtonElement;
    await act(async () => { btn().click(); });
    expect(btn().disabled).toBe(true);
    expect(btn().textContent).toContain('Measuring…');
    await act(async () => { btn().click(); });
    expect(apiGet).toHaveBeenCalledTimes(2); // mount + the first click only
    await act(async () => { resolve(RESPONSE); });
    expect(btn().disabled).toBe(false);
    expect(btn().textContent).toContain('Refresh');
  });
});

describe('InfoTab — missing mount', () => {
  it('renders the disk_error text as an error and no disk number', async () => {
    apiGet.mockImplementation(() => Promise.resolve({
      ...RESPONSE,
      disk: null,
      disk_error: '/surreal-data/lore.db is not mounted',
    }));
    await render();
    const err = container.querySelector('[data-info-error]');
    expect(err).toBeDefined();
    expect(err!.textContent).toBe('/surreal-data/lore.db is not mounted');
    // Exactly ONE headline number remains (deleted data) — no 0 for the disk.
    expect(container.querySelectorAll('[data-info-stat]')).toHaveLength(1);
  });
});
