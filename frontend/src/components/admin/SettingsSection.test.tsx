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
  { key: 'CHAT_QUERY_REWRITE_ENABLED', env: 'CHAT_QUERY_REWRITE_ENABLED', tab: 'models', section: 'Query rewrite', type: 'bool', label: 'Query rewrite on/off', help: 'Off = retrieval embeds the raw user message.', effect: 'live', min: null, max: null, source: 'default', value: true },
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

/** The field block of one key (data-setting-key) or fail. */
function field(key: string): HTMLElement {
  const el = container.querySelector(`[data-setting-key="${key}"]`);
  expect(el, `field block "${key}"`).toBeDefined();
  return el as HTMLElement;
}

function sectionTitles(): string[] {
  return Array.from(container.querySelectorAll('[data-section-title]')).map(e => e.textContent ?? '');
}

async function typeInto(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  await act(async () => {
    const proto = input instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function clickButton(scope: ParentNode, text: string) {
  const btn = Array.from(scope.querySelectorAll('button'))
    .find(b => b.textContent?.trim() === text);
  expect(btn, `button "${text}"`).toBeDefined();
  act(() => btn!.click());
}

describe('SettingsSection — layout', () => {
  it("renders only this tab's entries under section sub-headers, in server order", async () => {
    await render('models');
    expect(sectionTitles()).toEqual(['AI API', 'Embeddings', 'Query rewrite']);
    expect(field('AI_API_URL')).toBeDefined();
    expect(field('CHAT_QUERY_REWRITE_ENABLED')).toBeDefined();
    // The agent-tab entry is filtered out.
    expect(container.querySelector('[data-setting-key="TURN_TIMEOUT_S"]')).toBeNull();
  });

  it('a section declared twice non-adjacently renders ONE heading with all its entries', async () => {
    await render('storage');
    // No duplicate heading → no duplicate React key → no pinned/ghost heading
    // when the tab prop switches on the same mounted instance.
    expect(sectionTitles()).toEqual(['CRDT / Backplane / Job Queue', 'Collab limits']);
    const keys = Array.from(container.querySelectorAll('[data-setting-key]'))
      .map(e => e.getAttribute('data-setting-key'));
    expect(keys).toEqual(['YDOC_COMPACT_EVERY', 'COMFY_CONCURRENCY', 'COLLAB_MAX_CLIENTS']);
  });

  it('chip states: | default / | .env / | override per entry source', async () => {
    await render();
    expect(field('AI_API_URL').textContent).toContain('| .env');
    expect(field('AI_API_KEY').textContent).toContain('| override');
    expect(field('EMBEDDING_BATCH_SIZE').textContent).toContain('| default');
  });
});

describe('SettingsSection — save per type', () => {
  it('int: Save PUTs a TYPED number and adopts the returned row', async () => {
    await render();
    await typeInto(field('EMBEDDING_BATCH_SIZE').querySelector('input')!, '32');
    apiPut.mockResolvedValueOnce({ ...ENTRIES[3], source: 'override', value: 32 });
    clickButton(field('EMBEDDING_BATCH_SIZE'), 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/settings/EMBEDDING_BATCH_SIZE', { value: 32 }]]);
    expect(field('EMBEDDING_BATCH_SIZE').textContent).toContain('| override');
    expect(field('EMBEDDING_BATCH_SIZE').textContent).toContain('Saved');
  });

  it('bool: checkbox + Save PUTs {value: false}', async () => {
    await render();
    act(() => { (field('CHAT_QUERY_REWRITE_ENABLED').querySelector('input[type=checkbox]') as HTMLInputElement).click(); });
    apiPut.mockResolvedValueOnce({ ...ENTRIES[5], value: false });
    clickButton(field('CHAT_QUERY_REWRITE_ENABLED'), 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/settings/CHAT_QUERY_REWRITE_ENABLED', { value: false }]]);
  });

  it('secret: password input prefilled with the mask; unchanged → Save disabled (no PUT); changed → PUTs the typed string', async () => {
    await render();
    const input = field('AI_API_KEY').querySelector('input')!;
    expect(input.type).toBe('password');
    expect(input.value).toBe('••••key1');
    const save = () => Array.from(field('AI_API_KEY').querySelectorAll('button')).find(b => b.textContent?.trim() === 'Save')!;
    expect(save().disabled).toBe(true);
    await typeInto(input, 'sk-new-secret');
    apiPut.mockResolvedValueOnce({ ...ENTRIES[1], value: '••••ret1' });
    clickButton(field('AI_API_KEY'), 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/settings/AI_API_KEY', { value: 'sk-new-secret' }]]);
  });

  it('unchanged or invalid drafts keep Save disabled — no PUT fires', async () => {
    await render();
    const f = field('EMBEDDING_BATCH_SIZE');
    const save = () => Array.from(f.querySelectorAll('button')).find(b => b.textContent?.trim() === 'Save')!;
    expect(save().disabled).toBe(true); // unchanged
    await typeInto(f.querySelector('input')!, 'abc');
    expect(save().disabled).toBe(true); // not an integer
    await typeInto(f.querySelector('input')!, '8.5');
    expect(save().disabled).toBe(true);
    expect(apiPut).not.toHaveBeenCalled();
  });

  it('a refused PUT renders the server detail inline (no toast)', async () => {
    const { HttpError } = await vi.importActual<typeof import('../../api/client')>('../../api/client');
    await render();
    await typeInto(field('EMBEDDING_BATCH_SIZE').querySelector('input')!, '0');
    apiPut.mockRejectedValueOnce(new HttpError(422, '{"detail":"EMBEDDING_BATCH_SIZE: minimum is 1"}'));
    clickButton(field('EMBEDDING_BATCH_SIZE'), 'Save');
    await act(async () => {});
    expect(field('EMBEDDING_BATCH_SIZE').textContent).toContain('EMBEDDING_BATCH_SIZE: minimum is 1');
    expect(appState.showToast).not.toHaveBeenCalled();
  });
});

describe('SettingsSection — text', () => {
  const WORKFLOW = '{\n  "6": {\n    "class_type": "CLIPTextEncode",\n    "_meta": {"title": "Positive [lore:prompt]"}\n  }\n}\n';
  const TEXT_ENTRY = { key: 'COMFYUI_WORKFLOW', env: 'COMFYUI_WORKFLOW', tab: 'tools', section: 'ComfyUI image generation', type: 'text', label: 'Workflow (API format)', help: 'Paste Export (API).', effect: 'live', min: null, max: null, choices: null, visible_if: null, source: 'default', value: WORKFLOW };

  it('renders a textarea with the full value; unchanged → Save disabled; an edit PUTs the string', async () => {
    apiGet.mockImplementation(() => Promise.resolve({ settings: [TEXT_ENTRY] }));
    await render('tools');
    const f = field('COMFYUI_WORKFLOW');
    expect(f.querySelector('input')).toBeNull();
    const area = f.querySelector('textarea')!;
    expect(area.value).toBe(WORKFLOW);
    const save = () => Array.from(f.querySelectorAll('button')).find(b => b.textContent?.trim() === 'Save')!;
    expect(save().disabled).toBe(true);
    const edited = WORKFLOW.replace('Positive', 'Prompt');
    await typeInto(area, edited);
    expect(save().disabled).toBe(false);
    apiPut.mockResolvedValueOnce({ ...TEXT_ENTRY, source: 'override', value: edited });
    clickButton(f, 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/settings/COMFYUI_WORKFLOW', { value: edited }]]);
    expect(field('COMFYUI_WORKFLOW').textContent).toContain('| override');
  });
});

describe('SettingsSection — restart keys and reset', () => {
  it('restart key: disabled input showing the env value, no Save/Reset buttons, restart note names the env var', async () => {
    await render();
    const f = field('EMBEDDING_CONCURRENCY');
    const input = f.querySelector('input') as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe('4');
    expect(Array.from(f.querySelectorAll('button'))).toEqual([]);
    expect(f.textContent).toContain('set EMBEDDING_CONCURRENCY in .env');
  });

  it('Reset on an overridden key DELETEs the row and refetches the whole list', async () => {
    await render();
    const refetched = ENTRIES.map(e => e.key === 'CHAT_MODEL' ? { ...e, source: '.env', value: 'fallback-model' } : e);
    apiGet.mockImplementation(() => Promise.resolve({ settings: refetched }));
    clickButton(field('CHAT_MODEL'), 'Reset');
    await act(async () => {});
    expect(apiDelete.mock.calls).toEqual([['/admin/settings/CHAT_MODEL']]);
    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(field('CHAT_MODEL').textContent).toContain('| .env');
    // A default-sourced key offers no Reset.
    expect(Array.from(field('EMBEDDING_BATCH_SIZE').querySelectorAll('button')).map(b => b.textContent)).toEqual(['Save']);
  });
});

describe('SettingsSection — choices', () => {
  it('a choices entry renders a Dropdown (no text input) and Save PUTs the picked value', async () => {
    await render('tools');
    const block = field('WEB_SEARCH_PROVIDER');
    expect(block.querySelector('input')).toBeNull();
    const trigger = block.querySelector('button')!;
    expect(trigger.textContent).toContain('off');
    act(() => trigger.click());
    const options = Array.from(block.querySelectorAll('[role="option"]'));
    expect(options.map(o => o.textContent)).toEqual(['off', 'deepseek', 'brave']);
    act(() => (options[2] as HTMLElement).click());
    apiPut.mockResolvedValueOnce({ ...ENTRIES[10], source: 'override', value: 'brave' });
    clickButton(block, 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/settings/WEB_SEARCH_PROVIDER', { value: 'brave' }]]);
    expect(field('WEB_SEARCH_PROVIDER').textContent).toContain('| override');
  });
});

describe('SettingsSection — visible_if', () => {
  function shownKeys(): (string | null)[] {
    return Array.from(container.querySelectorAll('[data-setting-key]')).map(e => e.getAttribute('data-setting-key'));
  }

  it("shows only the selected provider's key row, and switches it when the provider is saved", async () => {
    apiGet.mockImplementation(() => Promise.resolve({
      settings: ENTRIES.map(e => (e.key === 'WEB_SEARCH_PROVIDER' ? { ...e, value: 'deepseek' } : e)),
    }));
    await render('tools');
    expect(shownKeys()).toEqual(['WEB_SEARCH_PROVIDER', 'DEEPSEEK_API_KEY']);

    const block = field('WEB_SEARCH_PROVIDER');
    act(() => block.querySelector('button')!.click());
    act(() => (Array.from(block.querySelectorAll('[role="option"]'))[2] as HTMLElement).click());
    // A picked-but-unsaved provider does not move the rows yet.
    expect(shownKeys()).toEqual(['WEB_SEARCH_PROVIDER', 'DEEPSEEK_API_KEY']);
    apiPut.mockResolvedValueOnce({ ...ENTRIES[10], source: 'override', value: 'brave' });
    clickButton(block, 'Save');
    await act(async () => {});
    expect(shownKeys()).toEqual(['WEB_SEARCH_PROVIDER', 'BRAVE_API_KEY']);
  });

  it('off shows no key row at all', async () => {
    await render('tools');
    expect(shownKeys()).toEqual(['WEB_SEARCH_PROVIDER']);
  });
});
