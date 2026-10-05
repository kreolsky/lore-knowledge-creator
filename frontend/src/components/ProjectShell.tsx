/**
 * ProjectShell — shared layout chrome for the authenticated project page and the
 * anonymous public-share page.
 *
 * Owns: outer div tree, left tab bar container, resizable left aside + resizer,
 * center slot, right resizer, right tab bar container, right panel container,
 * and the two `useResizer` instances. Panel open/tab/width state is read from
 * `ui-store` (per-document in authed mode via `rightPanelDocId`; ORPHAN slice in
 * public mode where `rightPanelDocId=null`).
 *
 * Exposes `openRightPanel(tab?)` / `openSidebar(tab?)` via an imperative ref so
 * the parent's `useEvent` handlers (open-notes, open-find, open-sidebar-docs,
 * open-right-panel) can drive the same Shell the user clicks on — no second
 * source of truth for resizer state.
 *
 * Compact viewport (ui-store `compactLayout`): center only; the tree is a drawer
 * over the center and the right panel covers the whole screen. At most one is
 * open. Same element tree as desktop — only classes change.
 *
 * SYSTEM: project-shell — shared layout for project + public-share surfaces.
 */
// ARCH: Slot-based shell — parent owns all hooks (collab, recording, refs events);
//       Shell owns only the chrome + resizers + tab-click handlers.
// ARCH: Public mode is just `accessLevel='readonly'` + minimal UserControls + a
//       single right tab — no special-case branch inside Shell.

import type React from 'react';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import { PanelLeft, PanelRight, X } from 'lucide-react';
import { useResizer } from '../hooks/useResizer';
import { useDocumentNavigation } from '../navigation/useDocumentNavigation';
import { useUIStore, type RightTab } from '../store/ui-store';
import { useNoteStore } from '../store/note-store';
import { useAppStore } from '../store/app-store';
import { useTranslation } from '../i18n';
import { Header } from './Header';
import { UserControls } from './UserControls';
import { IconButton, PanelLoading } from './ui';
import s from './Sidebar.module.css';

export type LeftTab = 'docs' | 'toc';

// Compact tab rows: every cell is a square as wide as the row is tall.
const COMPACT_TAB_CLS = '!w-auto !h-full aspect-square';
const COMPACT_CELL_CLS = 'h-full aspect-square shrink-0 flex items-center justify-center';

/** A renderable right-tab entry. Parent filters/extends this list per role. */
export interface RightTabEntry {
  tab: RightTab;
  icon: React.ReactNode;
  title: string;
  renderPanel: () => React.ReactNode;
  /** Extra className on the tab button (e.g. 'notes-tab-thread', 'access-tab--red'). */
  className?: string;
  /** Project-scoped tab (search/settings) — rendered AFTER the flex-1 spacer so the
   *  right-tab bar visually separates document-scoped tabs from project-scoped tabs.
   *
   *  INVARIANT: project-scoped tabs (search, settings) are visually separated from
   *  document-scoped tabs (chat/refs/notes/links/checkpoint/access/find) by a
   *  `<div className="flex-1" />` spacer between the two groups. Why: project
   *  settings are logically a separate surface from per-document work — the user
   *  reads the gap as "these tabs apply to the whole project, not the open doc".
   *  This was the original ProjectPage layout; the Shell preserves it by routing
   *  `aside: true` entries past the spacer. Lost during the initial Shell
   *  extraction (flat config dropped the inter-item layout chrome) — pinned here
   *  to keep a future contributor from re-flattening the bar. */
  aside?: boolean;
}

/** A renderable left-tab entry. */
export interface LeftTabEntry {
  tab: LeftTab;
  icon: React.ReactNode;
  title: string;
  renderPanel: () => React.ReactNode;
}

export interface ProjectShellHandle {
  /** Open the right panel; optionally switch to a tab. No-op if already open with the same tab. */
  openRightPanel: (tab?: RightTab) => void;
  /** Open the sidebar; optionally switch to a tab. */
  openSidebar: (tab?: LeftTab) => void;
}

interface ProjectShellProps {
  /** Left-tab entries (parent decides which to include). Empty = no left tab bar. */
  leftTabs: LeftTabEntry[];
  /** Right-tab entries (parent decides which to include based on role/reference). */
  rightTabs: RightTabEntry[];
  /** Center slot — Outlet (authed) or Editor (public). */
  renderCenter: () => React.ReactNode;
  /** Bottom-left controls: 'full' = profile/admin/pin/logout; 'minimal' = theme+language only. */
  userControlsVariant: 'full' | 'minimal';
  /** Doc id whose per-doc right-panel slice the Shell reads/writes. null = ORPHAN slice (public). */
  rightPanelDocId: string | null;
  /** Whether the right-panel content is ready to mount (authed: prefs+doc committed; public: true). */
  rightPanelReady: boolean;
  /** Active right tab (parent-computed; role-validated for authed mode). */
  effectiveRightTab: RightTab | null;
  /** Initial sidebar open state — used only on first mount (public passes true). */
  initialSidebarOpen?: boolean;
  /** Initial right-panel open state — used only on first mount (public passes true). */
  initialRightOpen?: boolean;
  /** Optional drag-enter handler on the right panel container (authed: refs drag-drop). */
  onPanelDragEnter?: (e: React.DragEvent) => void;
  /** Optional callback fired AFTER a right-tab click — lets the parent clear snapshot
   *  preview / take any tab-specific side effect (authed-only). */
  onRightTabClick?: (tab: RightTab) => void;
  /** Optional overlays rendered at the shell root (SnapshotModal, SelectionToolbar, ...). */
  overlays?: React.ReactNode;
  /** Optional header slot — defaults to <Header/>. */
  renderHeader?: () => React.ReactNode;
}

/**
 * ProjectShell — see file docstring.
 *
 * INVARIANT: the Shell NEVER opens a WS / fetches data / reads the URL — it is  Why: keeping the Shell a pure layout component lets ProjectPage and PublicSharePage reuse it without dragging each other's side effects in.
 * a pure layout component. All side-effectful work stays in the parent
 * (ProjectPage wires collab/recording/refs events; PublicSharePage wires the
 * anonymous reads). Why: keeps the public surface auditable (no authed fetches
 * can sneak in via a shared component).
 *
 * Sole exception: the document-navigation bus handler
 * (useDocumentNavigation → SYSTEM: document-navigation) is mounted ONCE here —
 * the Shell is the one component both parents (ProjectPage, PublicSharePage)
 * share. The handler opens no WS, fetches nothing, reads no URL: it commits the
 * target document and navigates (guarded off on the public surface).
 */
export const ProjectShell = forwardRef<ProjectShellHandle, ProjectShellProps>(function ProjectShell(
  {
    leftTabs, rightTabs, renderCenter, userControlsVariant,
    rightPanelDocId, rightPanelReady, effectiveRightTab,
    initialSidebarOpen = true, initialRightOpen = true,
    onPanelDragEnter, onRightTabClick, overlays, renderHeader,
  },
  ref,
) {
  // ── Document navigation (the ONE bus handler — see INVARIANT above) ──────
  useDocumentNavigation();

  const { t } = useTranslation();

  // ── Compact viewport ─────────────────────────────────────────────────────
  // WHY: compact panel state is shell-local, never the persisted sidebarOpen /
  // rightPanelOpen. The per-doc default rightPanelOpen=true would open a full-screen
  // panel on every navigation, and a phone session must not rewrite the desktop
  // layout. One value makes "only one open" structural.
  const compact = useUIStore(s => s.compactLayout);
  const [compactPanel, setCompactPanel] = useState<'none' | 'left' | 'right'>('none');
  const docKey = useAppStore(s => s.currentDocument?.document_id ?? null);
  const refKey = useAppStore(s => s.currentReference?.reference_id ?? null);
  const tableKey = useAppStore(s => s.currentTable);
  // Whatever opens an entity in the center (tree pick, link in chat, a reference)
  // closes the compact panel — one rule, no per-call-site wiring.
  useEffect(() => { setCompactPanel('none'); }, [docKey, refKey, tableKey]);

  // ── Store reads (panel state) ────────────────────────────────────────────
  const sidebarTab = useUIStore(s => s.sidebarTab);
  const setSidebarTab = useUIStore(s => s.setSidebarTab);
  const setSidebarOpen = useUIStore(s => s.setSidebarOpen);
  const setRightPanelTab = useUIStore(s => s.setRightPanelTab);
  const setRightPanelOpen = useUIStore(s => s.setRightPanelOpen);
  const setPanelWidth = useUIStore(s => s.setPanelWidth);

  const sidebarOpenStored = useUIStore(s => s.sidebarOpen);
  const leftWidth = useUIStore(s => s.panelWidths.left);
  const rightWidth = useUIStore(s => s.panelWidths.right);

  // Effective left tab: if the persisted sidebarTab isn't among the rendered
  // leftTabs, fall back to the first available tab. Why: doc-scope public shares
  // render only ['toc'] (the docs tab is hidden) but the store default is 'docs' —
  // without this, no tab is active and the aside renders empty on load. Derived,
  // not mutated — keeps the Shell a pure layout component (no store write). No-op
  // for authed ProjectPage where both docs+toc are always present.
  const effectiveSidebarTab: LeftTab | undefined =
    leftTabs.some(e => e.tab === sidebarTab) ? sidebarTab : leftTabs[0]?.tab;
  // Per-doc right-panel state. null docId → ORPHAN slice (public mode).
  const rightPanelOpenStored = useUIStore(s => (s.documents[rightPanelDocId ?? '__orphan__']?.rightPanelOpen));

  // The Shell uses the store as the source of truth for open state. On first
  // mount (when the per-doc slice is missing or the global sidebarOpen is the
  // initial default), surface the requested initial open state.
  // NOTE: useResizer is a controlled hook (isOpen from parent); we feed it the
  // stored value, and the parent's `initialSidebarOpen`/`initialRightOpen` props
  // are honored via a one-shot effect that opens the panel if the stored state
  // is unset on first mount.
  const firstMountRef = useRef(true);
  useLayoutEffect(() => {
    if (!firstMountRef.current) return;
    firstMountRef.current = false;
    if (compact) return;
    if (initialSidebarOpen && !sidebarOpenStored) setSidebarOpen(true);
    if (initialRightOpen && rightPanelOpenStored === undefined) setRightPanelOpen(rightPanelDocId, true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Right panel state: prefer stored value; fall back to initialRightOpen.
  const rightPanelOpen = rightPanelOpenStored ?? initialRightOpen;
  const sidebarOpen = sidebarOpenStored;

  // ── Resizers ─────────────────────────────────────────────────────────────
  const sidebarResizer = useResizer({
    side: 'left',
    defaultWidth: 220,
    minWidth: 140,
    maxWidth: 500,
    initialWidth: leftWidth ?? undefined,
    isOpen: sidebarOpen,
    onWidthChange: (w) => setPanelWidth('left', w),
    onOpenChange: (open) => setSidebarOpen(open),
  });

  const rightPanelResizer = useResizer({
    side: 'right',
    defaultWidth: 500,
    minWidth: 300,
    maxWidth: 1000,
    initialWidth: rightWidth ?? undefined,
    isOpen: rightPanelOpen,
    onWidthChange: (w) => setPanelWidth('right', w),
    onOpenChange: (open) => setRightPanelOpen(rightPanelDocId, open),
  });

  // Closing kills a live search overlay, like the desktop close (setRightPanelOpen).
  const clearSearchTabOverlay = useUIStore(s => s.clearSearchTabOverlay);
  const closeCompactRight = () => { clearSearchTabOverlay(); setCompactPanel('none'); };

  const sidebarIsOpen = compact ? compactPanel === 'left' : sidebarResizer.isOpen;
  const rightIsOpen = compact ? compactPanel === 'right' : rightPanelResizer.isOpen;

  // ── Tab-click handlers (the Shell owns these) ───────────────────────────
  const handleSidebarTabClick = useCallback((tab: LeftTab) => {
    if (compact) {
      setSidebarTab(tab);
    } else if (!sidebarResizer.isOpen) {
      setSidebarTab(tab);
      sidebarResizer.open();
    } else if (effectiveSidebarTab === tab) {
      sidebarResizer.close();
    } else {
      setSidebarTab(tab);
    }
  }, [compact, sidebarResizer, effectiveSidebarTab, setSidebarTab]);

  const handleRightTabClick = useCallback((tab: RightTab) => {
    if (compact) {
      // Tab-only write — the compact panel is already open (its tab row is visible).
      setRightPanelTab(rightPanelDocId, tab);
    } else if (!rightPanelResizer.isOpen) {
      setRightPanelTab(rightPanelDocId, tab);
      rightPanelResizer.open();
    } else if (effectiveRightTab === tab) {
      // Close keeps the tab — it becomes the "last opened tab" so reopening
      // (drag-enter or re-clicking the tab) restores it.
      rightPanelResizer.close();
    } else {
      setRightPanelTab(rightPanelDocId, tab);
    }
    onRightTabClick?.(tab);
  }, [compact, rightPanelResizer, effectiveRightTab, setRightPanelTab, rightPanelDocId, onRightTabClick]);

  // ── Imperative handle for parent's event handlers ───────────────────────
  useImperativeHandle(ref, () => ({
    openRightPanel: (tab?: RightTab) => {
      if (tab) setRightPanelTab(rightPanelDocId, tab);
      if (compact) setCompactPanel('right');
      else if (!rightPanelResizer.isOpen) rightPanelResizer.open();
    },
    openSidebar: (tab?: LeftTab) => {
      if (tab) setSidebarTab(tab);
      if (compact) setCompactPanel('left');
      else if (!sidebarResizer.isOpen) sidebarResizer.open();
    },
  }), [compact, rightPanelResizer, sidebarResizer, setRightPanelTab, setSidebarTab, rightPanelDocId]);

  // ── Right-tab-bar width CSS var (Header reads it for layout margin) ─────
  const rightTabBarRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = rightTabBarRef.current;
    if (!el) return;
    const w = el.offsetWidth;
    document.documentElement.style.setProperty('--right-tab-bar-w', `${w}px`);
    return () => { document.documentElement.style.removeProperty('--right-tab-bar-w'); };
  });

  const activeNoteThreadId = useNoteStore(s => s.activeNoteThreadId);

  const header = renderHeader ? renderHeader() : <Header />;

  // No left tabs → no left tab bar at all (single-doc public share shows only toc).
  const hasLeftBar = leftTabs.length > 0;
  const hasRight = rightTabs.length > 0;

  const renderLeftTabButtons = () => leftTabs.map(entry => (
    <button
      key={entry.tab}
      className={`left-bar-tab ${compact ? COMPACT_TAB_CLS : ''} ${sidebarIsOpen && effectiveSidebarTab === entry.tab ? 'active' : ''}`}
      onClick={() => handleSidebarTabClick(entry.tab)}
      title={entry.title}
    >
      {entry.icon}
    </button>
  ));

  return (
    /* data-testid: the shell root is the node e2e captures to prove a route change
       did not remount the whole app. */
    <div data-testid="project-shell" className="relative flex h-[100dvh] overflow-hidden bg-bg text-text">
      <div className="flex flex-col flex-1 min-w-0 h-full">
        {/* The wrapper is rendered in both modes so crossing the breakpoint does
            not remount the header. */}
        <div className="flex shrink-0">
          {compact && hasLeftBar && (
            <div className="flex items-center h-12 pl-2 bg-header-bg border-b border-border-soft">
              <IconButton
                title={t('compactToggleTree')}
                onClick={() => setCompactPanel(p => p === 'left' ? 'none' : 'left')}
              >
                <PanelLeft size={16} />
              </IconButton>
            </div>
          )}
          <div className="flex-1 min-w-0">{header}</div>
          {compact && hasRight && (
            <div className="flex items-center h-12 pr-2 bg-header-bg border-b border-border-soft">
              <IconButton title={t('compactOpenPanel')} onClick={() => setCompactPanel('right')}>
                <PanelRight size={16} />
              </IconButton>
            </div>
          )}
        </div>

        <div className="flex flex-1 overflow-hidden">
          {hasLeftBar && (
            <div
              data-testid="shell-left"
              className={compact
                ? `compact-drawer ${sidebarIsOpen ? 'compact-drawer--open' : ''} fixed inset-y-0 left-0 z-[31] w-[min(calc(100vw-56px),360px)] flex flex-col bg-surface`
                : 'flex shrink-0 h-full'}
            >
              {!compact && (
                <div className="left-tab-bar">
                  <div className="flex flex-col items-center gap-0.5">
                    {renderLeftTabButtons()}
                  </div>
                  <div className="flex-1" />
                  <UserControls layout="vertical" variant={userControlsVariant} />
                </div>
              )}
              {compact && (
                <div className="flex shrink-0 h-12 bg-header-bg border-b border-border-soft">
                  {renderLeftTabButtons()}
                  <div className="flex-1" />
                  <div className={COMPACT_CELL_CLS}>
                    <IconButton title={t('close')} onClick={() => setCompactPanel('none')}>
                      <X size={16} />
                    </IconButton>
                  </div>
                </div>
              )}

              <aside
                className={`${s.sidebar} ${compact ? 'flex-1 min-h-0 !w-full !max-w-none' : ''}`}
                style={compact ? undefined : {
                  width: sidebarResizer.isOpen ? sidebarResizer.width : 0,
                  minWidth: sidebarResizer.isOpen ? undefined : 0,
                  overflow: sidebarResizer.isOpen ? undefined : 'hidden',
                }}
              >
                {leftTabs.map(entry => effectiveSidebarTab === entry.tab ? (
                  <div key={entry.tab} className="h-full">{entry.renderPanel()}</div>
                ) : null)}
              </aside>
              {compact && (
                <div className="flex items-center gap-0.5 shrink-0 px-1 pt-1 border-t border-border-soft pb-[env(safe-area-inset-bottom)]">
                  <UserControls variant={userControlsVariant} />
                </div>
              )}
            </div>
          )}

          {!compact && sidebarResizer.isOpen && hasLeftBar && (
            <div className="resizer" ref={sidebarResizer.resizerRef} />
          )}

          <main className="flex-1 flex flex-col h-full overflow-hidden min-w-0">
            {renderCenter()}
          </main>
        </div>
      </div>

      {!compact && rightIsOpen && hasRight && (
        <div className="resizer resizer--right" ref={rightPanelResizer.resizerRef} />
      )}

      {hasRight && (!compact || rightIsOpen) && (
        <div
          data-testid="shell-right"
          className={compact
            ? 'fixed inset-0 z-[31] flex flex-col bg-bg'
            : `flex flex-col shrink-0 h-full ${!rightIsOpen ? 'absolute right-0 top-0 z-[13] pointer-events-none' : ''}`}
          style={{ width: !compact && rightIsOpen ? rightPanelResizer.width : undefined }}
          onDragEnter={onPanelDragEnter}
        >
          <div ref={rightTabBarRef} className={`right-panel-header ${compact ? 'overflow-x-auto' : ''} ${!rightIsOpen ? 'pointer-events-auto bg-transparent' : ''}`}>
            {compact && (
              <div className={COMPACT_CELL_CLS}>
                <IconButton title={t('close')} onClick={closeCompactRight}>
                  <X size={16} />
                </IconButton>
              </div>
            )}
            {/* Document-scoped tabs (chat/refs/notes/links/checkpoint/access/find). */}
            {rightTabs.filter(e => !e.aside).map(entry => (
              <button
                key={entry.tab}
                className={`right-tab ${compact ? COMPACT_TAB_CLS : ''} ${entry.className ?? ''} ${rightIsOpen && effectiveRightTab === entry.tab ? 'active' : 'inactive'}`}
                onClick={() => handleRightTabClick(entry.tab)}
                title={entry.title}
              >
                {entry.icon}
              </button>
            ))}
            {/* INVARIANT: the flex-1 spacer between the two right-tab groups is  Why: the spacer pushes project-scoped tabs away from document-scoped tabs; removing it collapses the two groups together.
                load-bearing — see RightTabEntry.aside. Removing it collapses the
                project-scoped tabs (search/settings) against the document-scoped
                ones, losing the visual grouping the user relies on. */}
            <div className="flex-1" />
            {/* Project-scoped tabs (search/settings). */}
            {rightTabs.filter(e => e.aside).map(entry => (
              <button
                key={entry.tab}
                className={`right-tab ${compact ? COMPACT_TAB_CLS : ''} ${entry.className ?? ''} ${rightIsOpen && effectiveRightTab === entry.tab ? 'active' : 'inactive'}`}
                onClick={() => handleRightTabClick(entry.tab)}
                title={entry.title}
              >
                {entry.icon}
              </button>
            ))}
          </div>

          {rightIsOpen && !rightPanelReady && (
            <aside className="right-panel">
              <PanelLoading />
            </aside>
          )}
          {rightIsOpen && rightPanelReady && effectiveRightTab && (
            <aside
              className={`right-panel${effectiveRightTab === 'notes' && activeNoteThreadId ? ' right-panel--notes-thread' : ''}`}
            >
              {(() => {
                const entry = rightTabs.find(e => e.tab === effectiveRightTab);
                return entry ? entry.renderPanel() : null;
              })()}
            </aside>
          )}
        </div>
      )}

      {compact && sidebarIsOpen && (
        <div
          data-testid="compact-scrim"
          role="presentation"
          className="fixed inset-0 z-[30] bg-black/40"
          onClick={() => setCompactPanel('none')}
        />
      )}

      {overlays}
    </div>
  );
});
