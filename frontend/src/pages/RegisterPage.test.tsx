/** RegisterPage — the error line tells the truth per HTTP status; the repeat
 * password is a client-side guard; the page rides the shared landing frame.
 *
 * Pins: a 422 renders the server's pydantic detail (e.g. the password-length
 * message), NOT the invalid-link line; 409 → registerEmailTaken; 404 →
 * registerInvalid; a rejected submit fetch → registerNetworkError (the submit
 * path catches — no unhandled rejection with a silent form). Mismatched
 * passwords render passwordsDoNotMatch and fire no POST. The landing subtext
 * and footer render — proof the page is LandingLayout, not a clone.
 *
 * Harness: manual createRoot + act (mirrors AdminPage.test.tsx); fetch is
 * stubbed per scenario (RegisterPage uses raw fetch, not apiClient).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let RegisterPage: typeof import('./RegisterPage').RegisterPage;
let container: HTMLDivElement;
let root: Root;
let navigate: ReturnType<typeof vi.fn>;
let setCurrentUser: ReturnType<typeof vi.fn>;
let fetchMock: ReturnType<typeof vi.fn>;

const I18N: Record<string, string> = {
  landingTagline1: 'Document-first.',
  landingTagline2: 'Agent-ready.',
  landingSubtext: 'A knowledge workspace built around the document.',
  landingCopyright: '\u00A9 2026 Lore',
  registerTitle: 'Create account',
  registerInvitedBy: 'Invited by {name}',
  registerInviterUnknown: 'a Lore member',
  registerInvalid: 'This invite link is invalid, already used, or expired.',
  registerEmailTaken: 'This email is already registered.',
  registerNetworkError: 'Network error — try again.',
  registerAction: 'Create account',
  registerCreating: 'Creating…',
  name: 'Name',
  email: 'Email',
  password: 'Password',
  passwordRepeat: 'Repeat password',
  passwordsDoNotMatch: 'Passwords do not match',
};
const tFn = (k: string) => I18N[k] ?? k;

/** Minimal Response stand-in: RegisterPage reads ok / status / json() only. */
const res = (status: number, body: unknown) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

/** GET answers the inviter; POST answers 200 unless a test overrides. */
function defaultFetch(_input: unknown, init?: RequestInit) {
  if (init?.method === 'POST') return Promise.resolve(res(200, { user_id: 'u-new' }));
  return Promise.resolve(res(200, { inviter_name: 'Inviter' }));
}

beforeEach(async () => {
  vi.resetModules();
  navigate = vi.fn();
  setCurrentUser = vi.fn();
  fetchMock = vi.fn(defaultFetch);
  vi.stubGlobal('fetch', fetchMock);
  vi.doMock('react-router-dom', () => ({
    useNavigate: () => navigate,
    useParams: () => ({ token: 'tok123' }),
  }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: (sel: (s: Record<string, unknown>) => unknown) => sel({ setCurrentUser }),
  }));
  vi.doMock('../i18n', () => ({
    t: tFn,
    useTranslation: () => ({ t: tFn }),
  }));

  ({ RegisterPage } = await import('./RegisterPage'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../i18n');
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => { root.render(createElement(RegisterPage)); });
  // flush the mount-effect GET microtasks
  await act(async () => {});
}

/** React-controlled input: native value setter + input event. */
async function typeInto(input: HTMLInputElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

/** Fill name + email + password + repeat (matching) and click submit. */
async function fillAndSubmit(repeat: string = 'secret') {
  const inputs = Array.from(container.querySelectorAll('input'));
  expect(inputs.length, 'name/email/password/passwordRepeat inputs').toBe(4);
  await typeInto(inputs[0], 'newbie');
  await typeInto(inputs[1], 'newbie@x.test');
  await typeInto(inputs[2], 'secret');
  await typeInto(inputs[3], repeat);
  const btn = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent === I18N.registerAction);
  expect(btn, 'submit button').toBeDefined();
  await act(async () => { btn!.click(); });
}

/** Every fetch call that issued a POST (the register submit). */
const postCalls = () => fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === 'POST');

function postResponds(status: number, body: unknown) {
  fetchMock.mockImplementation((_i: unknown, init?: RequestInit) =>
    init?.method === 'POST'
      ? Promise.resolve(res(status, body))
      : Promise.resolve(res(200, { inviter_name: 'Inviter' })));
}

describe('RegisterPage submit errors', () => {
  it('the page is the landing frame: subtext and footer render', async () => {
    await render();
    expect(container.textContent).toContain(I18N.landingSubtext);
    expect(container.textContent).toContain(I18N.landingCopyright);
    // The dead "Guide" placeholder anchor is gone — the footer is copyright only.
    expect(container.querySelector('a[href="#"]')).toBeNull();
  });

  it('mismatched repeat renders passwordsDoNotMatch and fires no POST', async () => {
    await render();
    await fillAndSubmit('different');
    expect(container.textContent).toContain(I18N.passwordsDoNotMatch);
    expect(postCalls()).toHaveLength(0);
  });

  it('422 renders the pydantic detail, not the invalid-link line', async () => {
    postResponds(422, {
      detail: [
        { loc: ['body', 'password'], msg: 'String should have at least 6 characters', type: 'string_too_short' },
      ],
    });
    await render();
    await fillAndSubmit();
    expect(container.textContent).toContain('String should have at least 6 characters');
    expect(container.textContent).not.toContain(I18N.registerInvalid);
  });

  it('409 renders registerEmailTaken', async () => {
    postResponds(409, { detail: 'Email already in use' });
    await render();
    await fillAndSubmit();
    expect(container.textContent).toContain(I18N.registerEmailTaken);
    expect(container.textContent).not.toContain(I18N.registerInvalid);
  });

  it('404 renders registerInvalid', async () => {
    postResponds(404, { detail: 'Invite not found or no longer valid' });
    await render();
    await fillAndSubmit();
    expect(container.textContent).toContain(I18N.registerInvalid);
  });

  it('a rejected submit fetch renders registerNetworkError', async () => {
    fetchMock.mockImplementation((_i: unknown, init?: RequestInit) =>
      init?.method === 'POST'
        ? Promise.reject(new Error('net down'))
        : Promise.resolve(res(200, { inviter_name: 'Inviter' })));
    await render();
    await fillAndSubmit();
    expect(container.textContent).toContain(I18N.registerNetworkError);
    expect(container.textContent).not.toContain(I18N.registerInvalid);
  });

  it('success logs the new user in and navigates to /', async () => {
    await render();
    await fillAndSubmit();
    expect(setCurrentUser).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith('/');
  });
});
