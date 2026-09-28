/**
 * SectionShell — layout chrome for the admin panel and the cabinet: a vertical
 * left tab bar (same `left-tab-bar` / `left-bar-tab` chrome as a project) with a
 * resizable aside beside it, and the page body in the center slot.
 *
 * NOT a ProjectShell reuse, by design: ProjectShell mounts useDocumentNavigation
 * and writes ui-store panel prefs that persist into a project's UI state — a
 * section's tab/width choices must never land there. The shell itself stays
 * store-free: persistence is OPT-IN via the width props, and it goes to GLOBAL
 * per-user prefs (`/preferences/_global`), never a project blob. AdminPage wires
 * them (aside width + last section survive reload per user); the cabinet passes
 * nothing and stays ephemeral. Section (tab) state remains local useState.
 *
 * The aside's top row is the back button: it navigates to the app-store's
 * sectionOrigin (where the user entered the section from), degrading to
 * /projects + "My projects" when no origin is stored.
 *
 * When the section was entered FROM a project (origin.fromProject), the left
 * rail shows ONE inherited tab above the section tabs: the project's Documents
 * tab (same icon/title as ProjectPage's docs tab), navigating to origin.path.
 * Never `active` — the section aside shows sections, not documents. Rendered
 * by the shell (not the pages) because both pages would duplicate it and the
 * shell already owns the origin fallback. It does NOT touch ui-store on click:
 * the project restores its own persisted sidebarTab/sidebarOpen, and the
 * button's contract is "go back where I was".
 */

import type React from 'react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, FileText } from 'lucide-react';
import { useResizer } from '../hooks/useResizer';
import { useAppStore } from '../store/app-store';
import { UserControls } from './UserControls';
import { Button } from './ui';
import { useTranslation } from '../i18n';
import s from './Sidebar.module.css';

/** A renderable section-tab entry. */
export interface SectionTabEntry<T extends string = string> {
  tab: T;
  icon: React.ReactNode;
  title: string;
  renderPanel: () => React.ReactNode;
}

/** An aside row: one entity name per line, doc-item chrome, highlighted when
 *  selected. Lives here (not in the pages) because the raw <button> carrying
 *  the .doc-item hover-chain is exempt from forbid-elements only in this file. */
export function SectionAsideRow({ label, selected, icon, onSelect }: { label: string; selected: boolean; icon?: React.ReactNode; onSelect?: () => void }) {
  return (
    <button
      className={`doc-item w-full text-left ${selected ? 'active' : ''}`}
      onClick={onSelect}
      title={label}
    >
      {icon && <span className="doc-icon flex items-center">{icon}</span>}
      <span className="truncate">{label}</span>
    </button>
  );
}

interface SectionShellProps<T extends string = string> {
  tabs: SectionTabEntry<T>[];
  renderCenter: () => React.ReactNode;
  /** Aside body when there is no active tab (a tab-less section like the
   *  cabinet, whose "tab" is the bottom UserControls button itself). */
  renderAside?: () => React.ReactNode;
  /** Fired when the active tab CHANGES (not on active-tab close) — lets the
   *  parent swap the center content without owning the tab state. */
  onTabChange?: (tab: T) => void;
  /** Opt-in aside-width persistence (admin → global per-user prefs; cabinet
   *  passes neither). initialAsideWidth seeds the resizer ONCE (useResizer is a
   *  one-time seed); onAsideWidthChange fires with the final width on drag end. */
  initialAsideWidth?: number;
  onAsideWidthChange?: (width: number) => void;
}

export function SectionShell<T extends string = string>({ tabs, renderCenter, renderAside, onTabChange, initialAsideWidth, onAsideWidthChange }: SectionShellProps<T>) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const sectionOrigin = useAppStore(st => st.sectionOrigin);

  const [activeTab, setActiveTab] = useState<T>(tabs[0]?.tab ?? ('' as T));
  const [asideOpen, setAsideOpen] = useState(true);

  // Same defaults ProjectShell passes its sidebar resizer — the aside must feel
  // like a project's sidebar, including snap-to-close. Width persistence (when
  // the page opts in) rides initialWidth/onWidthChange, mirroring
  // ProjectShell.tsx's sidebar resizer wiring.
  const resizer = useResizer({
    side: 'left',
    defaultWidth: 220,
    minWidth: 140,
    maxWidth: 500,
    initialWidth: initialAsideWidth,
    isOpen: asideOpen,
    onOpenChange: setAsideOpen,
    onWidthChange: onAsideWidthChange,
  });

  // Tab parity with ProjectShell's handleSidebarTabClick: a click while the
  // aside is snap-closed reopens it; a click on the ACTIVE tab closes it — so
  // the back button is never lost behind a snap-close.
  const handleTabClick = (tab: T) => {
    if (!resizer.isOpen) {
      setActiveTab(tab);
      resizer.open();
      onTabChange?.(tab);
    } else if (activeTab === tab) {
      resizer.close();
    } else {
      setActiveTab(tab);
      onTabChange?.(tab);
    }
  };

  const activeEntry = tabs.find(entry => entry.tab === activeTab);
  const origin = sectionOrigin ?? { path: '/projects', label: t('projects'), fromProject: false };

  return (
    <div data-testid="section-shell" className="flex h-full overflow-hidden">
      <div className="flex shrink-0 h-full">
        <div className="left-tab-bar">
          <div className="flex flex-col items-center gap-0.5">
            {/* Inherited Documents tab — the project's docs icon as a return
                button. Same raw-button exemption as the section tabs below. */}
            {origin.fromProject && (
              <button
                className="left-bar-tab"
                onClick={() => navigate(origin.path)}
                title={t('tabDocuments')}
              >
                <FileText size={15} />
              </button>
            )}
            {tabs.map(entry => (
              <button
                key={entry.tab}
                className={`left-bar-tab ${resizer.isOpen && activeTab === entry.tab ? 'active' : ''}`}
                onClick={() => handleTabClick(entry.tab)}
                title={entry.title}
              >
                {entry.icon}
              </button>
            ))}
          </div>
          <div className="flex-1" />
          <UserControls layout="vertical" />
        </div>

        <aside
          className={s.sidebar}
          style={{
            width: resizer.isOpen ? resizer.width : 0,
            minWidth: resizer.isOpen ? undefined : 0,
            overflow: resizer.isOpen ? undefined : 'hidden',
          }}
        >
          <div className="flex flex-col h-full">
            {/* Back button: same dashed button as the tree's "Add document",
                in the document-row font (13px). truncate keeps a long document
                title from wrapping or overflowing the aside. */}
            <div className="px-[3px] pt-[10px]">
              <Button variant="dashed" onClick={() => navigate(origin.path)} title={origin.label}>
                <ArrowLeft size={14} className="shrink-0" />
                <span className="truncate">{origin.label}</span>
              </Button>
            </div>
            {activeEntry ? activeEntry.renderPanel() : renderAside?.()}
          </div>
        </aside>
      </div>

      {resizer.isOpen && (
        <div className="resizer" ref={resizer.resizerRef} />
      )}

      <main className="flex-1 flex flex-col h-full overflow-hidden min-w-0">
        {renderCenter()}
      </main>
    </div>
  );
}
