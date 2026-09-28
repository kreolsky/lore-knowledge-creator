/** Shared aside-width wiring for the section pages (/admin, /cabinet).
 *
 * The section asides and the project sidebar share ONE width: panelWidths.left
 * (per-user, persisted on /preferences/_global). No inheritance machinery —
 * whichever surface the user drags writes the same field through the same
 * setter, so every hop (project → section, section → project, section →
 * section, F5 anywhere) opens at the width last seen. Why not a separate
 * section-width field with sync-on-exit: React renders the ENTERING page
 * before the exiting page's unmount cleanup runs, so a back-navigation sync
 * wrote the width after the project sidebar had already seeded from the stale
 * value — a narrow panel that only widened on F5 (reported case 2026-09-18).
 */
import { useUIStore } from '../store/ui-store';

export interface SectionAsideWidth {
  initialAsideWidth: number | undefined;
  onAsideWidthChange: (width: number) => void;
}

export function useSectionAsideWidth(): SectionAsideWidth {
  const width = useUIStore(s => s.panelWidths.left);
  const setPanelWidth = useUIStore(s => s.setPanelWidth);
  return { initialAsideWidth: width ?? undefined, onAsideWidthChange: (w) => setPanelWidth('left', w) };
}
