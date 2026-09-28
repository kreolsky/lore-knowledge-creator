/** Centered full-panel loading spinner — shared by all right-panel tabs. */
// ARCH: Single loading UI for the right panel. INVARIANT: every right-panel tab
// renders <PanelLoading/> while its scope fetch is in flight and MUST NOT render
// its empty state until the load settles. Why: empty state (no chats / no refs /
// no snapshots) is derived from data.length===0, which is ALSO true mid-fetch —
// without this gate the empty state flashes before content arrives (navigation
// flicker). Empty state ≠ loading state.
import { Loader2 } from 'lucide-react';
import { useTranslation } from '../../i18n';

export function PanelLoading() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col h-full items-center justify-center gap-2 py-8 px-2">
      <Loader2 size={16} className="animate-spin text-text-dim" />
      <p className="text-ui-base text-text-dim">{t('loading')}</p>
    </div>
  );
}
