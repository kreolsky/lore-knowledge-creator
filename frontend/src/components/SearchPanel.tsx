/** Search panel — full-text and semantic (vector) search across documents and references,
 * plus the person's own chats ("Chats" mode: a client-side "By title" group over the loaded
 * chat list above the semantic hits).
 * Store slices: currentProject, currentDocument, searchQuery, searchResults, setSearchCache,
 * searchMode, setRightPanelTab; chat-store sessions, loadSessions, setActiveSession,
 * openBranch, setPendingScroll.
 * Events emitted: navigate-to-reference, navigate-to-document, open-right-panel. */

import { useState, useEffect, useRef, useCallback } from 'react';
import { Search, Sparkles, MessagesSquare } from 'lucide-react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { useChatStore } from '../store/chat-store';
import { isChatScopeLoaded, markChatScopeLoaded } from '../chat/scope-tracker';
import { apiClient, ForbiddenError } from '../api/client';
import { emit } from '../events';
import { formatDate } from '../utils/format';
import { ChatSearchHit, ChatSession, SearchMode, SearchResult, SemanticSearchHit } from '../types';
import { FieldInput, FieldCheckbox, Button, ListPill, PillList } from './ui';
import { useTranslation } from '../i18n';
import { filterSessionsByTitle } from './chat/useSortedSessions';

// The chat-search route's 403 while the administrator switched the chat-recall
// skill off — told apart from "No access to this project" by its detail.
const CHAT_RECALL_DISABLED_DETAIL = 'chat-recall is disabled by the administrator';
const isChatRecallDisabled = (err: unknown) =>
  err instanceof ForbiddenError && err.detail === CHAT_RECALL_DISABLED_DETAIL;

const MIN_CHARS: Record<SearchMode, number> = { fulltext: 3, semantic: 2, chats: 2 };
const PLACEHOLDER_KEY = {
  fulltext: 'searchPlaceholder', semantic: 'searchPlaceholderSemantic', chats: 'searchPlaceholderChats',
} as const satisfies Record<SearchMode, string>;

export function SearchPanel() {
  const projectId = useAppStore(s => s.currentProject?.project_id);
  const currentDocument = useAppStore(s => s.currentDocument);
  const query = useUIStore(s => s.searchQuery);
  const results = useUIStore(s => s.searchResults);
  const setSearchCache = useUIStore(s => s.setSearchCache);
  const mode = useUIStore(s => s.searchMode);
  const setSearchMode = useUIStore(s => s.setSearchMode);
  const chatSessions = useChatStore(s => s.sessions);

  const { t } = useTranslation();
  const [semanticHits, setSemanticHits] = useState<SemanticSearchHit[]>([]);
  const [chatHits, setChatHits] = useState<ChatSearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  // Read only beside `error` in chats mode: each chats failure sets it afresh.
  const [chatsDisabled, setChatsDisabled] = useState(false);
  // Semantic scope controls — component-local by design: a narrow persisted
  // across sessions is a silent filter. The row stays visible while active, so
  // the narrow is always one glance away.
  const [scopeThisDoc, setScopeThisDoc] = useState(false);
  const [memoryOnly, setMemoryOnly] = useState(false);
  // Fulltext exact toggle — component-local, not persisted (same rationale:
  // a survived "exact only" is a silent recall cut on the next session).
  const [exactMatch, setExactMatch] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const getMinChars = (m: SearchMode) => MIN_CHARS[m];
  const minChars = getMinChars(mode);

  const doSearch = useCallback((value: string) => {
    const currentMode = useUIStore.getState().searchMode;
    const mc = getMinChars(currentMode);
    if (!projectId || value.length < mc) return;

    if (currentMode === 'semantic') {
      const params = new URLSearchParams({ q: value });
      // The narrow binds to the document open at request time. Memory-only is
      // the two existing route switches — no third param.
      const underId = useAppStore.getState().currentDocument?.document_id;
      if (scopeThisDoc && underId) params.set('under_document_id', underId);
      if (memoryOnly) {
        params.set('include_docs', 'false');
        params.set('include_refs', 'false');
      }
      apiClient
        .get(`/projects/${projectId}/semantic-search?${params}`)
        .then((data: { hits: SemanticSearchHit[] }) => setSemanticHits(data.hits))
        .catch(() => { setError(true); })
        .finally(() => setLoading(false));
    } else if (currentMode === 'chats') {
      const params = new URLSearchParams({ q: value });
      // The anchor is the document open at request time. Unchecked: the whole
      // project, chats under the anchor first (marked). Checked: only chats
      // attached to the anchor or its subtree.
      const anchorId = useAppStore.getState().currentDocument?.document_id;
      params.set('scope', scopeThisDoc && anchorId ? 'siblings' : 'project');
      if (anchorId) params.set('anchor_document_id', anchorId);
      apiClient
        .get(`/projects/${projectId}/chat-search?${params}`)
        .then((data: { hits: ChatSearchHit[] }) => setChatHits(data.hits))
        .catch((err: unknown) => { setChatsDisabled(isChatRecallDisabled(err)); setError(true); })
        .finally(() => setLoading(false));
    } else {
      const params = new URLSearchParams({ q: value });
      // The narrow binds to the document open at request time — same as semantic.
      const underId = useAppStore.getState().currentDocument?.document_id;
      if (scopeThisDoc && underId) params.set('under_document_id', underId);
      if (exactMatch) params.set('exact', 'true');
      apiClient
        .get(`/projects/${projectId}/search?${params}`)
        .then((data: { results: SearchResult[] }) => setSearchCache(value, data.results))
        .catch(() => { setError(true); })
        .finally(() => setLoading(false));
    }
  }, [projectId, setSearchCache, scopeThisDoc, memoryOnly, exactMatch]);

  // Re-anchor: the toggle means "the document I have open" — navigating to a
  // different document opts the new one out instead of silently narrowing by it.
  useEffect(() => { setScopeThisDoc(false); }, [currentDocument?.document_id]);

  // Latest doSearch for deferred (debounced) calls — the timeout must see the
  // committed toggle state, not the closure of the render that scheduled it.
  const doSearchRef = useRef(doSearch);
  useEffect(() => { doSearchRef.current = doSearch; }, [doSearch]);

  // Toggling a scope control re-runs the debounced search in the CURRENT mode
  // (3-char gate + result clear for fulltext; semantic behavior unchanged).
  const rerunScoped = () => {
    const currentMode = useUIStore.getState().searchMode;
    const q = useUIStore.getState().searchQuery;
    setSemanticHits([]);
    setChatHits([]);
    if (currentMode === 'fulltext') useUIStore.setState({ searchResults: [] });
    if (!projectId || q.length < getMinChars(currentMode)) return;
    setLoading(true);
    setError(false);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => doSearchRef.current(q), 300);
  };

  const toggleScopeThisDoc = (on: boolean) => {
    setScopeThisDoc(on);
    rerunScoped();
  };
  const toggleMemoryOnly = (on: boolean) => {
    setMemoryOnly(on);
    rerunScoped();
  };
  const toggleExactMatch = (on: boolean) => {
    setExactMatch(on);
    rerunScoped();
  };

  useEffect(() => {
    const mc = getMinChars(mode);
    if (projectId && query.length >= mc) {
      setLoading(true);
      doSearch(query);
    }
  }, []);

  const handleQueryChange = (value: string) => {
    useUIStore.setState({ searchQuery: value });
    setSemanticHits([]);
    setChatHits([]);

    if (timerRef.current) clearTimeout(timerRef.current);
    if (!projectId || value.length < minChars) {
      setLoading(false);
      setError(false);
      useUIStore.setState({ searchResults: [] });
      return;
    }

    setLoading(true);
    setError(false);
    timerRef.current = setTimeout(() => doSearch(value), 300);
  };

  const switchMode = (next: SearchMode) => {
    setSearchMode(next);
    setSemanticHits([]);
    setChatHits([]);
    useUIStore.setState({ searchResults: [] });
    setError(false);
    setLoading(false);
    scrollRef.current?.scrollTo({ top: 0 });
    if (timerRef.current) clearTimeout(timerRef.current);
    const mc = getMinChars(next);
    if (query.length >= mc) {
      setLoading(true);
      timerRef.current = setTimeout(() => doSearch(query), 200);
    }
  };

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  // WHY load first: a chat panel that never mounted in this tab would, on
  // the reveal below, run its own first load — and its resolver restores the
  // last-active chat over the one opened here. Marking the scope loaded
  // makes that mount a no-op (the same move openChatWithReference makes).
  // The same load feeds the "By title" group, whose source is that list.
  const ensureChatScope = useCallback(async (): Promise<boolean> => {
    if (!projectId) return false;
    if (isChatScopeLoaded(projectId)) return true;
    markChatScopeLoaded(projectId);
    try {
      await useChatStore.getState().loadSessions(projectId, useAppStore.getState().currentDocument?.document_id);
      return true;
    } catch {
      useAppStore.getState().showToast(t('chatSearchOpenFailed'), 'error');
      return false;
    }
  }, [projectId, t]);

  useEffect(() => { if (mode === 'chats') void ensureChatScope(); }, [mode, ensureChatScope]);

  // No navigation: the active chat is project-scoped and survives document
  // switches, so the hit's chat opens beside whatever document is open.
  const revealChat = () => {
    useUIStore.getState().setRightPanelTab(useAppStore.getState().currentDocument?.document_id ?? null, 'chat');
    emit('open-right-panel');
  };

  const openChatHit = async (h: ChatSearchHit) => {
    if (!(await ensureChatScope())) return;
    const chat = useChatStore.getState();
    chat.openBranch(h.chat_id, h.thread_id);
    chat.setPendingScroll(h.chat_id, h.message_id);
    revealChat();
  };

  const openTitleHit = async (session: ChatSession) => {
    if (!(await ensureChatScope())) return;
    useChatStore.getState().setActiveSession(session.session_id);
    revealChat();
  };

  const showHint = query.length < minChars;
  // WHY a client-side title group beside the semantic chat search: semantic misses
  // short title queries, is refused while chat-recall is disabled and does not see
  // chats not yet indexed; the loaded chat list covers all three, with no request.
  // A chat in both groups stays in both — the title row opens the chat, the
  // content row scrolls to the message.
  const titleHits = mode === 'chats' && !showHint ? filterSessionsByTitle(chatSessions, query) : [];
  const activeResults = { fulltext: results, semantic: semanticHits, chats: chatHits }[mode];
  const showEmpty = !loading && !error && !showHint && activeResults.length === 0 && titleHits.length === 0;

  return (
    // data-testid: e2e tab-identity guard reads this node (search overlay spec) —
    // same pattern as refs-panel-root / chat-panel-root.
    <div className="flex flex-col h-full" data-testid="search-panel-root">
      <div className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
        <Button variant="ghost" size="sm" onClick={() => switchMode('fulltext')}>
          <Search size={13} className={mode === 'fulltext' ? 'text-accent-text' : undefined} />
          <span className={mode === 'fulltext' ? 'text-accent-text' : undefined}>{t('searchModeFulltext')}</span>
        </Button>
        <Button variant="ghost" size="sm" onClick={() => switchMode('semantic')}>
          <Sparkles size={13} className={mode === 'semantic' ? 'text-accent-text' : undefined} />
          <span className={mode === 'semantic' ? 'text-accent-text' : undefined}>{t('searchModeSemantic')}</span>
        </Button>
        <Button variant="ghost" size="sm" onClick={() => switchMode('chats')}>
          <MessagesSquare size={13} className={mode === 'chats' ? 'text-accent-text' : undefined} />
          <span className={mode === 'chats' ? 'text-accent-text' : undefined}>{t('searchModeChats')}</span>
        </Button>
      </div>

      <div className="px-3 py-2">
        <FieldInput
          autoFocus
          placeholder={t(PLACEHOLDER_KEY[mode])}
          value={query}
          onChange={e => handleQueryChange(e.target.value)}
        />
      </div>

      {mode === 'fulltext' && (
        <div className="flex items-center gap-4 px-3 py-1.5 border-b border-border text-ui-xs text-text-dim">
          {currentDocument && (
            <FieldCheckbox
              label={t('searchScopeThisDoc')}
              checked={scopeThisDoc}
              onChange={toggleScopeThisDoc}
            />
          )}
          <FieldCheckbox
            label={t('searchExactMatch')}
            checked={exactMatch}
            onChange={toggleExactMatch}
            title={t('searchExactMatchTip')}
          />
        </div>
      )}

      {mode === 'semantic' && (
        <div className="flex items-center gap-4 px-3 py-1.5 border-b border-border text-ui-xs text-text-dim">
          {currentDocument && (
            <FieldCheckbox
              label={t('searchScopeThisDoc')}
              checked={scopeThisDoc}
              onChange={toggleScopeThisDoc}
            />
          )}
          <FieldCheckbox
            label={t('searchMemoryOnly')}
            checked={memoryOnly}
            onChange={toggleMemoryOnly}
          />
        </div>
      )}

      {mode === 'chats' && currentDocument && (
        <div className="flex items-center gap-4 px-3 py-1.5 border-b border-border text-ui-xs text-text-dim">
          <FieldCheckbox
            label={t('searchScopeThisDoc')}
            checked={scopeThisDoc}
            onChange={toggleScopeThisDoc}
            title={t('chatSearchScopeTip')}
          />
        </div>
      )}

      <PillList ref={scrollRef}>
        {titleHits.map((s: ChatSession) => (
          <ListPill
            key={`title-${s.session_id}`}
            variant="plain"
            data-chat-title-hit={s.session_id}
            onClick={() => { void openTitleHit(s); }}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-ui-base font-semibold text-text overflow-hidden text-ellipsis whitespace-nowrap">
                {s.title}
              </span>
              <span className="flex items-center gap-1.5 shrink-0">
                {s.last_message_at && (
                  <span className="text-ui-xs text-text-dim">{formatDate(s.last_message_at)}</span>
                )}
                {/* Same badge slot as a content hit's score: a title match has no score. */}
                <span className="text-ui-xs text-text-dim bg-surface3 py-px px-1.5">
                  {t('chatSearchTitleBadge')}
                </span>
              </span>
            </div>
          </ListPill>
        ))}
        {showHint && (
          <p className="text-ui-base text-text-dim text-center py-4 px-2">
            {t('typeAtLeastN', { n: minChars })}
          </p>
        )}
        {loading && (
          <p className="text-ui-base text-text-dim text-center py-4 px-2">
            {t('searching')}
          </p>
        )}
        {!loading && error && mode === 'chats' && chatsDisabled && (
          <p className="text-ui-base text-text-dim text-center py-4 px-2">
            {t('chatSearchDisabled')}
          </p>
        )}
        {!loading && error && !(mode === 'chats' && chatsDisabled) && (
          <p className="text-ui-base text-text-dim text-center py-4 px-2">
            {t('searchFailed')}{' '}
            <Button type="button" variant="ghost" size="sm" onClick={() => handleQueryChange(query)}>{t('retry')}</Button>
          </p>
        )}
        {showEmpty && (
          <p className="text-ui-base text-text-dim text-center py-4 px-2">
            {t('noMatchesFound')}
          </p>
        )}

        {mode === 'fulltext' && !loading && results.map((r: SearchResult) => {
          const isRef = !!r.reference_id;
          const key = isRef ? r.reference_id! : r.document_id!;

          return (
            <ListPill
              key={key}
              variant="plain"
              onClick={() => {
                if (isRef) emit('navigate-to-reference', { referenceId: r.reference_id! });
                else emit('navigate-to-document', { documentId: r.document_id! });
              }}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-ui-base font-semibold text-text overflow-hidden text-ellipsis whitespace-nowrap">
                  {isRef && <span className="opacity-50 text-ui-xs mr-1">ref</span>}
                  {r.title}
                </span>
                <span className="flex items-center gap-1.5 shrink-0">
                  {r.updated_at && (
                    <span className="text-ui-xs text-text-dim">
                      {formatDate(r.updated_at)}
                    </span>
                  )}
                  <span className="text-ui-xs text-text-dim bg-surface3 py-px px-1.5">
                    {r.match_count}
                  </span>
                </span>
              </div>

              {r.snippet && (
                <div className="text-xs text-text-dim mt-1.5 leading-relaxed">
                  {r.snippet.before && <>{r.snippet.before} </>}
                  <strong className="text-text">{r.snippet.match}</strong>
                  {r.snippet.after && <> {r.snippet.after}</>}
                </div>
              )}
            </ListPill>
          );
        })}

        {mode === 'semantic' && !loading && semanticHits.map((h: SemanticSearchHit) => {
          const isRef = h.kind === 'reference';
          const navEvent = isRef ? 'navigate-to-reference' : 'navigate-to-document';
          const navId = isRef ? { referenceId: h.parent_id } : { documentId: h.parent_id };

          return (
            <ListPill
              key={`${h.kind}-${h.parent_id}-${h.offset_start ?? h.snippet.slice(0, 40)}`}
              variant="plain"
              onClick={() => emit(navEvent, navId)}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-ui-base font-semibold text-text overflow-hidden text-ellipsis whitespace-nowrap">
                  {isRef && <span className="opacity-50 text-ui-xs mr-1">ref</span>}
                  {h.parent_title}
                </span>
                <span className="flex items-center gap-1.5 shrink-0">
                  <span className="text-ui-xs text-text-dim bg-surface3 py-px px-1.5">
                    {(h.score * 100).toFixed(0)}%
                  </span>
                </span>
              </div>
              {h.heading && (
                <div className="text-ui-xs text-text-dim mt-0.5">{h.heading}</div>
              )}
              <div className="text-xs text-text-dim mt-1.5 leading-relaxed line-clamp-3">
                {h.snippet}
              </div>
            </ListPill>
          );
        })}

        {mode === 'chats' && !loading && chatHits.map((h: ChatSearchHit) => (
          <ListPill
            key={`${h.chat_id}-${h.message_id}`}
            variant="plain"
            data-chat-hit={h.message_id}
            data-sibling={h.sibling ? 'true' : undefined}
            onClick={() => { void openChatHit(h); }}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-ui-base font-semibold text-text overflow-hidden text-ellipsis whitespace-nowrap">
                {h.chat_title}
              </span>
              <span className="flex items-center gap-1.5 shrink-0">
                {h.sibling && (
                  <span className="text-ui-xs text-accent-text" title={t('chatSearchSiblingTip')}>
                    {t('chatSearchSibling')}
                  </span>
                )}
                {h.created_at && (
                  <span className="text-ui-xs text-text-dim">{formatDate(h.created_at)}</span>
                )}
                <span className="text-ui-xs text-text-dim bg-surface3 py-px px-1.5">
                  {(h.score * 100).toFixed(0)}%
                </span>
              </span>
            </div>
            {h.question && (
              <div className="text-ui-xs text-text-dim mt-0.5 italic line-clamp-1">{h.question}</div>
            )}
            <div className="text-xs text-text-dim mt-1.5 leading-relaxed line-clamp-3">
              {h.snippet}
            </div>
          </ListPill>
        ))}
      </PillList>
    </div>
  );
}
