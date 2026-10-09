/** Plan chat-draft-persistence (component round-trip): the composer text must survive
 * ChatInput unmount (right-panel tab switch unmounts ChatPanel) because it lives in
 * chat-store's shared draft, not local useState. Mount → type → unmount → remount. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const appStoreState: Record<string, unknown> = {
  currentProject: null,
  currentDocument: null,
  currentReference: null,
  accessLevel: 'full',
  documents: [],
  references: [],
  maxAttachmentMb: 5,
  deletedRefIds: new Set<string>(),
  showToast: vi.fn(),
  currentUser: null,
};
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign(
    (sel: (s: unknown) => unknown) => sel(appStoreState),
    { getState: () => appStoreState, subscribe: vi.fn() },
  ),
}));
vi.mock('../../store/ui-store', () => {
  const uiState = {
    documents: {},
    getRefOpenMode: () => 'center',
    setRightPanelTab: vi.fn(),
    getLastActiveChatSession: () => null,
    setLastActiveChatSession: vi.fn(),
  };
  return {
    useUIStore: Object.assign((sel: (s: unknown) => unknown) => sel(uiState), {
      getState: () => uiState,
      subscribe: vi.fn(),
    }),
  };
});
vi.mock('../../api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  RequestTooLargeError: class extends Error {},
}));
vi.mock('../../events', () => ({ on: vi.fn(), off: vi.fn(), emit: vi.fn() }));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  t: (k: string) => k,
}));
vi.mock('../../chat/context', () => ({
  setupChatContextBridge: vi.fn(),
  hydrateFromSessions: vi.fn(),
  resolveCompletionContext: () => ({ context_document_ids: [], context_reference_ids: [] }),
  useChatContext: () => ({ documentIds: [], referenceIds: [] }),
  pruneContext: vi.fn(),
  computeContextPrune: () => null,
  GHOST_SESSION_ID: '__ghost__',
}));
vi.mock('../../chat/use-ghost-context', () => ({
  useGhostChatContext: () => ({ documentIds: [], referenceIds: [] }),
}));
vi.mock('../../hooks/useSimpleVoiceRecording', () => ({
  useSimpleVoiceRecording: () => ({ recording: false, transcribing: false, toggleRecording: vi.fn(), cancelRecording }),
}));
vi.mock('./chat-input-hooks', () => ({
  useSystemPrompts: () => ({ systemPrompts: [], activeSystemPromptTitle: null }),
  useAttachmentBudget: () => ({
    projectedAttachmentBytes: 0,
    contextOverLimit: false,
    attachmentsOverLimit: false,
    historyBytes: 0,
  }),
}));
vi.mock('../ui', () => ({
  Button: () => null,
  // Prop capturer: the reasoning-effort dropdown test asserts the OPTIONS the
  // composer passes, not DOM chrome — the Dropdown itself is exercised by its
  // own consumers' tests.
  Dropdown: (props: { title?: string; value?: string; options?: { value: string }[] }) => {
    dropdownProps.push(props);
    return null;
  },
  FieldCheckbox: () => null,
}));
vi.mock('./SelectionPill', () => ({ SelectionPill: () => null }));
vi.mock('./TokenUsageGauge', () => ({ TokenUsageGauge: () => null }));
vi.mock('./ContentPickerPopup', () => ({ ContentPickerPopup: () => null }));
// The composer is reduced to the contract under test: a controlled textarea bound
// to value/onChange/textareaRef, PLUS the leftControls slot mounted verbatim —
// the Dropdown prop-capture tests below read what the composer's control row
// would render; the real composer chrome is exercise for its own consumers.
vi.mock('./ChatComposer', () => ({
  ChatComposer: (props: {
    value: string;
    onChange: (e: { target: { value: string } }) => void;
    textareaRef: { current: HTMLTextAreaElement | null };
    leftControls?: unknown;
  }) => {
    composerProps.current = props as unknown as Record<string, unknown>;
    return createElement('div', null,
      props.leftControls as never,
      createElement('textarea', {
        value: props.value,
        onChange: props.onChange as never,
        ref: props.textareaRef as never,
      }),
    );
  },
}));

import { ChatInput } from './ChatInput';
import { useChatStore } from '../../store/chat-store';

const cancelRecording = vi.fn();
const composerProps: { current: Record<string, unknown> | null } = { current: null };
const dropdownProps: { title?: string; value?: string; options?: { value: string }[] }[] = [];
// The reasoning map the dropdown reads: m1 advertises low|high; a row pinned
// to something else (here 'max') is the stale-foreign case under test.
const REASONING_M1 = { supported: true, effort_levels: ['low', 'high'] };

function render(node: ReturnType<typeof createElement>): { container: HTMLElement; root: Root } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => { root.render(node); });
  return { container, root };
}

function unmount(rig: { container: HTMLElement; root: Root }) {
  act(() => { rig.root.unmount(); });
  rig.container.remove();
}

function textareaOf(container: HTMLElement): HTMLTextAreaElement {
  const ta = container.querySelector('textarea');
  if (!ta) throw new Error('no textarea rendered');
  return ta;
}

function typeInto(ta: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(ta, value);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  useChatStore.setState({ draft: '' });
});

describe('ChatInput draft round-trip (unmount on tab switch)', () => {
  it('text typed before unmount is restored after remount', () => {
    const first = render(createElement(ChatInput));
    typeInto(textareaOf(first.container), 'hello draft');

    // Tab switch: ChatPanel unmounts ChatInput — the draft must survive in the store.
    unmount(first);
    expect(useChatStore.getState().draft).toBe('hello draft');

    // Back to the chat tab: a fresh mount initializes from the store draft.
    const second = render(createElement(ChatInput));
    expect(textareaOf(second.container).value).toBe('hello draft');
    unmount(second);
  });
});

describe('ChatInput Escape wiring', () => {
  it('hands the composer the turn stop and the voice discard (it owns the Escape order)', () => {
    const rig = render(createElement(ChatInput));
    expect(composerProps.current?.onStop).toBe(useChatStore.getState().stopGeneration);
    expect(composerProps.current?.onCancelRecording).toBe(cancelRecording);
    unmount(rig);
  });
});

describe('ChatInput reasoning-effort dropdown options (plan reasoning-effort-selector)', () => {
  function effortDropdown() {
    const hit = dropdownProps.find(p => p.title === 'chatReasoningTitle');
    if (!hit) throw new Error('no reasoning-effort dropdown rendered');
    return hit;
  }

  // Same shape as the store tests' fixture (chat-store.test.ts makeSession):
  // a minimal COMPLETE ChatSession — setState types against ChatState, so a
  // partial object fails tsc here even though vitest would run it.
  function makeSession(overrides: Record<string, unknown> = {}) {
    return {
      session_id: 's1',
      project_id: 'p1',
      document_id: 'd1',
      reference_id: null as string | null,
      user_id: 'u1',
      title: '',
      model: 'm1',
      reasoning_effort: null as string | null,
      system_prompt_id: null as string | null,
      agent_auto: false,
      is_note: false,
      context_ids: [] as string[],
      created_at: '',
      updated_at: '',
      ...overrides,
    };
  }

  /** Base store state: m1 advertises low|high; the active session pins 'max' —
   * a level the model does NOT advertise (raw-API model switch without the
   * reset, or cross-client staleness). */
  function baseState(overrides: Record<string, unknown> = {}) {
    return {
      sessions: [makeSession({ reasoning_effort: 'max' })],
      activeSessionId: 's1',
      streaming: null,
      models: ['m1'],
      defaultModel: 'm1',
      modelsLoaded: true,
      reasoning: { m1: REASONING_M1 },
      ghostModel: '',
      ghostReasoningEffort: null,
      ...overrides,
    };
  }

  it('a pinned effort outside the advertised list renders as its OWN option, selected — never clamps to Default', () => {
    dropdownProps.length = 0;
    useChatStore.setState(baseState());
    const rig = render(createElement(ChatInput));

    // The trigger must display the TRUE row value: clamping to the first
    // option (Default) would show a value the wire would not send — silent
    // degradation. The foreign option rides AFTER the advertised ones.
    const dd = effortDropdown();
    expect(dd.value).toBe('max');
    expect(dd.options!.map(o => o.value)).toEqual(['', 'low', 'high', 'max']);

    unmount(rig);
  });

  it('an advertised pinned effort adds no duplicate option', () => {
    dropdownProps.length = 0;
    useChatStore.setState(baseState({
      sessions: [makeSession({ reasoning_effort: 'low' })],
    }));
    const rig = render(createElement(ChatInput));

    const dd = effortDropdown();
    expect(dd.options!.map(o => o.value)).toEqual(['', 'low', 'high']);

    unmount(rig);
  });

  it('the ghost path shows the same truth: a ghost effort under a model that does not advertise it stays visible', () => {
    dropdownProps.length = 0;
    useChatStore.setState(baseState({
      sessions: [],
      activeSessionId: null,
      ghostModel: 'm1',
      ghostReasoningEffort: 'max',
    }));
    const rig = render(createElement(ChatInput));

    const dd = effortDropdown();
    expect(dd.value).toBe('max');
    expect(dd.options!.map(o => o.value)).toEqual(['', 'low', 'high', 'max']);

    unmount(rig);
  });
});
