/** Misc slice — pending images, models, reset. */
import { apiClient } from '../../api/client';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import type { ChatState, Set, Get } from './types';
import { clearChatCaches } from './reset-registry';

// Floor cap when neither the /models context_windows map nor the session's
// persisted cap is available (gateway omitted context_length, cold models load,
// or a model with no known window). The client's OWN constant — the server-side
// twin is deleted (the turn's cap is the driver's own resolution, plan
// collapse-the-editor-harness-layer step 4); it mirrors the composition's
// defaultContextWindow (128000) rather than any backend constant.
export const CHAT_CONTEXT_WINDOW_FALLBACK = 128000;

/**
 * Resolve the gauge cap for a session's model: the /models `context_windows` map
 * entry for this model when present (LIVE — updates on a model switch, sourced
 * from the gateway's real `context_length`), else CHAT_CONTEXT_WINDOW_FALLBACK.
 */
export function effectiveCap(
  contextWindows: Record<string, number>,
  model: string | undefined,
): number {
  if (model && contextWindows[model]) return contextWindows[model];
  return CHAT_CONTEXT_WINDOW_FALLBACK;
}

type MiscSlice = Pick<
  ChatState,
  | 'addPendingImage'
  | 'removePendingImage'
  | 'clearPendingImages'
  | 'setDraft'
  | 'setListFilter'
  | 'loadModels'
  | 'reset'
>;

export function createMiscSlice(set: Set, get: Get): MiscSlice {
  return {
    addPendingImage(dataUrl: string) {
      set(s => ({ pendingImages: [...s.pendingImages, dataUrl] }));
    },
    removePendingImage(index: number) {
      set(s => ({ pendingImages: s.pendingImages.filter((_, i) => i !== index) }));
    },
    clearPendingImages() {
      set({ pendingImages: [] });
    },
    // Composer draft (shared, session-agnostic — see ChatState.draft). Held in the
    // store so the text survives ChatPanel unmount on right-panel tab switches.
    setDraft(v: string) {
      set({ draft: v });
    },
    setListFilter(v: string | null) {
      set({ listFilter: v });
    },

    async loadModels() {
      if (get().modelsLoaded) return;
      try {
        const data = await apiClient.get('/chat/models');
        // L1: the shared attachment budget now lives
        // in app-store (single source for AI-chat + note-chat). loadModels stays in
        // chat-store (it also loads models/defaultModel/agent flags).
        useAppStore.getState().setMaxAttachmentMb(
          Number(data.max_attachment_mb) > 0 ? Number(data.max_attachment_mb) : 5,
        );
        set({
          models: data.models ?? [],
          // ARCH: vision set projected server-side off the gateway metadata;
          // [] on miss.
          visionModels: data.vision_models ?? [],
          // Per-model real context window (gateway context_length). The gauge's
          // primary cap source; {} on miss → fallback.
          contextWindows: data.context_windows ?? {},
          // Per-model reasoning capability —
          // the effort dropdown's single source. {} on miss (gateway without
          // /capabilities) → no dropdown, no banner.
          reasoning: data.reasoning ?? {},
          defaultModel: data.default_model ?? '',
          // Agent availability flag + reason sourced from the
          // backend's cached probe of the agent line.
          agentAvailable: !!data.agent_available,
          agentUnavailableReason: data.agent_unavailable_reason ?? null,
          modelsLoaded: true,
        });
      } catch {
        useAppStore.getState().showToast(t('failedToLoadModels'), 'error');
      }
    },

    reset() {
      const controller = get().streaming?.controller ?? null;
      if (controller) controller.abort();
      // Clear every self-registered module-level cache (children-map + active-
      // path memo, in-flight session patches/loads, pending context PATCH timers).
      // Each cache owner registers its own clear() at module
      // load (see reset-registry.ts) — adding a future cache needs no edit here.
      clearChatCaches();
      set({
        sessions: [],
        activeSessionId: null,
        documentId: null,
        messages: [],
        listFilter: null,
        messagesLoading: false,
        messagesError: false,
        chatScopeLoading: false,
        selectedSiblings: {},
        streaming: null,
        // The assembler's published timeline — the registry handler above
        // cleared the engine/feed state itself (conversation-feed.ts).
        conversation: [],
        turnRanges: {},
        turnStartSeq: null,
        imageGen: {},
        pendingImages: [],
        // Shared composer draft — logout hygiene: the prior user's draft must
        // never render after a same-tab re-login.
        draft: '',
        ghostAgentAuto: false,
        ghostSystemPromptId: null,
        ghostModel: '',
        ghostReasoningEffort: null,
        ghostRegion: null,
      });
    },
  };
}
