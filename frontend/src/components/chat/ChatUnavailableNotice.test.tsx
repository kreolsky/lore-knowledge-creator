/** Chat with no models: the admin is sent to Settings → Models, anyone else is told to ask an admin; the gate fires only on a LOADED empty catalog. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const navigate = vi.fn();
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: '/docs/d1' }),
}));

const appState: Record<string, unknown> = {};
vi.mock('../../store/app-store', () => ({
  useAppStore: (sel: (s: unknown) => unknown) => sel(appState),
}));

const chatState: { modelsLoaded: boolean; models: string[] } = { modelsLoaded: false, models: [] };
vi.mock('../../store/chat-store', () => ({
  useChatStore: (sel: (s: unknown) => unknown) => sel(chatState),
}));

const setAdminSectionTab = vi.fn();
vi.mock('../../store/ui-store', () => ({
  useUIStore: (sel: (s: unknown) => unknown) => sel({ setAdminSectionTab }),
}));

import { ChatUnavailableNotice, useChatUnavailable } from './ChatUnavailableNotice';

let container: HTMLDivElement;
let root: Root;

function render(el: ReturnType<typeof createElement>) {
  act(() => { root.render(el); });
}

beforeEach(() => {
  navigate.mockReset();
  setAdminSectionTab.mockReset();
  Object.assign(appState, {
    currentUser: { is_admin: false },
    currentDocument: { title: 'Doc' },
    currentProject: null,
    setSectionOrigin: vi.fn(),
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

describe('ChatUnavailableNotice', () => {
  it('admin: the link opens Settings → Models and records the way back', () => {
    appState.currentUser = { is_admin: true };
    render(createElement(ChatUnavailableNotice));
    const link = container.querySelector('button');
    expect(link).not.toBeNull();
    act(() => { link!.click(); });
    expect(setAdminSectionTab).toHaveBeenCalledWith('settings:models');
    expect(navigate).toHaveBeenCalledWith('/admin');
    expect(appState.setSectionOrigin).toHaveBeenCalledWith({ path: '/docs/d1', label: 'Doc', fromProject: true });
  });

  it('non-admin: no link, only the ask-an-admin text', () => {
    render(createElement(ChatUnavailableNotice));
    expect(container.querySelector('button')).toBeNull();
    expect(container.textContent).toContain('Ask an administrator');
  });
});

describe('useChatUnavailable', () => {
  function Probe() {
    return createElement('span', null, String(useChatUnavailable()));
  }
  const cases: [string, { modelsLoaded: boolean; models: string[] }, string][] = [
    ['not loaded yet (or load failed) — composer stays', { modelsLoaded: false, models: [] }, 'false'],
    ['loaded, models served', { modelsLoaded: true, models: ['m1'] }, 'false'],
    ['loaded, zero models for this user', { modelsLoaded: true, models: [] }, 'true'],
  ];
  it.each(cases)('%s', (_name, state, expected) => {
    Object.assign(chatState, state);
    render(createElement(Probe));
    expect(container.textContent).toBe(expected);
  });
});
