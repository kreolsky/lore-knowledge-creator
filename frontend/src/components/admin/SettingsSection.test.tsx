/** SettingsSection — one registry tab's keys in the admin center.
 *
 * Pins the instance-settings plan's frontend contract:
 * - renders ONLY the tab's entries, grouped under SectionHeader sub-headers
 *   per registry section in server order; one key = one cabinet field block
 *   (label + source chip `| default|.env|override` + help + control + Save,
 *   Reset only when overridden, inline result line);
 * - per-type controls: int/float/str → FieldInput, secret → password input,
 *   bool → FieldCheckbox; PUT bodies carry TYPED values (numbers as numbers);
 * - restart keys are read-only: disabled input, no Save/Reset;
 * - a masked secret round-trips (unchanged → Save disabled, no PUT);
 * - Reset = DELETE the row + full refetch; failures render the server detail
 *   inline (the toast stays for list-load failure only).
 *
 * Harness: manual createRoot + act (mirrors AdminPage.test.tsx); the api
 * client is URL-routed in-memory.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let SettingsSection: typeof import('./SettingsSection').SettingsSection;
let container: HTMLDivElement;
let root: Root;
let appState: Record<string, unknown>;
let apiGet: ReturnType<typeof vi.fn>;
let apiPut: ReturnType<typeof vi.fn>;
let apiDelete: ReturnType<typeof vi.fn>;

/** A representative registry slice: every tab (agent entry must filter out),
 * every type, every source/effect class. */
const ENTRIES = [
  { key: 'AI_API_URL', env: 'AI_API_URL', tab: 'models', section: 'AI API', type: 'str', label: 'AI API base URL', help: 'The one OpenAI-compatible base URL STT and Chat both default to.', effect: 'live', min: null, max: null, source: '.env', value: 'https://api.example.com/v1' },
  { key: 'AI_API_KEY', env: 'AI_API_KEY', tab: 'models', section: 'AI API', type: 'secret', label: 'AI API base key', help: 'The shared key of the AI API base.', effect: 'live', min: null, max: null, source: 'override', value: '••••key1' },
  { key: 'CHAT_MODEL', env: 'CHAT_MODEL', tab: 'models', section: 'AI API', type: 'str', label: 'Default chat model', help: 'The model a new session pins.', effect: 'live', min: null, max: null, source: 'override', value: 'gpt-test' },
  { key: 'EMBEDDING_BATCH_SIZE', env: 'EMBEDDING_BATCH_SIZE', tab: 'models', section: 'Embeddings', type: 'int', label: 'Embedding batch size', help: 'Texts per embedding API call.', effect: 'live', min: 1, max: null, source: 'default', value: 16 },
  { key: 'EMBEDDING_CONCURRENCY', env: 'EMBEDDING_CONCURRENCY', tab: 'models', section: 'Embeddings', type: 'int', label: 'Embedding concurrency', help: 'Parallel embedding requests.', effect: 'restart', min: 1, max: null, source: 'default', value: 4 },
  { key: 'MCP_RUN_EXTRACTOR', env: 'MCP_RUN_EXTRACTOR', tab: 'tools', section: 'MCP gateway', type: 'bool', label: 'Run extractor over MCP', help: 'Lets an MCP key launch the extractor.', effect: 'live', min: null, max: null, source: 'default', value: true },
  { key: 'TURN_TIMEOUT_S', env: 'TURN_TIMEOUT_S', tab: 'agent', section: 'Agent line — the rented harness', type: 'float', label: 'No-progress budget (legacy), s', help: 'Legacy spelling.', effect: 'live', min: 1, max: null, source: 'default', value: 240 },
  // A section config.py aims at TWICE, non-adjacently (the real registry does
  // this for "CRDT / Backplane / Job Queue"): must fold into ONE heading.
  { key: 'YDOC_COMPACT_EVERY', env: 'YDOC_COMPACT_EVERY', tab: 'storage', section: 'CRDT / Backplane / Job Queue', type: 'int', label: 'Compact every', help: 'Updates per compaction.', effect: 'live', min: 1, max: null, source: 'default', value: 100 },
  { key: 'COLLAB_MAX_CLIENTS', env: 'COLLAB_MAX_CLIENTS', tab: 'storage', section: 'Collab limits', type: 'int', label: 'Max clients', help: 'Per-entity cap.', effect: 'live', min: 1, max: null, source: 'default', value: 20 },
  { key: 'COMFY_CONCURRENCY', env: 'COMFY_CONCURRENCY', tab: 'storage', section: 'CRDT / Backplane / Job Queue', type: 'int', label: 'ComfyUI concurrency', help: 'Concurrent jobs.', effect: 'restart', min: 1, max: null, source: 'default', value: 2 },
  // A `choices` entry: the value is picked, never typed.
  { key: 'WEB_SEARCH_PROVIDER', env: 'WEB_SEARCH_PROVIDER', tab: 'tools', section: 'Web search', type: 'str', label: 'Search provider', help: 'One provider.', effect: 'live', min: null, max: null, choices: ['off', 'deepseek', 'brave'], source: 'default', value: 'off' },
  // `visible_if` rows: shown only while the provider's saved value matches.
  { key: 'DEEPSEEK_API_KEY', env: 'DEEPSEEK_API_KEY', tab: 'tools', section: 'Web search', type: 'secret', label: 'DeepSeek key', help: '', effect: 'live', min: null, max: null, visible_if: { key: 'WEB_SEARCH_PROVIDER', value: 'deepseek' }, source: 'default', value: '' },
  { key: 'BRAVE_API_KEY', env: 'BRAVE_API_KEY', tab: 'tools', section: 'Web search', type: 'secret', label: 'Brave key', help: '', effect: 'live', min: null, max: null, visible_if: { key: 'WEB_SEARCH_PROVIDER', value: 'brave' }, source: 'default', value: '' },
];

const I18N: Record<string, string> = {
  settingsModels: 'Models & APIs',
  settingsAgent: 'Agent',
  settingsTools: 'Tools',
  settingsSearch: 'Search',
  settingsStorage: 'Storage & Jobs',
  sourceDefault: 'default',
  sourceEnv: '.env',
  sourceOverride: 'override',
  settingsReset: 'Reset',
  settingsRestartNote: 'Fixed at process start — set {env} in .env and restart.',
  settingsSaveFailed: 'Failed to save the setting',
  saved: 'Saved',
  save: 'Save',
};
const tFn = (k: string, vars?: Record<string, string>) =>
  vars ? (I18N[k] ?? k).replace('{env}', vars.env ?? '') : (I18N[k] ?? k);

/** Serve the list; tests may override via apiGet.mockImplementation*. */
function defaultGet() {
  apiGet = vi.fn(() => Promise.resolve({ settings: ENTRIES }));
}

beforeEach(async () => {
  vi.resetModules();
  appState = { showToast: vi.fn() };
  defaultGet();
  apiPut = vi.fn(() => Promise.resolve({}));
  apiDelete = vi.fn(() => Promise.resolve({}));

  vi.doMock('../../api/client', async () => {
    const actual = await vi.importActual<Record<string, unknown>>('../../api/client');
    return {
      ...actual,
      apiClient: {
        get: apiGet,
        post: vi.fn(() => Promise.resolve({})),
        patch: vi.fn(() => Promise.resolve({})),
        put: apiPut,
        delete: apiDelete,
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

  ({ SettingsSection } = await import('./SettingsSection'));

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

async function render(tab: 'models' | 'agent' | 'storage' | 'tools' = 'models') {
  await act(async () => { root.render(createElement(SettingsSection, { tab })); });
  await act(async () => {});
}

function sectionTitles(): string[] {
  return Array.from(container.querySelectorAll('[data-section-title]')).map(e => e.textContent ?? '');
}

describe('SettingsSection — layout', () => {

  it('a section declared twice non-adjacently renders ONE heading with all its entries', async () => {
    await render('storage');
    // No duplicate heading → no duplicate React key → no pinned/ghost heading
    // when the tab prop switches on the same mounted instance.
    expect(sectionTitles()).toEqual(['CRDT / Backplane / Job Queue', 'Collab limits']);
    const keys = Array.from(container.querySelectorAll('[data-setting-key]'))
      .map(e => e.getAttribute('data-setting-key'));
    expect(keys).toEqual(['YDOC_COMPACT_EVERY', 'COMFY_CONCURRENCY', 'COLLAB_MAX_CLIENTS']);
  });
});
