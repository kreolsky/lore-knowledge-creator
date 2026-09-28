/** SkillsSection — the instance-skills admin surface.
 *
 * Pins the contract:
 * - one card per DISTINCT union entry, no source chip; the enabled checkbox
 *   acts IMMEDIATELY: shipped row → PUT {enabled:false} (tombstone),
 *   contentless tombstone → DELETE (the shipped file reappears), instance
 *   row with content → PUT;
 * - the chevron expands the card into the editor over the CURRENT body:
 *   instance content where it exists, else the shipped body as the draft;
 *   there is no separate "original" view and no Override/Edit buttons;
 * - "modified" mark + armed Reset exist ONLY on an overridden row (content
 *   over a shipped file); armed Delete exists ONLY on a skill with no shipped
 *   fallback; both fire DELETE and refetch;
 * - every row control is the 22px size, so an icon's presence never changes
 *   the row height;
 * - Save PUTs to the name extracted from the frontmatter (new skills) or the
 *   row's name; the server's 422 detail renders inline.
 *
 * Harness: manual createRoot + act (mirrors AdminPage.test.tsx).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let SkillsSection: typeof import('./SkillsSection').SkillsSection;
let container: HTMLDivElement;
let root: Root;
let appState: Record<string, unknown>;
let apiGet: ReturnType<typeof vi.fn>;
let apiPut: ReturnType<typeof vi.fn>;
let apiDelete: ReturnType<typeof vi.fn>;

const SHIPPED_BODY = '---\nname: web_search\ndescription: "Search the web"\ntools: []\n---\n\nSearches the web.';

const SKILLS = [
  { name: 'web_search', source: 'shipped', shipped: true, enabled: true, content: null, shipped_body: SHIPPED_BODY, updated_by: null, updated_at: null },
  // A pure tombstone: the instance row shadows the shipped file, contentless.
  { name: 'cir_run', source: 'instance', shipped: true, enabled: false, content: null, shipped_body: '---\nname: cir_run\n---\n\nBenchmark.', updated_by: 'u1', updated_at: '2026-09-19T10:00:00Z' },
  { name: 'hello', source: 'instance', shipped: false, enabled: true, content: '---\nname: hello\ndescription: "hi"\ntools: []\n---\n\nSays hi.', shipped_body: null, updated_by: 'u1', updated_at: '2026-09-19T11:00:00Z' },
];

const I18N: Record<string, string> = {
  skills: 'Skills',
  skillEnabled: 'Enabled',
  skillModified: 'modified',
  skillReset: 'Reset to shipped',
  skillNew: 'New skill',
  edit: 'Edit',
  delete: 'Delete',
  save: 'Save',
  cancel: 'Cancel',
  saved: 'Saved',
  skillSaveFailed: 'Failed to save the skill',
  skillDeleteFailed: 'Failed to delete the skill',
  skillNoFrontmatterName: 'The body has no frontmatter name (--- name: …)',
  adminListLoadFailed: 'Failed to load the list',
};
const tFn = (k: string) => I18N[k] ?? k;

beforeEach(async () => {
  vi.resetModules();
  appState = { showToast: vi.fn() };
  apiGet = vi.fn(() => Promise.resolve({ skills: SKILLS }));
  apiPut = vi.fn(body => Promise.resolve({ ...SKILLS[0], ...body, source: 'instance' }));
  apiDelete = vi.fn(() => Promise.resolve({ name: 'x', reset: true }));

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

  ({ SkillsSection } = await import('./SkillsSection'));

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
  await act(async () => { root.render(createElement(SkillsSection)); });
  await act(async () => {});
}

function row(name: string): HTMLElement {
  const el = container.querySelector(`[data-skill-name="${name}"]`);
  expect(el, `skill row "${name}"`).toBeDefined();
  return el as HTMLElement;
}

function checkbox(scope: ParentNode): HTMLInputElement {
  const el = scope.querySelector('input[type=checkbox]');
  expect(el, 'enabled checkbox').toBeDefined();
  return el as HTMLInputElement;
}

const OVERRIDDEN = { name: 'web_fetch', source: 'instance', shipped: true, enabled: true, content: '---\nname: web_fetch\n---\n\nMine.', shipped_body: '---\nname: web_fetch\n---\n\nTheirs.', updated_by: 'u1', updated_at: '2026-09-19T12:00:00Z' };

/** The chevron is the first button of the card's header row. */
function expand(scope: ParentNode) {
  const btn = scope.querySelector('button') as HTMLButtonElement;
  expect(btn, 'chevron').toBeDefined();
  act(() => btn.click());
}

function iconButton(scope: ParentNode, title: string): HTMLButtonElement | null {
  return scope.querySelector(`button[title="${title}"]`) as HTMLButtonElement | null;
}

function clickButton(scope: ParentNode, text: string) {
  const btn = Array.from(scope.querySelectorAll('button'))
    .find(b => b.textContent?.trim() === text);
  expect(btn, `button "${text}"`).toBeDefined();
  act(() => btn!.click());
}

async function typeIntoTextarea(el: HTMLTextAreaElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

describe('SkillsSection — list', () => {
  it('renders one row per union entry with honest enabled state and no source chip', async () => {
    await render();
    for (const name of ['web_search', 'cir_run', 'hello']) {
      expect(row(name).textContent).not.toContain('| shipped');
      expect(row(name).textContent).not.toContain('| instance');
    }
    expect(checkbox(row('web_search')).checked).toBe(true);
    expect(checkbox(row('cir_run')).checked).toBe(false);
    expect(checkbox(row('hello')).checked).toBe(true);
  });

  it('"modified" + Reset only on an overridden row; Delete only on a skill with no shipped fallback', async () => {
    apiGet.mockResolvedValueOnce({ skills: [...SKILLS, OVERRIDDEN] });
    await render();
    for (const name of ['web_search', 'cir_run', 'hello']) {
      expect(row(name).textContent, name).not.toContain('modified');
      expect(iconButton(row(name), 'Reset to shipped'), name).toBeNull();
    }
    expect(row('web_fetch').textContent).toContain('modified');
    expect(iconButton(row('web_fetch'), 'Reset to shipped')).not.toBeNull();
    expect(iconButton(row('web_fetch'), 'Delete')).toBeNull();
    expect(iconButton(row('hello'), 'Delete')).not.toBeNull();
    expect(iconButton(row('web_search'), 'Delete')).toBeNull();
    expect(iconButton(row('cir_run'), 'Delete')).toBeNull();
    // No leftovers of the button-per-action layout.
    expect(container.textContent).not.toContain('Override');
    expect(container.textContent).not.toContain('original');
  });

  it('the icon actions are the same 22px size as the chevron (row height is constant)', async () => {
    await render();
    const del = iconButton(row('hello'), 'Delete')!;
    expect(del.classList.contains('h-[22px]')).toBe(true);
  });

  it('load failure toasts and shows the error line above the list', async () => {
    apiGet.mockRejectedValueOnce(new Error('boom'));
    await render();
    expect(appState.showToast).toHaveBeenCalledWith('Failed to load the list', 'error');
    expect(container.textContent).toContain('Failed to load the list');
  });
});

describe('SkillsSection — enabled toggle (immediate)', () => {
  it('shipped row: unchecking creates a tombstone (PUT {enabled:false})', async () => {
    await render();
    act(() => { checkbox(row('web_search')).click(); });
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/skills/web_search', { enabled: false }]]);
  });

  it('contentless tombstone: checking DELETEs the row so the shipped file reappears', async () => {
    await render();
    act(() => { checkbox(row('cir_run')).click(); });
    await act(async () => {});
    expect(apiDelete.mock.calls).toEqual([['/admin/skills/cir_run']]);
    expect(apiGet).toHaveBeenCalledTimes(2);
  });

  it('instance row with content: unchecking PUTs {enabled:false}', async () => {
    await render();
    act(() => { checkbox(row('hello')).click(); });
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/skills/hello', { enabled: false }]]);
  });
});

describe('SkillsSection — editor', () => {
  it('expanding an instance row prefills its content; Save PUTs the new body and collapses', async () => {
    await render();
    expand(row('hello'));
    const ta = row('hello').querySelector('textarea')!;
    expect(ta.value).toBe(SKILLS[2].content);
    await typeIntoTextarea(ta, '---\nname: hello\ndescription: "hi"\n---\n\nChanged.');
    clickButton(row('hello'), 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/skills/hello', { content: '---\nname: hello\ndescription: "hi"\n---\n\nChanged.' }]]);
    expect(row('hello').querySelector('textarea')).toBeNull();
  });

  it('expanding a shipped row prefills the SHIPPED body as the draft; Save PUTs it as content', async () => {
    await render();
    expand(row('web_search'));
    const ta = row('web_search').querySelector('textarea')!;
    expect(ta.value).toBe(SHIPPED_BODY);
    clickButton(row('web_search'), 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/skills/web_search', { content: SHIPPED_BODY }]]);
  });

  it('a tombstone row expands over the shipped body; an overridden row over ITS content', async () => {
    apiGet.mockResolvedValueOnce({ skills: [...SKILLS, OVERRIDDEN] });
    await render();
    expand(row('cir_run'));
    expect(row('cir_run').querySelector('textarea')!.value).toBe(SKILLS[1].shipped_body);
    expand(row('web_fetch'));
    expect(row('web_fetch').querySelector('textarea')!.value).toBe(OVERRIDDEN.content);
  });

  it('Cancel collapses the card without a PUT', async () => {
    await render();
    expand(row('hello'));
    clickButton(row('hello'), 'Cancel');
    expect(row('hello').querySelector('textarea')).toBeNull();
    expect(apiPut).not.toHaveBeenCalled();
  });

  it('new skill: Save extracts the frontmatter name and PUTs to it', async () => {
    await render();
    clickButton(container, 'New skill');
    const ta = container.querySelector('textarea')!;
    const body = '---\nname: fresh_skill\ndescription: "new"\n---\n\nBody.';
    await typeIntoTextarea(ta, body);
    clickButton(container, 'Save');
    await act(async () => {});
    expect(apiPut.mock.calls).toEqual([['/admin/skills/fresh_skill', { content: body }]]);
  });

  it('no frontmatter name → inline error, no PUT', async () => {
    await render();
    clickButton(container, 'New skill');
    await typeIntoTextarea(container.querySelector('textarea')!, 'Just prose, no frontmatter.');
    clickButton(container, 'Save');
    await act(async () => {});
    expect(apiPut).not.toHaveBeenCalled();
    expect(container.textContent).toContain('no frontmatter name');
  });

  it('a 422 refusal renders the server detail inline', async () => {
    const { HttpError } = await vi.importActual<typeof import('../../api/client')>('../../api/client');
    await render();
    expand(row('hello'));
    await typeIntoTextarea(row('hello').querySelector('textarea')!, '---\nname: other\n---\n\nBody.');
    apiPut.mockRejectedValueOnce(new HttpError(422, '{"detail":"the body\'s frontmatter name is \'other\', the row\'s name is \'hello\'"}'));
    clickButton(row('hello'), 'Save');
    await act(async () => {});
    expect(row('hello').textContent).toContain("the row's name is 'hello'");
    // The draft survives for fixing.
    expect(row('hello').querySelector('textarea')!.value).toContain('name: other');
  });
});

describe('SkillsSection — reset / delete (armed)', () => {
  it('delete: two clicks DELETE the instance row and refetch', async () => {
    await render();
    const del = iconButton(row('hello'), 'Delete')!;
    act(() => { del.click(); }); // arms
    expect(apiDelete).not.toHaveBeenCalled();
    await act(async () => { del.click(); }); // fires
    expect(apiDelete.mock.calls).toEqual([['/admin/skills/hello']]);
    expect(apiGet).toHaveBeenCalledTimes(2);
  });

  it('reset: two clicks DELETE the overridden row so the shipped body serves again', async () => {
    apiGet.mockResolvedValueOnce({ skills: [...SKILLS, OVERRIDDEN] });
    await render();
    const reset = iconButton(row('web_fetch'), 'Reset to shipped')!;
    act(() => { reset.click(); }); // arms
    expect(apiDelete).not.toHaveBeenCalled();
    await act(async () => { reset.click(); }); // fires
    expect(apiDelete.mock.calls).toEqual([['/admin/skills/web_fetch']]);
    expect(apiGet).toHaveBeenCalledTimes(2);
  });
});
