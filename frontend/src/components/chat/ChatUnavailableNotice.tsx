/** Replaces the whole chat composer when the user has no model to talk to: admin gets a link to Settings → Models, everyone else is told to ask an admin. */

import { useLocation, useNavigate } from 'react-router-dom';
import { useAppStore } from '../../store/app-store';
import { useChatStore } from '../../store/chat-store';
import { useUIStore } from '../../store/ui-store';
import { useTranslation } from '../../i18n';
import { Button } from '../ui';

/** True once the catalog has loaded and serves this user zero models. Before the load
 * settles (or when it failed — that path toasts) the composer stays, so an error or a
 * slow fetch never reads as "chat unavailable". */
export function useChatUnavailable(): boolean {
  return useChatStore(s => s.modelsLoaded && s.models.length === 0);
}

export function ChatUnavailableNotice() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const isAdmin = useAppStore(s => s.currentUser?.is_admin ?? false);
  const originLabel = useAppStore(s => s.currentDocument?.title ?? s.currentProject?.name ?? null);
  const setSectionOrigin = useAppStore(s => s.setSectionOrigin);
  const setAdminSectionTab = useUIStore(s => s.setAdminSectionTab);

  // WHY: the link goes to is_admin only — the Models section is admin-only (a
  // moderator would land on Users); a non-admin's actionable step is to ask an admin.
  const openModelsSettings = () => {
    // Same origin record as UserControls.openSection, so the section's back button
    // returns to the document the chat was opened from.
    setSectionOrigin({ path: location.pathname, label: originLabel ?? t('projects'), fromProject: true });
    setAdminSectionTab('settings:models');
    navigate('/admin');
  };

  return (
    <div className="flex-shrink-0 px-3 pb-3" data-testid="chat-unavailable">
      <div className="py-6 px-2 text-ui-base text-text-dim text-center border-2 border-dashed border-border flex flex-col items-center gap-2">
        <span>{t('chatNoModels')}</span>
        {isAdmin ? (
          <Button variant="ghost" size="sm" onClick={openModelsSettings}>{t('chatConnectApi')}</Button>
        ) : (
          <span>{t('chatAskAdminForModels')}</span>
        )}
      </div>
    </div>
  );
}
