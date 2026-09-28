/**
 * SelectionPill — shows the pinned fragment for the active region session.
 *
 * // see SYSTEM: selection-region-agent — chat pill over the composer.
 *
 * Renders the live-resolved pinned fragment (first line, truncated) for the active
 * session's region; ✕ unpins (PATCH has_region=false + clear localStorage). The
 * region is frontend-owned (Yjs RelativePosition in localStorage); the pill re-
 * resolves it against the focused editor's ydoc for display. The live editor
 * highlight (re-derived every transaction) lives in the Editor StateField.
 */
import { useMemo } from 'react';
import { X } from 'lucide-react';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';
import { IconButton } from '../ui';
import { getPendingRegion, resolveRegionRange } from '../../store/chat-store/pending-selection';
import { getActiveHandle } from '../../editor/active-editor';

export function SelectionPill() {
  const { t } = useTranslation();
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const session = useChatStore(s =>
    s.sessions.find(x => x.session_id === activeSessionId) ?? null,
  );
  const ghostRegion = useChatStore(s => s.ghostRegion);
  const unpinRegion = useChatStore(s => s.unpinRegion);
  const clearGhostRegion = useChatStore(s => s.clearGhostRegion);

  // Single branch key (activeSessionId === null) keeps the ghost and materialized region
  // sources from drifting. Re-reads on every chat-store
  // change; the pill is display-only, the editor highlight tracks transactions in real time.
  const region = activeSessionId ? getPendingRegion(activeSessionId) : ghostRegion;
  // Show the pill for a materialized has_region session OR a ghost pin.
  const pinned = activeSessionId ? !!session?.has_region : !!ghostRegion;

  const preview = useMemo(() => {
    if (!region) return null;
    const range = resolveRegionRange(region);
    if (!range) return null; // offline / anchor lost
    if (range.to <= range.from) return ''; // collapsed region
    return (getActiveHandle()?.ytext?.toString() ?? '').slice(range.from, range.to);
  }, [region]);

  if (!pinned) return null;

  const display = preview === null ? null : preview === '' ? t('selectionPillEmpty') : preview.replace(/\n/g, ' ').trim().slice(0, 60);

  const onUnpin = () => {
    // Ghost pin (no session row) → drop the in-memory region; materialized → PATCH unpin.
    if (activeSessionId) void unpinRegion(activeSessionId);
    else clearGhostRegion();
  };

  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 mb-1 text-xs bg-accent/15 border border-accent/40 text-text">
      <span className="font-semibold text-accent shrink-0">{t('selectionPillLabel')}:</span>
      <span className="truncate opacity-80">{display ?? '…'}</span>
      <IconButton
        size="sm"
        title={t('selectionPillUnpin')}
        onClick={onUnpin}
      >
        <X size={12} />
      </IconButton>
    </div>
  );
}
